"""The election store and its single-flight extraction bookkeeping.

Three collections: validated elections keyed by *identity* hash, one extraction
job per *page*, and an index from page to the election that page produced.

Two different keys, because an election's identity — nation, region, date — is
not known until the page has been read. Work is therefore claimed by page
(:func:`identity.source_url_key`), while storage and de-duplication happen by
election identity (:func:`identity.election_hash`). The index between them is
what lets a page that has been imported before short-circuit without any
extraction at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Protocol

from .schema import Election

# An extraction that has not reported back within this window is presumed dead
# and may be reclaimed, so a crashed container cannot pin a page in "pending".
DEFAULT_STALE_AFTER_SECONDS = 300.0


class JobStatus(str, Enum):
    PENDING = "pending"
    # Extracted, shown to the user, not yet saved. Nothing reaches the election
    # collection until someone confirms the identity the agent inferred.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Statuses meaning "an extraction already happened or is happening for this page".
LIVE_STATUSES = (JobStatus.PENDING, JobStatus.AWAITING_CONFIRMATION)


class ClaimOutcome(str, Enum):
    """What a caller should do after claiming a page."""

    STORED = "stored"      # this page already produced a stored election
    STARTED = "started"    # this caller owns the extraction
    ATTACHED = "attached"  # someone else is extracting; wait for their result


@dataclass(frozen=True)
class ImportRequest:
    """What a user supplies to import an election: just where to read it."""

    source_url: str


@dataclass(frozen=True)
class Job:
    page_key: str
    status: JobStatus
    source_url: str
    started_at: float
    attempt: int = 1
    error: str | None = None
    result: Election | None = None
    """The extracted election awaiting confirmation; never served as stored."""


@dataclass(frozen=True)
class Claim:
    outcome: ClaimOutcome
    page_key: str
    election: Election | None = None
    election_hash: str | None = None
    job: Job | None = None


@dataclass(frozen=True)
class Confirmation:
    election_hash: str
    election: Election
    duplicate: bool
    """True when this page turned out to describe an already-stored election."""


@dataclass(frozen=True)
class StoredElection:
    election_hash: str
    election: Election
    stored_at: float


class ElectionStore(Protocol):
    """Storage seam. Implementations must make :meth:`claim` atomic."""

    def get_election(self, election_hash: str) -> Election | None: ...

    def list_elections(self) -> list[StoredElection]: ...

    def get_job(self, page_key: str) -> Job | None: ...

    def resolve_page(self, page_key: str) -> str | None:
        """The election hash this page produced, if it has produced one."""
        ...

    def claim(self, page_key: str, request: ImportRequest) -> Claim: ...

    def stage(self, page_key: str, election: Election) -> None:
        """Record an extracted election as awaiting the user's confirmation."""
        ...

    def confirm(self, page_key: str, election_hash: str) -> Confirmation | None:
        """Store a staged election under its identity. Idempotent."""
        ...

    def link(self, page_key: str, election_hash: str) -> None:
        """Record that this page describes an already-stored election."""
        ...

    def discard(self, page_key: str) -> bool:
        """Throw away a staged election, freeing the page for another attempt."""
        ...

    def fail(self, page_key: str, error: str) -> None: ...


def _decide(
    election: Election | None,
    job: Job | None,
    now: float,
    stale_after: float,
) -> ClaimOutcome:
    """The claim rule, shared by every implementation so they cannot drift.

    A page already resolved to a stored election always wins. A fresh job —
    extracting, or extracted and waiting for confirmation — is joined. Anything
    else (no job, a failed job, or a job past its lease) is claimable, which is
    what keeps failures and abandoned previews from poisoning the page.
    """
    if election is not None:
        return ClaimOutcome.STORED
    if job is not None and job.status in LIVE_STATUSES and now - job.started_at < stale_after:
        return ClaimOutcome.ATTACHED
    return ClaimOutcome.STARTED


class InMemoryElectionStore:
    """Process-local store used by the tests and by local runs without Firestore.

    A single lock stands in for a Firestore transaction: the read-decide-write
    of :meth:`claim` is indivisible, which is the only property the single-flight
    logic depends on.
    """

    def __init__(self, *, stale_after: float = DEFAULT_STALE_AFTER_SECONDS, clock=time.monotonic):
        self._elections: dict[str, StoredElection] = {}
        self._jobs: dict[str, Job] = {}
        self._pages: dict[str, str] = {}  # page key -> election hash
        self._lock = Lock()
        self._stale_after = stale_after
        self._clock = clock

    def get_election(self, election_hash: str) -> Election | None:
        with self._lock:
            stored = self._elections.get(election_hash)
            return stored.election if stored else None

    def list_elections(self) -> list[StoredElection]:
        with self._lock:
            return sorted(self._elections.values(), key=lambda s: s.stored_at)

    def get_job(self, page_key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(page_key)

    def resolve_page(self, page_key: str) -> str | None:
        with self._lock:
            return self._pages.get(page_key)

    def claim(self, page_key: str, request: ImportRequest) -> Claim:
        with self._lock:
            election_hash = self._pages.get(page_key)
            stored = self._elections.get(election_hash) if election_hash else None
            job = self._jobs.get(page_key)
            outcome = _decide(
                stored.election if stored else None, job, self._clock(), self._stale_after
            )
            if outcome is ClaimOutcome.STORED:
                return Claim(outcome, page_key, election=stored.election,
                             election_hash=election_hash, job=job)
            if outcome is ClaimOutcome.ATTACHED:
                return Claim(outcome, page_key, job=job)
            new_job = Job(
                page_key=page_key,
                status=JobStatus.PENDING,
                source_url=request.source_url,
                started_at=self._clock(),
                attempt=(job.attempt + 1) if job else 1,
            )
            self._jobs[page_key] = new_job
            return Claim(outcome, page_key, job=new_job)

    def stage(self, page_key: str, election: Election) -> None:
        with self._lock:
            job = self._jobs.get(page_key)
            self._jobs[page_key] = Job(
                page_key=page_key,
                status=JobStatus.AWAITING_CONFIRMATION,
                source_url=job.source_url if job else "",
                # Restart the lease so the user gets a full window to confirm.
                started_at=self._clock(),
                attempt=job.attempt if job else 1,
                result=election,
            )

    def confirm(self, page_key: str, election_hash: str) -> Confirmation | None:
        with self._lock:
            job = self._jobs.get(page_key)
            already = self._elections.get(election_hash)
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION or job.result is None:
                # Confirming twice is harmless as long as the page resolved here.
                if already is not None and self._pages.get(page_key) == election_hash:
                    return Confirmation(election_hash, already.election, duplicate=False)
                return None

            duplicate = already is not None
            if not duplicate:
                self._elections[election_hash] = StoredElection(
                    election_hash=election_hash, election=job.result, stored_at=self._clock()
                )
            self._pages[page_key] = election_hash
            self._jobs[page_key] = Job(
                page_key=page_key,
                status=JobStatus.SUCCEEDED,
                source_url=job.source_url,
                started_at=job.started_at,
                attempt=job.attempt,
            )
            stored = self._elections[election_hash]
            return Confirmation(election_hash, stored.election, duplicate=duplicate)

    def link(self, page_key: str, election_hash: str) -> None:
        with self._lock:
            if election_hash not in self._elections:
                return
            self._pages[page_key] = election_hash
            job = self._jobs.get(page_key)
            self._jobs[page_key] = Job(
                page_key=page_key,
                status=JobStatus.SUCCEEDED,
                source_url=job.source_url if job else "",
                started_at=job.started_at if job else self._clock(),
                attempt=job.attempt if job else 1,
            )

    def discard(self, page_key: str) -> bool:
        with self._lock:
            job = self._jobs.get(page_key)
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION:
                return False
            del self._jobs[page_key]
            return True

    def fail(self, page_key: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(page_key)
            self._jobs[page_key] = Job(
                page_key=page_key,
                status=JobStatus.FAILED,
                source_url=job.source_url if job else "",
                started_at=job.started_at if job else self._clock(),
                attempt=job.attempt if job else 1,
                error=error,
            )
