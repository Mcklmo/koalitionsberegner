"""Import orchestration: short-circuit on known requests, single-flight on the rest.

The store decides *what* happens (see :meth:`ElectionStore.claim`); this module
carries it out — running at most one import per request, letting every other
caller share it, and reconciling the identity that came back against what is
already stored before anything is saved.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import anyio.to_thread

from .identity import election_hash, request_key
from .observability import io_span, scrub
from .parser import ElectionParser
from .schema import Election
from .store import ClaimOutcome, ElectionStore, ImportRequest, JobStatus, StoredElection

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.05


class ImportState(str, Enum):
    READY = "ready"        # the election is stored and returned
    PENDING = "pending"    # an import is running; poll or wait
    PREVIEW = "preview"    # found and shown for confirmation; not saved yet
    CHOOSE = "choose"      # not held yet: its forecasts are offered; none saved yet
    FAILED = "failed"      # the last import failed; importing again retries
    UNKNOWN = "unknown"    # nothing stored and no job for this request


#: States a caller can stop polling on.
TERMINAL_STATES = (
    ImportState.READY, ImportState.PREVIEW, ImportState.CHOOSE,
    ImportState.FAILED, ImportState.UNKNOWN,
)


@dataclass(frozen=True)
class ImportResult:
    request_key: str
    state: ImportState
    election: Election | None = None
    """The stored election, or — in the PREVIEW state — the unsaved extraction."""
    election_hash: str | None = None
    """Known only once the results have been found and read."""
    error: str | None = None
    attempt: int | None = None
    reused: bool = False
    """True when the caller was served without triggering any import work."""
    duplicate: bool = False
    """True when this request turned out to name an already-stored election."""
    forecasts: tuple[Election, ...] = ()
    """In the CHOOSE state: the upcoming election's forecasts, newest first."""


def identity_of(election: Election) -> str:
    forecast = election.forecast
    return election_hash(
        election.nation, election.state, election.election_date,
        (forecast.publisher, forecast.published_on) if forecast else None,
    )


class ImportService:
    def __init__(self, store: ElectionStore, parser: ElectionParser):
        self._store = store
        self._parser = parser
        # Holding task references keeps the event loop from garbage-collecting
        # an import that no caller is awaiting.
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def request_key_for(request: ImportRequest) -> str:
        return request_key(request.year, request.nation, request.subnation)

    async def _in_thread(self, fn, *args):
        """Firestore's client is blocking; keep it off the event loop."""
        return await anyio.to_thread.run_sync(fn, *args)

    async def submit(
        self, request: ImportRequest, *, on_parse_failed: Callable[[], None] | None = None
    ) -> ImportResult:
        """Claim the request and import only if nobody else already has.

        ``on_parse_failed`` runs if this call's own import fails. It exists so a
        caller that paid for the attempt can be refunded: a request whose
        election could not be found produced nothing, and charging for it would
        make a misremembered year cost the same as a stored election.
        """
        key = self.request_key_for(request)
        claim = await self._in_thread(self._store.claim, key, request)
        # One line per state transition, so the decision is visible whichever
        # store backend is in use (Firestore logs its own wire-level spans).
        log.info(
            "claim request=%s outcome=%s query=%s",
            key[:12], claim.outcome.value, scrub(request.describe()),
        )

        if claim.outcome is ClaimOutcome.STORED:
            # This request has been made before: no search, no fetch, no model call.
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
            if job is not None and job.status is JobStatus.AWAITING_CHOICE:
                return ImportResult(
                    key, ImportState.CHOOSE, forecasts=job.forecasts,
                    attempt=job.attempt, reused=True,
                )
            return ImportResult(
                key, ImportState.PENDING, attempt=job.attempt if job else None, reused=True
            )

        task = asyncio.create_task(self._import(key, request, on_parse_failed))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        job = claim.job
        return ImportResult(key, ImportState.PENDING, attempt=job.attempt if job else 1)

    async def _import(
        self, key: str, request: ImportRequest, on_parse_failed: Callable[[], None] | None = None
    ) -> None:
        async def failed(error: str) -> None:
            # Refund first: a caller polling for the outcome must never see the
            # failure while still being charged for it.
            if on_parse_failed is not None:
                try:
                    await self._in_thread(on_parse_failed)
                except Exception:  # noqa: BLE001 - a refund must not mask the failure
                    log.exception("could not run the failure hook for %s", key[:12])
            await self._in_thread(self._store.fail, key, error)

        try:
            # One span covering the whole outward attempt: resolve, search,
            # fetch, extract.
            with io_span(
                log, "import", "run", request=key[:12], query=request.describe()
            ) as span:
                outcome = await self._parser.parse(request)
                if isinstance(outcome, list):
                    span["forecasts"] = len(outcome)
                else:
                    span["identity"] = identity_of(outcome)[:12]
                    span["total_seats"] = outcome.total_seats
        except Exception as exc:  # noqa: BLE001 - a failed import must not kill the worker
            await failed(str(exc))
            log.info("failed request=%s reason=%s", key[:12], scrub(exc))
            return

        if isinstance(outcome, list):
            await self._offer(key, outcome, failed)
            return
        election = outcome

        try:
            # This may be an election we already hold — another way of asking
            # for the same one, or a spelling the request key could not match.
            # Link the request to it instead of asking the user to confirm a
            # duplicate.
            existing_hash = identity_of(election)
            existing = await self._in_thread(self._store.get_election, existing_hash)
            if existing is not None:
                log.info(
                    "import matched a stored election request=%s hash=%s",
                    key[:12], existing_hash[:12],
                )
                await self._in_thread(self._store.link, key, existing_hash)
                return
            # Staged, not stored: the user still has to confirm the identity the
            # agent inferred, and the numbers it read.
            await self._in_thread(self._store.stage, key, election)
            log.info(
                "staged request=%s hash=%s awaiting confirmation", key[:12], existing_hash[:12]
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("staging %s failed", key)
            await failed(f"could not stage result: {exc}")

    async def _offer(self, key: str, forecasts: list[Election], failed) -> None:
        """Hold an upcoming election's forecasts for the user to choose from.

        Unlike a result, none is matched against the store first: whether a
        poll is already saved is answered when it is chosen, and a list with the
        saved ones left out would be a list that changes depending on who asked.
        """
        if not forecasts:
            await failed("no polls could be found for that election")
            return
        try:
            await self._in_thread(self._store.offer, key, forecasts)
            log.info(
                "offered request=%s forecasts=%s awaiting a choice", key[:12], len(forecasts)
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("offering %s failed", key)
            await failed(f"could not stage forecasts: {exc}")

    async def status(self, key: str) -> ImportResult:
        job = await self._in_thread(self._store.get_job, key)
        resolved = await self._in_thread(self._store.resolve_request, key)
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
        if job.status is JobStatus.AWAITING_CHOICE:
            return ImportResult(
                key, ImportState.CHOOSE, forecasts=job.forecasts, attempt=job.attempt
            )
        return ImportResult(key, ImportState.PENDING, attempt=job.attempt)

    async def wait_for(self, key: str, timeout: float) -> ImportResult:
        """Poll until the import reaches a terminal state or ``timeout`` elapses.

        Callers that attached to someone else's import get that import's result
        here — the same election, from the same single run.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = await self.status(key)
            if result.state in TERMINAL_STATES:
                return result
            if asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def confirm(self, key: str, option: int | None = None) -> ImportResult:
        """Save a previewed election under the identity that was read.

        Nothing reaches storage without this. For an upcoming election,
        ``option`` says which of the offered forecasts to save; without one the
        import is still waiting for a choice, and that is what comes back.
        Raises :class:`ValueError` for an option that was never offered.
        """
        job = await self._in_thread(self._store.get_job, key)
        if job is not None and job.status is JobStatus.AWAITING_CHOICE:
            return await self._confirm_forecast(key, job, option)
        if job is None or job.result is None:
            return await self.status(key)
        confirmation = await self._in_thread(self._store.confirm, key, identity_of(job.result))
        if confirmation is None:
            log.info("confirm request=%s rejected nothing-staged", key[:12])
            return await self.status(key)
        log.info(
            "confirm request=%s hash=%s %s",
            key[:12], confirmation.election_hash[:12],
            "duplicate" if confirmation.duplicate else "stored",
        )
        return ImportResult(
            key, ImportState.READY,
            election=confirmation.election,
            election_hash=confirmation.election_hash,
            duplicate=confirmation.duplicate,
        )

    async def _confirm_forecast(self, key: str, job, option: int | None) -> ImportResult:
        if option is None:
            return await self.status(key)
        if not 0 <= option < len(job.forecasts):
            raise ValueError(f"there is no forecast {option} to choose")
        chosen = job.forecasts[option]
        confirmation = await self._in_thread(
            self._store.confirm_forecast, key, chosen, identity_of(chosen)
        )
        if confirmation is None:
            log.info("confirm request=%s rejected forecast-no-longer-offered", key[:12])
            return await self.status(key)
        log.info(
            "confirm request=%s forecast=%s hash=%s %s",
            key[:12], option, confirmation.election_hash[:12],
            "duplicate" if confirmation.duplicate else "stored",
        )
        return ImportResult(
            key, ImportState.READY,
            election=confirmation.election,
            election_hash=confirmation.election_hash,
            duplicate=confirmation.duplicate,
        )

    async def discard(self, key: str) -> bool:
        """Reject a previewed election, freeing the request for another attempt."""
        discarded = await self._in_thread(self._store.discard, key)
        log.info("discard request=%s discarded=%s", key[:12], discarded)
        return discarded

    async def get_stored(self, election_hash: str) -> StoredElection | None:
        """The stored election and its curation flag; callers decide who may see it."""
        return await self._in_thread(self._store.get_stored, election_hash)

    async def find_by_place(
        self, year: int, nation: str, subnation: str | None = None
    ) -> StoredElection | None:
        """The election already stored for that year and place, if there is one."""
        return await self._in_thread(
            lambda: self._store.find_by_place(year, nation, subnation)
        )

    async def list_elections(self, *, selected_only: bool = False) -> list[StoredElection]:
        return await self._in_thread(
            lambda: self._store.list_elections(selected_only=selected_only)
        )

    async def set_selected(self, election_hash: str, selected: bool) -> bool:
        """Curate an election into, or out of, what signed-out visitors see."""
        changed = await self._in_thread(self._store.set_selected, election_hash, selected)
        log.info("selected hash=%s value=%s found=%s", election_hash[:12], selected, changed)
        return changed
