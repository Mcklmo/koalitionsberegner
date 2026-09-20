"""FastAPI surface over the shared election store.

Access follows one rule: *viewing and asking are open, importing is the
owner's*. Anyone sees everything stored and may ask for an election that is
missing; only a caller presenting ``ADMIN_SECRET`` can make the server go and
look for an election it does not already hold, because that is the one thing
here that costs money. An election once imported is served to everyone for
free, which is the whole point of the shared store.
"""

from __future__ import annotations

import dataclasses
import hmac
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

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

from .calendar import CalendarScanner
from .config import (
    admin_secret,
    get_calendar_scanner,
    get_mailer,
    get_outreach_store,
    get_parser,
    get_reddit_poster,
    get_refresh_config,
    get_refresh_parser,
    get_store,
    get_tracked_store,
    get_usage_recorder,
    get_wishlist,
    imports_enabled,
    issues_url,
    max_wait_seconds,
    origin_secret,
    outreach_allowed_subreddits,
    outreach_daily_cap,
    outreach_subreddit_weekly_cap,
    public_base_url,
    refresh_max_per_tick,
    usage_report_secret,
    validate_configuration,
)
from .identity import normalize_year, request_key
from .mailer import Mailer, MailUnavailable
from .observability import configure_logging, io_span, scrub
from .og_image import render as render_og_image
from .outreach import (
    DraftStatus,
    NewDraft,
    OutreachDraft,
    OutreachStore,
    REJECTABLE_STATUSES,
    RETRYABLE_STATUSES,
    TOKEN_LIFETIME_SECONDS,
    clean as clean_outreach,
    finalize_reply_text,
    hash_token,
    new_token,
)
from .reddit import RedditPoster, RedditUnavailable
from .refresh import RefreshService
from .refresh_config import RefreshConfig
from .schema import Election, Forecast, clean_text
from .service import AmbiguousId, ImportRequest, ImportResult, ImportService, ImportState
from .share import card as build_card
from .share import image_etag, parse_seats, parse_selection, valid_id
from .store import ElectionStore, StoredElection, TrackedElection, TrackedStatus, TrackedStore
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


def get_outreach_store_provider() -> OutreachStore:
    return get_outreach_store()


def get_reddit_poster_provider() -> RedditPoster:
    return get_reddit_poster()


def get_store_provider() -> ElectionStore:
    return get_store()


def get_tracked_store_provider() -> TrackedStore:
    return get_tracked_store()


def get_refresh_parser_provider():
    return get_refresh_parser()


def get_refresh_config_provider() -> RefreshConfig:
    return get_refresh_config()


def get_calendar_scanner_provider() -> CalendarScanner:
    return get_calendar_scanner()


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
    provenance: str = "manual"
    """``auto`` for an election the scheduled refresh stored, which nobody
    confirmed — the page footnotes those and offers a way to report them."""

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
            provenance=stored.provenance,
        )


class PublicConfig(BaseModel):
    """Everything the page needs before it offers the import form."""

    requests_enabled: bool
    """Whether anyone may ask for an election to be imported."""
    imports_enabled: bool
    """Whether this deployment imports at all; the owner still needs the secret."""
    imports_open: bool
    """Whether importing needs no secret here: a local run with none configured."""
    issues_url: str = ""
    """Where a wrong figure is reported, empty where no repository is configured."""


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
        issues_url=issues_url(),
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


async def _resolve_shared_id(election_hash: str, service: ImportService) -> StoredElection:
    """The stored election a share link's id names: a hash or a prefix of one.

    Shared by the picker route and the two link-preview routes below: all three
    take the same id and answer the same way when it does not resolve to
    exactly one election (``doc/plans/02-share-links.md``, WP1).
    """
    if not valid_id(election_hash):
        raise HTTPException(
            status_code=422, detail="id must be 12 to 64 lowercase hex characters"
        )
    try:
        stored = await service.resolve_id(election_hash)
    except AmbiguousId:
        raise HTTPException(
            status_code=409, detail="that prefix matches more than one election"
        ) from None
    if stored is None:
        raise HTTPException(status_code=404, detail="no stored election with that hash")
    return stored


@app.get("/api/elections/{election_hash}", response_model=ImportResponse)
async def get_election(
    election_hash: str,
    background: BackgroundTasks,
    service: ImportService = Depends(get_service),
    usage: UsageRecorder = Depends(get_usage),
) -> ImportResponse:
    """Fetch a stored election by its hash or a prefix of it. Open to anyone."""
    stored = await _resolve_shared_id(election_hash, service)
    # Only the picker asks for one election by hash, so this is a pick.
    background.add_task(usage.record, UsageEvent.ELECTION_PICKED)
    return ImportResponse(
        request_key="",
        state=ImportState.READY,
        election=stored.election,
        election_hash=stored.election_hash,
    )


# --- shared links: the card a link unfurls into, and its preview image ------

SHARE_CACHE_CONTROL = "public, max-age=3600"


def _first_param(request: Request, name: str) -> str | None:
    """The first value of a query parameter, ignoring any repeats.

    ``js/share.js``'s ``parseLocation`` reads ``c``/``s`` with
    ``URLSearchParams.get``, which returns the first value of a repeated
    parameter (``?c=0&c=3``); FastAPI's own ``Query`` binding returns the
    *last* one for a plain scalar. Reading the raw list here keeps the two
    sides agreeing on which one a stray duplicate means.
    """
    values = request.query_params.getlist(name)
    return values[0] if values else None


@app.get("/api/elections/{election_hash}/card")
async def get_card(
    election_hash: str,
    request: Request,
    background: BackgroundTasks,
    service: ImportService = Depends(get_service),
    usage: UsageRecorder = Depends(get_usage),
) -> Response:
    """The wording and numbers a shared link unfurls into.

    The Worker calls this once per ``/e/*`` page view, crawlers included, to
    fill in the page's ``<meta>`` tags; :func:`get_og_image` renders the same
    :class:`~app.share.Card` as the picture, so the two never disagree.

    ``c`` (selected parties, as positions: ``0,2,3``) and ``s`` (the seat
    total the link was made with) are read straight off the request rather
    than bound by FastAPI, so a duplicated parameter is not read differently
    here than the frontend reads it (see :func:`_first_param`).
    """
    stored = await _resolve_shared_id(election_hash, service)
    selection = parse_selection(_first_param(request, "c"), stored.election)
    seats_claimed = parse_seats(_first_param(request, "s"))
    result = build_card(stored.election, stored.election_hash, selection, seats_claimed)
    background.add_task(usage.record, UsageEvent.LINK_OPENED)
    return JSONResponse(
        dataclasses.asdict(result), headers={"Cache-Control": SHARE_CACHE_CONTROL}
    )


@app.get("/api/og/{election_hash}.png")
async def get_og_image(
    election_hash: str,
    request: Request,
    service: ImportService = Depends(get_service),
) -> Response:
    """The 1200x630 preview image a shared link unfurls into.

    ``c``/``s`` are read the same way :func:`get_card` reads them — see
    :func:`_first_param`.
    """
    stored = await _resolve_shared_id(election_hash, service)
    selection = parse_selection(_first_param(request, "c"), stored.election)
    seats_claimed = parse_seats(_first_param(request, "s"))
    result = build_card(stored.election, stored.election_hash, selection, seats_claimed)
    etag = image_etag(stored.election_hash, selection, seats_claimed, stored.stored_at)
    image_bytes = await run_in_threadpool(render_og_image, result)
    return Response(
        image_bytes,
        media_type="image/png",
        headers={"Cache-Control": SHARE_CACHE_CONTROL, "ETag": etag},
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


# --- tracked elections and their scheduled refresh --------------------------
# doc/plans/03-remaining-work.md, section A. The Worker's crons call the two
# ``/api/internal`` routes with ``x-report-secret`` (`require_schedule`); the
# ``/api/admin`` routes are the owner's own table, reached from no page —
# there is no admin UI for this yet, only `curl` and `x-admin-secret`.


class RefreshRun(BaseModel):
    """What one refresh tick did. The Worker's cron logs it; nobody else sees it."""

    refreshed: int
    stored_polls: int
    stored_results: int
    finalised: int
    failed: int
    skipped_unchanged: int


@app.post("/api/internal/refresh", response_model=RefreshRun)
async def run_refresh(
    _schedule: None = Depends(require_schedule),
    tracked_store: TrackedStore = Depends(get_tracked_store_provider),
    store: ElectionStore = Depends(get_store_provider),
    parser=Depends(get_refresh_parser_provider),
    config: RefreshConfig = Depends(get_refresh_config_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> RefreshRun:
    """Refresh the tracked elections that are due. Called every 30 minutes.

    Sequential and synchronous on purpose (plan 3, A3): Cloud Run throttles
    CPU after the response unless the service is set to always-on, and the
    Worker's fetch is content to wait a minute for this one. The cap
    (``REFRESH_MAX_PER_TICK``) is what keeps one tick inside Cloud Run's own
    request timeout.

    Every tracked election taken up here is counted into the daily usage
    report's "Tracked elections" section (plan 3, A5) — numbers only, never
    which election; ``GET /api/admin/tracked`` still names those.
    """
    now = datetime.now(UTC)
    due = await run_in_threadpool(tracked_store.due_tracked, now, refresh_max_per_tick())
    service = RefreshService(tracked_store, store, parser, config)
    counts = {
        "stored_polls": 0, "stored_results": 0, "finalised": 0, "failed": 0, "skipped_unchanged": 0,
    }
    for tracked in due:
        outcome = await service.run(tracked)
        counts["stored_polls"] += outcome.stored_polls
        counts["stored_results"] += outcome.stored_results
        counts["finalised"] += int(outcome.finalised)
        counts["failed"] += int(outcome.failed)
        counts["skipped_unchanged"] += int(outcome.skipped_unchanged)
        await run_in_threadpool(usage.record, UsageEvent.TRACKED_REFRESHED)
        if outcome.failed:
            await run_in_threadpool(usage.record, UsageEvent.TRACKED_FAILED)
        if outcome.parked:
            await run_in_threadpool(usage.record, UsageEvent.TRACKED_PARKED)
    return RefreshRun(refreshed=len(due), **counts)


class CalendarScanOut(BaseModel):
    """What a calendar scan proposed. The owner reads this before it runs unwatched."""

    proposed: int
    tracked: int
    skipped: dict[str, int]
    failures: list[str]


@app.post("/api/internal/calendar-scan", response_model=CalendarScanOut)
async def run_calendar_scan(
    years: str = Query(..., description="Years to scan, comma-separated: 2026,2027"),
    _schedule: None = Depends(require_schedule),
    scanner: CalendarScanner = Depends(get_calendar_scanner_provider),
    tracked_store: TrackedStore = Depends(get_tracked_store_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> CalendarScanOut:
    """Propose elections to track from Wikidata and Wikipedia's calendars.

    Called once a month by the Worker's cron. Every proposal that survives
    de-duplication is tracked at once, ``added_by="calendar"``: what needs a
    human is not whether it gets tracked but whether it should have been
    (plan 3, A5) — the owner reads the report and untracks what does not
    belong through :func:`update_tracked_election`.
    """
    try:
        years_wanted = [int(part.strip()) for part in years.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(422, detail="years must be a comma-separated list of years") from None
    if not years_wanted:
        raise HTTPException(422, detail="years must name at least one year")

    existing = await run_in_threadpool(tracked_store.list_tracked)
    already_tracked = {row.request_key for row in existing}
    try:
        result = await scanner.scan(years_wanted, tracked=already_tracked)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from None

    added = 0
    for entry in result.entries:
        new_row = TrackedElection(
            request_key=entry.request_key, year=entry.year, nation=entry.nation,
            subnation=entry.state, election_date=entry.election_date,
            status=TrackedStatus.UPCOMING, added_by="calendar",
        )
        if await run_in_threadpool(tracked_store.add_tracked, new_row):
            added += 1
            await run_in_threadpool(usage.record, UsageEvent.TRACKED_ADDED)
    return CalendarScanOut(
        proposed=len(result.entries), tracked=added, skipped=result.skipped, failures=result.failures
    )


class TrackedOut(BaseModel):
    """One tracked election, for the owner's table. Nobody else sees this route."""

    request_key: str
    year: int
    nation: str
    subnation: str | None
    election_date: date | None
    status: TrackedStatus
    last_refresh_at: datetime | None
    next_refresh_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    result_hash: str | None
    unchanged_reads: int
    added_by: str

    @classmethod
    def of(cls, tracked: TrackedElection) -> "TrackedOut":
        return cls(**{name: getattr(tracked, name) for name in cls.model_fields})


@app.get("/api/admin/tracked", response_model=list[TrackedOut])
async def list_tracked_elections(
    _admin: None = Depends(require_admin),
    tracked_store: TrackedStore = Depends(get_tracked_store_provider),
) -> list[TrackedOut]:
    """Every tracked election, soonest due first. The owner's alone."""
    rows = await run_in_threadpool(tracked_store.list_tracked)
    return [TrackedOut.of(row) for row in rows]


class TrackedIn(BaseModel):
    """A tracked election the owner adds by hand — the rollout's first step (A7)."""

    model_config = ConfigDict(extra="forbid")

    year: int
    nation: str
    subnation: str | None = None
    election_date: date

    @field_validator("nation")
    @classmethod
    def _nation(cls, value: str) -> str:
        return clean_text(value, field="nation")

    @field_validator("subnation")
    @classmethod
    def _subnation(cls, value: str | None) -> str | None:
        return None if value is None else clean_text(value, field="subnation")


@app.post("/api/admin/tracked", status_code=status.HTTP_201_CREATED, response_model=TrackedOut)
async def add_tracked_election(
    body: TrackedIn,
    _admin: None = Depends(require_admin),
    tracked_store: TrackedStore = Depends(get_tracked_store_provider),
) -> TrackedOut:
    try:
        year = normalize_year(body.year)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from None
    try:
        key = request_key(year, body.nation, body.subnation)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    new_row = TrackedElection(
        request_key=key, year=year, nation=body.nation, subnation=body.subnation,
        election_date=body.election_date, status=TrackedStatus.UPCOMING, added_by="owner",
    )
    added = await run_in_threadpool(tracked_store.add_tracked, new_row)
    if not added:
        raise HTTPException(409, detail="this election is already tracked")
    return TrackedOut.of(new_row)


class TrackedPatch(BaseModel):
    """What the owner may change about a tracked election."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["untracked"] | None = None
    election_date: date | None = None
    refresh_now: bool = False


@app.put("/api/admin/tracked/{request_key}", response_model=TrackedOut)
async def update_tracked_election(
    request_key: str,
    body: TrackedPatch,
    _admin: None = Depends(require_admin),
    tracked_store: TrackedStore = Depends(get_tracked_store_provider),
) -> TrackedOut:
    """Untrack it, correct its date, or make it due right now.

    ``refresh_now`` reactivates a parked or corrected row (a park is the
    scheduler giving up; the owner asking for one more try is not it holding
    an opinion) by putting it back among the active statuses and clearing
    ``next_refresh_at`` — which the schedule already reads as "due now"
    (plan 3, A1).
    """
    current = await run_in_threadpool(tracked_store.get_tracked, request_key)
    if current is None:
        raise HTTPException(404, detail="no such tracked election")

    fields: dict = {}
    if body.status == "untracked":
        fields.update(status=TrackedStatus.UNTRACKED, next_refresh_at=None)
    if body.election_date is not None:
        fields["election_date"] = body.election_date
    if body.refresh_now:
        if current.status is TrackedStatus.FINAL:
            raise HTTPException(409, detail="a final election is not read again")
        fields.setdefault(
            "status", TrackedStatus.COUNTING if current.result_hash else TrackedStatus.UPCOMING
        )
        fields.update(next_refresh_at=None, consecutive_failures=0, last_error=None)
    if not fields:
        raise HTTPException(422, detail="nothing to change")

    updated = await run_in_threadpool(tracked_store.update_tracked, request_key, **fields)
    if updated is None:
        raise HTTPException(404, detail="no such tracked election")
    return TrackedOut.of(updated)


# --- outreach: the approval gate --------------------------------------------
# doc/plans/04-reddit-outreach.md. The plugin (plugins/outreach/scripts/scan.py)
# is the only caller of the first route, with the owner's secret; the rest are
# the owner's browser, reached from the emailed link. Two factors gate a send:
# the one-time token in the link, and ADMIN_SECRET pasted into this tab
# (require_admin) — see the plan's "Does this bring Firebase back? No."

#: A reply's parent, Reddit's own shape: t3_<id> for a post, t1_<id> for a
#: comment. Re-checked here, on what the plugin sent, before it is ever stored.
_THING_ID = re.compile(r"t[13]_[0-9a-z]{1,16}")

#: An election hash or a prefix of one — the same shape `app.share.valid_id`
#: accepts, reused rather than duplicated.


class OutreachDraftIn(BaseModel):
    """One accepted find, exactly as ``scan.py`` posts it.

    See doc/plans/04-reddit-outreach.md, "What already exists", for the field
    table this mirrors. Every free-text field is re-cleaned here — normalised,
    no control or invisible characters — because nothing that crosses the
    network is trusted twice, even though the plugin already sanitised it.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["reddit"]
    subreddit: str
    thing_id: str
    kind: Literal["post", "comment"]
    permalink: str
    title: str
    excerpt: str
    election_hash: str
    election_title: str
    link: str
    reply_text: str
    verification: dict
    classifier_reason: str
    created_at: str

    @field_validator("subreddit")
    @classmethod
    def _subreddit(cls, value: str) -> str:
        # Lowercased once, here, so every later comparison -- the allowed
        # list, thread etiquette's per-subreddit cap -- agrees on the same
        # spelling. Before this, "Denmark" and "denmark" were stored as two
        # different subreddits and counted separately against the weekly cap.
        return clean_outreach(value, field="subreddit", max_len=100).lower()

    @field_validator("thing_id")
    @classmethod
    def _thing_id(cls, value: str) -> str:
        if not isinstance(value, str) or not _THING_ID.fullmatch(value):
            raise ValueError("thing_id must be a post or comment fullname (t3_… or t1_…)")
        return value

    @field_validator("permalink", "link")
    @classmethod
    def _url_field(cls, value: str, info) -> str:
        text = clean_outreach(value, field=info.field_name, max_len=2000)
        if not re.match(r"^https?://", text):
            raise ValueError(f"{info.field_name} must be an absolute URL")
        return text

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return clean_outreach(value, field="title", max_len=500)

    @field_validator("excerpt")
    @classmethod
    def _excerpt(cls, value: str) -> str:
        # The only field that carries Reddit's own prose rather than a single
        # line typed into a form, so it is the only one that keeps the
        # newlines and tabs scan.py's own clean_text lets through -- without
        # this, real multi-paragraph excerpts 422 here on nearly every draft.
        return clean_outreach(value, field="excerpt", max_len=1000, allow_newlines=True)

    @field_validator("election_hash")
    @classmethod
    def _election_hash(cls, value: str) -> str:
        if not isinstance(value, str) or not valid_id(value):
            raise ValueError("election_hash must be a hex election hash")
        return value

    @field_validator("election_title")
    @classmethod
    def _election_title(cls, value: str) -> str:
        return clean_outreach(value, field="election_title", max_len=300)

    @field_validator("classifier_reason")
    @classmethod
    def _classifier_reason(cls, value: str) -> str:
        return clean_outreach(value, field="classifier_reason", max_len=1000)

    @field_validator("created_at")
    @classmethod
    def _created_at(cls, value: str) -> str:
        return clean_outreach(value, field="created_at", max_len=64)


class OutreachDraftOut(BaseModel):
    """A queued draft, for the owner's own eyes (``require_admin``)."""

    id: str
    source: str
    subreddit: str
    thing_id: str
    thread_id: str
    kind: str
    permalink: str
    title: str
    excerpt: str
    election_hash: str
    election_title: str
    link: str
    reply_text: str
    verification: dict
    classifier_reason: str
    created_at: str
    status: DraftStatus
    emailed_at: float | None = None
    decided_at: float | None = None
    posted_at: float | None = None
    posted_url: str | None = None
    last_error: str | None = None
    edited: bool = False

    @classmethod
    def of(cls, draft: OutreachDraft) -> "OutreachDraftOut":
        return cls(**{name: getattr(draft, name) for name in cls.model_fields})


class OutreachSendBody(BaseModel):
    """What the owner may change before sending: the text alone."""

    model_config = ConfigDict(extra="forbid")

    reply_text: str | None = None


#: How long an approval token has been usable when this is queued, in seconds.
DRAFT_ID_BYTES = 8  # "draft_" + 16 hex characters (doc/plans/04, "Storage").
ROLLING_WEEK_SECONDS = 7 * 24 * 3600
ROLLING_DAY_SECONDS = 24 * 3600


def _new_draft_id() -> str:
    return "draft_" + secrets.token_hex(DRAFT_ID_BYTES)


def _approval_url(token: str) -> str:
    base = public_base_url()
    return f"{base}/approve/{token}" if base else f"/approve/{token}"


def _draft_email(draft: NewDraft, token: str) -> tuple[str, str]:
    subject = f"Approve a reply in r/{draft.subreddit} — {draft.election_title}"
    body = "\n\n".join((
        f"Thread: {draft.title}",
        f"Link: {draft.permalink}",
        f"Excerpt:\n{draft.excerpt}",
        f"Proposed reply:\n{draft.reply_text}",
        f"About: {draft.election_title} ({draft.link})",
        f"Approve or reject it here: {_approval_url(token)}",
        "This link works once and expires in 72 hours.",
    ))
    return subject, body


@app.post("/api/admin/outreach/drafts", status_code=status.HTTP_201_CREATED)
async def queue_outreach_draft(
    body: OutreachDraftIn,
    background: BackgroundTasks,
    _admin: None = Depends(require_admin),
    store: OutreachStore = Depends(get_outreach_store_provider),
    mailer: Mailer = Depends(get_mailer_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> dict:
    """Queue a reply the ``outreach`` plugin found, and email the owner to approve it.

    The plugin is the only caller (``x-admin-secret``, `require_admin`).
    ``409`` for a ``thing_id`` already queued — the plugin reports that as
    "already queued" and moves on. ``503`` when nobody could be emailed about
    it: a draft nobody can approve is not stored.
    """
    if not mailer.enabled:
        raise HTTPException(503, detail="outreach needs the usage-report mailer configured")

    try:
        reply_text = finalize_reply_text(body.reply_text, own_link=body.link)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    draft = NewDraft(
        source=body.source,
        subreddit=body.subreddit,
        thing_id=body.thing_id,
        kind=body.kind,
        permalink=body.permalink,
        title=body.title,
        excerpt=body.excerpt,
        election_hash=body.election_hash,
        election_title=body.election_title,
        link=body.link,
        reply_text=reply_text,
        verification=body.verification,
        classifier_reason=body.classifier_reason,
        created_at=body.created_at,
    )
    draft_id = _new_draft_id()
    token, token_hash = new_token()
    now = time.time()
    created = await run_in_threadpool(
        store.create, draft_id, draft,
        token_hash=token_hash, token_expires_at=now + TOKEN_LIFETIME_SECONDS, emailed_at=now,
    )
    if created is None:
        raise HTTPException(409, detail="a draft for this thing_id is already queued")

    subject, email_body = _draft_email(draft, token)
    try:
        await run_in_threadpool(mailer.send, subject, email_body)
    except MailUnavailable as exc:
        await run_in_threadpool(store.delete, draft_id)
        raise HTTPException(502, detail=str(exc)) from None

    background.add_task(usage.record, UsageEvent.OUTREACH_QUEUED)
    return {"id": draft_id}


@app.get("/api/admin/outreach/drafts", response_model=list[OutreachDraftOut])
async def list_outreach_drafts(
    _admin: None = Depends(require_admin),
    store: OutreachStore = Depends(get_outreach_store_provider),
) -> list[OutreachDraftOut]:
    """The queue, for the daily report and for a second look. The owner's alone."""
    drafts = await run_in_threadpool(store.list_drafts)
    return [OutreachDraftOut.of(draft) for draft in drafts]


async def _outreach_draft_for_token(
    token: str, store: OutreachStore, usage: UsageRecorder
) -> OutreachDraft:
    """The draft an approval token names, or the ``404`` every wrong guess gets.

    Unknown, already-consumed and expired tokens all answer identically —
    never say which. An expiry found here consumes the token and records it,
    so the same stale link never does this twice.

    The usage event is recorded synchronously, not via ``BackgroundTasks``:
    this helper is called right before its caller may itself raise (a ``409``
    from a status or etiquette check), and FastAPI drops a request's
    background tasks entirely when the endpoint raises instead of returning —
    a task scheduled here would silently never run.
    """
    draft = await run_in_threadpool(store.get_by_token_hash, hash_token(token))
    if draft is None:
        raise HTTPException(404, detail="no such approval")
    if draft.token_expires_at is None or draft.token_expires_at < time.time():
        await run_in_threadpool(
            store.set_status, draft.id, DraftStatus.EXPIRED, consume_token=True
        )
        await run_in_threadpool(usage.record, UsageEvent.OUTREACH_EXPIRED)
        raise HTTPException(404, detail="no such approval")
    return draft


@app.get("/api/outreach/approval/{token}")
async def get_outreach_approval(
    token: str,
    background: BackgroundTasks,
    store: OutreachStore = Depends(get_outreach_store_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> Response:
    """The draft a link names: the thread, the reply, what it links to, and
    when the link stops working. The token alone is enough to view; sending
    needs `x-admin-secret` as well (`send_outreach_reply`). Never cached, and
    marked for no search index — an approval link must never unfurl."""
    draft = await _outreach_draft_for_token(token, store, usage)
    payload = {
        "subreddit": draft.subreddit,
        "permalink": draft.permalink,
        "title": draft.title,
        "excerpt": draft.excerpt,
        "reply_text": draft.reply_text,
        "election_title": draft.election_title,
        "link": draft.link,
        "status": draft.status.value,
        "last_error": draft.last_error,
        "expires_at": draft.token_expires_at,
    }
    return JSONResponse(
        payload, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
    )


@app.post("/api/outreach/approval/{token}/send")
async def send_outreach_reply(
    token: str,
    background: BackgroundTasks,
    body: OutreachSendBody | None = None,
    _admin: None = Depends(require_admin),
    store: OutreachStore = Depends(get_outreach_store_provider),
    poster: RedditPoster = Depends(get_reddit_poster_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> dict:
    """Post the reply, consuming the link. Two factors: the token and
    `x-admin-secret` (`require_admin`) — see doc/plans/04-reddit-outreach.md,
    "Does this bring Firebase back? No."

    Refuses, without changing the draft, when a ceiling in section 5 already
    stopped it, when the draft is not pending or retryable, or when the
    (possibly owner-edited) text fails the same checks the plugin already
    applied. A Reddit failure that may have gone through anyway
    (`maybe_posted`) consumes the token instead of leaving a retry button:
    check the thread by hand before trying again, never a blind resend.

    The checks above this point read the draft the caller already holds, so
    two overlapping requests for the same token both pass them; what actually
    keeps them from both posting is `store.claim`, an atomic compare-and-swap
    from a retryable status to `APPROVED` that at most one caller can win.
    The loser gets this route's ordinary `409`, never reaching `poster`.
    """
    draft = await _outreach_draft_for_token(token, store, usage)

    if draft.status not in RETRYABLE_STATUSES:
        raise HTTPException(409, detail=f"this draft is already {draft.status.value}")
    if draft.subreddit not in outreach_allowed_subreddits():
        raise HTTPException(409, detail="this subreddit is not on this deployment's allowed list")
    if await run_in_threadpool(store.thread_posted, draft.thread_id):
        raise HTTPException(409, detail="a reply has already been posted in this thread")

    now = time.time()
    weekly = await run_in_threadpool(
        store.count_posted, draft.subreddit, now - ROLLING_WEEK_SECONDS
    )
    if weekly >= outreach_subreddit_weekly_cap():
        raise HTTPException(409, detail="this subreddit has reached its weekly reply limit")
    daily = await run_in_threadpool(store.count_posted_total, now - ROLLING_DAY_SECONDS)
    if daily >= outreach_daily_cap():
        raise HTTPException(409, detail="the daily reply limit has been reached")

    edited = bool(
        body and body.reply_text and body.reply_text.strip() != draft.reply_text.strip()
    )
    try:
        final_text = finalize_reply_text(
            body.reply_text if body and body.reply_text else draft.reply_text,
            own_link=draft.link,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    if not poster.enabled:
        raise HTTPException(503, detail="posting to Reddit is not configured")

    claimed = await run_in_threadpool(store.claim, draft.id, decided_at=now, edited=edited)
    if claimed is None:
        raise HTTPException(409, detail="this draft is already being sent or was already decided")

    # Recorded synchronously, not via BackgroundTasks: a failure below raises
    # rather than returns, and FastAPI discards a request's background tasks
    # entirely when the endpoint raises (see _outreach_draft_for_token).
    await run_in_threadpool(usage.record, UsageEvent.OUTREACH_APPROVED)

    try:
        posted = await poster.comment(draft.thing_id, final_text)
    except RedditUnavailable as exc:
        await run_in_threadpool(
            store.set_status, draft.id, DraftStatus.FAILED,
            consume_token=exc.maybe_posted, last_error=str(exc),
        )
        await run_in_threadpool(usage.record, UsageEvent.OUTREACH_FAILED)
        raise HTTPException(502, detail=str(exc)) from None

    await run_in_threadpool(
        store.set_status, draft.id, DraftStatus.POSTED, consume_token=True,
        posted_at=time.time(), posted_url=posted.url,
    )
    background.add_task(usage.record, UsageEvent.OUTREACH_POSTED)
    return {"posted_url": posted.url}


@app.post("/api/outreach/approval/{token}/reject")
async def reject_outreach_reply(
    token: str,
    background: BackgroundTasks,
    _admin: None = Depends(require_admin),
    store: OutreachStore = Depends(get_outreach_store_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> dict:
    """Decline a queued reply. The owner's alone, the same two factors as sending.

    Also accepted from `APPROVED`: a claimed draft whose send then hit
    anything other than a clean `RedditUnavailable` (a client disconnect, a
    cancelled request, a pod restart) never gets a further status update on
    its own, and without this the owner would find both this route and
    `/send` refusing forever with no way to close it out. `/send` itself still
    only accepts `RETRYABLE_STATUSES`, so this never reopens a path to
    resending — only to rejecting.
    """
    draft = await _outreach_draft_for_token(token, store, usage)
    if draft.status not in REJECTABLE_STATUSES:
        raise HTTPException(409, detail=f"this draft is already {draft.status.value}")
    await run_in_threadpool(
        store.set_status, draft.id, DraftStatus.REJECTED,
        consume_token=True, decided_at=time.time(),
    )
    background.add_task(usage.record, UsageEvent.OUTREACH_REJECTED)
    return {"status": "rejected"}


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

    # Local parity for shared links: in production the Worker owns `/e/*` and
    # injects the card's `<meta>` tags before this app ever sees the request,
    # so serving it here too is only for a local run with no Worker in front.
    # Plain `index.html`, no tags — `js/main.js` reads the id from the URL
    # itself, and nothing here can drift from the Worker because nothing here
    # runs in production.
    @app.api_route("/e/{election_id}", methods=["GET", "HEAD"], include_in_schema=False)
    def shared_page(election_id: str) -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="scripts")
