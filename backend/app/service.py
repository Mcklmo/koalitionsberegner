"""Import orchestration: short-circuit on known pages, single-flight on the rest.

The store decides *what* happens (see :meth:`ElectionStore.claim`); this module
carries it out — running at most one extraction per page, letting every other
caller share it, and reconciling the identity the agent inferred against what is
already stored before anything is saved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

import anyio.to_thread

from .identity import election_hash, source_url_key
from .parser import ElectionParser
from .schema import Election
from .store import ClaimOutcome, ElectionStore, ImportRequest, JobStatus

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.05


class ImportState(str, Enum):
    READY = "ready"        # the election is stored and returned
    PENDING = "pending"    # an extraction is running; poll or wait
    PREVIEW = "preview"    # extracted and shown for confirmation; not saved yet
    FAILED = "failed"      # the last extraction failed; importing again retries
    UNKNOWN = "unknown"    # nothing stored and no job for this page


#: States a caller can stop polling on.
TERMINAL_STATES = (ImportState.READY, ImportState.PREVIEW, ImportState.FAILED, ImportState.UNKNOWN)


@dataclass(frozen=True)
class ImportResult:
    page_key: str
    state: ImportState
    election: Election | None = None
    """The stored election, or — in the PREVIEW state — the unsaved extraction."""
    election_hash: str | None = None
    """Known only once the agent has inferred the election's identity."""
    error: str | None = None
    attempt: int | None = None
    reused: bool = False
    """True when the caller was served without triggering any extraction."""
    duplicate: bool = False
    """True when this page turned out to describe an already-stored election."""


def identity_of(election: Election) -> str:
    return election_hash(election.nation, election.state, election.election_date)


class ImportService:
    def __init__(self, store: ElectionStore, parser: ElectionParser):
        self._store = store
        self._parser = parser
        # Holding task references keeps the event loop from garbage-collecting
        # an extraction that no request is awaiting.
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def page_key_for(request: ImportRequest) -> str:
        return source_url_key(request.source_url)

    async def _in_thread(self, fn, *args):
        """Firestore's client is blocking; keep it off the event loop."""
        return await anyio.to_thread.run_sync(fn, *args)

    async def submit(self, request: ImportRequest) -> ImportResult:
        """Claim the page and extract only if nobody else already has."""
        key = self.page_key_for(request)
        claim = await self._in_thread(self._store.claim, key, request)

        if claim.outcome is ClaimOutcome.STORED:
            # This page has been imported before: no fetch, no model call.
            return ImportResult(
                key, ImportState.READY, election=claim.election,
                election_hash=claim.election_hash, reused=True,
            )

        if claim.outcome is ClaimOutcome.ATTACHED:
            job = claim.job
            if job is not None and job.status is JobStatus.AWAITING_CONFIRMATION:
                return ImportResult(
                    key, ImportState.PREVIEW, election=job.result,
                    election_hash=identity_of(job.result) if job.result else None,
                    attempt=job.attempt, reused=True,
                )
            return ImportResult(
                key, ImportState.PENDING, attempt=job.attempt if job else None, reused=True
            )

        task = asyncio.create_task(self._extract(key, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        job = claim.job
        return ImportResult(key, ImportState.PENDING, attempt=job.attempt if job else 1)

    async def _extract(self, key: str, request: ImportRequest) -> None:
        try:
            election = await self._parser.parse(request)
        except Exception as exc:  # noqa: BLE001 - a failed extraction must not kill the worker
            log.warning("extraction failed for %s: %s", key, exc)
            await self._in_thread(self._store.fail, key, str(exc))
            return

        try:
            # The agent may have identified an election we already hold — a second
            # URL for the same results. Link the page to it instead of asking the
            # user to confirm a duplicate.
            existing_hash = identity_of(election)
            existing = await self._in_thread(self._store.get_election, existing_hash)
            if existing is not None:
                await self._in_thread(self._store.link, key, existing_hash)
                return
            # Staged, not stored: the user still has to confirm the identity the
            # agent inferred, and the numbers it read.
            await self._in_thread(self._store.stage, key, election)
        except Exception as exc:  # noqa: BLE001
            log.exception("staging %s failed", key)
            await self._in_thread(self._store.fail, key, f"could not stage result: {exc}")

    async def status(self, key: str) -> ImportResult:
        job = await self._in_thread(self._store.get_job, key)
        resolved = await self._in_thread(self._store.resolve_page, key)
        if resolved is not None:
            election = await self._in_thread(self._store.get_election, resolved)
            if election is not None:
                return ImportResult(
                    key, ImportState.READY, election=election, election_hash=resolved
                )
        if job is None:
            return ImportResult(key, ImportState.UNKNOWN)
        if job.status is JobStatus.FAILED:
            return ImportResult(key, ImportState.FAILED, error=job.error, attempt=job.attempt)
        if job.status is JobStatus.AWAITING_CONFIRMATION and job.result is not None:
            return ImportResult(
                key, ImportState.PREVIEW, election=job.result,
                election_hash=identity_of(job.result), attempt=job.attempt,
            )
        return ImportResult(key, ImportState.PENDING, attempt=job.attempt)

    async def wait_for(self, key: str, timeout: float) -> ImportResult:
        """Poll until the extraction reaches a terminal state or ``timeout`` elapses.

        Callers that attached to someone else's extraction get that extraction's
        result here — the same election, from the same single run.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = await self.status(key)
            if result.state in TERMINAL_STATES:
                return result
            if asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def confirm(self, key: str) -> ImportResult:
        """Save a previewed election under the identity the agent inferred.

        Nothing reaches storage without this.
        """
        job = await self._in_thread(self._store.get_job, key)
        if job is None or job.result is None:
            return await self.status(key)
        confirmation = await self._in_thread(self._store.confirm, key, identity_of(job.result))
        if confirmation is None:
            return await self.status(key)
        return ImportResult(
            key, ImportState.READY,
            election=confirmation.election,
            election_hash=confirmation.election_hash,
            duplicate=confirmation.duplicate,
        )

    async def discard(self, key: str) -> bool:
        """Reject a previewed election, freeing the page for another attempt."""
        return await self._in_thread(self._store.discard, key)

    async def get_stored(self, election_hash: str) -> Election | None:
        return await self._in_thread(self._store.get_election, election_hash)

    async def list_elections(self):
        return await self._in_thread(self._store.list_elections)
