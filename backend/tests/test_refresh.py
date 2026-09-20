"""What one refresh tick does to one tracked election (plan 3, A4).

Offline throughout: :class:`FakeRefreshParser` stands in for
``LlmElectionParser``, answering ``peek_source`` and ``parse_resolved`` from
what each test stages, so nothing here reaches a network or a model. The
store side is the in-memory implementations :mod:`test_tracked_store.py`
already trusts for the contract; what is new here is what :class:`RefreshService`
does with what they hand back.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.extractor import (
    ExtractedBlock,
    ExtractedElection,
    ExtractedForecasts,
    ExtractedParty,
    ExtractedPoll,
    PollParty,
)
from app.fetcher import FetchedPage, FetchError
from app.parser import LlmElectionParser, ParseError
from app.refresh import GateFailed, RefreshService, _result_hash, result_digest, source_digest
from app.refresh_config import AfterRow, BeforeRow, RefreshConfig
from app.resolver import ResolvedElection, StubResolver
from app.store import (
    ImportRequest,
    InMemoryElectionStore,
    InMemoryTrackedStore,
    TrackedElection,
    TrackedStatus,
)
from tests.factories import make_election, make_forecast

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


D, H, M = timedelta(days=1), timedelta(hours=1), timedelta(minutes=1)

#: A schedule small enough to walk through by hand: every day before the
#: election, every 30 minutes for two days after, then once a day for 45 more.
CONFIG = RefreshConfig(
    before_election=(BeforeRow(more_than=timedelta(0), every=D),),
    after_election=(AfterRow(within=2 * D, every=30 * M), AfterRow(within=45 * D, every=D)),
    stable_after=3,
    min_finalize_after=timedelta(0),  # off, except in the tests that give it its own value
    keep_newest=3,
    backoff_factor=2,
    max_backoff=7 * D,
    park_after=8,
)

REQUEST_KEY = "k" * 64
ELECTION_DATE = date(2026, 11, 3)
BEFORE = datetime(2026, 11, 1, tzinfo=UTC)   # two days before election day
ON_DAY = datetime(2026, 11, 3, 6, tzinfo=UTC)  # election day, morning
AFTER = datetime(2026, 11, 3, 12, tzinfo=UTC)  # election day, midday: a second read


def resolved(**overrides) -> ResolvedElection:
    data = {
        "nation": "Danmark", "state": None, "election_date": "2026-11-03",
        "title": "Folketingsvalg 2026", "sources": ["https://www.dst.dk/valg"],
        "assembly_seats": 10,
    }
    data.update(overrides)
    return ResolvedElection.model_validate(data)


def row(**overrides) -> TrackedElection:
    data = {
        "request_key": REQUEST_KEY, "year": 2026, "nation": "Danmark",
        "election_date": ELECTION_DATE, "resolved": resolved().model_dump(mode="json"),
        "status": TrackedStatus.UPCOMING, "added_by": "owner",
    }
    data.update(overrides)
    return TrackedElection(**data)


class FakeRefreshParser:
    """Answers ``peek_source``/``parse_resolved``/``_resolve`` from a script.

    ``pages`` and ``outcomes`` are consumed one per call, in order; running out
    of either is a test bug, not something to default around.
    """

    def __init__(self, *, resolve_result: ResolvedElection | None = None, pages=(), outcomes=()):
        self._resolve_result = resolve_result
        self._pages = list(pages)
        self._outcomes = list(outcomes)
        self.resolve_calls = 0
        self.parse_calls: list[str] = []
        self.peek_calls: list[str] = []

    async def _resolve(self, request: ImportRequest) -> ResolvedElection:
        self.resolve_calls += 1
        if self._resolve_result is None:
            raise AssertionError("the resolver should not be called on a refresh")
        return self._resolve_result

    async def peek_source(self, resolved: ResolvedElection, *, want: str) -> FetchedPage | None:
        self.peek_calls.append(want)
        text = self._pages.pop(0)
        return None if text is None else FetchedPage(url="https://en.wikipedia.org/wiki/X", text=text)

    async def parse_resolved(self, resolved: ResolvedElection, request: ImportRequest, *, want: str):
        self.parse_calls.append(want)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def service(parser: FakeRefreshParser, tracked=None, elections=None, *, now: datetime, config=CONFIG):
    clock = [now]
    return (
        RefreshService(
            tracked or InMemoryTrackedStore(), elections or InMemoryElectionStore(),
            parser, config, clock=lambda: clock[0],
        ),
        clock,
    )


# --- polls: before election day ----------------------------------------------


async def test_the_poll_path_stores_new_forecasts_and_nothing_on_the_unchanged_second_run():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    forecasts = [
        make_forecast(publisher="A", published_on="2026-09-01", election_date="2026-11-03"),
        make_forecast(publisher="B", published_on="2026-09-02", election_date="2026-11-03"),
        make_forecast(publisher="C", published_on="2026-09-03", election_date="2026-11-03"),
    ]
    parser = FakeRefreshParser(pages=["<table>v1</table>", "<table>v1</table>"], outcomes=[forecasts])
    svc, clock = service(parser, tracked_store, elections, now=BEFORE)

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome.stored_polls == 3
    assert len(elections.list_elections()) == 3
    assert parser.parse_calls == ["polls"]

    # Second tick, same page: no model call, nothing new stored.
    clock[0] = BEFORE + D
    outcome2 = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome2.stored_polls == 0
    assert outcome2.skipped_unchanged is True
    assert parser.parse_calls == ["polls"]  # not called again
    assert len(elections.list_elections()) == 3


async def test_a_changed_polling_page_is_read_again_and_only_new_forecasts_are_stored():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    first = [make_forecast(publisher="A", published_on="2026-09-01", election_date="2026-11-03")]
    second = [
        make_forecast(publisher="A", published_on="2026-09-01", election_date="2026-11-03"),
        make_forecast(publisher="B", published_on="2026-09-08", election_date="2026-11-03"),
    ]
    parser = FakeRefreshParser(pages=["v1", "v2"], outcomes=[first, second])
    svc, clock = service(parser, tracked_store, elections, now=BEFORE)

    await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    clock[0] = BEFORE + D
    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome.stored_polls == 1  # "A" was already stored; only "B" is new
    assert len(elections.list_elections()) == 2


# --- results: on and after election day ---------------------------------------


async def test_the_results_path_stores_replaces_and_finalises_on_the_third_identical_read():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    first = make_election(election_date="2026-11-03", total_seats=10, majority_seats=6)
    changed = make_election(
        election_date="2026-11-03", total_seats=10, majority_seats=6,
        blocks=[
            {"name": "Left", "parties": [{"name": "Left Party", "abbr": "L", "seats": 7, "color": "#C0392B"}]},
            {"name": "Right", "parties": [{"name": "Right Party", "abbr": "R", "seats": 3, "color": "#2980B9"}]},
        ],
    )
    # Four distinct source pages, so every tick does a full parse and none hits
    # the source-page-skip path — that path must never count towards
    # finalising (plan 3 review, finding 5; see the dedicated test below).
    parser = FakeRefreshParser(
        pages=["r1", "r2", "r3", "r4"],
        outcomes=[first, changed, changed, changed],
    )
    svc, clock = service(parser, tracked_store, elections, now=ON_DAY)

    first_outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert first_outcome.stored_results == 1
    assert first_outcome.status is TrackedStatus.COUNTING
    stored_hash = tracked_store.get_tracked(REQUEST_KEY).result_hash
    assert elections.get_election(stored_hash).blocks[0].parties[0].seats == 6

    clock[0] += H
    changed_outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert changed_outcome.stored_results == 1
    assert changed_outcome.status is TrackedStatus.COUNTING
    assert elections.get_election(stored_hash).blocks[0].parties[0].seats == 7

    clock[0] += H
    identical_1 = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert identical_1.status is TrackedStatus.COUNTING
    assert identical_1.stored_results == 0

    clock[0] += H
    final_outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert final_outcome.finalised is True
    assert final_outcome.status is TrackedStatus.FINAL
    assert tracked_store.get_tracked(REQUEST_KEY).next_refresh_at is None


async def test_a_result_disagreeing_with_the_resolvers_seat_count_is_refused():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row(resolved=resolved(assembly_seats=200).model_dump(mode="json")))
    elections = InMemoryElectionStore()
    wrong = make_election(election_date="2026-11-03", total_seats=10, majority_seats=6)
    parser = FakeRefreshParser(pages=["r1"], outcomes=[wrong])
    svc, _ = service(parser, tracked_store, elections, now=ON_DAY)

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome.failed is True
    assert "200" in outcome.error
    assert elections.list_elections() == []
    assert tracked_store.get_tracked(REQUEST_KEY).consecutive_failures == 1


async def test_a_result_from_an_untrusted_source_is_refused():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    untrusted = make_election(
        election_date="2026-11-03", total_seats=10, majority_seats=6,
        source_url="https://example.com/results",
    )
    parser = FakeRefreshParser(pages=["r1"], outcomes=[untrusted])
    svc, _ = service(parser, tracked_store, elections, now=ON_DAY)

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome.failed is True
    assert "source" in outcome.error
    assert elections.list_elections() == []


# --- failures: backoff and parking --------------------------------------------


async def test_a_failed_run_backs_off_and_a_persistent_one_is_parked():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    failures = [RuntimeError("boom")] * 8
    parser = FakeRefreshParser(pages=[None] * 8, outcomes=failures)
    svc, clock = service(parser, tracked_store, elections, now=ON_DAY)

    for expected_failures in range(1, 8):
        outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
        assert outcome.failed is True
        current = tracked_store.get_tracked(REQUEST_KEY)
        assert current.consecutive_failures == expected_failures
        assert current.status is not TrackedStatus.PARKED
        assert current.next_refresh_at > clock[0]  # backed off into the future
        clock[0] = current.next_refresh_at  # jump straight to the next due time

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome.parked is True
    final = tracked_store.get_tracked(REQUEST_KEY)
    assert final.status is TrackedStatus.PARKED
    assert final.consecutive_failures == 8
    assert final.next_refresh_at is None


async def test_a_success_after_failures_resets_the_backoff():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    ok = make_election(election_date="2026-11-03", total_seats=10, majority_seats=6)
    parser = FakeRefreshParser(
        pages=[None, "r1"], outcomes=[RuntimeError("boom"), ok],
    )
    svc, clock = service(parser, tracked_store, elections, now=ON_DAY)

    await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert tracked_store.get_tracked(REQUEST_KEY).consecutive_failures == 1

    clock[0] += H
    await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    current = tracked_store.get_tracked(REQUEST_KEY)
    assert current.consecutive_failures == 0
    assert current.last_error is None


# --- resolving and leasing -----------------------------------------------------


async def test_the_resolver_runs_once_then_never_again():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row(resolved=None))  # a fresh calendar row: not yet resolved
    elections = InMemoryElectionStore()
    forecasts = [make_forecast(publisher="A", published_on="2026-09-01", election_date="2026-11-03")]
    parser = FakeRefreshParser(
        resolve_result=resolved(), pages=["v1", "v2"], outcomes=[forecasts, forecasts],
    )
    svc, clock = service(parser, tracked_store, elections, now=BEFORE)

    await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert parser.resolve_calls == 1
    assert tracked_store.get_tracked(REQUEST_KEY).resolved is not None

    clock[0] += D
    await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert parser.resolve_calls == 1  # not called again


async def test_a_leased_row_is_left_alone():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    tracked_store.lease(REQUEST_KEY, until=BEFORE + timedelta(hours=1))
    parser = FakeRefreshParser()
    svc, _ = service(parser, tracked_store, InMemoryElectionStore(), now=BEFORE)

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert outcome.leased is False
    assert parser.peek_calls == []


async def test_result_digest_and_source_digest_are_stable_and_distinct():
    election = make_election()
    assert result_digest(election) == result_digest(make_election())
    assert result_digest(election) != result_digest(make_election(total_seats=11, majority_seats=6,
        blocks=[
            {"name": "Left", "parties": [{"name": "Left Party", "abbr": "L", "seats": 7, "color": "#C0392B"}]},
            {"name": "Right", "parties": [{"name": "Right Party", "abbr": "R", "seats": 4, "color": "#2980B9"}]},
        ]))
    assert source_digest("a") != source_digest("b")


async def test_a_gate_failure_raises_gate_failed_directly():
    with pytest.raises(GateFailed):
        from app.refresh import _check_gates

        wrong_place = make_election(
            election_date="2026-11-03", total_seats=10, majority_seats=6, nation="Sverige",
        )
        _check_gates(wrong_place, resolved(), ImportRequest(2026, "Danmark"))


# --- finding 1: store calls must not block the event loop --------------------


class ThreadRecordingTrackedStore(InMemoryTrackedStore):
    """Notes which OS thread called each store method it wraps."""

    def __init__(self):
        super().__init__()
        self.threads: set[str] = set()

    def lease(self, request_key, until):
        self.threads.add(threading.current_thread().name)
        return super().lease(request_key, until)

    def release(self, request_key):
        self.threads.add(threading.current_thread().name)
        return super().release(request_key)

    def update_tracked(self, request_key, **fields):
        self.threads.add(threading.current_thread().name)
        return super().update_tracked(request_key, **fields)


class ThreadRecordingElectionStore(InMemoryElectionStore):
    """Notes which OS thread called each store method it wraps."""

    def __init__(self):
        super().__init__()
        self.threads: set[str] = set()

    def put_election(self, *args, **kwargs):
        self.threads.add(threading.current_thread().name)
        return super().put_election(*args, **kwargs)

    def replace_election(self, *args, **kwargs):
        self.threads.add(threading.current_thread().name)
        return super().replace_election(*args, **kwargs)


async def test_store_calls_are_made_off_the_event_loop():
    """plan 3 review, finding 1: with Firestore, every one of these is a
    blocking gRPC call; made straight from the event loop, one tick stalls
    every other request the container is serving. ``ImportService`` already
    keeps its store calls off the loop the same way (``_in_thread``)."""
    tracked_store = ThreadRecordingTrackedStore()
    tracked_store.add_tracked(row())
    elections = ThreadRecordingElectionStore()
    forecasts = [make_forecast(publisher="A", published_on="2026-09-01", election_date="2026-11-03")]
    parser = FakeRefreshParser(pages=["v1"], outcomes=[forecasts])
    svc, _ = service(parser, tracked_store, elections, now=BEFORE)
    here = threading.current_thread().name

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))

    assert outcome.stored_polls == 1
    assert tracked_store.threads, "lease/release/update_tracked were never called"
    assert here not in tracked_store.threads
    assert elections.threads, "put_election was never called"
    assert here not in elections.threads


# --- findings 2 and 3: peek and parse must agree on the page, whatever the
# frozen resolution's own ``upcoming`` flag says -------------------------------

POLL_URL = "https://en.wikipedia.org/wiki/Opinion_polling_for_the_2026_Danish_general_election"
RESULT_URL = "https://en.wikipedia.org/wiki/2026_Danish_general_election"


class FlagWiki:
    """Answers Wikipedia's lookup differently for polls and results, exactly
    as the real module does — by the ``upcoming`` flag on what it is asked,
    never by ``want`` or by the calendar."""

    async def find(self, resolved, *, limit=1):
        return [POLL_URL if getattr(resolved, "upcoming", False) else RESULT_URL]


class UrlFetcher:
    """A little web keyed by URL, whose pages a test can rewrite mid-run."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    async def fetch(self, url):
        if url not in self.pages:
            raise FetchError("the page returned HTTP 404")
        return FetchedPage(url=url, text=self.pages[url])


class UrlRoutedExtractor:
    """Answers by which URL was actually fetched, not by a marker in the
    text — the point of these tests is *which page* the pipeline read."""

    def __init__(self, *, results=None, forecasts=None):
        self._results = results
        self._forecasts = list(forecasts) if forecasts is not None else []

    async def extract(self, page, wanted):
        if page.url == RESULT_URL and self._results is not None:
            return self._results
        raise ParseError("it was not a set of election results")

    async def extract_forecasts(self, page, wanted):
        if page.url == POLL_URL and self._forecasts:
            return self._forecasts.pop(0)
        raise ParseError("no polls could be found on it")


def _result_extraction(**overrides) -> ExtractedElection:
    data = dict(
        nation="Danmark", state=None, election_date="2026-11-03", title="Election",
        total_seats=10, majority_seats=6,
        blocks=[
            ExtractedBlock(
                name="Left", parties=[ExtractedParty(name="Left Party", abbr="L", seats=6, color="#C0392B")]
            ),
            ExtractedBlock(
                name="Right", parties=[ExtractedParty(name="Right Party", abbr="R", seats=4, color="#2980B9")]
            ),
        ],
    )
    data.update(overrides)
    return ExtractedElection(**data)


def _poll(publisher: str, published_on: str) -> ExtractedPoll:
    return ExtractedPoll(
        publisher=publisher, published_on=published_on, unit="seats",
        parties=[
            PollParty(name="A", abbr="A", value=6, color="#111111"),
            PollParty(name="B", abbr="B", value=4, color="#222222"),
        ],
    )


async def test_a_stuck_upcoming_true_resolution_still_reads_the_results_article():
    """plan 3 review, finding 2: a tracked election's ``resolved`` is frozen
    from whichever tick first resolved it, and on election day that can still
    say ``upcoming=True`` — the calendar has moved on, the resolver's own
    answer has not. Left unmodified, the results branch would search
    Wikipedia for the *polling* article and this election would never
    produce a result."""
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(
        row(resolved=resolved(upcoming=True, sources=[]).model_dump(mode="json"))
    )
    elections = InMemoryElectionStore()
    fetcher = UrlFetcher({RESULT_URL: "results-v1", POLL_URL: "polls-v1"})
    extractor = UrlRoutedExtractor(results=_result_extraction())
    parser = LlmElectionParser(fetcher, extractor, StubResolver(resolved()), wikipedia=FlagWiki())
    svc, _ = service(parser, tracked_store, elections, now=ON_DAY)

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))

    assert outcome.failed is False, outcome.error
    assert outcome.stored_results == 1
    [stored] = elections.list_elections()
    assert stored.election.source_url == RESULT_URL


async def test_peek_source_and_parse_resolved_watch_the_same_polling_page():
    """plan 3 review, finding 3: with the stored resolution's own
    ``upcoming`` at ``False``, ``peek_source`` must still hash the polling
    article ``parse_resolved`` actually reads — otherwise the digest tracks
    an unrelated page, and a genuine change on the polling page is hidden
    behind that other page's unchanged digest."""
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(
        row(resolved=resolved(upcoming=False, sources=[]).model_dump(mode="json"))
    )
    elections = InMemoryElectionStore()
    fetcher = UrlFetcher({RESULT_URL: "results-v1", POLL_URL: "polls-v1"})
    forecasts_v1 = ExtractedForecasts(
        nation="Danmark", state=None, election_year=None, polls=[_poll("Voxmeter", "2026-09-01")]
    )
    forecasts_v2 = ExtractedForecasts(
        nation="Danmark", state=None, election_year=None,
        polls=[_poll("Voxmeter", "2026-09-01"), _poll("Voxmeter", "2026-09-08")],
    )
    extractor = UrlRoutedExtractor(forecasts=[forecasts_v1, forecasts_v2])
    parser = LlmElectionParser(fetcher, extractor, StubResolver(resolved()), wikipedia=FlagWiki())
    svc, clock = service(parser, tracked_store, elections, now=BEFORE)

    first = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert first.stored_polls == 1, first.error

    # The polling page gets a new poll; the results article — not yet
    # published, and (before the fix) what the digest was actually of — has
    # not changed at all.
    fetcher.pages[POLL_URL] = "polls-v2"
    clock[0] += D
    second = await svc.run(tracked_store.get_tracked(REQUEST_KEY))

    assert second.skipped_unchanged is False, "the polling page did change"
    assert second.stored_polls == 1, second.error


# --- finding 4: never overwrite a manually confirmed election ----------------


async def test_a_manually_confirmed_election_is_adopted_not_overwritten():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    manual = make_election(election_date="2026-11-03", total_seats=10, majority_seats=6)
    manual_hash = _result_hash(manual)
    elections.put_election(manual_hash, manual, provenance="manual")
    different = make_election(
        election_date="2026-11-03", total_seats=10, majority_seats=6,
        blocks=[
            {"name": "Left", "parties": [{"name": "Left Party", "abbr": "L", "seats": 7, "color": "#C0392B"}]},
            {"name": "Right", "parties": [{"name": "Right Party", "abbr": "R", "seats": 3, "color": "#2980B9"}]},
        ],
    )
    # The refresh reads the same election first — the owner got there first —
    # then, later, unreviewed figures for it.
    parser = FakeRefreshParser(pages=["r1", "r2"], outcomes=[manual, different])
    svc, clock = service(parser, tracked_store, elections, now=ON_DAY)

    first = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert first.stored_results == 0, "nothing was actually written"
    assert first.failed is False
    assert tracked_store.get_tracked(REQUEST_KEY).result_hash == manual_hash
    assert elections.get_stored(manual_hash).provenance == "manual"

    clock[0] += H
    second = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert second.stored_results == 0, "a manually confirmed election is never overwritten"
    assert elections.get_election(manual_hash).blocks[0].parties[0].seats == 6, "unchanged"


# --- finding 5: finalising needs time to pass, and a source-page skip is not
# an unchanged result ----------------------------------------------------------


async def test_a_source_page_skip_never_counts_toward_finalising():
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    result = make_election(election_date="2026-11-03", total_seats=10, majority_seats=6)
    # One real extraction, then two ticks whose source page never moved.
    parser = FakeRefreshParser(pages=["r1", "r1", "r1"], outcomes=[result])
    svc, clock = service(parser, tracked_store, elections, now=ON_DAY)

    first = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert first.stored_results == 1
    assert tracked_store.get_tracked(REQUEST_KEY).unchanged_reads == 1

    for _ in range(2):
        clock[0] += H
        outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
        assert outcome.skipped_unchanged is True
        assert outcome.finalised is False

    final_row = tracked_store.get_tracked(REQUEST_KEY)
    assert final_row.unchanged_reads == 1, "the source-page skip never advanced it"
    assert final_row.status is TrackedStatus.COUNTING


async def test_finalising_waits_for_min_finalize_after_even_once_stable():
    slow_config = replace(CONFIG, stable_after=2, min_finalize_after=3 * H)
    tracked_store = InMemoryTrackedStore()
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    result = make_election(election_date="2026-11-03", total_seats=10, majority_seats=6)
    # Three distinct pages, so every tick does a full parse of the same
    # (unchanged) result rather than hitting the source-page skip.
    parser = FakeRefreshParser(pages=["r1", "r2", "r3"], outcomes=[result, result, result])
    svc, clock = service(parser, tracked_store, elections, now=ON_DAY, config=slow_config)

    first = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert first.status is TrackedStatus.COUNTING

    clock[0] += 30 * M
    second = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    # unchanged_reads reaches stable_after (2) here, but only 30 minutes have
    # passed since the first result — nowhere near min_finalize_after (3h).
    assert second.finalised is False
    assert second.status is TrackedStatus.COUNTING

    clock[0] += 3 * H
    third = await svc.run(tracked_store.get_tracked(REQUEST_KEY))
    assert third.finalised is True
    assert third.status is TrackedStatus.FINAL


# --- finding 6: a store failure inside a run must not escape it --------------


class RaisingTrackedStore(InMemoryTrackedStore):
    """Behaves normally except for the one method named, which always raises."""

    def __init__(self, *, fail_on: str):
        super().__init__()
        self._fail_on = fail_on

    def lease(self, request_key, until):
        if self._fail_on == "lease":
            raise RuntimeError("boom: lease")
        return super().lease(request_key, until)

    def update_tracked(self, request_key, **fields):
        if self._fail_on == "update_tracked":
            raise RuntimeError("boom: update_tracked")
        return super().update_tracked(request_key, **fields)

    def release(self, request_key):
        if self._fail_on == "release":
            raise RuntimeError("boom: release")
        return super().release(request_key)


@pytest.mark.parametrize("fail_on", ["lease", "update_tracked", "release"])
async def test_run_never_raises_even_when_the_store_itself_fails(fail_on):
    tracked_store = RaisingTrackedStore(fail_on=fail_on)
    tracked_store.add_tracked(row())
    elections = InMemoryElectionStore()
    forecasts = [make_forecast(publisher="A", published_on="2026-09-01", election_date="2026-11-03")]
    parser = FakeRefreshParser(pages=["v1"], outcomes=[forecasts])
    svc, _ = service(parser, tracked_store, elections, now=BEFORE)

    outcome = await svc.run(tracked_store.get_tracked(REQUEST_KEY))  # must not raise

    if fail_on == "release":
        # Only the best-effort release failed; the refresh itself succeeded.
        assert outcome.failed is False
        assert outcome.stored_polls == 1
    else:
        assert outcome.failed is True
