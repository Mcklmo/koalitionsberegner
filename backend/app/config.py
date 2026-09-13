"""Environment-driven configuration and dependency wiring.

Values come from the environment, which a ``.env`` file at the repo root fills
in for a local run without overriding anything already exported — see
:mod:`app.env`.

Configuration is validated eagerly at startup (see :func:`validate_configuration`),
because the alternative is a container that passes its health check and only
reveals a typo when the first real request arrives. A value that is not
recognised is an error, never a silent fallback to a default — a misspelled
``LLM_MODE`` must not quietly serve mock election data.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

from . import ENV_FILE_LOADED, ENV_NAMES_LOADED
from .accounts import DEFAULT_MONTHLY_IMPORTS, AccountStore, InMemoryAccountStore, QuotaPolicy, Tier
from .auth import (
    DisabledVerifier,
    FirebaseCredentials,
    PasswordCredentialStore,
    PrincipalRules,
    StoreBackedVerifier,
    StubCredentials,
    TokenVerifier,
)
from .billing import Billing, DisabledBilling, StripeBilling
from .parser import DEFAULT_PAGE_LIMIT, ElectionParser, UnavailableParser
from .search import DEFAULT_SEARCH_LIMIT
from .store import DEFAULT_STALE_AFTER_SECONDS, ElectionStore, InMemoryElectionStore
from .wikipedia import DEFAULT_LANGUAGE
from .wishlist import DisabledWishlist, GithubWishlist, Wishlist

log = logging.getLogger(__name__)

#: Every recognised value, so an unknown one can name the alternatives.
STORE_BACKENDS = ("firestore", "sqlite", "memory")
LLM_MODES = ("mock", "live", "off")
AUTH_MODES = ("firebase", "sqlite", "stub", "off")
SEARCH_MODES = ("auto", "google", "anthropic", "off")
WIKIPEDIA_MODES = ("on", "off")

DEFAULT_SQLITE_PATH = "./data/elections.db"


class ConfigError(RuntimeError):
    """The environment asks for something this app cannot provide."""


def _env_choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    raw = os.environ.get(name)
    value = (default if raw is None or not raw.strip() else raw).strip().lower()
    if value not in allowed:
        raise ConfigError(
            f"{name} must be one of {', '.join(allowed)}, got {raw!r}"
        )
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}")
    return value


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _store_backend() -> str:
    """Which backend both the elections and the accounts live in."""
    return _env_choice(
        "ELECTION_STORE", STORE_BACKENDS,
        "firestore" if os.environ.get("GOOGLE_CLOUD_PROJECT") else "memory",
    )


def firebase_project_id() -> str:
    """The Firebase project whose ID tokens this API accepts.

    Defaults to the GCP project, because a Firebase project *is* a GCP project
    and running both in one is the ordinary setup.
    """
    return _env_str("FIREBASE_PROJECT_ID") or _env_str("GOOGLE_CLOUD_PROJECT")


def auth_mode() -> str:
    """``firebase`` once a project exists; ``off`` on a bare local checkout.

    ``off`` is not "anonymous everywhere" — it is "no gating at all", so the app
    behaves exactly as it did before accounts existed. It must never be the
    mode in a deployment, which is why the default follows the project id.

    ``sqlite`` is the third real option: the same gating, with the passwords and
    sessions in the local database instead of a Firebase project.
    """
    return _env_choice(
        "AUTH_MODE", AUTH_MODES, "firebase" if firebase_project_id() else "off"
    )


@lru_cache(maxsize=1)
def get_store() -> ElectionStore:
    """Pick a storage backend.

    ``ELECTION_STORE`` selects it: ``firestore`` (shared, the deployed default),
    ``sqlite`` (a local file that survives restarts, so repeated local runs do
    not re-fetch and re-extract the same pages), or ``memory`` (fastest, forgets
    everything on exit). Without ``GOOGLE_CLOUD_PROJECT`` Firestore is not
    available, so the default falls back to ``memory``.
    """
    stale_after = _env_float("PARSE_LEASE_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    backend = _env_choice(
        "ELECTION_STORE", STORE_BACKENDS, "firestore" if project else "memory"
    )

    if backend == "memory":
        return InMemoryElectionStore(stale_after=stale_after)

    if backend == "sqlite":
        from .sqlite_store import SqliteElectionStore

        return SqliteElectionStore(
            os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH), stale_after=stale_after
        )

    if not project:
        raise ConfigError("ELECTION_STORE=firestore requires GOOGLE_CLOUD_PROJECT")

    from google.cloud import firestore

    from .cached_store import CachedElectionStore
    from .firestore_store import FirestoreElectionStore

    client = firestore.Client(
        project=project, database=os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    # Firestore bills every document read, and viewing is open to anyone.
    return CachedElectionStore(FirestoreElectionStore(client, stale_after=stale_after))


def search_mode() -> str:
    """Which search engine an import consults besides the resolver, in effect.

    ``auto`` — the default — is Google where it is configured and ``off``
    otherwise. Off is not a degraded mode: the resolver searches as part of
    identifying the election and comes back with candidate pages, so a second
    round through the same hosted search tool would mostly pay twice for the
    same answer. ``SEARCH_MODE=anthropic`` asks for it anyway, which is worth
    it where the resolver keeps naming elections it cannot find pages for.

    Always ``off`` unless extraction is live: nothing a mocked pipeline does
    depends on a real page being found, so searching could only ever be dead
    code.
    """
    mode = _env_choice("SEARCH_MODE", SEARCH_MODES, "auto")
    if _env_choice("LLM_MODE", LLM_MODES, "mock") != "live":
        return "off"
    if mode != "auto":
        return mode
    if _env_str("GOOGLE_SEARCH_API_KEY") and _env_str("GOOGLE_SEARCH_CX"):
        return "google"
    return "off"


@lru_cache(maxsize=1)
def get_search():
    """The search seam, built from ``SEARCH_MODE``.

    Consulted only when the resolver named fewer candidate pages than an import
    may read, so a deployment whose resolutions come back complete never calls a
    search engine.

    A mode asked for by name must have its credentials, whether or not this
    process will get as far as using it — ``SEARCH_MODE=google`` without a key
    is a mistake worth hearing about at startup rather than on the one import
    that needed it. ``auto`` asks for nothing and settles for what is there.
    """
    from .search import AnthropicWebSearch, DisabledSearch, GoogleSearch

    requested = _env_choice("SEARCH_MODE", SEARCH_MODES, "auto")
    key, cx = _env_str("GOOGLE_SEARCH_API_KEY"), _env_str("GOOGLE_SEARCH_CX")
    if requested == "google" and not (key and cx):
        raise ConfigError(
            "SEARCH_MODE=google requires GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX"
        )
    if requested == "anthropic" and not _env_str("ANTHROPIC_API_KEY"):
        raise ConfigError("SEARCH_MODE=anthropic requires ANTHROPIC_API_KEY")

    mode = search_mode()
    if mode == "off":
        return DisabledSearch()
    if mode == "google":
        return GoogleSearch(key, cx)
    return AnthropicWebSearch()


def wikipedia_mode() -> str:
    """Whether an import looks the election up in Wikipedia first.

    ``on`` by default: the lookup needs no credential, costs nothing, and an
    encyclopedia article is the page most likely to *state seats* rather than
    votes — see :meth:`app.parser.LlmElectionParser._candidates` for why that
    earns it first place in the queue. ``WIKIPEDIA=off`` takes it out again, and
    an import then reads the resolver's candidates as it did before.

    Always ``off`` unless extraction is live, for the same reason searching is:
    the mock extractor ignores the page it is handed, so fetching a real article
    could only ever be a call made for nothing.
    """
    mode = _env_choice("WIKIPEDIA", WIKIPEDIA_MODES, "on")
    if _env_choice("LLM_MODE", LLM_MODES, "mock") != "live":
        return "off"
    return mode


def wikipedia_language() -> str:
    """Which Wikipedia to search. A language code, because it becomes a hostname."""
    language = _env_str("WIKIPEDIA_LANGUAGE").lower() or DEFAULT_LANGUAGE
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", language):
        raise ConfigError(
            f"WIKIPEDIA_LANGUAGE must be a language code like 'en' or 'pt-br', got {language!r}"
        )
    return language


@lru_cache(maxsize=1)
def get_wikipedia():
    """The Wikipedia seam, or ``None`` where it is switched off.

    ``None`` rather than a disabled stand-in, because this seam is two things at
    once — the candidate source the parser asks, and the fetcher that serves
    those articles — and "no Wikipedia" means an import is wired exactly as it
    was before either existed.

    The language is validated whether or not this process will use it: like a
    named ``SEARCH_MODE`` without its keys, a typo there is worth hearing about
    at startup rather than on the one import that needed it.
    """
    language = wikipedia_language()
    if wikipedia_mode() == "off":
        return None

    from .wikipedia import Wikipedia

    # ``WIKIPEDIA_CONTACT`` only replaces the address in the User-Agent; there is
    # always one, because a client that names nobody is refused outright.
    return Wikipedia(language=language, contact=_env_str("WIKIPEDIA_CONTACT"))


@lru_cache(maxsize=1)
def get_parser() -> ElectionParser:
    """Wire the import pipeline: resolve the request, read pages, extract seats.

    ``LLM_MODE`` selects the agents: ``mock`` (the default) reads the request
    back and extracts a fixed set of parties without contacting the Anthropic
    API; ``live`` runs the real ones and requires ``ANTHROPIC_API_KEY``, mounted
    from GCP Secret Manager. ``off`` disables importing entirely.

    ``IMPORT_PAGE_LIMIT`` caps how many candidate pages one import may read, and
    ``IMPORT_SEARCH_LIMIT`` how many of those candidates a search engine may
    contribute beyond the ones the resolver named (see :func:`get_search`).
    Each page read costs a fetch and a model call, so both are small numbers;
    ``IMPORT_PAGE_LIMIT=1`` reads only the first candidate — which, with
    Wikipedia on, is the article rather than the resolver's best answer.
    """
    from .extractor import AnthropicExtractor, MockExtractor
    from .fetcher import HttpPageFetcher
    from .parser import LlmElectionParser
    from .resolver import AnthropicResolver, MockResolver

    mode = _env_choice("LLM_MODE", LLM_MODES, "mock")
    if mode == "off":
        return UnavailableParser()
    limits = {
        "page_limit": _env_int("IMPORT_PAGE_LIMIT", DEFAULT_PAGE_LIMIT),
        "search_limit": _env_int("IMPORT_SEARCH_LIMIT", DEFAULT_SEARCH_LIMIT),
    }
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigError("LLM_MODE=live requires ANTHROPIC_API_KEY")
        # Wikipedia is both halves of its seam: the source the parser asks for
        # candidates, and the fetcher those candidates are served by. Wrapping
        # rather than replacing keeps every other address on the ordinary path.
        wikipedia = get_wikipedia()
        fetcher = HttpPageFetcher()
        if wikipedia is not None:
            from .wikipedia import WikipediaFetcher

            fetcher = WikipediaFetcher(wikipedia, fetcher)
        return LlmElectionParser(
            fetcher, AnthropicExtractor(), AnthropicResolver(),
            search=get_search(), wikipedia=wikipedia, **limits
        )
    # Mock: a page is still fetched and the whole pipeline runs, minus the models.
    return LlmElectionParser(
        HttpPageFetcher(), MockExtractor(), MockResolver(), search=get_search(), **limits
    )


@lru_cache(maxsize=1)
def get_accounts() -> AccountStore:
    """Accounts live wherever the elections do — one database, one deployment."""
    backend = _store_backend()
    if backend == "memory":
        return InMemoryAccountStore()

    if backend == "sqlite":
        from .sqlite_accounts import SqliteAccountStore

        return SqliteAccountStore(os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))

    from google.cloud import firestore

    from .firestore_accounts import FirestoreAccountStore

    client = firestore.Client(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
    )
    return FirestoreAccountStore(client)


@lru_cache(maxsize=1)
def get_password_store() -> PasswordCredentialStore | None:
    """The store that owns passwords itself, when one is configured.

    Only ``AUTH_MODE=sqlite`` has one. Firebase keeps the passwords and the
    sign-in endpoints are Google's, so there is nothing here to register
    against — which is what makes ``/api/auth/*`` answer 404 in that mode.
    """
    if auth_mode() != "sqlite":
        return None

    from .sqlite_auth import SqliteCredentialStore

    return SqliteCredentialStore(os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))


@lru_cache(maxsize=1)
def get_verifier() -> TokenVerifier:
    """Wire token verification: a credential store, plus the rules.

    ``AUTH_MODE`` selects the store — ``firebase`` verifies real ID tokens
    against Google's certificates, ``sqlite`` resolves sessions this app issued
    itself, ``stub`` trusts the token's text and exists only so the gated
    behaviour can be exercised without either. ``off`` disables gating entirely
    for local runs and so has no store at all.

    The rules that turn a resolved credential into a principal — a subject is
    mandatory, ``ADMIN_EMAILS`` grants curation rights — are the same object in
    every mode, so they cannot drift apart per backend.
    """
    mode = auth_mode()
    if mode in ("off", "stub") and on_cloud_run():
        # ``off`` is also the default once no project id is set, so this is
        # what turns a lost environment variable into a failed boot instead of
        # a deployment where every caller is an unlimited administrator.
        raise ConfigError(
            f"AUTH_MODE={mode} cannot run on Cloud Run: it lets any caller in as anyone. "
            "Set AUTH_MODE=firebase with FIREBASE_PROJECT_ID (or GOOGLE_CLOUD_PROJECT)"
        )
    if mode == "off":
        return DisabledVerifier()

    rules = PrincipalRules.of(admin_emails())

    if mode == "stub":
        # Loud, because a deployment reaching this line is trusting whatever a
        # caller types into the Authorization header.
        log.warning("AUTH_MODE=stub: identities are unverified — local use only")
        return StoreBackedVerifier(StubCredentials(), rules)

    if mode == "sqlite":
        store = get_password_store()
        if store is None:  # unreachable: this mode is what builds it
            raise ConfigError("AUTH_MODE=sqlite could not open its credential store")
        if _store_backend() != "sqlite":
            # Sessions would survive a restart while the accounts they belong
            # to did not, so a signed-in user would keep landing on a new
            # free account. Legal, but never what anybody wants.
            log.warning(
                "AUTH_MODE=sqlite with ELECTION_STORE=%s: sign-ins persist but accounts do not",
                _store_backend(),
            )
        return StoreBackedVerifier(store, rules)

    project = firebase_project_id()
    if not project:
        raise ConfigError("AUTH_MODE=firebase requires FIREBASE_PROJECT_ID")
    return StoreBackedVerifier(FirebaseCredentials(project), rules)


def on_cloud_run() -> bool:
    """Whether this process is a Cloud Run service or job, which set these themselves."""
    return bool(_env_str("K_SERVICE") or _env_str("CLOUD_RUN_JOB"))


def admin_emails() -> frozenset[str]:
    """Accounts allowed to curate which elections signed-out visitors see."""
    raw = _env_str("ADMIN_EMAILS")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


@lru_cache(maxsize=1)
def get_quota_policy() -> QuotaPolicy:
    """How many imports a month each paid tier gets."""
    return QuotaPolicy(
        {
            Tier.FREE: 0,
            Tier.BASIC: _env_int("BASIC_MONTHLY_IMPORTS", DEFAULT_MONTHLY_IMPORTS[Tier.BASIC]),
            Tier.PREMIUM: _env_int(
                "PREMIUM_MONTHLY_IMPORTS", DEFAULT_MONTHLY_IMPORTS[Tier.PREMIUM]
            ),
        }
    )


@lru_cache(maxsize=1)
def get_billing() -> Billing:
    """Stripe when it is configured, otherwise nothing is for sale.

    Billing is optional on purpose: the app is useful without it, and a
    deployment that has not set up Stripe should serve free accounts rather
    than fail to start.
    """
    api_key = _env_str("STRIPE_API_KEY")
    prices = {
        Tier.BASIC: _env_str("STRIPE_PRICE_BASIC"),
        Tier.PREMIUM: _env_str("STRIPE_PRICE_PREMIUM"),
    }
    if not api_key:
        if any(prices.values()):
            raise ConfigError("STRIPE_PRICE_* is set but STRIPE_API_KEY is not")
        return DisabledBilling()
    if not any(prices.values()):
        raise ConfigError("STRIPE_API_KEY is set but no STRIPE_PRICE_BASIC/PREMIUM is")
    if not public_base_url():
        raise ConfigError("Stripe checkout requires PUBLIC_BASE_URL to return the user to")
    webhook_secret = _env_str("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        # Without it every webhook is refused, so the tier would never change.
        raise ConfigError("Stripe billing requires STRIPE_WEBHOOK_SECRET")
    return StripeBilling(api_key, prices=prices, webhook_secret=webhook_secret)


#: Repository that election requests are filed against, as ``owner/name``.
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


@lru_cache(maxsize=1)
def get_wishlist() -> Wishlist:
    """Where an account without a subscription can ask for an election.

    Optional in the same way billing is: with no ``GITHUB_ISSUES_TOKEN`` there
    is nowhere to file a request, and the page is told so by ``/api/config``
    and stops offering it. A token with no repository to file against is a
    configuration error rather than a default, because guessing which
    repository to open issues on is not something to get wrong quietly.

    Named ``GITHUB_ISSUES_*`` rather than ``GITHUB_TOKEN``: that name is
    already taken — GitHub Actions sets it in every workflow run, and so do
    several agent runtimes — and a token that happens to be in the environment
    is not a decision to file issues with it.
    """
    token = _env_str("GITHUB_ISSUES_TOKEN")
    repo = _env_str("GITHUB_ISSUES_REPO")
    if not token:
        if repo:
            raise ConfigError("GITHUB_ISSUES_REPO is set but GITHUB_ISSUES_TOKEN is not")
        return DisabledWishlist()
    if not repo:
        raise ConfigError("GITHUB_ISSUES_TOKEN is set but GITHUB_ISSUES_REPO is not (owner/name)")
    if not _REPO_PATTERN.match(repo):
        raise ConfigError(f"GITHUB_ISSUES_REPO must be owner/name, not {repo!r}")
    owner, name = repo.split("/", 1)
    return GithubWishlist(token, owner=owner, repo=name)


def payments_paused() -> bool:
    """Whether checkout is closed while payments are being fixed.

    Defaults to *paused*, which is the state this was added in: the deploy that
    carries this code is the one saying the card form does not work. Set
    ``PAYMENTS_PAUSED=false`` to open it again — one variable, no code change.
    Only new checkouts stop; an existing subscriber keeps their tier and can
    still reach Stripe's portal to change or cancel it.
    """
    return _env_str("PAYMENTS_PAUSED", "true").lower() not in {"false", "0", "no", "off"}


def public_base_url() -> str:
    """Where Stripe sends the user back to. No trailing slash."""
    return _env_str("PUBLIC_BASE_URL").rstrip("/")


#: Shortest ``ORIGIN_SECRET`` accepted, so a placeholder is a boot failure.
MIN_ORIGIN_SECRET_CHARS = 32


def origin_secret() -> str:
    """The secret a fronting proxy must present, or empty when there is none.

    Set it when the app sits behind Cloudflare (or any proxy) so that the
    platform's own address — ``*.run.app`` — stops answering on its own.
    """
    return _env_str("ORIGIN_SECRET")


def firebase_web_config() -> dict[str, str]:
    """The public Firebase settings the browser needs to sign users in.

    An API key here is an identifier, not a secret — it names the project for
    the identity endpoints. Everything that grants access is checked server-side.
    """
    return {
        "apiKey": _env_str("FIREBASE_API_KEY"),
        "projectId": firebase_project_id(),
    }


def max_wait_seconds() -> float:
    return _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)


def describe_configuration() -> dict[str, str]:
    """The choices actually in effect, for the startup log."""
    return {
        "store": _store_backend(),
        "llm_mode": _env_choice("LLM_MODE", LLM_MODES, "mock"),
        "auth_mode": auth_mode(),
        "billing": (
            "paused"
            if _env_str("STRIPE_API_KEY") and payments_paused()
            else ("stripe" if _env_str("STRIPE_API_KEY") else "off")
        ),
        "requests": "github" if _env_str("GITHUB_ISSUES_TOKEN") else "off",
        "search": search_mode(),
        "wikipedia": (
            f"{wikipedia_language()}.wikipedia.org" if wikipedia_mode() == "on" else "off"
        ),
    }


def validate_configuration() -> dict[str, str]:
    """Build every configured dependency now, so a bad value stops the boot.

    Called from the app's startup. Without it the process comes up healthy and
    a typo in ``ELECTION_STORE`` only surfaces on the first import — after the
    revision has already been rolled out and started taking traffic.
    """
    chosen = describe_configuration()
    if ENV_FILE_LOADED is not None:
        # Names only, never values: this file is where the API keys are. Worth a
        # line even so — "why is billing on?" is answered by seeing which file
        # was picked up, especially one found by walking up from the cwd.
        log.info(
            "loaded %s from %s", ", ".join(ENV_NAMES_LOADED) or "nothing", ENV_FILE_LOADED
        )
    _env_float("PARSE_LEASE_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)
    get_store()
    get_parser()
    # Not reached by ``get_parser`` when importing is off, and a misspelled
    # SEARCH_MODE or WIKIPEDIA_LANGUAGE must still stop the boot.
    get_search()
    get_wikipedia()
    get_accounts()
    get_password_store()
    get_verifier()
    get_quota_policy()
    get_billing()
    get_wishlist()
    if origin_secret() and len(origin_secret()) < MIN_ORIGIN_SECRET_CHARS:
        raise ConfigError(
            f"ORIGIN_SECRET must be at least {MIN_ORIGIN_SECRET_CHARS} characters"
        )
    if chosen["auth_mode"] == "firebase" and not _env_str("FIREBASE_API_KEY"):
        # Not fatal: the API still verifies tokens. But nothing in the browser
        # can obtain one, so sign-in is dead until this is set.
        log.warning("AUTH_MODE=firebase without FIREBASE_API_KEY: the page cannot sign anyone in")
    log.info(
        "configuration ok store=%s llm_mode=%s auth_mode=%s billing=%s requests=%s "
        "search=%s wikipedia=%s",
        chosen["store"], chosen["llm_mode"], chosen["auth_mode"], chosen["billing"],
        chosen["requests"], chosen["search"], chosen["wikipedia"],
    )
    return chosen
