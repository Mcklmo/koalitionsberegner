"""The HTTP surface, driven end to end against the in-memory store.

A caller supplies a year and a place; the server works out which election that
is and where to read it.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser, make_election

BODY = {"year": 2026, "nation": "Danmark"}
#: What the same election looks like asked for with a slip of the fingers. A
#: different request key, so a separate import — but the election it resolves
#: to is the one ``BODY`` names.
TYPO_BODY = {"year": 2026, "nation": "Danmrak"}
#: And asked for in another language, which no amount of normalising bridges:
#: only the identity that comes back can tell these are one election.
SAME_ELECTION_BODY = {"year": 2026, "nation": "Denmark"}
OTHER_BODY = {"year": 2021, "nation": "Deutschland", "subnation": "Sachsen-Anhalt"}


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
    confirmed = client.post(f"/api/elections/imports/{previewed['request_key']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_import_takes_a_year_and_a_place(client, parser):
    body = preview(client)
    assert body["election"]["nation"] == "Danmark"
    assert body["election_hash"], "identity is known once the results have been read"
    assert body["election"]["source_url"], "and so is the page they were read from"
    assert (parser.calls[0].year, parser.calls[0].nation) == (2026, "Danmark")
    assert [f.name for f in dataclasses.fields(parser.calls[0])] == [
        "year", "nation", "subnation"
    ], "an import request carries nothing else"


def test_a_url_is_not_something_a_caller_may_supply(client):
    response = client.post("/api/elections/import", json={**BODY, "source_url": "https://x.example"})
    assert response.status_code == 422, "which page is read is not the caller's to choose"


def test_a_year_that_is_not_a_year_is_rejected_without_looking_anything_up(client, parser):
    response = client.post("/api/elections/import", json={"year": "sometime", "nation": "Danmark"})
    assert response.status_code == 422
    assert parser.call_count == 0


def test_a_mistyped_year_is_read_as_what_it_means(client, parser):
    """The digit row is easy to miss; that is not worth a round trip to fix."""
    body = preview(client, {"year": "2o26", "nation": " Danmark "})
    assert body["state"] == "preview"
    assert parser.calls[0].year == 2026
    assert parser.calls[0].nation == "Danmark"


def test_a_misspelled_place_is_passed_on_rather_than_refused(client, parser):
    """Spelling is the resolver's job. The API must not get in its way."""
    preview(client, {"year": 2026, "nation": "Danmrak"})
    assert parser.calls[0].nation == "Danmrak"


def test_an_empty_region_means_the_national_election(client, parser):
    preview(client, {"year": 2026, "nation": "Danmark", "subnation": "  "})
    assert parser.calls[0].subnation is None


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
    key = body["request_key"]
    assert client.delete(f"/api/elections/imports/{key}/preview").status_code == 204
    assert client.get("/api/elections").json() == []
    assert client.get(f"/api/elections/imports/{key}").status_code == 404


def test_confirming_an_unknown_import_is_404(client):
    assert client.post("/api/elections/imports/" + "0" * 64 + "/confirm").status_code == 404


def test_confirming_while_an_import_is_running_is_409(client, parser):
    parser.hold()
    body = client.post("/api/elections/import", json=BODY).json()
    assert client.post(
        f"/api/elections/imports/{body['request_key']}/confirm"
    ).status_code == 409
    parser.release()


def test_import_without_waiting_reports_pending(client, parser):
    parser.hold()
    response = client.post("/api/elections/import", json=BODY)
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert response.json()["election_hash"] is None
    parser.release()


def test_asking_again_for_the_same_election_is_short_circuited(client, parser):
    first = save(client)
    second = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()

    assert second["request_key"] == first["request_key"]
    assert second["state"] == "ready"
    assert second["reused"] is True
    assert parser.call_count == 1


def test_lookup_detects_the_same_request_without_looking_anything_up(client, parser):
    """The up-front check the UI runs before spending an import."""
    first = save(client)
    calls_before = parser.call_count

    response = client.get("/api/elections/lookup", params={"year": 2026, "nation": " DANMARK "})
    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    assert response.json()["election_hash"] == first["election_hash"]
    assert parser.call_count == calls_before, "a duplicate check must not import anything"


def test_lookup_finds_a_stored_election_however_it_was_first_asked_for(client, parser):
    """The stronger half of the check. The first importer mistyped the country,
    so their request key is not one anybody else will produce — but the
    election was stored under the name that came back, and that is what a later
    request is matched against. Nobody pays twice for it."""
    first = save(client, TYPO_BODY)

    response = client.get("/api/elections/lookup", params=BODY)
    assert response.json()["state"] == "ready"
    assert response.json()["election_hash"] == first["election_hash"]
    assert response.json()["reused"] is True
    assert parser.call_count == 1


def test_lookup_of_an_election_nobody_has_imported_reports_unknown(client):
    response = client.get("/api/elections/lookup", params={"year": 1999, "nation": "Danmark"})
    assert response.json()["state"] == "unknown"


def test_lookup_rejects_a_malformed_request(client):
    response = client.get("/api/elections/lookup", params={"year": "sometime", "nation": "x"})
    assert response.status_code == 422


def test_another_way_of_asking_for_a_stored_election_is_deduplicated(client, store, parser):
    first = save(client)
    second = client.post(
        "/api/elections/import?wait_seconds=2", json=SAME_ELECTION_BODY
    ).json()

    assert parser.call_count == 2, "the new request had to be answered to be identified"
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
        store, CountingParser(by_year={2026: danish, 2021: german})
    )

    save(client)
    save(client, OTHER_BODY)

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
    assert "Denmark — 2026" in page.text

    assert client.get("/js/app.js").status_code == 200
    assert client.get("/api/elections").status_code == 200, "the mount must not shadow the API"


# --- the owner's secret -----------------------------------------------------

SECRET = "an-administrator-secret-of-forty-chars!!"
OWNER = {"x-admin-secret": SECRET}


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", SECRET)


def test_with_a_secret_configured_an_import_without_it_is_refused(client, parser, secret):
    for headers in ({}, {"x-admin-secret": "not-the-secret"}):
        refused = client.post("/api/elections/import", json=BODY, headers=headers)

        assert refused.status_code == 403
        assert "administrator's secret" in refused.json()["detail"]
    assert parser.call_count == 0, "nothing is looked up for a caller who is not the owner"


def test_the_right_secret_imports_and_saves(client, parser, secret):
    parser.hold()
    started = client.post("/api/elections/import", json=BODY, headers=OWNER)
    assert started.status_code == 202
    parser.release()

    key = started.json()["request_key"]
    previewed = client.get(f"/api/elections/imports/{key}?wait_seconds=2", headers=OWNER)
    assert previewed.status_code == 200
    assert previewed.json()["state"] == "preview"

    saved = client.post(f"/api/elections/imports/{key}/confirm", headers=OWNER)
    assert saved.status_code == 200
    assert len(client.get("/api/elections").json()) == 1


def test_with_no_secret_configured_a_local_run_is_the_owners_own(client, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)

    assert client.post("/api/elections/import?wait_seconds=2", json=BODY).status_code == 200


def test_every_step_past_the_import_needs_the_secret_too(client, secret):
    key = "0" * 64
    refused = [
        client.get(f"/api/elections/imports/{key}"),
        client.post(f"/api/elections/imports/{key}/confirm"),
        client.delete(f"/api/elections/imports/{key}/preview"),
        client.get("/api/admin/usage"),
    ]

    assert [response.status_code for response in refused] == [403] * 4


def test_reading_and_looking_up_need_no_secret(client, secret):
    assert client.get("/api/elections").status_code == 200
    assert client.get("/api/elections/lookup?year=2026&nation=Danmark").status_code == 200


def test_the_page_is_told_whether_it_may_import(client, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    local = client.get("/api/config").json()
    assert local == {"requests_enabled": False, "imports_enabled": True, "imports_open": True}

    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    deployed = client.get("/api/config").json()
    assert (deployed["imports_enabled"], deployed["imports_open"]) == (True, False)

    monkeypatch.setenv("LLM_MODE", "off")
    assert client.get("/api/config").json()["imports_enabled"] is False
