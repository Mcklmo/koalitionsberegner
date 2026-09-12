"""The election store and its single-flight import bookkeeping.

Three collections: validated elections keyed by *identity* hash, one import job
per *request*, and an index from request to the election that request produced.

Two different keys, because an election's identity — nation, region, date — is
not known until the results have been found: a user asks for a year and a
place, not for a day. Work is therefore claimed by request
(:func:`identity.request_key`), while storage and de-duplication happen by
election identity (:func:`identity.election_hash`). The index between them is
what lets a request that has been made before short-circuit without any
searching or extraction at all.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from threading import Lock
from typing import Protocol

from .identity import same_place
from .schema import Election

# An import that has not reported back within this window is presumed dead and
# may be reclaimed, so a crashed container cannot pin a request in "pending".
DEFAULT_STALE_AFTER_SECONDS = 300.0


class JobStatus(str, Enum):
    PENDING = "pending"
    # Extracted, shown to the user, not yet saved. Nothing reaches the election
    # collection until someone confirms the identity the agent inferred.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    # An election not yet held: its polls were read, and the user picks which
    # to save. Any number of them may be, so a choice does not end the job —
    # its lease does, after which asking again reads the polls afresh.
    AWAITING_CHOICE = "awaiting_choice"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Statuses meaning "an import already ran, or is running, for this request".
LIVE_STATUSES = (JobStatus.PENDING, JobStatus.AWAITING_CONFIRMATION, JobStatus.AWAITING_CHOICE)

#: Statuses holding something the user has not accepted yet, and may throw away.
DISCARDABLE_STATUSES = (JobStatus.AWAITING_CONFIRMATION, JobStatus.AWAITING_CHOICE)


class ClaimOutcome(str, Enum):
    """What a caller should do after claiming a request."""

    STORED = "stored"      # this request already produced a stored election
    STARTED = "started"    # this caller owns the import
    ATTACHED = "attached"  # someone else is importing; wait for their result


@dataclass(frozen=True)
class ImportRequest:
    """What a user supplies to import an election: which election, roughly.

    A year, a nation, and — for a regional election — the region within it.
    Spelling is not expected to be exact; resolving "Germny" and finding the
    day the election was held is the resolver's job (see :mod:`app.resolver`).
    """

    year: int
    nation: str
    subnation: str | None = None

    def describe(self) -> str:
        """One line naming the request, for logs and for the job record.

        The user's own words, not the resolver's reading of them: when an
        import goes wrong this is the thing that needs to be recognisable.
        """
        where = f"{self.nation} — {self.subnation}" if self.subnation else self.nation
        return f"{where} {self.year}"


@dataclass(frozen=True)
class Job:
    request_key: str
    status: JobStatus
    query: str
    """What was asked for, as :meth:`ImportRequest.describe` put it."""
    started_at: float
    attempt: int = 1
    error: str | None = None
    result: Election | None = None
    """The extracted election awaiting confirmation; never served as stored."""
    forecasts: tuple[Election, ...] = ()
    """The polls of an upcoming election, awaiting the user's choice; newest first."""


@dataclass(frozen=True)
class Claim:
    outcome: ClaimOutcome
    request_key: str
    election: Election | None = None
    election_hash: str | None = None
    job: Job | None = None


@dataclass(frozen=True)
class Confirmation:
    election_hash: str
    election: Election
    duplicate: bool
    """True when this request turned out to name an already-stored election."""


@dataclass(frozen=True)
class StoredElection:
    election_hash: str
    election: Election
    stored_at: float
    selected: bool = False
    """Curated for the front page: the only elections a visitor sees signed out."""


def select_by_place(
    stored: Iterable[StoredElection], year: int, nation: str, subnation: str | None = None
) -> StoredElection | None:
    """The stored election held in ``year`` in that place, if there is one.

    The bridge between the two keys: a request names a year and a place, an
    identity names a day, and this is how one finds the other without a model
    call. Names are compared as :func:`identity.place_token` reduces them, so
    the spelling a previous importer's page happened to use does not matter.

    Shared by every backend — a filter over ``list_elections`` rather than an
    index, because this store holds tens of elections, not millions, and one
    rule that cannot drift between three implementations is worth more here
    than a query per backend.

    Only results count. A stored forecast is one poll of an election that has
    not been held: it must neither stop somebody reading newer polls, nor stand
    in for the result once there is one.
    """
    for candidate in stored:
        election = candidate.election
        if (
            election.forecast is None
            and election.election_date.year == year
            and same_place(election.nation, nation)
            and same_place(election.state, subnation)
        ):
            return candidate
    return None


class ElectionStore(Protocol):
    """Storage seam. Implementations must make :meth:`claim` atomic."""

    def get_election(self, election_hash: str) -> Election | None: ...

    def get_stored(self, election_hash: str) -> StoredElection | None:
        """The election plus its curation flag, for callers that must check it."""
        ...

    def list_elections(self, *, selected_only: bool = False) -> list[StoredElection]: ...

    def find_by_place(
        self, year: int, nation: str, subnation: str | None = None
    ) -> StoredElection | None:
        """The election already stored for that year and place, if any.

        What makes a repeated request free: asked for an election somebody has
        already imported, the server answers from storage without resolving,
        searching, fetching or extracting anything.
        """
        ...

    def set_selected(self, election_hash: str, selected: bool) -> bool:
        """Mark an election visible to signed-out visitors. False if unknown."""
        ...

    def get_job(self, request_key: str) -> Job | None: ...

    def resolve_request(self, request_key: str) -> str | None:
        """The election hash this request produced, if it has produced one."""
        ...

    def claim(self, request_key: str, request: ImportRequest) -> Claim: ...

    def stage(self, request_key: str, election: Election) -> None:
        """Record an extracted election as awaiting the user's confirmation."""
        ...

    def confirm(self, request_key: str, election_hash: str) -> Confirmation | None:
        """Store a staged election under its identity. Idempotent."""
        ...

    def offer(self, request_key: str, forecasts: list[Election]) -> None:
        """Record the polls of an upcoming election as awaiting the user's choice."""
        ...

    def confirm_forecast(
        self, request_key: str, forecast: Election, election_hash: str
    ) -> Confirmation | None:
        """Store one offered forecast under its identity. ``None`` unless it is on offer.

        Passed the forecast itself rather than its position, so a list offered
        afresh between reading the job and confirming cannot file one poll
        under another poll's identity.

        The job keeps offering the rest, and the request is not linked to what
        was saved: a forecast is not *the* answer to a year and a place, and
        asking again once the lease is up should read the newest polls.
        """
        ...

    def link(self, request_key: str, election_hash: str) -> None:
        """Record that this request names an already-stored election."""
        ...

    def discard(self, request_key: str) -> bool:
        """Throw away a staged election or offered forecasts, freeing the request."""
        ...

    def fail(self, request_key: str, error: str) -> None: ...


def _decide(
    election: Election | None,
    job: Job | None,
    now: float,
    stale_after: float,
) -> ClaimOutcome:
    """The claim rule, shared by every implementation so they cannot drift.

    A request already resolved to a stored election always wins. A fresh job —
    running, or finished and waiting for confirmation — is joined. Anything
    else (no job, a failed job, or a job past its lease) is claimable, which is
    what keeps failures and abandoned previews from poisoning the request.
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
        self._resolved: dict[str, str] = {}  # request key -> election hash
        self._lock = Lock()
        self._stale_after = stale_after
        self._clock = clock

    def get_election(self, election_hash: str) -> Election | None:
        with self._lock:
            stored = self._elections.get(election_hash)
            return stored.election if stored else None

    def get_stored(self, election_hash: str) -> StoredElection | None:
        with self._lock:
            return self._elections.get(election_hash)

    def list_elections(self, *, selected_only: bool = False) -> list[StoredElection]:
        with self._lock:
            stored = [s for s in self._elections.values() if s.selected or not selected_only]
            return sorted(stored, key=lambda s: s.stored_at)

    def find_by_place(
        self, year: int, nation: str, subnation: str | None = None
    ) -> StoredElection | None:
        return select_by_place(self.list_elections(), year, nation, subnation)

    def set_selected(self, election_hash: str, selected: bool) -> bool:
        with self._lock:
            stored = self._elections.get(election_hash)
            if stored is None:
                return False
            self._elections[election_hash] = replace(stored, selected=selected)
            return True

    def get_job(self, request_key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(request_key)

    def resolve_request(self, request_key: str) -> str | None:
        with self._lock:
            return self._resolved.get(request_key)

    def claim(self, request_key: str, request: ImportRequest) -> Claim:
        with self._lock:
            election_hash = self._resolved.get(request_key)
            stored = self._elections.get(election_hash) if election_hash else None
            job = self._jobs.get(request_key)
            outcome = _decide(
                stored.election if stored else None, job, self._clock(), self._stale_after
            )
            if outcome is ClaimOutcome.STORED:
                return Claim(outcome, request_key, election=stored.election,
                             election_hash=election_hash, job=job)
            if outcome is ClaimOutcome.ATTACHED:
                return Claim(outcome, request_key, job=job)
            new_job = Job(
                request_key=request_key,
                status=JobStatus.PENDING,
                query=request.describe(),
                started_at=self._clock(),
                attempt=(job.attempt + 1) if job else 1,
            )
            self._jobs[request_key] = new_job
            return Claim(outcome, request_key, job=new_job)

    def stage(self, request_key: str, election: Election) -> None:
        with self._lock:
            job = self._jobs.get(request_key)
            self._jobs[request_key] = Job(
                request_key=request_key,
                status=JobStatus.AWAITING_CONFIRMATION,
                query=job.query if job else "",
                # Restart the lease so the user gets a full window to confirm.
                started_at=self._clock(),
                attempt=job.attempt if job else 1,
                result=election,
            )

    def confirm(self, request_key: str, election_hash: str) -> Confirmation | None:
        with self._lock:
            job = self._jobs.get(request_key)
            already = self._elections.get(election_hash)
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION or job.result is None:
                # Confirming twice is harmless as long as the request resolved here.
                if already is not None and self._resolved.get(request_key) == election_hash:
                    return Confirmation(election_hash, already.election, duplicate=False)
                return None

            duplicate = already is not None
            if not duplicate:
                self._elections[election_hash] = StoredElection(
                    election_hash=election_hash, election=job.result, stored_at=self._clock()
                )
            self._resolved[request_key] = election_hash
            self._jobs[request_key] = Job(
                request_key=request_key,
                status=JobStatus.SUCCEEDED,
                query=job.query,
                started_at=job.started_at,
                attempt=job.attempt,
            )
            stored = self._elections[election_hash]
            return Confirmation(election_hash, stored.election, duplicate=duplicate)

    def offer(self, request_key: str, forecasts: list[Election]) -> None:
        with self._lock:
            job = self._jobs.get(request_key)
            self._jobs[request_key] = Job(
                request_key=request_key,
                status=JobStatus.AWAITING_CHOICE,
                query=job.query if job else "",
                # Restart the lease so the user gets a full window to choose.
                started_at=self._clock(),
                attempt=job.attempt if job else 1,
                forecasts=tuple(forecasts),
            )

    def confirm_forecast(
        self, request_key: str, forecast: Election, election_hash: str
    ) -> Confirmation | None:
        with self._lock:
            job = self._jobs.get(request_key)
            if job is None or job.status is not JobStatus.AWAITING_CHOICE \
                    or forecast not in job.forecasts:
                return None
            already = self._elections.get(election_hash)
            if already is None:
                self._elections[election_hash] = StoredElection(
                    election_hash=election_hash, election=forecast, stored_at=self._clock()
                )
            stored = self._elections[election_hash]
            return Confirmation(election_hash, stored.election, duplicate=already is not None)

    def link(self, request_key: str, election_hash: str) -> None:
        with self._lock:
            if election_hash not in self._elections:
                return
            self._resolved[request_key] = election_hash
            job = self._jobs.get(request_key)
            self._jobs[request_key] = Job(
                request_key=request_key,
                status=JobStatus.SUCCEEDED,
                query=job.query if job else "",
                started_at=job.started_at if job else self._clock(),
                attempt=job.attempt if job else 1,
            )

    def discard(self, request_key: str) -> bool:
        with self._lock:
            job = self._jobs.get(request_key)
            if job is None or job.status not in DISCARDABLE_STATUSES:
                return False
            del self._jobs[request_key]
            return True

    def fail(self, request_key: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(request_key)
            self._jobs[request_key] = Job(
                request_key=request_key,
                status=JobStatus.FAILED,
                query=job.query if job else "",
                started_at=job.started_at if job else self._clock(),
                attempt=job.attempt if job else 1,
                error=error,
            )
