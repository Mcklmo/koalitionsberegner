"""The HTTP surface, driven end to end against the in-memory store."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser

BODY = {
    "nation": "Danmark",
    "election_date": "2026-03-25",
    "source_url": "https://www.dst.dk/valg",
}


@pytest.fixture
def parser():
    return CountingParser()


@pytest.fixture
def store():
    return InMemoryElectionStore()


@pytest.fixture
def client(parser, store):
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(store, parser)
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def preview(client, body=None):
    """Submit an import and wait for the extraction to be ready for review."""
    response = client.post("/api/elections/import?wait_seconds=2", json=body or BODY)
    assert response.json()["state"] == "preview", response.text
    return response.json()


def save(client, body=None):
    previewed = preview(client, body)
    confirmed = client.post(f"/api/elections/{previewed['election_hash']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_import_returns_an_unsaved_preview(client, store, parser):
    body = preview(client)
    assert body["election"]["nation"] == "Danmark"
    assert body["election"]["total_seats"] == 10
    assert parser.call_count == 1
    assert store.get_election(body["election_hash"]) is None, "not saved without confirmation"
    assert client.get("/api/elections").json() == []


def test_confirming_saves_the_election(client):
    saved = save(client)
    assert saved["state"] == "ready"
    listed = client.get("/api/elections").json()
    assert [row["election_hash"] for row in listed] == [saved["election_hash"]]


def test_discarding_a_preview_saves_nothing(client, store):
    body = preview(client)
    assert client.delete(f"/api/elections/{body['election_hash']}/preview").status_code == 204
    assert client.get("/api/elections").json() == []
    assert client.get(f"/api/elections/{body['election_hash']}").status_code == 404


def test_confirming_an_unknown_hash_is_404(client):
    assert client.post("/api/elections/" + "0" * 64 + "/confirm").status_code == 404


def test_confirming_while_a_parse_is_still_running_is_409(client, parser):
    parser.hold()
    body = client.post("/api/elections/import", json=BODY).json()
    assert client.post(f"/api/elections/{body['election_hash']}/confirm").status_code == 409
    parser.release()


def test_import_without_waiting_reports_pending(client, parser):
    parser.hold()
    response = client.post("/api/elections/import", json=BODY)
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    parser.release()


def test_reimporting_a_saved_election_is_short_circuited(client, parser):
    first = save(client)
    second = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()

    assert second["election_hash"] == first["election_hash"]
    assert second["state"] == "ready"
    assert second["reused"] is True
    assert parser.call_count == 1


def test_lookup_detects_a_duplicate_before_any_extraction(client, parser):
    """The UI's up-front duplicate check: metadata in, stored state out,
    without touching the parser."""
    first = save(client)
    calls_before = parser.call_count

    response = client.get(
        "/api/elections/lookup", params={"nation": "  DANMARK ", "election_date": "2026-03-25"}
    )
    assert response.status_code == 200
    assert response.json()["election_hash"] == first["election_hash"]
    assert response.json()["state"] == "ready"
    assert parser.call_count == calls_before, "a duplicate check must not parse anything"


def test_lookup_of_an_unknown_election_reports_unknown(client):
    response = client.get(
        "/api/elections/lookup", params={"nation": "Sverige", "election_date": "2026-09-13"}
    )
    assert response.json()["state"] == "unknown"


def test_get_by_hash_returns_the_saved_election(client):
    saved_election = save(client)
    response = client.get(f"/api/elections/{saved_election['election_hash']}")
    assert response.status_code == 200
    assert response.json()["election"]["title"] == "Koalitionsberegner"


def test_get_by_unknown_hash_is_404(client):
    assert client.get("/api/elections/" + "0" * 64).status_code == 404


def test_listing_returns_saved_elections(client):
    assert client.get("/api/elections").json() == []
    save(client)
    save(client, {**BODY, "state": "Nordjylland"})

    listed = client.get("/api/elections").json()
    assert len(listed) == 2
    assert {row["state"] for row in listed} == {None, "Nordjylland"}
    assert all(row["nation"] == "Danmark" for row in listed)


def test_a_failed_import_is_reported_and_can_be_retried(store):
    failing = CountingParser(fail_times=1)
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(store, failing)
    with TestClient(main.app) as client:
        failed = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
        assert failed["state"] == "failed"
        assert "extraction failed" in failed["error"]

        retried = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
        assert retried["state"] == "preview"
    main.app.dependency_overrides.clear()


def test_unknown_fields_in_the_body_are_rejected(client):
    assert client.post("/api/elections/import", json={**BODY, "sneaky": "v"}).status_code == 422


def test_a_malformed_date_is_rejected(client):
    body = {**BODY, "election_date": "25/03/2026"}
    assert client.post("/api/elections/import", json=body).status_code == 422


def test_the_page_is_served_from_the_same_origin_as_the_api(client):
    """The frontend defaults to a same-origin API, so one server must serve both."""
    page = client.get("/")
    assert page.status_code == 200
    assert "Koalitionsberegner" in page.text

    assert client.get("/js/app.js").status_code == 200
    assert client.get("/api/elections").status_code == 200, "the mount must not shadow the API"
