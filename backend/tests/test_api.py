"""The HTTP surface, driven end to end against the in-memory store.

A caller supplies a URL and nothing else; the agent identifies the election.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser, make_election

URL = "https://wahlergebnisse.sachsen-anhalt.de/"
OTHER_URL = "https://mirror.example.net/sachsen-anhalt"
BODY = {"source_url": URL}


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
    response = client.post("/api/elections/import?wait_seconds=2", json=body or BODY)
    assert response.json()["state"] == "preview", response.text
    return response.json()


def save(client, body=None):
    previewed = preview(client, body)
    confirmed = client.post(f"/api/elections/pages/{previewed['page_key']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_import_takes_only_a_url(client, parser):
    body = preview(client)
    assert body["election"]["nation"] == "Danmark", "the agent supplied the identity"
    assert body["election_hash"], "identity is known once the page has been read"
    assert parser.calls[0].source_url == URL
    assert [f.name for f in dataclasses.fields(parser.calls[0])] == ["source_url"], (
        "an import request carries nothing but the URL"
    )


def test_extra_fields_such_as_nation_are_rejected(client):
    response = client.post("/api/elections/import", json={**BODY, "nation": "Danmark"})
    assert response.status_code == 422, "identity is not the caller's to supply"


def test_a_malformed_url_is_rejected_without_fetching(client, parser):
    response = client.post("/api/elections/import", json={"source_url": "javascript:alert(1)"})
    assert response.status_code == 422
    assert parser.call_count == 0


def test_import_returns_an_unsaved_preview(client, store, parser):
    body = preview(client)
    assert parser.call_count == 1
    assert store.list_elections() == [], "not saved without confirmation"
    assert client.get("/api/elections").json() == []


def test_confirming_saves_the_election(client):
    saved = save(client)
    assert saved["state"] == "ready"
    assert saved["duplicate"] is False
    listed = client.get("/api/elections").json()
    assert [row["election_hash"] for row in listed] == [saved["election_hash"]]


def test_discarding_a_preview_saves_nothing(client):
    body = preview(client)
    assert client.delete(f"/api/elections/pages/{body['page_key']}/preview").status_code == 204
    assert client.get("/api/elections").json() == []
    assert client.get(f"/api/elections/pages/{body['page_key']}").status_code == 404


def test_confirming_an_unknown_page_is_404(client):
    assert client.post("/api/elections/pages/" + "0" * 64 + "/confirm").status_code == 404


def test_confirming_while_an_extraction_is_running_is_409(client, parser):
    parser.hold()
    body = client.post("/api/elections/import", json=BODY).json()
    assert client.post(f"/api/elections/pages/{body['page_key']}/confirm").status_code == 409
    parser.release()


def test_import_without_waiting_reports_pending(client, parser):
    parser.hold()
    response = client.post("/api/elections/import", json=BODY)
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert response.json()["election_hash"] is None
    parser.release()


def test_reimporting_the_same_page_is_short_circuited(client, parser):
    first = save(client)
    second = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()

    assert second["page_key"] == first["page_key"]
    assert second["state"] == "ready"
    assert second["reused"] is True
    assert parser.call_count == 1


def test_lookup_detects_a_known_page_without_fetching_it(client, parser):
    """The up-front check the UI runs — by page, since the election is not yet known."""
    first = save(client)
    calls_before = parser.call_count

    response = client.get("/api/elections/lookup", params={"source_url": URL.upper().replace("HTTPS", "https")})
    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    assert response.json()["election_hash"] == first["election_hash"]
    assert parser.call_count == calls_before, "a duplicate check must not extract anything"


def test_lookup_of_an_unknown_page_reports_unknown(client):
    response = client.get("/api/elections/lookup", params={"source_url": "https://example.org/x"})
    assert response.json()["state"] == "unknown"


def test_lookup_rejects_a_malformed_url(client):
    response = client.get("/api/elections/lookup", params={"source_url": "not a url"})
    assert response.status_code == 422


def test_a_second_url_for_the_same_election_is_deduplicated(client, store, parser):
    first = save(client)
    second = client.post("/api/elections/import?wait_seconds=2", json={"source_url": OTHER_URL}).json()

    assert parser.call_count == 2, "the new page had to be read to be identified"
    assert second["state"] == "ready", "recognised as one we hold; no confirmation needed"
    assert second["election_hash"] == first["election_hash"]
    assert len(client.get("/api/elections").json()) == 1


def test_get_by_hash_returns_the_saved_election(client):
    saved_election = save(client)
    response = client.get(f"/api/elections/{saved_election['election_hash']}")
    assert response.status_code == 200
    assert response.json()["election"]["title"] == "Koalitionsberegner"


def test_get_by_unknown_hash_is_404(client):
    assert client.get("/api/elections/" + "0" * 64).status_code == 404


def test_listing_returns_saved_elections(client, store):
    assert client.get("/api/elections").json() == []
    danish = make_election(nation="Danmark", election_date="2026-03-25")
    german = make_election(nation="Deutschland", state="Sachsen-Anhalt", election_date="2021-06-06")
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(
        store, CountingParser(by_url={URL: danish, OTHER_URL: german})
    )

    save(client)
    save(client, {"source_url": OTHER_URL})

    listed = client.get("/api/elections").json()
    assert len(listed) == 2
    assert {row["state"] for row in listed} == {None, "Sachsen-Anhalt"}


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


def test_the_page_is_served_from_the_same_origin_as_the_api(client):
    """The frontend defaults to a same-origin API, so one server must serve both."""
    page = client.get("/")
    assert page.status_code == 200
    assert "Koalitionsberegner" in page.text

    assert client.get("/js/app.js").status_code == 200
    assert client.get("/api/elections").status_code == 200, "the mount must not shadow the API"
