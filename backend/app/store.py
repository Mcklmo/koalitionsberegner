"""The election store and its single-flight parse-job bookkeeping.

Two collections: validated elections keyed by identity hash, and one parse-job
record per hash. The store exposes a single atomic primitive — :meth:`claim` —
which decides, in one transaction, whether a caller must parse, may attach to
an in-flight parse, or can be served from storage immediately. Everything else
in the backend is built on that decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Protocol

from .schema import Election

# A parse that has not reported back within this window is presumed dead and may
# be reclaimed, so a crashed container cannot pin an election in "pending".
DEFAULT_STALE_AFTER_SECONDS = 300.0


class JobStatus(str, Enum):
    PENDING = "pending"
    # Parsed, shown to the user, not yet saved. Nothing reaches the election
    # collection until someone confirms what the extraction produced.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Statuses that mean "a parse already happened or is happening for this hash".
LIVE_STATUSES = (JobStatus.PENDING, JobStatus.AWAITING_CONFIRMATION)


class ClaimOutcome(str, Enum):
    """What a caller should do after claiming an election hash."""

    STORED = "stored"      # already parsed; serve it, parse nothing
    STARTED = "started"    # this caller owns the parse
    ATTACHED = "attached"  # someone else is parsing; wait for their result


@dataclass(frozen=True)
class ImportRequest:
    """What a user supplies to import an election."""

    nation: str
    state: str | None
    election_date: str
    source_url: str


@dataclass(frozen=True)
class Job:
    election_hash: str
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
    election_hash: str
    election: Election | None = None
    job: Job | None = None


@dataclass(frozen=True)
class StoredElection:
    election_hash: str
    election: Election
    stored_at: float


class ElectionStore(Protocol):
    """Storage seam. Implementations must make :meth:`claim` atomic."""

    def get_election(self, election_hash: str) -> Election | None: ...

    def list_elections(self) -> list[StoredElection]: ...

    def get_job(self, election_hash: str) -> Job | None: ...

    def claim(self, election_hash: str, request: ImportRequest) -> Claim: ...

    def stage(self, election_hash: str, election: Election) -> None:
        """Record a parsed election as awaiting the user's confirmation."""
        ...

    def confirm(self, election_hash: str) -> Election | None:
        """Promote a staged election into storage. Idempotent."""
        ...

    def discard(self, election_hash: str) -> bool:
        """Throw away a staged election, freeing the hash for another attempt."""
        ...

    def fail(self, election_hash: str, error: str) -> None: ...


def _decide(
    election: Election | None,
    job: Job | None,
    now: float,
    stale_after: float,
) -> ClaimOutcome:
    """The claim rule, shared by every implementation so they cannot drift.

    A stored election always wins. A fresh job — parsing, or parsed and waiting
    for the user to confirm — is joined. Anything else (no job, a failed job, or
    a job past its lease) is claimable, which is what keeps failures and
    abandoned previews from poisoning the hash.
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

    def get_job(self, election_hash: str) -> Job | None:
        with self._lock:
            return self._jobs.get(election_hash)

    def claim(self, election_hash: str, request: ImportRequest) -> Claim:
        with self._lock:
            stored = self._elections.get(election_hash)
            job = self._jobs.get(election_hash)
            outcome = _decide(
                stored.election if stored else None, job, self._clock(), self._stale_after
            )
            if outcome is ClaimOutcome.STORED:
                return Claim(outcome, election_hash, election=stored.election, job=job)
            if outcome is ClaimOutcome.ATTACHED:
                return Claim(outcome, election_hash, job=job)
            new_job = Job(
                election_hash=election_hash,
                status=JobStatus.PENDING,
                source_url=request.source_url,
                started_at=self._clock(),
                attempt=(job.attempt + 1) if job else 1,
            )
            self._jobs[election_hash] = new_job
            return Claim(outcome, election_hash, job=new_job)

    def stage(self, election_hash: str, election: Election) -> None:
        with self._lock:
            job = self._jobs.get(election_hash)
            self._jobs[election_hash] = Job(
                election_hash=election_hash,
                status=JobStatus.AWAITING_CONFIRMATION,
                source_url=job.source_url if job else "",
                # Restart the lease so the user gets a full window to confirm.
                started_at=self._clock(),
                attempt=job.attempt if job else 1,
                result=election,
            )

    def confirm(self, election_hash: str) -> Election | None:
        with self._lock:
            stored = self._elections.get(election_hash)
            if stored is not None:
                return stored.election  # already confirmed; confirming twice is harmless
            job = self._jobs.get(election_hash)
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION or job.result is None:
                return None
            self._elections[election_hash] = StoredElection(
                election_hash=election_hash, election=job.result, stored_at=self._clock()
            )
            self._jobs[election_hash] = Job(
                election_hash=election_hash,
                status=JobStatus.SUCCEEDED,
                source_url=job.source_url,
                started_at=job.started_at,
                attempt=job.attempt,
            )
            return job.result

    def discard(self, election_hash: str) -> bool:
        with self._lock:
            job = self._jobs.get(election_hash)
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION:
                return False
            del self._jobs[election_hash]
            return True

    def fail(self, election_hash: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(election_hash)
            started_at = job.started_at if job else self._clock()
            attempt = job.attempt if job else 1
            source_url = job.source_url if job else ""
            self._jobs[election_hash] = Job(
                election_hash=election_hash,
                status=JobStatus.FAILED,
                source_url=source_url,
                started_at=started_at,
                attempt=attempt,
                error=error,
            )
