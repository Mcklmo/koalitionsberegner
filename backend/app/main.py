"""FastAPI surface over the shared election store.

Access follows one rule, applied in three places below: *viewing is open,
importing is bought*. Anyone sees everything stored, and only a subscriber with
quota left can make the server go and look for an election it does not already
hold.

The quota is charged for the one thing that costs money — going out to find and
read an election — and nothing else. An election somebody already imported is
served to every subscriber for free, which is the whole point of the shared
store.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
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

from .accounts import (
    ENTITLING_STATUSES,
    AccountStore,
    QuotaPolicy,
    Tier,
    UserAccount,
    billing_period,
)
from .auth import (
    BadCredentials,
    EmailTaken,
    IdentityRemover,
    InvalidToken,
    PasswordCredentialStore,
    Principal,
    Session,
    SignUpRefused,
    TokenVerifier,
    bearer_token,
)
from .billing import Billing, BillingUnavailable
from .config import (
    firebase_web_config,
    get_accounts,
    get_billing,
    get_identity_remover,
    get_mailer,
    get_parser,
    get_password_store,
    get_quota_policy,
    get_store,
    get_usage_recorder,
    get_verifier,
    get_wishlist,
    max_wait_seconds,
    origin_secret,
    payments_paused,
    public_base_url,
    usage_report_secret,
    validate_configuration,
)
from .identity import normalize_year
from .mailer import Mailer, MailUnavailable
from .observability import configure_logging, io_span, scrub
from .retention import sweep_inactive_accounts
from .schema import Election, Forecast, clean_text
from .service import ImportRequest, ImportResult, ImportService, ImportState
from .store import StoredElection
from .usage import (
    ACTIVE_RETENTION_DAYS,
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
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        # The ID token rides in Authorization, so a cross-origin page cannot
        # sign in without it being allowed through.
        allow_headers=["content-type", "authorization"],
    )


# --- the edge of the app ----------------------------------------------------
# Declared last, so they wrap everything above: a request refused here costs
# no store read, no token check and no log line of its own.

#: The page loads its scripts from here and signs in against these two Google
#: endpoints; nothing else is fetched, framed or submitted anywhere. Styles stay
#: inline-capable because the page carries its stylesheet and a few style
#: attributes in index.html — scripts do not, and that is what the policy is for.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self' https://identitytoolkit.googleapis.com"
        " https://securetoken.googleapis.com",
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


#: Every body this API reads is a few hundred bytes of JSON. Stripe's events are
#: larger, and still far below their own cap.
MAX_BODY_BYTES = 64 * 1024
MAX_WEBHOOK_BYTES = 1024 * 1024
WEBHOOK_PATH = "/api/billing/webhook"


class LimitRequestBody:
    """Refuse an oversized body before a byte of it is buffered.

    Starlette reads a body into memory whole, and nothing upstream caps it far
    below Cloud Run's 32 MB — so without this, a few dozen concurrent large
    POSTs to the unauthenticated webhook are enough to run the container out of
    memory. A body with no declared length is refused rather than counted:
    browsers and Stripe always declare one.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            limit = MAX_WEBHOOK_BYTES if scope["path"] == WEBHOOK_PATH else MAX_BODY_BYTES
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


def get_account_store() -> AccountStore:
    return get_accounts()


def get_token_verifier() -> TokenVerifier:
    return get_verifier()


def get_password_credentials() -> PasswordCredentialStore | None:
    """The store that owns passwords, in the modes where this app owns them."""
    return get_password_store()


def get_billing_provider() -> Billing:
    return get_billing()


def get_policy() -> QuotaPolicy:
    return get_quota_policy()


def get_wishlist_provider() -> Wishlist:
    return get_wishlist()


def get_usage() -> UsageRecorder:
    return get_usage_recorder()


def get_mailer_provider() -> Mailer:
    return get_mailer()


def get_identity_remover_provider() -> IdentityRemover:
    return get_identity_remover()


# --- identity ---------------------------------------------------------------

UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def current_principal(
    authorization: str | None = Header(default=None),
    verifier: TokenVerifier = Depends(get_token_verifier),
) -> Principal | None:
    """Who is calling. ``None`` is a signed-out visitor, which is allowed here.

    A *bad* credential is not the same as no credential: presenting an expired
    or forged token is refused outright rather than quietly downgraded to
    anonymous, which would hide a broken client behind a thinner page.
    """
    token = bearer_token(authorization)
    if token is None:
        return verifier.anonymous()
    try:
        return verifier.verify(token)
    except InvalidToken as exc:
        raise HTTPException(401, detail=str(exc), headers=UNAUTHENTICATED) from None


def require_principal(principal: Principal | None = Depends(current_principal)) -> Principal:
    if principal is None:
        raise HTTPException(401, detail="sign in to use this", headers=UNAUTHENTICATED)
    return principal


#: Why an unconfirmed caller is stopped, worded so the page can show it as is.
EMAIL_NOT_VERIFIED = "confirm your email address before using your account"


def require_verified(principal: Principal = Depends(require_principal)) -> Principal:
    """A signed-in caller whose address is known to be theirs.

    Signing up proves only that somebody typed an address. Until it is confirmed
    the account may say who it is (``/api/me``) and nothing more: it cannot
    spend, pay, curate, or see past the curated selection.
    """
    if not principal.email_verified:
        raise HTTPException(403, detail=EMAIL_NOT_VERIFIED)
    return principal


def require_account(
    principal: Principal = Depends(require_verified),
    accounts: AccountStore = Depends(get_account_store),
) -> UserAccount:
    """The caller's account, created free on first sight. Accounts cost nothing."""
    return accounts.ensure(principal.uid, principal.email)


def require_admin(principal: Principal = Depends(require_verified)) -> Principal:
    if not principal.admin:
        raise HTTPException(403, detail="this needs an administrator")
    return principal


async def require_importer(
    request_key: str,
    service: ImportService = Depends(get_service),
    principal: Principal = Depends(require_verified),
    _account: UserAccount = Depends(require_account),
) -> Principal:
    """The caller, if they started the import at ``request_key`` or administer the app.

    A request key follows from the year and the place alone, so anyone can know
    one. What an import produced is still its importer's to accept or throw
    away: saving a preview they would have rejected puts wrong numbers in front
    of every account, and discarding one makes them pay for the import again.

    A job with no recorded importer stays open to any account. Either it
    predates the record, and its lease runs out within minutes, or it is over —
    saved or failed — and there is nothing left to accept or throw away.
    """
    owner = await service.owner_of(request_key)
    if owner is not None and owner != principal.uid and not principal.admin:
        raise HTTPException(403, detail="only the account that started this import can do that")
    return principal


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


class TierInfo(BaseModel):
    tier: Tier
    monthly_imports: int
    purchasable: bool


class PublicConfig(BaseModel):
    """Everything the page needs before anyone has signed in."""

    auth_required: bool
    """False when gating is off, so the page can skip the whole sign-in flow."""
    auth_provider: str
    """Which sign-in flow the page should run: ``firebase``, ``password``, ``none``."""
    firebase: dict[str, str]
    billing_enabled: bool
    payments_paused: bool
    """Checkout is closed for repairs; existing subscriptions are unaffected."""
    requests_enabled: bool
    """Whether an account with no subscription can ask for an election instead."""
    tiers: list[TierInfo]


class AccountResponse(BaseModel):
    uid: str
    email: str | None
    email_verified: bool
    tier: Tier
    admin: bool
    period: str
    used: int
    limit: int
    remaining: int
    may_import: bool
    subscription_status: str | None = None
    billing_enabled: bool = False


class CredentialsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    """A session token, returned once. The server keeps only its hash."""

    token: str
    uid: str
    email: str
    expires_at: float


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Tier


class CheckoutResponse(BaseModel):
    url: str


class ElectionRequestResponse(BaseModel):
    """Where an election request was written down, so the page can link to it."""

    url: str
    number: int
    duplicate: bool = False
    """True when somebody had already asked for this election."""


def _account_response(
    account: UserAccount, principal: Principal, policy: QuotaPolicy, billing: Billing
) -> AccountResponse:
    period = billing_period()
    limit = policy.limit(account.tier)
    return AccountResponse(
        uid=account.uid,
        email=account.email,
        email_verified=principal.email_verified,
        tier=account.tier,
        admin=principal.admin,
        period=period,
        used=account.used_in(period),
        # A local run with gating off, or an administrator, is not held to any
        # tier; say so as no limit rather than reporting the free tier's zero.
        limit=-1 if principal.unmetered else limit,
        remaining=-1 if principal.unmetered else account.remaining(policy, period),
        may_import=principal.email_verified
        and (principal.unmetered or account.may_import(policy, period)),
        subscription_status=account.subscription_status,
        billing_enabled=billing.enabled,
    )


# --- endpoints --------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", response_model=PublicConfig)
def public_config(
    request: Request,
    background: BackgroundTasks,
    policy: QuotaPolicy = Depends(get_policy),
    billing: Billing = Depends(get_billing_provider),
    verifier: TokenVerifier = Depends(get_token_verifier),
    wishlist: Wishlist = Depends(get_wishlist_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> PublicConfig:
    """Public settings for the page: how to sign in, and what is for sale.

    The Firebase API key here identifies the project to Google's identity
    endpoints; it grants nothing on its own, and every authorisation decision
    is made server-side from the token it eventually produces.

    Every page load asks for this exactly once, which makes it where page loads
    are counted — by the language the browser already sends, and nothing else
    about the visitor. No script was added to the page to do it.
    """
    background.add_task(usage.record, page_load_key(request.headers.get("accept-language")))
    purchasable = set(billing.tiers())
    paused = payments_paused()
    return PublicConfig(
        # Asked of the verifier rather than the environment: it is the thing
        # that decides, and with gating off it hands out an identity to everyone.
        auth_required=verifier.anonymous() is None,
        # Also the verifier's answer rather than the environment's: it is the
        # thing that knows what would satisfy it.
        auth_provider=verifier.provider,
        firebase=firebase_web_config(),
        billing_enabled=billing.enabled,
        payments_paused=paused,
        requests_enabled=wishlist.enabled,
        tiers=[
            TierInfo(
                tier=tier,
                monthly_imports=policy.limit(tier),
                # Nothing is purchasable while checkout is closed. Said here
                # rather than left to the page: a client that had the old
                # answer cached would otherwise offer a button that 503s.
                purchasable=tier in purchasable and not paused,
            )
            for tier in Tier
        ],
    )


@app.get("/api/me", response_model=AccountResponse)
def me(
    background: BackgroundTasks,
    principal: Principal = Depends(require_principal),
    accounts: AccountStore = Depends(get_account_store),
    policy: QuotaPolicy = Depends(get_policy),
    billing: Billing = Depends(get_billing_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> AccountResponse:
    """The caller's tier and what is left of this month's allowance.

    Open to an unconfirmed address too: this is how the page learns that it has
    to ask for the confirmation, which ``email_verified`` tells it.
    """
    account = accounts.ensure(principal.uid, principal.email)
    # The page asks this on every load while signed in, so it is where an
    # account counts as active that day.
    background.add_task(usage.active, principal.uid)
    return _account_response(account, principal, policy, billing)


@app.post("/api/elections/import", response_model=ImportResponse)
async def import_election(
    body: ImportBody,
    response: Response,
    background: BackgroundTasks,
    wait_seconds: float = Query(0.0, ge=0.0, description="Block for up to this long for a result."),
    service: ImportService = Depends(get_service),
    principal: Principal = Depends(require_principal),
    account: UserAccount = Depends(require_account),
    accounts: AccountStore = Depends(get_account_store),
    policy: QuotaPolicy = Depends(get_policy),
    usage: UsageRecorder = Depends(get_usage),
) -> ImportResponse:
    """Import an election named by year, nation and — optionally — region.

    The caller supplies no address: the resolver works out which election that
    is, and where its results are published. A request made before is served
    from storage without searching or extracting anything — and without costing
    quota.

    The allowance is taken *before* the request is claimed and given back if the
    claim turns out not to need any work. Reserving first is what keeps two
    simultaneous imports from both spending the last unit of a month.

    What is already there — a stored election, or an import running or waiting
    for its importer — is handed back before the allowance is looked at at all.
    Otherwise asking again for an import that spent the month's last unit is
    refused, and with it the way back to the preview that unit paid for. A race
    past this check is still caught by the claim, and refunded.
    """
    request = body.to_request()
    joined = await service.peek(request)
    if joined is not None:
        return await _answer(joined, response, service, wait_seconds)

    period = billing_period()
    limit = policy.limit(account.tier)
    charged = not principal.unmetered

    if charged:
        # Refusals are counted before raising: a response that is an error
        # carries no background tasks.
        if limit <= 0:
            await run_in_threadpool(usage.record, UsageEvent.REFUSED_NO_SUBSCRIPTION)
            raise HTTPException(
                402,
                detail=(
                    f"the {account.tier.value} tier cannot import elections — "
                    "subscribe to import new ones"
                ),
            )
        if not await run_in_threadpool(accounts.reserve_import, account.uid, period, limit):
            await run_in_threadpool(usage.record, UsageEvent.REFUSED_LIMIT_REACHED)
            current = await run_in_threadpool(accounts.get, account.uid) or account
            if current.used_in(period) < limit:
                raise HTTPException(
                    429,
                    detail=(
                        "too many of this month's imports found no election; "
                        "the allowance resets at the start of next month"
                    ),
                )
            raise HTTPException(
                429,
                detail=(
                    f"this month's {limit} imports are used up; "
                    "the allowance resets at the start of next month"
                ),
            )

    def refund(*, keep_attempt: bool = False) -> None:
        """Give the reserved import back. A no-op when nothing was taken.

        An import that searched and read pages before failing keeps its attempt:
        the allowance comes back, but failures are refunded only so often
        (:func:`app.accounts.attempt_limit`), because each one cost model calls.
        """
        if charged:
            accounts.release_import(account.uid, period, keep_attempt=keep_attempt)

    def parse_failed() -> None:
        usage.record(UsageEvent.IMPORT_FAILED)
        refund(keep_attempt=True)

    try:
        result = await service.submit(
            request,
            owner=principal.uid,
            on_parse_failed=parse_failed,
        )
    except ValueError as exc:
        await run_in_threadpool(refund)
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except BaseException:
        await run_in_threadpool(refund)
        raise

    if result.reused:
        # Joined to an import somebody started between the look above and the
        # claim: nothing was searched for or read on this caller's behalf.
        await run_in_threadpool(refund)
    else:
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
    _account: UserAccount = Depends(require_account),
) -> ImportResponse:
    """Has this election been imported already? Answered without looking it up.

    Two questions in one, both free. First: is there a job for exactly this
    request — one running, one waiting to be confirmed, one that failed? Then:
    is the election itself already stored, whoever asked for it and however they
    spelled it, which the store can answer from the year and the place alone.

    Part of the import flow rather than of viewing, so it needs an account — but
    not a subscription, because nothing here searches, fetches or extracts.
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

    The other end of the paywall. Importing makes the server go and read pages,
    which is what is sold; writing down *which* election was wanted costs
    nothing and is worth more than a refusal — so instead of being turned away,
    the asker has the election filed in the issue tracker to be imported by
    hand.

    Unlike every other route in the import flow this asks for no account at all,
    which is a deliberate exception to "part of the import flow, so it needs an
    account" (``/api/elections/lookup``). The reasoning that gates the rest does
    not apply: nothing here searches, fetches, extracts or spends. What it does
    do is *write*, to a public tracker, on behalf of a caller nobody
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
    _account: UserAccount = Depends(require_account),
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
    _importer: Principal = Depends(require_importer),
    usage: UsageRecorder = Depends(get_usage),
) -> ImportResponse:
    """Save a previewed election under the identity that was read off the page.

    Only its importer may (:func:`require_importer`), and without paying again:
    the import was paid for when it ran, so a subscription that has lapsed since
    cannot strand it halfway. For the same reason every forecast of an upcoming
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
    _importer: Principal = Depends(require_importer),
    usage: UsageRecorder = Depends(get_usage),
) -> Response:
    """Reject a previewed election, leaving the request free to import again.

    Only its importer may (:func:`require_importer`).
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


# --- sign-in, where this app is the one holding the passwords ---------------
#
# Only ``AUTH_MODE=sqlite`` reaches past the guard below. With Firebase the
# browser signs in against Google's endpoints and these three answer 404, which
# is the honest response: there is no account here to create.

def _session_response(session: Session) -> SessionResponse:
    return SessionResponse(
        token=session.token,
        uid=session.uid,
        email=session.email,
        expires_at=session.expires_at,
    )


def require_password_store(
    store: PasswordCredentialStore | None = Depends(get_password_credentials),
) -> PasswordCredentialStore:
    if store is None:
        raise HTTPException(404, detail="this server does not manage sign-in itself")
    return store


@app.post(
    "/api/auth/register",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: CredentialsBody,
    store: PasswordCredentialStore = Depends(require_password_store),
) -> SessionResponse:
    """Create an account and sign it in.

    The account this opens is a free one, exactly as a Firebase sign-up is:
    registering buys nothing, and the tier still only moves when Stripe says so.
    """
    try:
        session = await run_in_threadpool(store.register, body.email, body.password)
    except EmailTaken as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except SignUpRefused as exc:
        raise HTTPException(422, detail=str(exc)) from None
    log.info("registered uid=%s", session.uid[:12])
    return _session_response(session)


@app.post("/api/auth/login", response_model=SessionResponse)
async def login(
    body: CredentialsBody,
    store: PasswordCredentialStore = Depends(require_password_store),
) -> SessionResponse:
    try:
        session = await run_in_threadpool(store.sign_in, body.email, body.password)
    except BadCredentials as exc:
        # One message for a wrong address and a wrong password alike: which of
        # the two was wrong is not something a caller is entitled to learn.
        raise HTTPException(401, detail=str(exc), headers=UNAUTHENTICATED) from None
    return _session_response(session)


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    authorization: str | None = Header(default=None),
    store: PasswordCredentialStore = Depends(require_password_store),
) -> Response:
    """End the session this request carries. Unauthenticated on purpose — a
    token that is already worthless still deserves to be deleted."""
    token = bearer_token(authorization)
    if token:
        await run_in_threadpool(store.sign_out, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- billing ----------------------------------------------------------------

@app.post("/api/billing/checkout", response_model=CheckoutResponse)
async def start_checkout(
    body: CheckoutBody,
    background: BackgroundTasks,
    principal: Principal = Depends(require_principal),
    account: UserAccount = Depends(require_account),
    billing: Billing = Depends(get_billing_provider),
    usage: UsageRecorder = Depends(get_usage),
) -> CheckoutResponse:
    """A Stripe Checkout link for one tier.

    Nothing about the account changes here. The tier is granted only when
    Stripe reports a paid subscription over the webhook below, so abandoning
    the checkout page leaves the user exactly as they were.

    Closed entirely while ``PAYMENTS_PAUSED`` is on — see
    :func:`app.config.payments_paused`. Only new checkouts stop: the portal
    below stays open, so anyone already subscribed can still change or cancel.
    """
    if payments_paused():
        # Ahead of every other check: while the card form does not work, the
        # honest answer is the same whichever tier was asked for. 503 rather
        # than 500 — this is a service that is down, and it is coming back.
        raise HTTPException(
            503,
            detail=(
                "payments are temporarily unavailable — please try again tomorrow. "
                "In the meantime you can ask for an election and it will be added "
                "to the issue tracker"
            ),
        )
    if body.tier not in billing.tiers():
        raise HTTPException(400, detail=f"the {body.tier.value} tier is not for sale")
    if account.tier is not Tier.FREE and account.subscription_status in ENTITLING_STATUSES:
        # A second checkout is a second subscription, charged alongside the
        # first. Changing tier is the portal's job, which prorates instead.
        raise HTTPException(
            409, detail="this account already has a subscription — change it under manage subscription"
        )
    base = public_base_url()
    try:
        session = await run_in_threadpool(
            lambda: billing.checkout(
                uid=principal.uid,
                tier=body.tier,
                email=account.email or principal.email,
                customer_id=account.stripe_customer_id,
                success_url=f"{base}/?checkout=success",
                cancel_url=f"{base}/?checkout=cancelled",
            )
        )
    except BillingUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from None
    log.info("checkout started uid=%s tier=%s", principal.uid[:12], body.tier.value)
    background.add_task(usage.record, UsageEvent.CHECKOUT_STARTED)
    return CheckoutResponse(url=session.url)


@app.post("/api/billing/portal", response_model=CheckoutResponse)
async def open_portal(
    account: UserAccount = Depends(require_account),
    billing: Billing = Depends(get_billing_provider),
) -> CheckoutResponse:
    """Stripe's own page for changing or cancelling the subscription."""
    if not account.stripe_customer_id:
        raise HTTPException(409, detail="this account has never had a subscription")
    try:
        url = await run_in_threadpool(
            lambda: billing.portal(
                customer_id=account.stripe_customer_id, return_url=f"{public_base_url()}/"
            )
        )
    except BillingUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from None
    return CheckoutResponse(url=url)


def _paying(account: UserAccount | None) -> bool:
    return (
        account is not None
        and account.tier is not Tier.FREE
        and account.subscription_status in ENTITLING_STATUSES
    )


@app.post("/api/billing/webhook")
async def stripe_webhook(
    request: Request,
    background: BackgroundTasks,
    billing: Billing = Depends(get_billing_provider),
    accounts: AccountStore = Depends(get_account_store),
    usage: UsageRecorder = Depends(get_usage),
) -> dict[str, bool]:
    """Apply what Stripe says about a subscription.

    Unauthenticated by design — the signature *is* the authentication, and it
    is checked before a single field of the body is read. This is the only path
    by which a tier ever changes.
    """
    payload = await request.body()
    try:
        event = await run_in_threadpool(
            billing.event_from_webhook, payload, request.headers.get("stripe-signature")
        )
    except BillingUnavailable as exc:
        raise HTTPException(503, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - an unverifiable body is a 400, never a 500
        log.warning("rejected a Stripe webhook: %s", scrub(exc))
        raise HTTPException(400, detail="could not verify the webhook signature") from None

    if event is None:
        return {"handled": False}

    uid = event.uid
    if uid is None and event.customer_id:
        # Older subscriptions predate the uid we now attach to their metadata.
        found = await run_in_threadpool(accounts.find_by_customer, event.customer_id)
        uid = found.uid if found else None
    if uid is None:
        log.warning("Stripe webhook names no account we know; ignoring it")
        return {"handled": False}

    if event.tier is None:
        if event.customer_id:
            await run_in_threadpool(accounts.link_customer, uid, event.customer_id)
        return {"handled": True}

    # Read first, so the report can tell a subscription that started or ended
    # from one of Stripe's many updates that change neither.
    before = await run_in_threadpool(accounts.get, uid)
    updated = await run_in_threadpool(
        lambda: accounts.set_subscription(
            uid,
            event.tier,
            customer_id=event.customer_id,
            subscription_id=event.subscription_id,
            status=event.status,
        )
    )
    log.info(
        "billing uid=%s tier=%s status=%s applied=%s",
        uid[:12], event.tier.value, scrub(event.status), updated is not None,
    )
    if updated is not None and _paying(updated) != _paying(before):
        background.add_task(
            usage.record,
            UsageEvent.SUBSCRIPTION_STARTED if _paying(updated) else UsageEvent.SUBSCRIPTION_ENDED,
        )
    return {"handled": updated is not None}


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


def _usage_report(
    period: Period, today: date, usage: UsageRecorder, accounts: AccountStore
) -> tuple[str, UsageReport]:
    """The report for the last complete ``period`` before ``today``, and its key."""
    current = period_range(period, today)
    previous = previous_range(period, current)
    subject, body = build_report(
        period,
        current,
        gather(usage.store, accounts, current),
        previous,
        gather(usage.store, accounts, previous),
    )
    return report_key(period, current), UsageReport(subject=subject, body=body)


def require_schedule(x_report_secret: str | None = Header(default=None)) -> None:
    """The caller is the daily schedule, proven by ``USAGE_REPORT_SECRET``.

    The endpoints behind this belong to no account; the Worker's cron calls
    them. Without a secret configured they do not exist, which is the honest
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
    accounts: AccountStore = Depends(get_account_store),
    mailer: Mailer = Depends(get_mailer_provider),
) -> ReportRun:
    """Email the usage reports that are due. Called once a day by the Worker's cron.

    Each report is claimed before it is sent, so a run that is retried — or a
    cron that fires twice — sends nothing twice; a report whose email failed
    gives its claim back for the next run.

    Retention runs first, whatever happens to the email: deleting old
    active-account markers is an obligation, not a side effect of a report.
    """
    today = usage.today()
    await run_in_threadpool(
        usage.store.forget_active_before, today - timedelta(days=ACTIVE_RETENTION_DAYS)
    )

    sent: list[str] = []
    skipped: list[str] = []
    for due in [period] if period else due_periods(today):
        key, report = await run_in_threadpool(_usage_report, due, today, usage, accounts)
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


class InactiveAccountRun(BaseModel):
    """What the daily deletion did, as counts. It never says whose accounts."""

    dated: int
    deleted: int
    kept: int
    failed: int


@app.post("/api/internal/inactive-accounts", response_model=InactiveAccountRun)
async def delete_inactive_accounts(
    _schedule: None = Depends(require_schedule),
    accounts: AccountStore = Depends(get_account_store),
    identities: IdentityRemover = Depends(get_identity_remover_provider),
) -> InactiveAccountRun:
    """Delete accounts nobody has used for two years. Called once a day by the Worker's cron.

    Separate from the reports on purpose, so each can fail without the other.
    A sign-in that could not be deleted (in practice, a service account missing
    the Firebase role) answers ``502`` once the rest of the run is done, so the
    cron run shows as failed instead of looking fine while nothing is deleted.
    Its account is kept and tried again on the next run. See
    :mod:`app.retention`.
    """
    swept = await run_in_threadpool(sweep_inactive_accounts, accounts, identities)
    if swept.failed:
        raise HTTPException(
            502,
            detail=(
                f"{swept.failed} sign-ins could not be deleted and their accounts wait for "
                f"the next run (deleted {swept.deleted}, kept {swept.kept}, dated {swept.dated})"
            ),
        )
    return InactiveAccountRun(
        dated=swept.dated, deleted=swept.deleted, kept=swept.kept, failed=swept.failed
    )


@app.get("/api/admin/usage", response_model=UsageReport)
async def preview_usage_report(
    period: Period = Query(Period.DAILY),
    before: date | None = Query(
        None, description="Report the last complete period before this day; today by default."
    ),
    _admin: Principal = Depends(require_admin),
    usage: UsageRecorder = Depends(get_usage),
    accounts: AccountStore = Depends(get_account_store),
) -> UsageReport:
    """A usage report as it would be emailed, sent nowhere."""
    _, report = await run_in_threadpool(
        _usage_report, period, before or usage.today(), usage, accounts
    )
    return report


# Serving the page from this app keeps the API same-origin, which is what the
# frontend expects by default. Mounted last so it cannot shadow the API routes.
#
# The page and its scripts, and nothing else: mounting the directory itself
# would serve whatever sits next to them, which on a local checkout is the repo
# root — .env and its keys included.
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2]))
if (FRONTEND_DIR / "index.html").is_file():

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="scripts")
