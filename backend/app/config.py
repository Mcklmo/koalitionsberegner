"""Environment-driven configuration and dependency wiring.

Configuration is validated eagerly at startup (see :func:`validate_configuration`),
because the alternative is a container that passes its health check and only
reveals a typo when the first real request arrives. A value that is not
recognised is an error, never a silent fallback to a default — a misspelled
``LLM_MODE`` must not quietly serve mock election data.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from .accounts import DEFAULT_MONTHLY_IMPORTS, AccountStore, InMemoryAccountStore, QuotaPolicy, Tier
from .auth import DisabledVerifier, FirebaseTokenVerifier, StubTokenVerifier, TokenVerifier
from .billing import Billing, DisabledBilling, StripeBilling
from .parser import ElectionParser, UnavailableParser
from .store import DEFAULT_STALE_AFTER_SECONDS, ElectionStore, InMemoryElectionStore

log = logging.getLogger(__name__)

#: Every recognised value, so an unknown one can name the alternatives.
STORE_BACKENDS = ("firestore", "sqlite", "memory")
LLM_MODES = ("mock", "live", "off")
AUTH_MODES = ("firebase", "stub", "off")

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

    from .firestore_store import FirestoreElectionStore

    client = firestore.Client(
        project=project, database=os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    return FirestoreElectionStore(client, stale_after=stale_after)


@lru_cache(maxsize=1)
def get_parser() -> ElectionParser:
    """Wire the extraction pipeline.

    ``LLM_MODE`` selects the agent: ``mock`` (the default) returns a fixed
    result without contacting the Anthropic API; ``live`` runs the real agent
    and requires ``ANTHROPIC_API_KEY``, mounted from GCP Secret Manager.
    ``off`` disables importing entirely.
    """
    from .extractor import AnthropicExtractor, MockExtractor
    from .fetcher import HttpPageFetcher
    from .parser import LlmElectionParser

    mode = _env_choice("LLM_MODE", LLM_MODES, "mock")
    if mode == "off":
        return UnavailableParser()
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigError("LLM_MODE=live requires ANTHROPIC_API_KEY")
        return LlmElectionParser(HttpPageFetcher(), AnthropicExtractor())
    # Mock: the page is still fetched, so the whole pipeline runs except the model.
    return LlmElectionParser(HttpPageFetcher(), MockExtractor())


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
def get_verifier() -> TokenVerifier:
    """Wire token verification.

    ``AUTH_MODE`` selects it: ``firebase`` verifies real ID tokens against
    Google's certificates; ``stub`` trusts the token's text and exists only so
    the gated behaviour can be exercised without a Firebase project; ``off``
    disables gating entirely for local runs.
    """
    mode = auth_mode()
    if mode == "off":
        return DisabledVerifier()
    if mode == "stub":
        # Loud, because a deployment reaching this line is trusting whatever a
        # caller types into the Authorization header.
        log.warning("AUTH_MODE=stub: identities are unverified — local use only")
        return StubTokenVerifier()

    project = firebase_project_id()
    if not project:
        raise ConfigError("AUTH_MODE=firebase requires FIREBASE_PROJECT_ID")
    return FirebaseTokenVerifier(project, admin_emails=admin_emails())


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


def public_base_url() -> str:
    """Where Stripe sends the user back to. No trailing slash."""
    return _env_str("PUBLIC_BASE_URL").rstrip("/")


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
        "billing": "stripe" if _env_str("STRIPE_API_KEY") else "off",
    }


def validate_configuration() -> dict[str, str]:
    """Build every configured dependency now, so a bad value stops the boot.

    Called from the app's startup. Without it the process comes up healthy and
    a typo in ``ELECTION_STORE`` only surfaces on the first import — after the
    revision has already been rolled out and started taking traffic.
    """
    chosen = describe_configuration()
    _env_float("PARSE_LEASE_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)
    get_store()
    get_parser()
    get_accounts()
    get_verifier()
    get_quota_policy()
    get_billing()
    if chosen["auth_mode"] == "firebase" and not _env_str("FIREBASE_API_KEY"):
        # Not fatal: the API still verifies tokens. But nothing in the browser
        # can obtain one, so sign-in is dead until this is set.
        log.warning("AUTH_MODE=firebase without FIREBASE_API_KEY: the page cannot sign anyone in")
    log.info(
        "configuration ok store=%s llm_mode=%s auth_mode=%s billing=%s",
        chosen["store"], chosen["llm_mode"], chosen["auth_mode"], chosen["billing"],
    )
    return chosen
