"""The tracked-election table: one contract, two backends (plan 3, A1).

Every test runs against both implementations that can be exercised offline,
because :mod:`app.refresh` above them assumes one contract and cannot tell
which store it was handed — the same reason ``test_store.py`` is parametrised.
What is checked here is the part the refresh depends on being right: which rows
a tick sees, in what order, and that exactly one caller can lease a row.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from app.sqlite_store import SqliteElectionStore, SqliteTrackedStore
from app.store import (
    InMemoryElectionStore,
    InMemoryTrackedStore,
    TrackedElection,
    TrackedStatus,
)
from tests.factories import make_election

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HASH = "e" * 64


@pytest.fixture(params=["memory", "sqlite"])
def tracked(request, tmp_path):
    if request.param == "memory":
        yield InMemoryTrackedStore()
        return
    store = SqliteTrackedStore(tmp_path / "elections.db")
    yield store
    store.close()


def row(key="k" * 64, **fields) -> TrackedElection:
    return TrackedElection(
        **{
            "request_key": key,
            "year": 2026,
            "nation": "Denmark",
            "election_date": date(2026, 6, 1),
            **fields,
        }
    )


def test_a_row_survives_the_round_trip(tracked):
    """Every field, including the ones only the refresh writes."""
    original = row(
        subnation="Bavaria",
        resolved={"nation": "Denmark", "sources": ["https://example.org/a"]},
        status=TrackedStatus.COUNTING,
        last_refresh_at=NOW,
        next_refresh_at=NOW + timedelta(hours=1),
        consecutive_failures=2,
        last_error="the page could not be read",
        result_hash=HASH,
        result_digest="d" * 64,
        unchanged_reads=3,
        source_digest="s" * 64,
        added_by="calendar",
    )
    assert tracked.add_tracked(original)
    assert tracked.get_tracked(original.request_key) == original


def test_a_second_row_for_the_same_request_is_refused(tracked):
    assert tracked.add_tracked(row())
    assert not tracked.add_tracked(row(nation="Sweden"))
    assert tracked.get_tracked("k" * 64).nation == "Denmark"


def test_unknown_rows_and_fields_are_errors_not_silent_no_ops(tracked):
    assert tracked.update_tracked("z" * 64, unchanged_reads=1) is None
    tracked.add_tracked(row())
    with pytest.raises(ValueError, match="nation"):
        tracked.update_tracked("k" * 64, nation="Sweden")


def test_a_tick_sees_the_longest_overdue_rows_first(tracked):
    tracked.add_tracked(row("a" * 64, next_refresh_at=NOW - timedelta(minutes=5)))
    tracked.add_tracked(row("b" * 64, next_refresh_at=NOW - timedelta(days=2)))
    # Never scheduled: added a moment ago by the owner or the calendar scan.
    tracked.add_tracked(row("c" * 64, next_refresh_at=None))
    tracked.add_tracked(row("d" * 64, next_refresh_at=NOW + timedelta(hours=1)))

    due = tracked.due_tracked(NOW, 10)
    assert [t.request_key[0] for t in due] == ["c", "b", "a"]
    assert [t.request_key[0] for t in tracked.due_tracked(NOW, 2)] == ["c", "b"]


@pytest.mark.parametrize(
    "status", [TrackedStatus.FINAL, TrackedStatus.PARKED, TrackedStatus.UNTRACKED]
)
def test_a_row_nobody_should_touch_is_never_due(tracked, status):
    tracked.add_tracked(row(status=status, next_refresh_at=None))
    assert tracked.due_tracked(NOW, 10) == []


def test_a_leased_row_is_left_to_whoever_holds_it(tracked):
    tracked.add_tracked(row(next_refresh_at=None))
    assert tracked.lease("k" * 64, NOW + timedelta(minutes=10)) is not None
    assert tracked.due_tracked(NOW, 10) == []
    # An expired lease is no lease: a container that died mid-run must not pin
    # an election out of the schedule for ever.
    assert tracked.due_tracked(NOW + timedelta(minutes=11), 10) != []


def test_exactly_one_of_two_racing_ticks_gets_the_lease(tracked):
    tracked.add_tracked(row())
    until = datetime.now(UTC) + timedelta(minutes=10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: tracked.lease("k" * 64, until), range(8)))
    assert sum(1 for result in results if result is not None) == 1

    tracked.release("k" * 64)
    assert tracked.lease("k" * 64, until) is not None


def test_listing_puts_the_soonest_due_first(tracked):
    tracked.add_tracked(row("a" * 64, next_refresh_at=NOW + timedelta(days=1)))
    tracked.add_tracked(row("b" * 64, status=TrackedStatus.FINAL, next_refresh_at=None))
    assert [t.request_key[0] for t in tracked.list_tracked()] == ["b", "a"]


def test_a_sqlite_row_survives_a_restart(tmp_path):
    path = tmp_path / "elections.db"
    first = SqliteTrackedStore(path)
    first.add_tracked(row(resolved={"assembly_seats": 179}))
    first.close()

    second = SqliteTrackedStore(path)
    reread = second.get_tracked("k" * 64)
    second.close()
    assert reread.resolved == {"assembly_seats": 179}
    assert reread.election_date == date(2026, 6, 1)


def test_a_naive_timestamp_is_refused_rather_than_stored_wrongly(tracked):
    tracked.add_tracked(row())
    with pytest.raises(ValueError, match="time zone"):
        tracked.update_tracked("k" * 64, next_refresh_at=datetime(2026, 3, 1, 12, 0))


# --- provenance and put_election ---------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def elections(request, tmp_path):
    if request.param == "memory":
        yield InMemoryElectionStore()
        return
    store = SqliteElectionStore(tmp_path / "elections.db")
    yield store
    store.close()


def test_put_election_stores_once_and_says_so(elections):
    """Idempotent by construction: the refresh re-reads the same poll every tick."""
    election = make_election()
    assert elections.put_election(HASH, election)
    assert not elections.put_election(HASH, election)

    stored = elections.get_stored(HASH)
    assert stored.provenance == "auto"
    assert stored.election == election


def test_a_confirmed_election_is_manual(elections):
    """Nothing a person confirmed is footnoted as automatic."""
    from tests.factories import make_request

    elections.claim("k" * 64, make_request())
    elections.stage("k" * 64, make_election())
    elections.confirm("k" * 64, HASH)
    assert elections.get_stored(HASH).provenance == "manual"
