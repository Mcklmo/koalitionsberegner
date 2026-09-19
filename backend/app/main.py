"""FastAPI surface over the shared election store.

Access follows one rule: *viewing and asking are open, importing is the
owner's*. Anyone sees everything stored and may ask for an election that is
missing; only a caller presenting ``ADMIN_SECRET`` can make the server go and
look for an election it does not already hold, because that is the one thing
here that costs money. An election once imported is served to everyone for
free, which is the whole point of the shared store.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import (
    admin_secret,
    get_mailer,
    get_parser,
    get_store,
    get_usage_recorder,
    get_wishlist,
    imports_enabled,
    max_wait_seconds,
    origin_secret,
    usage_report_secret,
    validate_configuration,
)
from .identity import normalize_year
from .mailer import Mailer, MailUnavailable
from .observability import configure_logging, io_span, scrub
from .schema import Election, Forecast, clean_text
from .service import ImportRequest, ImportResult, ImportService, ImportState
from .store import StoredElection
from .usage import (
    Period,
    UsageEvent,
    UsageRecorder,
    build_report,
    due_periods,
    gather,
    page_load_key,
    period_range,
    previous_range,
    report_key,
)
from .wishlist import Wishlist, WishlistUnavailable

configure_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Fail the boot on bad configuration rather than the first request.

    A container that starts, answers /healthz, and only then discovers it cannot
    reach its store is worse than one that never starts: the rollout succeeds and
    the breakage lands on users.
    """
    validate_configuration()
    yield


app = FastAPI(
    title="Koalitionsberegner election store", version="1.2.0", lifespan=lifespan
)


@app.middleware("http")
async def log_requests(request, call_next):
    """One pair of lines per inbound request — the other side of every span below."""
    with io_span(log, "http", "request", method=request.method, path=request.url.path) as span:
        response = await call_next(request)
        span["status"] = response.status_code
        return response

# Only needed when the page is served from a different origin than this API
# (e.g. the frontend on Cloudflare, the API on Cloud Run).
_origins = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST", "DELETE"],
        # The owner's secret rides in its own header, so a cross-origin page
        # needs it allowed through.
        allow_headers=["content-type", "x-admin-secret"],
    )


# --- the edge of the app ----------------------------------------------------
# Declared last, so they wrap everything above: a request refused here costs
# no store read, no secret check and no log line of its own.

#: The page loads its scripts from here and talks to nothing but this origin;
#: nothing else is fetched, framed or submitted anywhere. Styles stay
#: inline-capable because the page carries its stylesheet and a few style
#: attributes in index.html — scripts do not, and that is what the policy is for.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}

#: FastAPI's own documentation pages load their scripts from a CDN.
_DOCS_PATHS = ("/docs", "/redoc")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        if name == "content-security-policy" and request.url.path.startswith(_DOCS_PATHS):
            continue
        response.headers.setdefault(name, value)
    return response


#: The header a fronting proxy (the Cloudflare Worker) proves itself with.
ORIGIN_SECRET_HEADER = "x-origin-secret"


@app.middleware("http")
async def require_origin_secret(request: Request, call_next):
    """With ``ORIGIN_SECRET`` set, answer only requests that came through the proxy.

    Everything the proxy protects — its rate limits, its bot checks — is worth
    nothing while the ``run.app`` address answers the same requests directly.
    The health check stays open so the platform can still probe the container.
    """
    expected = origin_secret()
    if expected and request.url.path != "/healthz":
        presented = request.headers.get(ORIGIN_SECRET_HEADER, "")
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            return JSONResponse({"detail": "use the public address"}, status_code=403)
    return await call_next(request)


#: Every body this API reads is a few hundred bytes of JSON.
MAX_BODY_BYTES = 64 * 1024


class LimitRequestBody:
    """Refuse an oversized body before a byte of it is buffered.

    Starlette reads a body into memory whole, and nothing upstream caps it far
    below Cloud Run's 32 MB — so without this, a few dozen concurrent large
    POSTs to the open request endpoint are enough to run the container out of
    memory. A body with no declared length is refused rather than counted:
    browsers always declare one.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            limit = MAX_BODY_BYTES
            declared = headers.get(b"content-length")
            if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
                response = JSONResponse({"detail": "a body must declare its length"}, 411)
                return await response(scope, receive, send)
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                response = JSONResponse({"detail": "request body too large"}, 413)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


app.add_middleware(LimitRequestBody)


# --- dependency seams -------------------------------------------------------
# Thin wrappers over app.config so tests can override one collaborator without
# touching the environment.

def get_service() -> ImportService:
    return ImportService(get_store(), get_parser())


def get_wishlist_provider() -> Wishlist:
    return get_wishlist()


def get_usage() -> UsageRecorder:
    return get_usage_recorder()


def get_mailer_provider() -> Mailer:
    return get_mailer()


# --- the owner --------------------------------------------------------------

#: The header the owner proves themselves with, from the page's admin mode.
ADMIN_SECRET_HEADER = "x-admin-secret"


def require_admin(x_admin_secret: str | None = Header(default=None)) -> None:
    """The caller is the owner, proven by ``ADMIN_SECRET``.

    Importing spends money and is the owner's alone, so everything that starts,
    follows, saves or throws away an import sits behind this, and so does the
    usage preview. Compared in constant time, exactly as the schedule's secret
    is. With no secret configured a local run is the owner's own and passes; a
    deployment on Cloud Run refuses to boot that way
    (:func:`app.config.validate_configuration`).
    """
    expected = admin_secret()
    if not expected:
        return  # a local run with no secret configured is the owner's own
    if not hmac.compare_digest((x_admin_secret or "").encode(), expected.encode()):
        raise HTTPException(403, detail="this needs the administrator's secret")


# --- request and response models -------------------------------------------

#: A place name is a few words. The cap is generous for the longest real ones
#: and still far short of anything that belongs in a prompt.
MAX_PLACE_CHARS = 80


class ImportBody(BaseModel):
    """Which election to import: a year, a nation, and optionally a region.

    Spelling is the resolver's problem, not this model's — the only thing
    checked here is that the values are short, printable text and a year that is
    a year. Refusing "Germny" at the door would be refusing the request the
    resolver exists to answer.
    """

    model_config = ConfigDict(extra="forbid")

    year: int
    nation: str = Field(max_length=MAX_PLACE_CHARS)
    subnation: str | None = Field(default=None, max_length=MAX_PLACE_CHARS)

    @field_validator("year", mode="before")
    @classmethod
    def _year(cls, value) -> int:
        # A mistyped digit row is read as what it means; anything else is
        # refused here rather than spent on a model call.
        return normalize_year(value)

    @field_validator("nation")
    @classmethod
    def _nation(cls, value: str) -> str:
        # The same cleaning every stored name gets: printable, normalised, and
        # free of the invisible characters that make one name render as another.
        return clean_text(value, field="nation")

    @field_validator("subnation")
    @classmethod
    def _subnation(cls, value: str | None) -> str | None:
        # An empty region box means "the national election", not an empty name.
        if value is None or not value.strip():
            return None
        return clean_text(value, field="subnation")

    def to_request(self) -> ImportRequest:
        return ImportRequest(year=self.year, nation=self.nation, subnation=self.subnation)


class ImportResponse(BaseModel):
    request_key: str
    state: ImportState
    election: Election | None = None
    election_hash: str | None = None
    error: str | None = None
    attempt: int | None = None
    reused: bool = False
    duplicate: bool = False
    forecasts: list[Election] = Field(default_factory=list)
    """In the ``choose`` state: the forecasts to pick from, newest first."""

    @classmethod
    def of(cls, result: ImportResult) -> "ImportResponse":
        return cls(
            request_key=result.request_key,
            state=result.state,
            election=result.election,
            election_hash=result.election_hash,
            error=result.error,
            attempt=result.attempt,
            reused=result.reused,
            duplicate=result.duplicate,
            forecasts=list(result.forecasts),
        )


class ElectionSummary(BaseModel):
    election_hash: str
    nation: str
    state: str | None
    election_date: date
    title: str
    total_seats: int
    forecast: Forecast | None = None

    @classmethod
    def of(cls, stored: StoredElection) -> "ElectionSummary":
        return cls(
            election_hash=stored.election_hash,
            nation=stored.election.nation,
            state=stored.election.state,
            election_date=stored.election.election_date,
            title=stored.election.title,
            total_seats=stored.election.total_seats,
            forecast=stored.election.forecast,
        )


class PublicConfig(BaseModel):
    """Everything the page needs before it offers the import form."""

    requests_enabled: bool
    """Whether anyone may ask for an election to be imported."""
    imports_enabled: bool
    """Whether this deployment imports at all; the owner still needs the secret."""
    imports_open: bool
    """Whether importing needs no secret here: a local run with none configured."""


class ElectionRequestResponse(BaseModel):
    """Where an election request was written down, so the page can link to it."""

    url: str
    number: int
    duplicate: bool = False
    """True when somebody had already asked for this election."""


# --- endpoints --------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", response_model=PublicConfig)
def public_config(
    request: Request,
    background: BackgroundTasks,
    wishlist: Wishlist = Depends(get_wishlist_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> PublicConfig:
    """Public settings for the page: whether it may ask, and whether it may import.

    Every page load asks for this exactly once, which makes it where page loads
    are counted — by the language the browser already sends, and nothing else
    about the visitor. No script was added to the page to do it.
    """
    background.add_task(usage.record, page_load_key(request.headers.get("accept-language")))
    enabled = imports_enabled()
    return PublicConfig(
        requests_enabled=wishlist.enabled,
        imports_enabled=enabled,
        imports_open=enabled and not admin_secret(),
    )


@app.post("/api/elections/import", response_model=ImportResponse)
async def import_election(
    body: ImportBody,
    response: Response,
    background: BackgroundTasks,
    wait_seconds: float = Query(0.0, ge=0.0, description="Block for up to this long for a result."),
    service: ImportService = Depends(get_service),
    _admin: None = Depends(require_admin),
    usage: UsageRecorder = Depends(get_usage),
) -> ImportResponse:
    """Import an election named by year, nation and — optionally — region.

    The owner's alone (:func:`require_admin`). The caller supplies no address:
    the resolver works out which election that is, and where its results are
    published. A request made before is served from storage without searching
    or extracting anything, and an import running or waiting to be checked is
    handed back rather than started again.
    """
    request = body.to_request()
    joined = await service.peek(request)
    if joined is not None:
        return await _answer(joined, response, service, wait_seconds)

    try:
        result = await service.submit(
            request, on_parse_failed=lambda: usage.record(UsageEvent.IMPORT_FAILED)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    if not result.reused:
        # A join to an import started between the look above and the claim
        # searched for and read nothing, so it is not counted as one.
        background.add_task(usage.record, UsageEvent.IMPORT_STARTED)

    return await _answer(result, response, service, wait_seconds)


async def _answer(
    result: ImportResult, response: Response, service: ImportService, wait_seconds: float
) -> ImportResponse:
    """Wait up to ``wait_seconds`` on an import still running, then say where it stands."""
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(
            result.request_key, min(wait_seconds, max_wait_seconds())
        )
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)


@app.get("/api/elections", response_model=list[ElectionSummary])
async def list_elections(
    service: ImportService = Depends(get_service),
) -> list[ElectionSummary]:
    """Every stored election, to anyone."""
    return [ElectionSummary.of(stored) for stored in await service.list_elections()]


@app.get("/api/elections/lookup", response_model=ImportResponse)
async def lookup_request(
    year: int,
    nation: str,
    subnation: str | None = None,
    service: ImportService = Depends(get_service),
) -> ImportResponse:
    """Has this election been imported already? Answered without looking it up.

    Two questions in one, both free. First: is there a job for exactly this
    request — one running, one waiting to be confirmed, one that failed? Then:
    is the election itself already stored, whoever asked for it and however they
    spelled it, which the store can answer from the year and the place alone.

    Open to anyone: it is store reads behind the read cache, and nothing here
    searches, fetches or extracts.
    """
    try:
        body = ImportBody(year=year, nation=nation, subnation=subnation)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    request = body.to_request()
    key = ImportService.request_key_for(request)
    result = await service.status(key)
    if result.state is not ImportState.UNKNOWN:
        return ImportResponse.of(result)

    stored = await service.find_by_place(request.year, request.nation, request.subnation)
    if stored is None:
        return ImportResponse.of(result)
    return ImportResponse(
        request_key=key,
        state=ImportState.READY,
        election=stored.election,
        election_hash=stored.election_hash,
        reused=True,
    )


@app.post(
    "/api/elections/requests",
    response_model=ElectionRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_election(
    body: ImportBody,
    response: Response,
    background: BackgroundTasks,
    service: ImportService = Depends(get_service),
    wishlist: Wishlist = Depends(get_wishlist_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> ElectionRequestResponse:
    """Ask for an election nobody has imported. Open to anyone who can see the page.

    Importing makes the server go and read pages, which costs money and is the
    owner's to do; writing down *which* election was wanted costs nothing and is
    worth more than a refusal. So a visitor's submit ends up here, and the
    election is filed in the issue tracker to be imported later.

    Nothing here searches, fetches, extracts or spends, so it asks for nobody.
    What it does do is *write*, to a public tracker, on behalf of a caller nobody
    authenticated — bounded by one issue per election and by the rate limiting
    in front of the app, and accepted as such (doc/threat-model.md T12).

    An election already held in the store is never filed: it is handed back the
    way the lookup would, because asking for it was a mistake about what is
    there, not a request for work.
    """
    request = body.to_request()
    stored = await service.find_by_place(request.year, request.nation, request.subnation)
    if stored is not None:
        await run_in_threadpool(usage.record, UsageEvent.REQUEST_ALREADY_IMPORTED)
        raise HTTPException(
            409,
            detail="this election is already imported — pick it in the list",
        )

    try:
        # Only the election: the issue is public, and nobody is asked who they are.
        filed = await wishlist.file(request)
    except WishlistUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from None

    if filed.duplicate:
        # Somebody asked first. Nothing was written, so this is not a creation.
        response.status_code = status.HTTP_200_OK
    background.add_task(
        usage.record,
        UsageEvent.REQUEST_DUPLICATE if filed.duplicate else UsageEvent.REQUEST_FILED,
    )
    log.info(
        "election request %s from anonymous issue=%s%s",
        scrub(request.describe()),
        filed.number,
        " (already open)" if filed.duplicate else "",
    )
    return ElectionRequestResponse(
        url=filed.url, number=filed.number, duplicate=filed.duplicate
    )


@app.get("/api/elections/imports/{request_key}", response_model=ImportResponse)
async def get_import(
    request_key: str,
    response: Response,
    wait_seconds: float = Query(0.0, ge=0.0),
    service: ImportService = Depends(get_service),
    _admin: None = Depends(require_admin),
) -> ImportResponse:
    """How one import is getting on. Polled while the state is pending."""
    result = await service.status(request_key)
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(request_key, min(wait_seconds, max_wait_seconds()))
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no such import")
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)


@app.post("/api/elections/imports/{request_key}/confirm", response_model=ImportResponse)
async def confirm_election(
    request_key: str,
    background: BackgroundTasks,
    option: int | None = Query(
        None, ge=0, description="Which offered forecast to save, for an election not yet held."
    ),
    service: ImportService = Depends(get_service),
    _admin: None = Depends(require_admin),
    usage: UsageRecorder = Depends(get_usage),
) -> ImportResponse:
    """Save a previewed election under the identity that was read off the page.

    The owner's alone (:func:`require_admin`). Every forecast of an upcoming
    election may be saved, not just one.
    """
    try:
        result = await service.confirm(request_key, option)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no preview to confirm for that import")
    if result.state is ImportState.CHOOSE:
        raise HTTPException(
            status_code=409, detail="choose which forecast to save with ?option="
        )
    if result.state is not ImportState.READY:
        raise HTTPException(
            status_code=409, detail=f"nothing awaiting confirmation (state: {result.state.value})"
        )
    if not result.duplicate:
        background.add_task(
            usage.record,
            UsageEvent.IMPORT_SAVED_RESULT if option is None else UsageEvent.IMPORT_SAVED_FORECAST,
        )
    return ImportResponse.of(result)


@app.delete(
    "/api/elections/imports/{request_key}/preview", status_code=status.HTTP_204_NO_CONTENT
)
async def discard_preview(
    request_key: str,
    service: ImportService = Depends(get_service),
    _admin: None = Depends(require_admin),
    usage: UsageRecorder = Depends(get_usage),
) -> Response:
    """Reject a previewed election, leaving the request free to import again.

    The owner's alone (:func:`require_admin`).
    """
    if not await service.discard(request_key):
        raise HTTPException(status_code=404, detail="no preview to discard for that import")
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        background=BackgroundTask(usage.record, UsageEvent.PREVIEW_DISCARDED),
    )


@app.get("/api/elections/{election_hash}", response_model=ImportResponse)
async def get_election(
    election_hash: str,
    background: BackgroundTasks,
    service: ImportService = Depends(get_service),
    usage: UsageRecorder = Depends(get_usage),
) -> ImportResponse:
    """Fetch a stored election by identity, for the picker. Open to anyone."""
    stored = await service.get_stored(election_hash)
    if stored is None:
        raise HTTPException(status_code=404, detail="no stored election with that hash")
    # Only the picker asks for one election by hash, so this is a pick.
    background.add_task(usage.record, UsageEvent.ELECTION_PICKED)
    return ImportResponse(
        request_key="",
        state=ImportState.READY,
        election=stored.election,
        election_hash=election_hash,
    )


# --- usage reports ----------------------------------------------------------

#: The header the scheduled caller (the Worker's cron) proves itself with.
REPORT_SECRET_HEADER = "x-report-secret"


class UsageReport(BaseModel):
    subject: str
    body: str


class ReportRun(BaseModel):
    sent: list[str]
    skipped: list[str]
    """Reports a previous run already sent."""


def _usage_report(period: Period, today: date, usage: UsageRecorder) -> tuple[str, UsageReport]:
    """The report for the last complete ``period`` before ``today``, and its key."""
    current = period_range(period, today)
    previous = previous_range(period, current)
    subject, body = build_report(
        period,
        current,
        gather(usage.store, current),
        previous,
        gather(usage.store, previous),
    )
    return report_key(period, current), UsageReport(subject=subject, body=body)


def require_schedule(x_report_secret: str | None = Header(default=None)) -> None:
    """The caller is the daily schedule, proven by ``USAGE_REPORT_SECRET``.

    The endpoint behind this belongs to nobody who visits; the Worker's cron
    calls it. Without a secret configured they do not exist, which is the honest
    answer on a deployment that runs no schedule.
    """
    expected = usage_report_secret()
    if not expected:
        raise HTTPException(404, detail="Not Found")
    if not hmac.compare_digest((x_report_secret or "").encode(), expected.encode()):
        raise HTTPException(403, detail="this is for the report schedule")


@app.post("/api/internal/usage-reports", response_model=ReportRun)
async def send_usage_reports(
    period: Period | None = Query(None, description="Send this report now instead of the due ones."),
    _schedule: None = Depends(require_schedule),
    usage: UsageRecorder = Depends(get_usage),
    mailer: Mailer = Depends(get_mailer_provider),
) -> ReportRun:
    """Email the usage reports that are due. Called once a day by the Worker's cron.

    Each report is claimed before it is sent, so a run that is retried — or a
    cron that fires twice — sends nothing twice; a report whose email failed
    gives its claim back for the next run.
    """
    today = usage.today()

    sent: list[str] = []
    skipped: list[str] = []
    for due in [period] if period else due_periods(today):
        key, report = await run_in_threadpool(_usage_report, due, today, usage)
        if not await run_in_threadpool(usage.store.claim_report, key):
            skipped.append(key)
            continue
        try:
            await run_in_threadpool(mailer.send, report.subject, report.body)
        except MailUnavailable as exc:
            await run_in_threadpool(usage.store.release_report, key)
            raise HTTPException(502, detail=str(exc)) from None
        log.info("usage report sent %s", key)
        sent.append(key)
    return ReportRun(sent=sent, skipped=skipped)


@app.get("/api/admin/usage", response_model=UsageReport)
async def preview_usage_report(
    period: Period = Query(Period.DAILY),
    before: date | None = Query(
        None, description="Report the last complete period before this day; today by default."
    ),
    _admin: None = Depends(require_admin),
    usage: UsageRecorder = Depends(get_usage),
) -> UsageReport:
    """A usage report as it would be emailed, sent nowhere."""
    _, report = await run_in_threadpool(_usage_report, period, before or usage.today(), usage)
    return report


# Serving the page from this app keeps the API same-origin, which is what the
# frontend expects by default. Mounted last so it cannot shadow the API routes.
#
# The page and its scripts, and nothing else: mounting the directory itself
# would serve whatever sits next to them, which locally is the repo root —
# .env and its keys included.
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2]))
if (FRONTEND_DIR / "index.html").is_file():

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="scripts")
