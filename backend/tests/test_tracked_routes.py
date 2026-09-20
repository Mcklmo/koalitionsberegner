"""The scheduled routes over tracked elections (plan 3, A3): the two crons call,
the owner's own table behind ``x-admin-secret``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.calendar import CalendarEntry, ScanResult
from app.identity import request_key as compute_request_key
from app.refresh_config import AfterRow, BeforeRow, RefreshConfig
from app.store import InMemoryElectionStore, InMemoryTrackedStore, TrackedElection, TrackedStatus

ADMIN_SECRET = "a" * 40
ADMIN = {"x-admin-secret": ADMIN_SECRET}
SCHEDULE_SECRET = "r" * 40
SCHEDULE = {"x-report-secret": SCHEDULE_SECRET}

REQUEST_KEY = "k" * 64
CONFIG = RefreshConfig(
    before_election=(BeforeRow(more_than=timedelta(0), every=timedelta(days=1)),),
    after_election=(AfterRow(within=timedelta(days=45), every=timedelta(days=1)),),
    stable_after=3, keep_newest=3, backoff_factor=2, max_backoff=timedelta(days=7), park_after=8,
)


def row(**overrides) -> TrackedElection:
    data = {
        "request_key": REQUEST_KEY, "year": 2026, "nation": "Danmark",
        "election_date": date(2026, 11, 3), "status": TrackedStatus.UPCOMING, "added_by": "owner",
    }
    data.update(overrides)
    return TrackedElection(**data)


class FakeScanner:
    def __init__(self, result: ScanResult | None = None, *, error: Exception | None = None):
        self.result = result or ScanResult()
        self.error = error
        self.calls: list[tuple[list[int], frozenset]] = []

    async def scan(self, years, *, tracked=frozenset()) -> ScanResult:
        self.calls.append((list(years), frozenset(tracked)))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def tracked_store():
    return InMemoryTrackedStore()


@pytest.fixture
def election_store():
    return InMemoryElectionStore()


@pytest.fixture
def client(tracked_store, election_store):
    overrides = main.app.dependency_overrides
    overrides[main.get_tracked_store_provider] = lambda: tracked_store
    overrides[main.get_store_provider] = lambda: election_store
    overrides[main.get_refresh_config_provider] = lambda: CONFIG
    overrides[main.get_refresh_parser_provider] = lambda: object()  # unused unless due rows exist
    overrides[main.get_calendar_scanner_provider] = lambda: FakeScanner()
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


# --- /api/internal/refresh ----------------------------------------------------


def test_refresh_is_not_found_with_no_schedule_secret_configured(client, monkeypatch):
    monkeypatch.delenv("USAGE_REPORT_SECRET", raising=False)
    assert client.post("/api/internal/refresh").status_code == 404


def test_refresh_refuses_the_wrong_secret(client, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", SCHEDULE_SECRET)
    assert client.post("/api/internal/refresh", headers={"x-report-secret": "wrong"}).status_code == 403


def test_refresh_with_nothing_due_answers_all_zeros(client, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", SCHEDULE_SECRET)
    response = client.post("/api/internal/refresh", headers=SCHEDULE)
    assert response.status_code == 200
    assert response.json() == {
        "refreshed": 0, "stored_polls": 0, "stored_results": 0,
        "finalised": 0, "failed": 0, "skipped_unchanged": 0,
    }


def test_refresh_counts_a_failed_row_and_still_answers_200(client, monkeypatch, tracked_store):
    """A row whose parser blows up (here: the fake has none of the needed methods)
    is a failed run, not a 500 — one broken election must not sink the tick."""
    monkeypatch.setenv("USAGE_REPORT_SECRET", SCHEDULE_SECRET)
    tracked_store.add_tracked(row())
    response = client.post("/api/internal/refresh", headers=SCHEDULE)
    assert response.status_code == 200
    body = response.json()
    assert body["refreshed"] == 1
    assert body["failed"] == 1
    assert tracked_store.get_tracked(REQUEST_KEY).consecutive_failures == 1


# --- /api/internal/calendar-scan ----------------------------------------------


def test_calendar_scan_is_not_found_with_no_schedule_secret_configured(client, monkeypatch):
    monkeypatch.delenv("USAGE_REPORT_SECRET", raising=False)
    assert client.post("/api/internal/calendar-scan?years=2026").status_code == 404


def test_calendar_scan_rejects_a_query_with_no_years(client, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", SCHEDULE_SECRET)
    response = client.post("/api/internal/calendar-scan?years=", headers=SCHEDULE)
    assert response.status_code == 422


def test_calendar_scan_tracks_every_proposed_entry(client, monkeypatch, tracked_store):
    monkeypatch.setenv("USAGE_REPORT_SECRET", SCHEDULE_SECRET)
    entry = CalendarEntry(
        nation="Danmark", election_date=date(2026, 11, 3), title="Folketingsvalg 2026",
        source_url="https://en.wikipedia.org/wiki/X", kind="national_legislature",
    )
    scanner = FakeScanner(ScanResult(entries=[entry], skipped={"past": 2}, failures=[]))
    main.app.dependency_overrides[main.get_calendar_scanner_provider] = lambda: scanner

    response = client.post("/api/internal/calendar-scan?years=2026,2027", headers=SCHEDULE)
    assert response.status_code == 200
    body = response.json()
    assert body == {"proposed": 1, "tracked": 1, "skipped": {"past": 2}, "failures": []}
    assert scanner.calls == [([2026, 2027], frozenset())]
    stored = tracked_store.get_tracked(entry.request_key)
    assert stored is not None
    assert stored.added_by == "calendar"
    assert stored.status is TrackedStatus.UPCOMING


def test_calendar_scan_does_not_re_track_what_is_already_tracked(client, monkeypatch, tracked_store):
    monkeypatch.setenv("USAGE_REPORT_SECRET", SCHEDULE_SECRET)
    entry = CalendarEntry(
        nation="Danmark", election_date=date(2026, 11, 3), title="Folketingsvalg 2026",
        source_url="https://en.wikipedia.org/wiki/X", kind="national_legislature",
    )
    tracked_store.add_tracked(row(request_key=entry.request_key))
    scanner = FakeScanner(ScanResult(entries=[entry]))
    main.app.dependency_overrides[main.get_calendar_scanner_provider] = lambda: scanner

    response = client.post("/api/internal/calendar-scan?years=2026", headers=SCHEDULE)
    assert response.json()["tracked"] == 0
    assert scanner.calls[0][1] == frozenset({entry.request_key})


# --- /api/admin/tracked --------------------------------------------------------


def test_admin_tracked_routes_need_the_admin_secret(client, monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    assert client.get("/api/admin/tracked").status_code == 403
    assert client.post("/api/admin/tracked", json={
        "year": 2026, "nation": "Danmark", "election_date": "2026-11-03",
    }).status_code == 403


def test_listing_shows_every_tracked_row(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    tracked_store.add_tracked(row())
    response = client.get("/api/admin/tracked", headers=ADMIN)
    assert response.status_code == 200
    [only] = response.json()
    assert only["request_key"] == REQUEST_KEY
    assert only["status"] == "upcoming"


def test_adding_a_tracked_election_by_hand(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    response = client.post(
        "/api/admin/tracked", headers=ADMIN,
        json={"year": 2026, "nation": "Danmark", "election_date": "2026-11-03"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["added_by"] == "owner"
    assert body["status"] == "upcoming"
    assert tracked_store.get_tracked(body["request_key"]) is not None


def test_adding_the_same_election_twice_is_a_conflict(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    tracked_store.add_tracked(row(request_key=compute_request_key(2026, "Danmark")))
    response = client.post(
        "/api/admin/tracked", headers=ADMIN,
        json={"year": 2026, "nation": "Danmark", "election_date": "2026-11-03"},
    )
    assert response.status_code == 409


def test_untracking_a_row_stops_it_being_read_again(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    tracked_store.add_tracked(row())
    response = client.put(
        f"/api/admin/tracked/{REQUEST_KEY}", headers=ADMIN, json={"status": "untracked"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "untracked"
    assert tracked_store.get_tracked(REQUEST_KEY).next_refresh_at is None


def test_correcting_a_date_and_asking_for_an_immediate_refresh(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    tracked_store.add_tracked(row(
        status=TrackedStatus.PARKED, consecutive_failures=8,
        next_refresh_at=None, last_error="boom",
    ))
    response = client.put(
        f"/api/admin/tracked/{REQUEST_KEY}", headers=ADMIN,
        json={"election_date": "2026-11-10", "refresh_now": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["election_date"] == "2026-11-10"
    assert body["status"] == "upcoming"
    assert body["consecutive_failures"] == 0
    assert body["next_refresh_at"] is None  # due now


def test_a_final_election_cannot_be_asked_to_refresh_again(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    tracked_store.add_tracked(row(status=TrackedStatus.FINAL, next_refresh_at=None))
    response = client.put(
        f"/api/admin/tracked/{REQUEST_KEY}", headers=ADMIN, json={"refresh_now": True},
    )
    assert response.status_code == 409


def test_updating_an_unknown_row_is_404(client, monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    response = client.put(
        f"/api/admin/tracked/{'z' * 64}", headers=ADMIN, json={"status": "untracked"},
    )
    assert response.status_code == 404


def test_an_empty_patch_is_rejected(client, monkeypatch, tracked_store):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    tracked_store.add_tracked(row())
    response = client.put(f"/api/admin/tracked/{REQUEST_KEY}", headers=ADMIN, json={})
    assert response.status_code == 422
