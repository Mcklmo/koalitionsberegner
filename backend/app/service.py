"""Import orchestration: short-circuit on stored elections, single-flight on the rest.

The store decides *what* happens (see :meth:`ElectionStore.claim`); this module
carries it out — launching at most one parse per election hash, letting every
other caller wait for that same parse, and recording the outcome so a failure
leaves the hash free for a later retry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

import anyio.to_thread

from .identity import election_hash
from .parser import ElectionParser
from .schema import Election
from .store import ClaimOutcome, ElectionStore, ImportRequest, JobStatus

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.05


class ImportState(str, Enum):
    READY = "ready"        # the election is stored and returned
    PENDING = "pending"    # a parse is running; poll or wait
    FAILED = "failed"      # the last parse failed; importing again retries
    UNKNOWN = "unknown"    # nothing stored and no job for this hash


@dataclass(frozen=True)
class ImportResult:
    election_hash: str
    state: ImportState
    election: Election | None = None
    error: str | None = None
    attempt: int | None = None
    reused: bool = False
    """True when the caller was served without triggering any parsing."""


class ImportService:
    def __init__(self, store: ElectionStore, parser: ElectionParser):
        self._store = store
        self._parser = parser
        # Holding task references keeps the event loop from garbage-collecting
        # a parse that no request is awaiting.
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def hash_for(request: ImportRequest) -> str:
        return election_hash(request.nation, request.state, request.election_date)

    async def _in_thread(self, fn, *args):
        """Firestore's client is blocking; keep it off the event loop."""
        return await anyio.to_thread.run_sync(fn, *args)

    async def submit(self, request: ImportRequest) -> ImportResult:
        """Claim the hash and start a parse only if nobody else already has."""
        key = self.hash_for(request)
        claim = await self._in_thread(self._store.claim, key, request)

        if claim.outcome is ClaimOutcome.STORED:
            # Second import of a stored election: short-circuited, nothing parsed.
            return ImportResult(key, ImportState.READY, election=claim.election, reused=True)

        if claim.outcome is ClaimOutcome.ATTACHED:
            job = claim.job
            return ImportResult(
                key, ImportState.PENDING, attempt=job.attempt if job else None, reused=True
            )

        task = asyncio.create_task(self._parse(key, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        job = claim.job
        return ImportResult(key, ImportState.PENDING, attempt=job.attempt if job else 1)

    async def _parse(self, key: str, request: ImportRequest) -> None:
        try:
            election = await self._parser.parse(request)
        except Exception as exc:  # noqa: BLE001 - a failed parse must not kill the worker
            log.warning("parse failed for %s: %s", key, exc)
            await self._in_thread(self._store.fail, key, str(exc))
            return

        # An election must agree with the identity it is filed under, or the same
        # election could end up stored twice under different hashes.
        parsed_key = election_hash(election.nation, election.state, election.election_date)
        if parsed_key != key:
            await self._in_thread(
                self._store.fail,
                key,
                "parsed election identity does not match the requested "
                f"nation/state/date (got {election.nation!r}, {election.state!r}, "
                f"{election.election_date.isoformat()})",
            )
            return
        try:
            await self._in_thread(self._store.complete, key, election)
        except Exception as exc:  # noqa: BLE001
            log.exception("storing %s failed", key)
            await self._in_thread(self._store.fail, key, f"could not store result: {exc}")

    async def status(self, key: str) -> ImportResult:
        election = await self._in_thread(self._store.get_election, key)
        if election is not None:
            return ImportResult(key, ImportState.READY, election=election)
        job = await self._in_thread(self._store.get_job, key)
        if job is None:
            return ImportResult(key, ImportState.UNKNOWN)
        if job.status is JobStatus.FAILED:
            return ImportResult(key, ImportState.FAILED, error=job.error, attempt=job.attempt)
        # A job that reports success without a stored election is still settling.
        return ImportResult(key, ImportState.PENDING, attempt=job.attempt)

    async def wait_for(self, key: str, timeout: float) -> ImportResult:
        """Poll until the parse reaches a terminal state or ``timeout`` elapses.

        Callers that attached to someone else's parse get that parse's result
        here — the same election, from the same single run.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = await self.status(key)
            if result.state in (ImportState.READY, ImportState.FAILED, ImportState.UNKNOWN):
                return result
            if asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def list_elections(self):
        return await self._in_thread(self._store.list_elections)
