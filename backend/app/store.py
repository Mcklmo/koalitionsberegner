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
from datetime import UTC, date, datetime
from enum import Enum
from threading import Lock
from typing import Any, Protocol

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


#: How an election got into the store. ``manual`` is a person confirming an
#: import; ``auto`` is the scheduled refresh (plan 3, A4) storing one nobody
#: looked at, which is why the page footnotes it and offers a way to report it.
PROVENANCES = ("manual", "auto")
DEFAULT_PROVENANCE = "manual"


@dataclass(frozen=True)
class StoredElection:
    election_hash: str
    election: Election
    stored_at: float
    selected: bool = False
    """Curated for the front page: the only elections a visitor sees signed out."""
    provenance: str = DEFAULT_PROVENANCE
    """``manual`` (somebody confirmed it) or ``auto`` (the refresh stored it)."""


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

    def replace_election(self, election_hash: str, election: Election) -> bool:
        """Put a corrected copy in a stored election's place. False if unknown.

        Keeps when it was stored and whether it is curated. The caller vouches
        that ``election`` is the same election with better data — the store
        files it under the hash it is handed, as it does on confirmation.
        """
        ...

    def put_election(
        self, election_hash: str, election: Election, *, provenance: str = "auto"
    ) -> bool:
        """Store an election outright, with no job and nobody confirming it.

        What the scheduled refresh writes with (plan 3, A4): the staging table
        is a conversation with a person, and the refresh is not having one. New
        returns ``True``; an election already under that hash is left alone and
        the answer is ``False``, which is what makes a re-read idempotent.
        """
        ...

    def get_job(self, request_key: str) -> Job | None: ...

    def resolve_request(self, request_key: str) -> str | None:
        """The election hash this request produced, if it has produced one."""
        ...

    def claim(self, request_key: str, request: ImportRequest) -> Claim: ...

    def peek(self, request_key: str) -> Claim:
        """What :meth:`claim` would decide right now, without claiming anything.

        On ``STARTED`` the ``job`` is whatever is already there — failed,
        abandoned, past its lease — rather than a new one. A read, not a
        reservation: somebody may claim before the caller acts on it, so it can
        spare a caller work but never stand in for the claim itself.
        """
        ...

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

    def replace_election(self, election_hash: str, election: Election) -> bool:
        with self._lock:
            stored = self._elections.get(election_hash)
            if stored is None:
                return False
            self._elections[election_hash] = replace(stored, election=election)
            return True

    def put_election(
        self, election_hash: str, election: Election, *, provenance: str = "auto"
    ) -> bool:
        with self._lock:
            if election_hash in self._elections:
                return False
            self._elections[election_hash] = StoredElection(
                election_hash=election_hash,
                election=election,
                stored_at=self._clock(),
                provenance=provenance,
            )
            return True

    def get_job(self, request_key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(request_key)

    def resolve_request(self, request_key: str) -> str | None:
        with self._lock:
            return self._resolved.get(request_key)

    def _peek_locked(self, request_key: str) -> Claim:
        election_hash = self._resolved.get(request_key)
        stored = self._elections.get(election_hash) if election_hash else None
        job = self._jobs.get(request_key)
        outcome = _decide(
            stored.election if stored else None, job, self._clock(), self._stale_after
        )
        if outcome is ClaimOutcome.STORED:
            return Claim(outcome, request_key, election=stored.election,
                         election_hash=election_hash, job=job)
        return Claim(outcome, request_key, job=job)

    def peek(self, request_key: str) -> Claim:
        with self._lock:
            return self._peek_locked(request_key)

    def claim(self, request_key: str, request: ImportRequest) -> Claim:
        with self._lock:
            peeked = self._peek_locked(request_key)
            if peeked.outcome is not ClaimOutcome.STARTED:
                return peeked
            job = peeked.job
            new_job = Job(
                request_key=request_key,
                status=JobStatus.PENDING,
                query=request.describe(),
                started_at=self._clock(),
                attempt=(job.attempt + 1) if job else 1,
            )
            self._jobs[request_key] = new_job
            return Claim(ClaimOutcome.STARTED, request_key, job=new_job)

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


# --- tracked elections (plan 3, A1) -------------------------------------------
# The other half of the store: not an election somebody imported, but one the
# app has undertaken to watch. A row per request key, carrying everything the
# scheduled refresh needs to decide whether to read it again and what to do with
# what it reads. A protocol of its own rather than more methods on
# ElectionStore: a deployment that runs no cron never touches it.


class TrackedStatus(str, Enum):
    UPCOMING = "upcoming"      # not held yet; polls are what there is to read
    COUNTING = "counting"      # held; a result is stored but not yet stable
    FINAL = "final"            # stable; nothing is fetched for it again
    PARKED = "parked"          # given up on after too many failures
    UNTRACKED = "untracked"    # the owner said stop


#: The statuses a tick may pick up. ``final``, ``parked`` and ``untracked`` all
#: mean "leave it alone": the first because it is done, the other two because a
#: person has to look before anything reads it again.
ACTIVE_STATUSES = (TrackedStatus.UPCOMING, TrackedStatus.COUNTING)

#: Who put the row there. Both are the owner's doing; the distinction is whether
#: they typed it (``owner``) or let a calendar scan propose it (``calendar``).
ADDED_BY = ("owner", "calendar")


@dataclass(frozen=True)
class TrackedElection:
    """One election the app re-reads on a schedule. Keyed by request, not identity.

    By request key, because tracking starts before the election is held and an
    election's identity is its *day*, which a calendar only guesses at. The
    resolver's answer is kept in :attr:`resolved`, so every refresh after the
    first is a fetch and one extraction with no resolution to pay for (plan 3,
    A6.2).
    """

    request_key: str
    year: int
    nation: str
    subnation: str | None = None
    election_date: date | None = None
    resolved: dict[str, Any] | None = None
    """The ``ResolvedElection`` from the first run, as JSON. Never re-derived."""
    status: TrackedStatus = TrackedStatus.UPCOMING
    last_refresh_at: datetime | None = None
    next_refresh_at: datetime | None = None
    """When this row is due. ``None`` means due now — or, once final, never again."""
    consecutive_failures: int = 0
    last_error: str | None = None
    result_hash: str | None = None
    """The identity hash of the stored election, once a result has been stored."""
    result_digest: str | None = None
    """SHA-256 of the last result read, to tell a changed count from a repeat."""
    first_result_at: datetime | None = None
    """When a result was first stored (or adopted) for this row. Set once, never
    moved: what a minimum stability duration is measured from (plan 3 review,
    finding 5), so two quiet ticks around one real read cannot finalise a
    partial count within the hour."""
    unchanged_reads: int = 0
    source_digest: str | None = None
    """SHA-256 of the condensed source text last read, so a page that has not
    changed costs no model call at all (plan 3, A6.1)."""
    lease_until: datetime | None = None
    added_by: str = "owner"

    def describe(self) -> str:
        where = f"{self.nation}/{self.subnation}" if self.subnation else self.nation
        return f"{self.year} {where}"


#: Every field of :class:`TrackedElection` a caller may write with
#: ``update_tracked``. A name outside this set is a programming error, not a
#: silent no-op — which is what an implementation writing straight through
#: would make of a typo.
TRACKED_FIELDS = frozenset(
    {
        "election_date", "resolved", "status", "last_refresh_at", "next_refresh_at",
        "consecutive_failures", "last_error", "result_hash", "result_digest",
        "first_result_at", "unchanged_reads", "source_digest", "lease_until",
    }
)


#: The fields holding an instant, which must always be told in UTC.
TRACKED_TIMES = ("last_refresh_at", "next_refresh_at", "lease_until", "first_result_at")


def check_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """The patch a store is about to apply, or a :class:`ValueError` saying why not.

    Here rather than in each backend, so the in-memory store — which could
    happily hold anything — refuses exactly what the databases would: an
    unknown field name, and a naive datetime, which two backends would read
    back as two different instants.
    """
    unknown = sorted(set(fields) - TRACKED_FIELDS)
    if unknown:
        raise ValueError(f"not a tracked election field: {', '.join(unknown)}")
    for name in TRACKED_TIMES:
        value = fields.get(name)
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError(f"{name} must carry a time zone, got {value!r}")
    return fields


def to_epoch(value: datetime | None) -> float | None:
    """A UTC instant as seconds since the epoch, which is how both databases hold it.

    Seconds rather than an ISO string, because SQLite and Firestore both sort
    and compare numbers without caring how the string was spelled, and a naive
    datetime slipping in is caught here rather than compared wrongly later.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("a tracked election's timestamps must carry a time zone")
    return value.timestamp()


def from_epoch(value: float | int | None) -> datetime | None:
    """The inverse of :func:`to_epoch`, always in UTC."""
    return None if value is None else datetime.fromtimestamp(float(value), UTC)


def due_order(tracked: TrackedElection) -> tuple[int, float]:
    """Sort key putting the longest-overdue row first, a never-scheduled one before all."""
    if tracked.next_refresh_at is None:
        return (0, 0.0)
    return (1, tracked.next_refresh_at.timestamp())


def is_available(tracked: TrackedElection, now: datetime) -> bool:
    """Whether a tick may pick this row up: active, due, and not already leased.

    One rule, shared by every implementation so that three backends cannot
    disagree about which elections a tick sees — the same reason
    :func:`select_by_place` is a filter rather than a query per backend.
    """
    return (
        tracked.status in ACTIVE_STATUSES
        and (tracked.next_refresh_at is None or now >= tracked.next_refresh_at)
        and (tracked.lease_until is None or tracked.lease_until <= now)
    )


class TrackedStore(Protocol):
    """Storage seam for tracked elections. :meth:`lease` must be atomic."""

    def get_tracked(self, request_key: str) -> TrackedElection | None: ...

    def list_tracked(self) -> list[TrackedElection]:
        """Every row, soonest due first. The admin table and the daily report."""
        ...

    def add_tracked(self, tracked: TrackedElection) -> bool:
        """Write a new row. ``False`` when that request key is already tracked."""
        ...

    def update_tracked(self, request_key: str, **fields) -> TrackedElection | None:
        """Change some fields of one row; ``None`` when there is no such row."""
        ...

    def due_tracked(self, now: datetime, limit: int) -> list[TrackedElection]:
        """At most ``limit`` active rows due at ``now``, longest overdue first.

        A row whose lease has not expired is left out: something is already
        reading it.
        """
        ...

    def lease(self, request_key: str, until: datetime) -> TrackedElection | None:
        """Take the single-flight lease, or ``None`` if somebody else holds it.

        The one indivisible operation here. Two containers whose crons fire in
        the same minute must not both read the same page: exactly one of them
        gets a row back (plan 3, A4.1).
        """
        ...

    def release(self, request_key: str) -> None:
        """Give the lease back, whether the run succeeded or not."""
        ...


class InMemoryTrackedStore:
    """Process-local tracked elections, for tests and for a run without a database."""

    def __init__(self, *, clock=lambda: datetime.now(UTC)):
        self._rows: dict[str, TrackedElection] = {}
        self._lock = Lock()
        self._clock = clock

    def get_tracked(self, request_key: str) -> TrackedElection | None:
        with self._lock:
            return self._rows.get(request_key)

    def list_tracked(self) -> list[TrackedElection]:
        with self._lock:
            return sorted(self._rows.values(), key=due_order)

    def add_tracked(self, tracked: TrackedElection) -> bool:
        with self._lock:
            if tracked.request_key in self._rows:
                return False
            self._rows[tracked.request_key] = tracked
            return True

    def update_tracked(self, request_key: str, **fields) -> TrackedElection | None:
        check_fields(fields)
        with self._lock:
            row = self._rows.get(request_key)
            if row is None:
                return None
            self._rows[request_key] = replace(row, **fields)
            return self._rows[request_key]

    def due_tracked(self, now: datetime, limit: int) -> list[TrackedElection]:
        with self._lock:
            rows = sorted(self._rows.values(), key=due_order)
        return [row for row in rows if is_available(row, now)][:limit]

    def lease(self, request_key: str, until: datetime) -> TrackedElection | None:
        with self._lock:
            row = self._rows.get(request_key)
            now = self._clock()
            if row is None or (row.lease_until is not None and row.lease_until > now):
                return None
            self._rows[request_key] = replace(row, lease_until=until)
            return self._rows[request_key]

    def release(self, request_key: str) -> None:
        with self._lock:
            row = self._rows.get(request_key)
            if row is not None:
                self._rows[request_key] = replace(row, lease_until=None)
