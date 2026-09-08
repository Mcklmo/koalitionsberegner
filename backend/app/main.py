"""FastAPI surface over the shared election store.

Access follows one rule, applied in three places below: *viewing is open,
importing is bought*. A signed-out visitor sees the curated selection, any
account sees everything stored, and only a subscriber with quota left can make
the server go out and read a page it has never read before.

The quota is charged for the one thing that costs money — a new extraction —
and nothing else. An election somebody already imported is served to every
subscriber for free, which is the whole point of the shared store.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .accounts import AccountStore, QuotaPolicy, Tier, UserAccount, billing_period
from .auth import InvalidToken, Principal, TokenVerifier, bearer_token
from .billing import Billing, BillingUnavailable
from .config import (
    firebase_web_config,
    get_accounts,
    get_billing,
    get_parser,
    get_quota_policy,
    get_store,
    get_verifier,
    max_wait_seconds,
    public_base_url,
    validate_configuration,
)
from .identity import source_url_key
from .observability import configure_logging, io_span, scrub
from .schema import Election
from .service import ImportRequest, ImportResult, ImportService, ImportState

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


# --- dependency seams -------------------------------------------------------
# Thin wrappers over app.config so tests can override one collaborator without
# touching the environment.

def get_service() -> ImportService:
    return ImportService(get_store(), get_parser())


def get_account_store() -> AccountStore:
    return get_accounts()


def get_token_verifier() -> TokenVerifier:
    return get_verifier()


def get_billing_provider() -> Billing:
    return get_billing()


def get_policy() -> QuotaPolicy:
    return get_quota_policy()


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


def require_account(
    principal: Principal = Depends(require_principal),
    accounts: AccountStore = Depends(get_account_store),
) -> UserAccount:
    """The caller's account, created free on first sight. Accounts cost nothing."""
    return accounts.ensure(principal.uid, principal.email)


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if not principal.admin:
        raise HTTPException(403, detail="this needs an administrator")
    return principal


# --- request and response models -------------------------------------------

class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1, max_length=2000)

    def to_request(self) -> ImportRequest:
        return ImportRequest(source_url=self.source_url)


class ImportResponse(BaseModel):
    page_key: str
    state: ImportState
    election: Election | None = None
    election_hash: str | None = None
    error: str | None = None
    attempt: int | None = None
    reused: bool = False
    duplicate: bool = False

    @classmethod
    def of(cls, result: ImportResult) -> "ImportResponse":
        return cls(
            page_key=result.page_key,
            state=result.state,
            election=result.election,
            election_hash=result.election_hash,
            error=result.error,
            attempt=result.attempt,
            reused=result.reused,
            duplicate=result.duplicate,
        )


class ElectionSummary(BaseModel):
    election_hash: str
    nation: str
    state: str | None
    election_date: date
    title: str
    total_seats: int
    selected: bool = False


class TierInfo(BaseModel):
    tier: Tier
    monthly_imports: int
    purchasable: bool


class PublicConfig(BaseModel):
    """Everything the page needs before anyone has signed in."""

    auth_required: bool
    """False when gating is off, so the page can skip the whole sign-in flow."""
    firebase: dict[str, str]
    billing_enabled: bool
    tiers: list[TierInfo]


class AccountResponse(BaseModel):
    uid: str
    email: str | None
    tier: Tier
    admin: bool
    period: str
    used: int
    limit: int
    remaining: int
    may_import: bool
    subscription_status: str | None = None
    billing_enabled: bool = False


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Tier


class CheckoutResponse(BaseModel):
    url: str


class SelectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: bool


def _account_response(
    account: UserAccount, principal: Principal, policy: QuotaPolicy, billing: Billing
) -> AccountResponse:
    period = billing_period()
    limit = policy.limit(account.tier)
    return AccountResponse(
        uid=account.uid,
        email=account.email,
        tier=account.tier,
        admin=principal.admin,
        period=period,
        used=account.used_in(period),
        # A local run with gating off is not on any tier; say so as no limit
        # rather than reporting the free tier's zero.
        limit=-1 if principal.unlimited else limit,
        remaining=-1 if principal.unlimited else account.remaining(policy, period),
        may_import=principal.unlimited or account.may_import(policy, period),
        subscription_status=account.subscription_status,
        billing_enabled=billing.enabled,
    )


# --- endpoints --------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", response_model=PublicConfig)
def public_config(
    policy: QuotaPolicy = Depends(get_policy),
    billing: Billing = Depends(get_billing_provider),
    verifier: TokenVerifier = Depends(get_token_verifier),
) -> PublicConfig:
    """Public settings for the page: how to sign in, and what is for sale.

    The Firebase API key here identifies the project to Google's identity
    endpoints; it grants nothing on its own, and every authorisation decision
    is made server-side from the token it eventually produces.
    """
    purchasable = set(billing.tiers())
    return PublicConfig(
        # Asked of the verifier rather than the environment: it is the thing
        # that decides, and with gating off it hands out an identity to everyone.
        auth_required=verifier.anonymous() is None,
        firebase=firebase_web_config(),
        billing_enabled=billing.enabled,
        tiers=[
            TierInfo(
                tier=tier,
                monthly_imports=policy.limit(tier),
                purchasable=tier in purchasable,
            )
            for tier in Tier
        ],
    )


@app.get("/api/me", response_model=AccountResponse)
def me(
    principal: Principal = Depends(require_principal),
    account: UserAccount = Depends(require_account),
    policy: QuotaPolicy = Depends(get_policy),
    billing: Billing = Depends(get_billing_provider),
) -> AccountResponse:
    """The caller's tier and what is left of this month's allowance."""
    return _account_response(account, principal, policy, billing)


@app.post("/api/elections/import", response_model=ImportResponse)
async def import_election(
    body: ImportBody,
    response: Response,
    wait_seconds: float = Query(0.0, ge=0.0, description="Block for up to this long for a result."),
    service: ImportService = Depends(get_service),
    principal: Principal = Depends(require_principal),
    account: UserAccount = Depends(require_account),
    accounts: AccountStore = Depends(get_account_store),
    policy: QuotaPolicy = Depends(get_policy),
) -> ImportResponse:
    """Import an election from a results URL.

    The agent identifies which election the page reports; the caller supplies
    nothing but the address. A page imported before is served from storage
    without fetching or extracting anything — and without costing quota.

    The allowance is taken *before* the page is claimed and given back if the
    claim turns out not to need an extraction. Reserving first is what keeps two
    simultaneous imports from both spending the last unit of a month.
    """
    period = billing_period()
    limit = policy.limit(account.tier)
    charged = not principal.unlimited

    if charged:
        if limit <= 0:
            raise HTTPException(
                402,
                detail=(
                    f"the {account.tier.value} tier cannot import elections — "
                    "subscribe to import new ones"
                ),
            )
        if not await run_in_threadpool(accounts.reserve_import, account.uid, period, limit):
            raise HTTPException(
                429,
                detail=(
                    f"this month's {limit} imports are used up; "
                    "the allowance resets at the start of next month"
                ),
            )

    def refund() -> None:
        """Give the reserved import back. A no-op when nothing was taken."""
        if charged:
            accounts.release_import(account.uid, period)

    try:
        result = await service.submit(body.to_request(), on_parse_failed=refund)
    except ValueError as exc:
        await run_in_threadpool(refund)
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except BaseException:
        await run_in_threadpool(refund)
        raise

    if result.reused:
        # Served from the store, or joined to somebody else's running extraction:
        # nothing was fetched or read on this caller's behalf.
        await run_in_threadpool(refund)

    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(result.page_key, min(wait_seconds, max_wait_seconds()))
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)


@app.get("/api/elections", response_model=list[ElectionSummary])
async def list_elections(
    service: ImportService = Depends(get_service),
    principal: Principal | None = Depends(current_principal),
) -> list[ElectionSummary]:
    """Every stored election — or only the curated ones, to a signed-out visitor."""
    return [
        ElectionSummary(
            election_hash=stored.election_hash,
            nation=stored.election.nation,
            state=stored.election.state,
            election_date=stored.election.election_date,
            title=stored.election.title,
            total_seats=stored.election.total_seats,
            selected=stored.selected,
        )
        for stored in await service.list_elections(selected_only=principal is None)
    ]


@app.get("/api/elections/lookup", response_model=ImportResponse)
async def lookup_page(
    source_url: str,
    service: ImportService = Depends(get_service),
    _account: UserAccount = Depends(require_account),
) -> ImportResponse:
    """Has this page been imported already? Answered without fetching it.

    Part of the import flow rather than of viewing, so it needs an account —
    but not a subscription, because it never causes a page to be read.
    """
    try:
        key = source_url_key(source_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return ImportResponse.of(await service.status(key))


@app.get("/api/elections/pages/{page_key}", response_model=ImportResponse)
async def get_page(
    page_key: str,
    response: Response,
    wait_seconds: float = Query(0.0, ge=0.0),
    service: ImportService = Depends(get_service),
    _account: UserAccount = Depends(require_account),
) -> ImportResponse:
    result = await service.status(page_key)
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(page_key, min(wait_seconds, max_wait_seconds()))
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no import for that page")
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)


@app.post("/api/elections/pages/{page_key}/confirm", response_model=ImportResponse)
async def confirm_election(
    page_key: str,
    service: ImportService = Depends(get_service),
    _account: UserAccount = Depends(require_account),
) -> ImportResponse:
    """Save a previewed election under the identity the agent inferred.

    Any account may confirm: the extraction that produced this preview has
    already been paid for, and charging again for saving what was read would
    mean a lapsed subscription could strand a page mid-import.
    """
    result = await service.confirm(page_key)
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no preview to confirm for that page")
    if result.state is not ImportState.READY:
        raise HTTPException(
            status_code=409, detail=f"nothing awaiting confirmation (state: {result.state.value})"
        )
    return ImportResponse.of(result)


@app.delete("/api/elections/pages/{page_key}/preview", status_code=status.HTTP_204_NO_CONTENT)
async def discard_preview(
    page_key: str,
    service: ImportService = Depends(get_service),
    _account: UserAccount = Depends(require_account),
) -> Response:
    """Reject a previewed election, leaving the page free to import again."""
    if not await service.discard(page_key):
        raise HTTPException(status_code=404, detail="no preview to discard for that page")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.put("/api/elections/{election_hash}/selected", response_model=ElectionSummary)
async def set_selected(
    election_hash: str,
    body: SelectedBody,
    service: ImportService = Depends(get_service),
    _admin: Principal = Depends(require_admin),
) -> ElectionSummary:
    """Curate an election into, or out of, what signed-out visitors can see."""
    if not await service.set_selected(election_hash, body.selected):
        raise HTTPException(status_code=404, detail="no stored election with that hash")
    stored = await service.get_stored(election_hash)
    return ElectionSummary(
        election_hash=stored.election_hash,
        nation=stored.election.nation,
        state=stored.election.state,
        election_date=stored.election.election_date,
        title=stored.election.title,
        total_seats=stored.election.total_seats,
        selected=stored.selected,
    )


@app.get("/api/elections/{election_hash}", response_model=ImportResponse)
async def get_election(
    election_hash: str,
    service: ImportService = Depends(get_service),
    principal: Principal | None = Depends(current_principal),
) -> ImportResponse:
    """Fetch a stored election by identity, for the picker."""
    stored = await service.get_stored(election_hash)
    if stored is None:
        raise HTTPException(status_code=404, detail="no stored election with that hash")
    if principal is None and not stored.selected:
        raise HTTPException(
            401,
            detail="sign in to view this election",
            headers=UNAUTHENTICATED,
        )
    return ImportResponse(
        page_key="",
        state=ImportState.READY,
        election=stored.election,
        election_hash=election_hash,
    )


# --- billing ----------------------------------------------------------------

@app.post("/api/billing/checkout", response_model=CheckoutResponse)
async def start_checkout(
    body: CheckoutBody,
    principal: Principal = Depends(require_principal),
    account: UserAccount = Depends(require_account),
    billing: Billing = Depends(get_billing_provider),
) -> CheckoutResponse:
    """A Stripe Checkout link for one tier.

    Nothing about the account changes here. The tier is granted only when
    Stripe reports a paid subscription over the webhook below, so abandoning
    the checkout page leaves the user exactly as they were.
    """
    if body.tier not in billing.tiers():
        raise HTTPException(400, detail=f"the {body.tier.value} tier is not for sale")
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


@app.post("/api/billing/webhook")
async def stripe_webhook(
    request: Request,
    billing: Billing = Depends(get_billing_provider),
    accounts: AccountStore = Depends(get_account_store),
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
    return {"handled": updated is not None}


# Serving the page from this app keeps the API same-origin, which is what the
# frontend expects by default. Mounted last so it cannot shadow the API routes.
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2]))
if (FRONTEND_DIR / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
