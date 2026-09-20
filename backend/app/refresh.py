"""What one scheduled refresh does to one tracked election (plan 3, A4).

:class:`RefreshService` is the whole of the tick: given a
:class:`~app.store.TrackedElection`, it leases it, resolves it once, reads
polls or a result depending on how close election day is, and writes back what
it learned. Everything it decides is checked in code before it is trusted —
the parser's job is to read a page, not to decide that what it read is the
election that was asked for.

Kept the bill small (plan 3, A6): the resolver runs once per tracked election,
never again (:meth:`RefreshService._resolved_for`); the page is hashed before
it is handed to a model, and an unchanged page costs nothing more than the
fetch (:meth:`RefreshService._refresh_polls`, :meth:`RefreshService
._refresh_results`, via :meth:`app.parser.LlmElectionParser.peek_source`); the
model itself can be a cheaper one (``REFRESH_MODEL``, wired in
:func:`app.config.get_refresh_parser`, not this module's concern); and one
tick reads at most ``REFRESH_MAX_PER_TICK`` rows, oldest due first (the route
in ``main.py`` picks those, not this module).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from .identity import election_hash, normalize_date
from .observability import io_span
from .parser import ElectionParser, is_wanted
from .refresh_config import RefreshConfig, election_day_start, interval_for
from .refresh_config import next_refresh_at as due_after
from .resolver import ResolvedElection
from .schema import Election
from .store import ElectionStore, ImportRequest, TrackedElection, TrackedStatus, TrackedStore
from .wikipedia import article_of

log = logging.getLogger(__name__)

#: How long one refresh may hold a tracked election's lease. Generous next to
#: what a fetch and one model call actually take, so a slow model call is not
#: mistaken for a dead worker and picked up twice.
LEASE_SECONDS = 10 * 60

#: A gate's message is shown nowhere a visitor reads it — only in the daily
#: report and the admin table — but it still goes through the same clip as any
#: other stored text, because a result's own field values (a party name, a
#: title) reach it once quoted.
MAX_ERROR_CHARS = 300


class GateFailed(Exception):
    """A read succeeded, but what it read is not trusted as this election's result."""


@dataclass(frozen=True)
class Outcome:
    """What happened to one tracked election on one tick. What the route counts."""

    request_key: str
    status: TrackedStatus
    stored_polls: int = 0
    stored_results: int = 0
    finalised: bool = False
    failed: bool = False
    skipped_unchanged: bool = False
    parked: bool = False
    error: str | None = None
    leased: bool = True
    """False when another runner already held the lease — not a failure, a no-op."""


def result_digest(election: Election) -> str:
    """SHA-256 of an election's canonical JSON, to tell a changed count from a repeat.

    Sorted keys, like :func:`identity.election_hash`'s payload: the same
    values must hash the same whichever order pydantic happened to dump them
    in, or "unchanged" would mean nothing.
    """
    payload = json.dumps(
        election.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_digest(text: str) -> str:
    """SHA-256 of a fetched page's text, condensed or not (plan 3, A6.1)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _result_hash(election: Election) -> str:
    forecast = election.forecast
    return election_hash(
        election.nation, election.state, election.election_date,
        (forecast.publisher, forecast.published_on) if forecast else None,
    )


class RefreshService:
    """Runs one tracked election through the schedule. Stateless beyond its clock."""

    def __init__(
        self,
        tracked_store: TrackedStore,
        election_store: ElectionStore,
        parser: ElectionParser,
        config: RefreshConfig,
        clock=lambda: datetime.now(UTC),
    ):
        self._tracked = tracked_store
        self._elections = election_store
        self._parser = parser
        self._config = config
        self._clock = clock

    async def run(self, tracked: TrackedElection) -> Outcome:
        """Refresh one row. Never raises: every failure is reported in the ``Outcome``."""
        now = self._clock()
        leased = self._tracked.lease(tracked.request_key, until=now + timedelta(seconds=LEASE_SECONDS))
        if leased is None:
            return Outcome(tracked.request_key, tracked.status, leased=False)
        try:
            with io_span(log, "import", "refresh", request=tracked.request_key[:12]) as span:
                outcome = await self._run_leased(leased, now)
                span["status"] = outcome.status.value
                span["stored_polls"] = outcome.stored_polls
                span["stored_results"] = outcome.stored_results
            return outcome
        finally:
            self._tracked.release(tracked.request_key)

    async def _run_leased(self, tracked: TrackedElection, now: datetime) -> Outcome:
        request = ImportRequest(tracked.year, tracked.nation, tracked.subnation)
        try:
            tracked, resolved = await self._resolved_for(tracked, request)
        except Exception as exc:  # noqa: BLE001 - reported as a failed run, not raised
            return self._fail(tracked, now, exc)

        election_date = normalize_date(resolved.election_date)
        want: Literal["polls", "results"] = "results" if now >= election_day_start(election_date) else "polls"
        try:
            if want == "polls":
                return await self._refresh_polls(tracked, resolved, request, now)
            return await self._refresh_results(tracked, resolved, request, now)
        except Exception as exc:  # noqa: BLE001 - one election's failure is not the tick's
            return self._fail(tracked, now, exc)

    async def _resolved_for(
        self, tracked: TrackedElection, request: ImportRequest
    ) -> tuple[TrackedElection, ResolvedElection]:
        """The resolution, from the row if it has one, from the resolver if not.

        Once per tracked election, ever (plan 3, A4.2, A6.2): every later tick
        reuses what the first one found, so a refresh is a fetch and at most one
        model call, never a second trip through the resolver.
        """
        if tracked.resolved is not None:
            return tracked, ResolvedElection.model_validate(tracked.resolved)
        resolved = await self._parser._resolve(request)  # noqa: SLF001 - the one caller allowed to
        updated = self._tracked.update_tracked(
            tracked.request_key,
            resolved=resolved.model_dump(mode="json"),
            election_date=normalize_date(resolved.election_date),
        )
        return updated or tracked, resolved

    # --- upcoming: polls ------------------------------------------------------

    async def _refresh_polls(
        self, tracked: TrackedElection, resolved: ResolvedElection, request: ImportRequest, now: datetime
    ) -> Outcome:
        page = await self._parser.peek_source(resolved, want="polls")
        digest = source_digest(page.text) if page is not None else None
        if digest is not None and digest == tracked.source_digest:
            return self._reschedule(tracked, now, skipped_unchanged=True)

        forecasts = await self._parser.parse_resolved(resolved, request, want="polls")
        stored = 0
        for forecast in forecasts[: self._config.keep_newest]:
            if self._elections.put_election(_result_hash(forecast), forecast, provenance="auto"):
                stored += 1
        return self._reschedule(
            tracked, now, stored_polls=stored, extra_fields={"source_digest": digest}
        )

    # --- held: results ----------------------------------------------------------

    async def _refresh_results(
        self, tracked: TrackedElection, resolved: ResolvedElection, request: ImportRequest, now: datetime
    ) -> Outcome:
        page = await self._parser.peek_source(resolved, want="results")
        digest = source_digest(page.text) if page is not None else None
        # An unchanged page only counts towards stability once a result exists to
        # be stable: the first read of a page that has not moved since the last
        # poll was fetched must still go on to extract, or nothing is ever stored.
        if digest is not None and digest == tracked.source_digest and tracked.result_hash is not None:
            return self._settle(tracked, now, tracked.unchanged_reads + 1, skipped_unchanged=True)

        election = await self._parser.parse_resolved(resolved, request, want="results")
        _check_gates(election, resolved, request)
        digest_of_result = result_digest(election)
        extra: dict = {"source_digest": digest}

        if tracked.result_hash is None:
            new_hash = _result_hash(election)
            self._elections.put_election(new_hash, election, provenance="auto")
            extra["result_hash"] = new_hash
            return self._settle(
                tracked, now, 1, result_digest_value=digest_of_result, stored_results=1, extra_fields=extra
            )

        if tracked.result_digest != digest_of_result:
            self._elections.replace_election(tracked.result_hash, election)
            return self._settle(
                tracked, now, 1, result_digest_value=digest_of_result, stored_results=1, extra_fields=extra
            )

        return self._settle(
            tracked, now, tracked.unchanged_reads + 1, result_digest_value=digest_of_result, extra_fields=extra
        )

    def _settle(
        self,
        tracked: TrackedElection,
        now: datetime,
        unchanged_reads: int,
        *,
        result_digest_value: str | None = None,
        stored_results: int = 0,
        skipped_unchanged: bool = False,
        extra_fields: dict | None = None,
    ) -> Outcome:
        """Store the new ``unchanged_reads``, finalising once it reaches ``stable_after``."""
        final = unchanged_reads >= self._config.stable_after
        fields: dict = {"unchanged_reads": unchanged_reads, **(extra_fields or {})}
        if result_digest_value is not None:
            fields["result_digest"] = result_digest_value
        if final:
            fields.update(
                status=TrackedStatus.FINAL, next_refresh_at=None, last_refresh_at=now,
                consecutive_failures=0, last_error=None,
            )
            self._tracked.update_tracked(tracked.request_key, **fields)
            return Outcome(
                tracked.request_key, TrackedStatus.FINAL, stored_results=stored_results,
                finalised=True, skipped_unchanged=skipped_unchanged,
            )
        return self._reschedule(
            tracked, now, stored_results=stored_results, skipped_unchanged=skipped_unchanged,
            status=TrackedStatus.COUNTING, extra_fields=fields,
        )

    def _reschedule(
        self,
        tracked: TrackedElection,
        now: datetime,
        *,
        stored_polls: int = 0,
        stored_results: int = 0,
        skipped_unchanged: bool = False,
        status: TrackedStatus = TrackedStatus.UPCOMING,
        extra_fields: dict | None = None,
    ) -> Outcome:
        """A run that neither failed nor finished: pick the next due time, or park.

        Every field a caller passes through ``extra_fields`` is one this run
        actually learned (a new digest, most often); ``consecutive_failures``
        and ``last_error`` are reset here regardless, because reaching this
        method at all means the run succeeded, whatever it found.

        ``interval_for`` returning ``None`` means the schedule has nothing left
        to say about this election — past its last after-election window
        without ever settling — which is a stuck row, not a done one, and needs
        the owner's eyes exactly as a parked failure does (plan 3, A2).
        """
        interval = self._interval(tracked, now)
        fields = {"consecutive_failures": 0, "last_error": None, **(extra_fields or {})}
        fields["last_refresh_at"] = now
        if interval is None:
            fields["status"] = TrackedStatus.PARKED
            fields["next_refresh_at"] = None
            fields["last_error"] = "past the refresh schedule's last window"
            self._tracked.update_tracked(tracked.request_key, **fields)
            return Outcome(
                tracked.request_key, TrackedStatus.PARKED, stored_polls=stored_polls,
                stored_results=stored_results, skipped_unchanged=skipped_unchanged, parked=True,
            )
        fields["status"] = status
        fields["next_refresh_at"] = due_after(self._config, now, interval, 0)
        self._tracked.update_tracked(tracked.request_key, **fields)
        return Outcome(
            tracked.request_key, status, stored_polls=stored_polls, stored_results=stored_results,
            skipped_unchanged=skipped_unchanged,
        )

    def _interval(self, tracked: TrackedElection, now: datetime) -> timedelta | None:
        """:func:`interval_for`, defensively: a row with no known date has nothing
        to schedule against and is treated the same as one past its last window."""
        if tracked.election_date is None:
            return None
        return interval_for(self._config, now, tracked.election_date)

    def _fail(self, tracked: TrackedElection, now: datetime, exc: Exception) -> Outcome:
        """Back off, and park once ``failures.park_after`` is reached (plan 3, A2, A4.5)."""
        from .service import _user_message

        message = str(exc) if isinstance(exc, GateFailed) else _user_message(exc)
        failures = tracked.consecutive_failures + 1
        log.warning("refresh %s failed (%s/%s): %s", tracked.request_key[:12], failures,
                    self._config.park_after, message)
        interval = self._interval(tracked, now)
        parked = failures >= self._config.park_after or interval is None
        # `refresh_config.next_refresh_at` is the one place the backoff formula
        # is written (plan 3, A2) — reused here rather than duplicated, so a
        # failed run and `is_due`'s idea of "on schedule" cannot drift apart.
        due = None if parked else due_after(self._config, now, interval, failures)
        status = TrackedStatus.PARKED if parked else tracked.status
        self._tracked.update_tracked(
            tracked.request_key,
            consecutive_failures=failures,
            last_error=message[:MAX_ERROR_CHARS],
            status=status,
            next_refresh_at=due,
            last_refresh_at=now,
        )
        return Outcome(
            tracked.request_key, status, failed=True, parked=parked,
            error=message[:MAX_ERROR_CHARS],
        )


def _check_gates(election: Election, resolved: ResolvedElection, request: ImportRequest) -> None:
    """Nobody confirms a refresh's result, so code has to (plan 3, A4, step 4)."""
    if not is_wanted(election, resolved, request):
        raise GateFailed("it reported a different election than the one being tracked")
    seat_sum = sum(party.seats for block in election.blocks for party in block.parties)
    if seat_sum != election.total_seats:
        raise GateFailed("its party seats do not sum to its own reported total")
    if resolved.assembly_seats is not None and election.total_seats != resolved.assembly_seats:
        raise GateFailed(
            f"it reported {election.total_seats} seats, but the assembly has "
            f"{resolved.assembly_seats}"
        )
    trusted = article_of(election.source_url) is not None or election.source_url in resolved.sources
    if not trusted:
        raise GateFailed("its source was neither Wikipedia nor one the resolver named")
    if sum(len(block.parties) for block in election.blocks) < 2:
        raise GateFailed("it named fewer than two parties")
