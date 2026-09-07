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

from .parser import ElectionParser, UnavailableParser
from .store import DEFAULT_STALE_AFTER_SECONDS, ElectionStore, InMemoryElectionStore

log = logging.getLogger(__name__)

#: Every recognised value, so an unknown one can name the alternatives.
STORE_BACKENDS = ("firestore", "sqlite", "memory")
LLM_MODES = ("mock", "live", "off")

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


def max_wait_seconds() -> float:
    return _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)


def describe_configuration() -> dict[str, str]:
    """The choices actually in effect, for the startup log."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    return {
        "store": _env_choice("ELECTION_STORE", STORE_BACKENDS,
                             "firestore" if project else "memory"),
        "llm_mode": _env_choice("LLM_MODE", LLM_MODES, "mock"),
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
    log.info("configuration ok store=%s llm_mode=%s", chosen["store"], chosen["llm_mode"])
    return chosen
