"""A short-lived read cache in front of the shared election store.

Viewing is open, and every view is a store read: the list a visitor loads is one
read per stored election, and a lookup reads them all. Firestore bills per document read, so without this
a script reloading the front page turns straight into a bill.

Only stored elections are cached, and only positively. They never change once
stored except for a backfill's corrections, so a copy up to :data:`TTL_SECONDS`
old is right in everything but whether a new one is listed yet. Jobs are never cached:
an import's progress has to be read fresh. An election that is *not* found is
not cached either, so one confirmed on another instance is visible here at once.
"""

from __future__ import annotations

import time
from threading import Lock

from .schema import Election
from .store import ElectionStore, StoredElection, select_by_place

TTL_SECONDS = 30.0


class CachedElectionStore:
    """Wraps any :class:`~app.store.ElectionStore`; everything uncached passes through."""

    def __init__(self, inner: ElectionStore, *, ttl: float = TTL_SECONDS, clock=time.monotonic):
        self._inner = inner
        self._ttl = ttl
        self._clock = clock
        self._lock = Lock()
        self._list: tuple[float, list[StoredElection]] | None = None
        self._stored: dict[str, tuple[float, StoredElection]] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _fresh(self, entry):
        return entry is not None and self._clock() - entry[0] < self._ttl

    def _forget(self, election_hash: str | None = None) -> None:
        with self._lock:
            self._list = None
            if election_hash is not None:
                self._stored.pop(election_hash, None)

    # --- reads --------------------------------------------------------------

    def list_elections(self) -> list[StoredElection]:
        with self._lock:
            entry = self._list
            if self._fresh(entry):
                return list(entry[1])
        listed = self._inner.list_elections()
        with self._lock:
            self._list = (self._clock(), listed)
        return list(listed)

    def find_by_place(
        self, year: int, nation: str, subnation: str | None = None
    ) -> StoredElection | None:
        return select_by_place(self.list_elections(), year, nation, subnation)

    def get_stored(self, election_hash: str) -> StoredElection | None:
        with self._lock:
            entry = self._stored.get(election_hash)
            if self._fresh(entry):
                return entry[1]
        stored = self._inner.get_stored(election_hash)
        if stored is not None:
            with self._lock:
                self._stored[election_hash] = (self._clock(), stored)
        return stored

    def get_election(self, election_hash: str) -> Election | None:
        stored = self.get_stored(election_hash)
        return stored.election if stored else None

    # --- writes that change what the reads above would say ------------------

    def confirm(self, request_key: str, election_hash: str):
        try:
            return self._inner.confirm(request_key, election_hash)
        finally:
            self._forget(election_hash)

    def confirm_forecast(self, request_key: str, forecast: Election, election_hash: str):
        try:
            return self._inner.confirm_forecast(request_key, forecast, election_hash)
        finally:
            self._forget(election_hash)

    def set_selected(self, election_hash: str, selected: bool) -> bool:
        try:
            return self._inner.set_selected(election_hash, selected)
        finally:
            self._forget(election_hash)

    def replace_election(self, election_hash: str, election: Election) -> bool:
        try:
            return self._inner.replace_election(election_hash, election)
        finally:
            self._forget(election_hash)

    def link(self, request_key: str, election_hash: str) -> None:
        try:
            return self._inner.link(request_key, election_hash)
        finally:
            self._forget(election_hash)
