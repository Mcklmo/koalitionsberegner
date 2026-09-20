"""What one refresh tick does to one tracked election (plan 3, A4).

Offline throughout: :class:`FakeRefreshParser` stands in for
``LlmElectionParser``, answering ``peek_source`` and ``parse_resolved`` from
what each test stages, so nothing here reaches a network or a model. The
store side is the in-memory implementations :mod:`test_tracked_store.py`
already trusts for the contract; what is new here is what :class:`RefreshService`
does with what they hand back.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.fetcher import FetchedPage
from app.refresh import GateFailed, RefreshService, result_digest, source_digest
from app.refresh_config import AfterRow, BeforeRow, RefreshConfig
from app.resolver import ResolvedElection
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
    parser = FakeRefreshParser(
        pages=["r1", "r2", "r3", "r3"],
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
