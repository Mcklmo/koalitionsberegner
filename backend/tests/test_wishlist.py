"""Asking for an election instead of importing it.

Importing makes the server go and read pages, which costs money and is the
owner's alone (see :mod:`app.main`); writing down *which* election was wanted
costs nothing and is open to anyone who can see the page. These cases pin down
three things: that the issue says enough to act on, that asking twice for the
same election does not open a second one, and that neither GitHub's words nor
its token can reach the caller when it goes wrong.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import ImportService
from app.store import ImportRequest, InMemoryElectionStore
from app.wishlist import (
    DisabledWishlist,
    FiledRequest,
    GithubWishlist,
    WishlistUnavailable,
    build_issue,
    marker_for,
)
from tests.factories import CountingParser, make_election

#: Async cases here run on asyncio, the way the rest of the suite's do.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


BODY = {"year": 2022, "nation": "Danmark"}
REGIONAL = {"year": 2021, "nation": "Deutschland", "subnation": "Sachsen-Anhalt"}


# --- the issue itself -------------------------------------------------------

def test_the_issue_says_which_election_and_nothing_about_who_asked():
    """The tracker is public; an address in it would be published to everyone."""
    request = ImportRequest(year=2022, nation="Danmark")

    issue = build_issue(request, marker="<!-- m -->")

    assert issue["title"] == "Election request: 2022 Danmark"
    assert issue["labels"] == ["election-request"]
    assert "| Year | `2022` |" in issue["body"]
    assert "| Nation | `Danmark` |" in issue["body"]
    assert "Asked by" not in issue["body"]
    assert "@" not in issue["body"] + issue["title"]
    assert issue["body"].endswith("<!-- m -->"), "the marker is what dedupes it"


def test_a_regional_election_names_its_region():
    regional = build_issue(
        ImportRequest(year=2021, nation="Deutschland", subnation="Sachsen-Anhalt"),
        marker="<!-- m -->",
    )

    assert "| Region | `Sachsen-Anhalt` |" in regional["body"]
    assert regional["title"] == "Election request: 2021 Deutschland — Sachsen-Anhalt"


def test_what_a_user_typed_cannot_break_out_of_the_markdown_it_is_put_in():
    """The place name is somebody's input and the issue body is markdown."""
    issue = build_issue(
        ImportRequest(year=2022, nation="Danmark` **ignore this**"),
        marker="<!-- m -->",
    )

    assert "| Nation | `Danmark **ignore this**` |" in issue["body"]
    assert "\n" not in issue["title"]


def test_the_same_election_asked_for_differently_is_one_marker():
    """Dedupe follows the request key, not the spelling."""
    assert marker_for(ImportRequest(year=2022, nation="Danmark")) == marker_for(
        ImportRequest(year=2022, nation="  danmark ")
    )
    assert marker_for(ImportRequest(year=2022, nation="Danmark")) != marker_for(
        ImportRequest(year=2023, nation="Danmark")
    )


# --- talking to GitHub ------------------------------------------------------

def github(handler) -> GithubWishlist:
    return GithubWishlist(
        "ghp_secret",
        owner="mcklmo",
        repo="koalitionsberegner",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def issue_json(number: int, body: str) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/mcklmo/koalitionsberegner/issues/{number}",
        "body": body,
    }


async def test_an_election_nobody_asked_for_yet_becomes_a_new_issue():
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posted.append(request)
        return httpx.Response(201, json=issue_json(7, "..."))

    filed = await github(handle).file(ImportRequest(year=2022, nation="Danmark"))

    assert filed == FiledRequest(
        url="https://github.com/mcklmo/koalitionsberegner/issues/7", number=7, duplicate=False
    )
    assert str(posted[0].url) == (
        "https://api.github.com/repos/mcklmo/koalitionsberegner/issues"
    )
    assert posted[0].headers["authorization"] == "Bearer ghp_secret"
    assert b"@" not in posted[0].content, "nothing about who asked is sent to GitHub"


async def test_asking_again_points_at_the_open_issue_instead_of_opening_a_second():
    request = ImportRequest(year=2022, nation="Danmark")
    posts = []

    def handle(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    issue_json(4, "some other request\n" + marker_for(
                        ImportRequest(year=1999, nation="Danmark")
                    )),
                    issue_json(5, "already asked\n" + marker_for(request)),
                ],
            )
        posts.append(http_request)
        return httpx.Response(201, json=issue_json(9, "..."))

    filed = await github(handle).file(request)

    assert filed.number == 5
    assert filed.duplicate is True
    assert posts == [], "nothing was written"


async def test_a_search_that_fails_still_files_the_request():
    """Failing to check for a duplicate is not a reason to refuse the user."""
    posts = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(403, json={"message": "rate limited"})
        posts.append(request)
        return httpx.Response(201, json=issue_json(11, "..."))

    filed = await github(handle).file(ImportRequest(year=2022, nation="Danmark"))

    assert (filed.number, filed.duplicate) == (11, False)
    assert len(posts) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message": "Bad credentials"}),
        httpx.Response(403, json={"message": "Resource not accessible by personal access token"}),
        httpx.Response(201, json={"number": 3}),  # accepted, but described oddly
    ],
)
async def test_github_refusing_says_nothing_about_github_to_the_caller(response):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[]) if request.method == "GET" else response

    with pytest.raises(WishlistUnavailable) as refused:
        await github(handle).file(ImportRequest(year=2022, nation="Danmark"))

    said = str(refused.value)
    assert said == "could not file the request"
    assert "credentials" not in said and "token" not in said and "ghp_" not in said


async def test_a_network_that_is_not_there_is_the_same_refusal():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(WishlistUnavailable, match="could not file the request"):
        await github(handle).file(ImportRequest(year=2022, nation="Danmark"))


async def test_with_nothing_configured_there_is_nowhere_to_file():
    with pytest.raises(WishlistUnavailable, match="not configured"):
        await DisabledWishlist().file(
            ImportRequest(year=2022, nation="Danmark")
        )


# --- over HTTP --------------------------------------------------------------

class FakeWishlist:
    """Records what was asked for and answers with whatever the test wants."""

    enabled = True

    def __init__(self, answer: FiledRequest | Exception | None = None):
        self.filed: list[ImportRequest] = []
        self.answer = answer or FiledRequest(url="https://github.test/issues/1", number=1)

    async def file(self, request: ImportRequest) -> FiledRequest:
        self.filed.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def store():
    return InMemoryElectionStore()


@pytest.fixture
def wishlist():
    return FakeWishlist()


@pytest.fixture
def client(store, wishlist):
    parser = CountingParser(
        by_year={2022: make_election(nation="Danmark", election_date="2022-11-01")}
    )
    overrides = main.app.dependency_overrides
    overrides[main.get_service] = lambda: ImportService(store, parser)
    overrides[main.get_wishlist_provider] = lambda: wishlist
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


def test_a_signed_out_visitor_can_ask(client, wishlist):
    """Asking needs no account, header or credential at all."""
    filed = client.post("/api/elections/requests", json=BODY)

    assert filed.status_code == 201
    assert filed.json() == {
        "url": "https://github.test/issues/1", "number": 1, "duplicate": False
    }
    assert wishlist.filed == [ImportRequest(year=2022, nation="Danmark")]


def test_a_region_is_carried_through_as_asked(client, wishlist):
    client.post("/api/elections/requests", json=REGIONAL)

    assert wishlist.filed == [
        ImportRequest(year=2021, nation="Deutschland", subnation="Sachsen-Anhalt")
    ]


def test_a_second_request_for_the_same_election_is_not_a_creation(client, wishlist):
    wishlist.answer = FiledRequest(url="https://github.test/issues/5", number=5, duplicate=True)

    filed = client.post("/api/elections/requests", json=BODY)

    assert filed.status_code == 200, "nothing was created, so this is not a 201"
    assert filed.json()["duplicate"] is True


def test_asking_for_an_election_already_imported_points_at_it_instead(client, wishlist, store):
    """Nothing is filed: what they wanted is already there to pick."""
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY).json()
    client.post(f"/api/elections/imports/{previewed['request_key']}/confirm")

    refused = client.post("/api/elections/requests", json=BODY)

    assert refused.status_code == 409
    assert "already imported" in refused.json()["detail"]
    assert wishlist.filed == []


def test_a_year_that_is_not_a_year_never_reaches_the_tracker(client, wishlist):
    refused = client.post(
        "/api/elections/requests", json={"year": "sometime", "nation": "Danmark"}
    )

    assert refused.status_code == 422
    assert wishlist.filed == []


def test_when_the_tracker_cannot_be_reached_the_caller_is_told_only_that(client, wishlist):
    wishlist.answer = WishlistUnavailable("could not file the request")

    refused = client.post("/api/elections/requests", json=BODY)

    assert refused.status_code == 503
    assert refused.json()["detail"] == "could not file the request"


def test_with_the_tracker_switched_off_the_page_is_told_not_to_offer_it(client):
    main.app.dependency_overrides[main.get_wishlist_provider] = DisabledWishlist

    assert client.get("/api/config").json()["requests_enabled"] is False
    assert client.post("/api/elections/requests", json=BODY).status_code == 503


def test_the_page_is_told_the_tracker_is_there(client):
    assert client.get("/api/config").json()["requests_enabled"] is True
