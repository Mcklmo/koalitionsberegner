"""Choosing a forecast over HTTP: offered as a list, saved by option."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import make_forecast

BODY = {"year": 2027, "nation": "Danmark"}
FORECASTS = [make_forecast("Voxmeter", "2026-09-07"), make_forecast("Epinion", "2026-08-30", computed=True)]


class ForecastParser:
    """What the parser hands back for an election not yet held."""

    def __init__(self, forecasts=FORECASTS):
        self.forecasts = forecasts
        self.calls = []

    async def parse(self, request):
        self.calls.append(request)
        return list(self.forecasts)


@pytest.fixture
def parser():
    return ForecastParser()


@pytest.fixture
def store():
    return InMemoryElectionStore()


@pytest.fixture
def client(parser, store):
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(store, parser)
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def offered(client):
    response = client.post("/api/elections/import?wait_seconds=2", json=BODY)
    assert response.json()["state"] == "choose", response.text
    return response.json()


def confirm(client, key, option=None):
    suffix = "" if option is None else f"?option={option}"
    return client.post(f"/api/elections/imports/{key}/confirm{suffix}")


def test_an_upcoming_election_is_offered_as_forecasts_and_nothing_is_saved(client, store):
    body = offered(client)

    assert [f["forecast"]["publisher"] for f in body["forecasts"]] == ["Voxmeter", "Epinion"]
    assert body["forecasts"][1]["forecast"]["computed"] is True
    assert body["election"] is None
    assert store.list_elections() == []


def test_confirming_without_choosing_is_409(client):
    key = offered(client)["request_key"]
    response = confirm(client, key)
    assert response.status_code == 409
    assert "option" in response.json()["detail"]


def test_choosing_a_forecast_that_was_not_offered_is_422(client):
    key = offered(client)["request_key"]
    assert confirm(client, key, 5).status_code == 422
    assert confirm(client, key, -1).status_code == 422


def test_choosing_saves_that_forecast_and_lists_it_as_one(client):
    key = offered(client)["request_key"]

    saved = confirm(client, key, 1)

    assert saved.status_code == 200, saved.text
    assert saved.json()["election"]["forecast"]["publisher"] == "Epinion"
    [row] = client.get("/api/elections").json()
    assert row["forecast"] == {"publisher": "Epinion", "published_on": "2026-08-30", "computed": True}
    assert client.get(f"/api/elections/{row['election_hash']}").json()["election"]["forecast"]


def test_more_than_one_forecast_may_be_saved_from_one_list(client):
    key = offered(client)["request_key"]
    confirm(client, key, 0)
    confirm(client, key, 1)
    again = confirm(client, key, 0)

    assert again.json()["duplicate"] is True
    assert len(client.get("/api/elections").json()) == 2


def test_asking_again_while_the_list_is_offered_reads_nothing_again(client, parser):
    offered(client)
    second = offered(client)

    assert second["reused"] is True
    assert len(parser.calls) == 1


def test_lookup_reports_the_list_without_reading_anything(client, parser):
    offered(client)
    looked = client.get("/api/elections/lookup", params=BODY).json()

    assert looked["state"] == "choose"
    assert len(looked["forecasts"]) == 2
    assert len(parser.calls) == 1


def test_a_saved_forecast_is_not_the_election_that_year(client):
    key = offered(client)["request_key"]
    confirm(client, key, 0)
    assert client.delete(f"/api/elections/imports/{key}/preview").status_code == 204

    looked = client.get("/api/elections/lookup", params=BODY).json()
    assert looked["state"] == "unknown", "newer polls, or the result, can still be imported"


def test_discarding_the_list_saves_nothing(client):
    key = offered(client)["request_key"]
    assert client.delete(f"/api/elections/imports/{key}/preview").status_code == 204
    assert client.get("/api/elections").json() == []
    assert client.get(f"/api/elections/imports/{key}").status_code == 404
