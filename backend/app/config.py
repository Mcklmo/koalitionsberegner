"""Environment-driven configuration and dependency wiring."""

from __future__ import annotations

import os
from functools import lru_cache

from .parser import ElectionParser, UnavailableParser
from .store import DEFAULT_STALE_AFTER_SECONDS, ElectionStore, InMemoryElectionStore


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


@lru_cache(maxsize=1)
def get_store() -> ElectionStore:
    """Firestore in deployment; an in-memory store when no project is configured.

    ``ELECTION_STORE=memory`` forces the in-memory store for local runs.
    """
    stale_after = _env_float("PARSE_LEASE_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    backend = os.environ.get("ELECTION_STORE", "firestore").lower()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")

    if backend == "memory" or not project:
        return InMemoryElectionStore(stale_after=stale_after)

    from google.cloud import firestore

    from .firestore_store import FirestoreElectionStore

    client = firestore.Client(
        project=project, database=os.environ.get("FIRESTORE_DATABASE", "(default)")
    )
    return FirestoreElectionStore(client, stale_after=stale_after)


@lru_cache(maxsize=1)
def get_parser() -> ElectionParser:
    """The LLM extraction agent lands here; until then imports fail cleanly."""
    return UnavailableParser()


def max_wait_seconds() -> float:
    return _env_float("IMPORT_MAX_WAIT_SECONDS", 25.0)
