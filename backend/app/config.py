"""Environment-driven configuration and dependency wiring."""

from __future__ import annotations

import os
from functools import lru_cache

from .parser import ElectionParser, UnavailableParser
from .store import DEFAULT_STALE_AFTER_SECONDS, ElectionStore, InMemoryElectionStore


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


DEFAULT_SQLITE_PATH = "./data/elections.db"


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
    backend = os.environ.get("ELECTION_STORE", "firestore" if project else "memory").lower()

    if backend == "memory":
        return InMemoryElectionStore(stale_after=stale_after)

    if backend == "sqlite":
        from .sqlite_store import SqliteElectionStore

        return SqliteElectionStore(
            os.environ.get("SQLITE_PATH", DEFAULT_SQLITE_PATH), stale_after=stale_after
        )

    if backend != "firestore":
        raise RuntimeError(
            f"ELECTION_STORE must be firestore, sqlite or memory, got {backend!r}"
        )
    if not project:
        raise RuntimeError("ELECTION_STORE=firestore requires GOOGLE_CLOUD_PROJECT")

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

    mode = os.environ.get("LLM_MODE", "mock").lower()
    if mode == "off":
        return UnavailableParser()
    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("LLM_MODE=live requires ANTHROPIC_API_KEY")
        return LlmElectionParser(HttpPageFetcher(), AnthropicExtractor())
    # Mock: the page is still fetched, so the whole pipeline runs except the model.
    return LlmElectionParser(HttpPageFetcher(), MockExtractor())


def max_wait_seconds() -> float:
    return _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)
