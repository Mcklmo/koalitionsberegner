"""Offered forecasts in the store: held for a choice, saved one at a time.

Runs against both local backends, like ``test_store.py``, because the service
cannot tell which one it was handed.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.sqlite_store import SqliteElectionStore
from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import make_election, make_forecast, make_request

KEY = "k" * 64
VOXMETER = make_forecast("Voxmeter", "2026-09-07")
EPINION = make_forecast("Epinion", "2026-08-30", computed=True)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryElectionStore()
        return
    backend = SqliteElectionStore(tmp_path / "elections.db")
    yield backend
    backend.close()


def offered(store):
    store.claim(KEY, make_request(year=2027))
    store.offer(KEY, [VOXMETER, EPINION])


def test_offering_holds_the_forecasts_and_stores_none(store):
    offered(store)

    job = store.get_job(KEY)
    assert job.status is JobStatus.AWAITING_CHOICE
    assert job.forecasts == (VOXMETER, EPINION)
    assert store.list_elections() == []
    assert store.claim(KEY, make_request(year=2027)).outcome is ClaimOutcome.ATTACHED


def test_choosing_stores_that_forecast_and_keeps_offering_the_rest(store):
    offered(store)

    confirmation = store.confirm_forecast(KEY, EPINION, "h-epinion")

    assert confirmation.election == EPINION and confirmation.duplicate is False
    assert [s.election for s in store.list_elections()] == [EPINION]
    assert store.get_job(KEY).status is JobStatus.AWAITING_CHOICE
    assert store.resolve_request(KEY) is None, "a forecast is not the answer to the request"

    store.confirm_forecast(KEY, VOXMETER, "h-voxmeter")
    assert len(store.list_elections()) == 2


def test_choosing_the_same_forecast_twice_stores_it_once(store):
    offered(store)
    store.confirm_forecast(KEY, VOXMETER, "h")

    again = store.confirm_forecast(KEY, VOXMETER, "h")

    assert again.duplicate is True
    assert len(store.list_elections()) == 1


def test_a_forecast_that_was_not_offered_cannot_be_chosen(store):
    offered(store)
    other = make_forecast("Megafon", "2026-09-01")

    assert store.confirm_forecast(KEY, other, "h") is None
    assert store.list_elections() == []


def test_nothing_can_be_chosen_from_a_request_with_nothing_on_offer(store):
    store.claim(KEY, make_request())
    store.stage(KEY, make_election())

    assert store.confirm_forecast(KEY, VOXMETER, "h") is None


def test_discarding_the_list_frees_the_request(store):
    offered(store)

    assert store.discard(KEY) is True
    assert store.get_job(KEY) is None
    assert store.claim(KEY, make_request(year=2027)).outcome is ClaimOutcome.STARTED


def test_a_stored_forecast_is_not_the_election_held_that_year(store):
    offered(store)
    store.confirm_forecast(KEY, VOXMETER, "h")

    assert store.find_by_place(2027, "Danmark") is None


def test_a_new_attempt_after_the_lease_forgets_the_old_list(tmp_path):
    for backend in (InMemoryElectionStore(stale_after=0),
                    SqliteElectionStore(tmp_path / "lease.db", stale_after=0)):
        offered(backend)
        assert backend.claim(KEY, make_request(year=2027)).outcome is ClaimOutcome.STARTED
        assert backend.get_job(KEY).forecasts == ()


def test_offered_forecasts_survive_a_restart(tmp_path):
    path = tmp_path / "elections.db"
    first = SqliteElectionStore(path)
    offered(first)
    first.close()

    reopened = SqliteElectionStore(path)
    assert reopened.get_job(KEY).forecasts == (VOXMETER, EPINION)
    reopened.close()


def test_a_database_from_before_forecasts_gains_the_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE import_jobs (request_key TEXT PRIMARY KEY, status TEXT NOT NULL,"
        " query TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,"
        " attempt INTEGER NOT NULL DEFAULT 1, error TEXT, result TEXT);"
        "INSERT INTO import_jobs VALUES ('k', 'failed', 'q', 1.0, 1, 'boom', NULL);"
    )
    conn.close()

    store = SqliteElectionStore(path)
    job = store.get_job("k")
    assert (job.error, job.forecasts) == ("boom", ())
    store.close()
