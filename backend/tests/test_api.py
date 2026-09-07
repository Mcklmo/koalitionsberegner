"""The HTTP surface, driven end to end against the in-memory store."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser


@pytest.fixture
def parser():
    return CountingParser()


@pytest.fixture
def client(parser):
    store = InMemoryElectionStore()
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(store, parser)
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


BODY = {
    "nation": "Danmark",
    "election_date": "2026-03-25",
    "source_url": "https://www.dst.dk/valg",
}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_import_waits_for_the_parse_and_returns_the_election(client, parser):
    response = client.post("/api/elections/import?wait_seconds=2", json=BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["election"]["nation"] == "Danmark"
    assert body["election"]["total_seats"] == 10
    assert parser.call_count == 1


def test_import_without_waiting_reports_pending(client):
    response = client.post("/api/elections/import", json=BODY)
    assert response.status_code == 202
    assert response.json()["state"] == "pending"


def test_reimporting_a_stored_election_is_short_circuited(client, parser):
    first = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
    second = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()

    assert second["election_hash"] == first["election_hash"]
    assert second["state"] == "ready"
    assert second["reused"] is True
    assert parser.call_count == 1


def test_get_by_hash_returns_the_stored_election(client):
    imported = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
    response = client.get(f"/api/elections/{imported['election_hash']}")
    assert response.status_code == 200
    assert response.json()["election"]["title"] == "Koalitionsberegner"


def test_get_by_unknown_hash_is_404(client):
    assert client.get("/api/elections/" + "0" * 64).status_code == 404


def test_lookup_resolves_metadata_to_the_same_hash(client):
    imported = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
    response = client.get(
        "/api/elections/lookup", params={"nation": "  DANMARK ", "election_date": "2026-03-25"}
    )
    assert response.status_code == 200
    assert response.json()["election_hash"] == imported["election_hash"]
    assert response.json()["state"] == "ready"


def test_listing_returns_stored_elections(client):
    assert client.get("/api/elections").json() == []
    client.post("/api/elections/import?wait_seconds=2", json=BODY)
    client.post(
        "/api/elections/import?wait_seconds=2", json={**BODY, "state": "Nordjylland"}
    )

    listed = client.get("/api/elections").json()
    assert len(listed) == 2
    assert {row["state"] for row in listed} == {None, "Nordjylland"}
    assert all(row["nation"] == "Danmark" for row in listed)


def test_a_failed_import_is_reported_and_can_be_retried(client):
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(
        InMemoryElectionStore(), _failing_then_working
    )
    failed = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
    assert failed["state"] == "failed"
    assert "extraction failed" in failed["error"]

    retried = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
    assert retried["state"] == "ready"


_failing_then_working = CountingParser(fail_times=1)


def test_unknown_fields_in_the_body_are_rejected(client):
    response = client.post("/api/elections/import", json={**BODY, "sneaky": "value"})
    assert response.status_code == 422


def test_a_malformed_date_is_rejected(client):
    response = client.post("/api/elections/import", json={**BODY, "election_date": "25/03/2026"})
    assert response.status_code == 422
