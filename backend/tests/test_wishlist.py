"""The other end of the paywall: asking for an election instead of importing it.

An account with no subscription cannot make the server go and read pages, which
is the part that costs money. What it can do is say which election it wanted,
and that is written down in the issue tracker to be imported by hand. These
cases pin down three things: that the issue says enough to act on, that asking
twice for the same election does not open a second one, and that neither
GitHub's words nor its token can reach the caller when it goes wrong.

Payments are closed while the card form is being fixed (``PAYMENTS_PAUSED``),
which is the reason the request path exists to point people at — so the pause
is asserted here too, next to the thing it redirects to.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.accounts import InMemoryAccountStore, QuotaPolicy, Tier, billing_period
from app.auth import StoreBackedVerifier, StubCredentials
from app.billing import CheckoutSession, DisabledBilling
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


VISITOR: dict[str, str] = {}
FREE = {"Authorization": "Bearer free-1:free@example.org"}
SUBSCRIBER = {"Authorization": "Bearer paid-1:paid@example.org"}
UNVERIFIED = {"Authorization": "Bearer new-1:new@example.org:unverified"}

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
def accounts():
    return InMemoryAccountStore()


@pytest.fixture
def store():
    return InMemoryElectionStore()


@pytest.fixture
def wishlist():
    return FakeWishlist()


@pytest.fixture
def client(store, accounts, wishlist, monkeypatch):
    parser = CountingParser(
        by_year={2022: make_election(nation="Danmark", election_date="2022-11-01")}
    )
    overrides = main.app.dependency_overrides
    overrides[main.get_service] = lambda: ImportService(store, parser)
    overrides[main.get_token_verifier] = lambda: StoreBackedVerifier(StubCredentials())
    overrides[main.get_account_store] = lambda: accounts
    overrides[main.get_policy] = lambda: QuotaPolicy({Tier.FREE: 0, Tier.BASIC: 5})
    overrides[main.get_billing_provider] = lambda: DisabledBilling()
    overrides[main.get_wishlist_provider] = lambda: wishlist
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


def test_an_account_with_no_subscription_may_ask_for_an_election(client, wishlist, accounts):
    filed = client.post("/api/elections/requests", json=BODY, headers=FREE)

    assert filed.status_code == 201
    assert filed.json() == {
        "url": "https://github.test/issues/1", "number": 1, "duplicate": False
    }
    assert wishlist.filed == [ImportRequest(year=2022, nation="Danmark")]
    assert accounts.get("free-1").used_in(billing_period()) == 0, "asking costs no quota"


def test_a_region_is_carried_through_as_asked(client, wishlist):
    client.post("/api/elections/requests", json=REGIONAL, headers=FREE)

    assert wishlist.filed == [
        ImportRequest(year=2021, nation="Deutschland", subnation="Sachsen-Anhalt")
    ]


def test_a_second_request_for_the_same_election_is_not_a_creation(client, wishlist):
    wishlist.answer = FiledRequest(url="https://github.test/issues/5", number=5, duplicate=True)

    filed = client.post("/api/elections/requests", json=BODY, headers=FREE)

    assert filed.status_code == 200, "nothing was created, so this is not a 201"
    assert filed.json()["duplicate"] is True


def test_asking_for_an_election_already_imported_points_at_it_instead(
    client, wishlist, accounts, store
):
    """Nothing is filed: what they wanted is already there to pick."""
    from app.accounts import Tier as _Tier

    accounts.ensure("paid-1", "paid@example.org")
    accounts.set_subscription("paid-1", _Tier.BASIC, customer_id="cus_1", status="active")
    previewed = client.post(
        "/api/elections/import?wait_seconds=2", json=BODY, headers=SUBSCRIBER
    ).json()
    client.post(
        f"/api/elections/imports/{previewed['request_key']}/confirm", headers=SUBSCRIBER
    )

    refused = client.post("/api/elections/requests", json=BODY, headers=FREE)

    assert refused.status_code == 409
    assert "already imported" in refused.json()["detail"]
    assert wishlist.filed == []


def test_asking_needs_an_account_and_a_confirmed_address(client, wishlist):
    assert client.post("/api/elections/requests", json=BODY, headers=VISITOR).status_code == 401
    assert client.post("/api/elections/requests", json=BODY, headers=UNVERIFIED).status_code == 403
    assert wishlist.filed == []


def test_a_year_that_is_not_a_year_never_reaches_the_tracker(client, wishlist):
    refused = client.post(
        "/api/elections/requests", json={"year": "sometime", "nation": "Danmark"}, headers=FREE
    )

    assert refused.status_code == 422
    assert wishlist.filed == []


def test_when_the_tracker_cannot_be_reached_the_caller_is_told_only_that(client, wishlist):
    wishlist.answer = WishlistUnavailable("could not file the request")

    refused = client.post("/api/elections/requests", json=BODY, headers=FREE)

    assert refused.status_code == 503
    assert refused.json()["detail"] == "could not file the request"


def test_with_the_tracker_switched_off_the_page_is_told_not_to_offer_it(client):
    main.app.dependency_overrides[main.get_wishlist_provider] = DisabledWishlist

    assert client.get("/api/config").json()["requests_enabled"] is False
    assert client.post("/api/elections/requests", json=BODY, headers=FREE).status_code == 503


def test_the_page_is_told_the_tracker_is_there(client):
    assert client.get("/api/config").json()["requests_enabled"] is True


# --- payments, while they are down ------------------------------------------

class SellingBilling:
    enabled = True

    def __init__(self):
        self.checkouts = []

    def tiers(self):
        return (Tier.BASIC,)

    def checkout(self, **kwargs):
        self.checkouts.append(kwargs)
        return CheckoutSession(url="https://checkout.test/basic")

    def portal(self, *, customer_id, return_url):
        return f"https://portal.test/{customer_id}"

    def event_from_webhook(self, payload, signature):
        return None


@pytest.fixture
def selling(client):
    billing = SellingBilling()
    main.app.dependency_overrides[main.get_billing_provider] = lambda: billing
    return billing


def test_checkout_is_closed_and_says_when_to_come_back(client, selling, monkeypatch):
    """The default while the card form is broken: PAYMENTS_PAUSED is unset here."""
    refused = client.post("/api/billing/checkout", json={"tier": "basic"}, headers=FREE)

    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert "try again tomorrow" in detail
    assert "issue tracker" in detail, "and where to go meanwhile"
    assert selling.checkouts == [], "Stripe was never asked"


def test_nothing_is_purchasable_while_checkout_is_closed(client, selling):
    """Said in the config too, so a page cannot offer a button that 503s."""
    config = client.get("/api/config").json()

    assert config["payments_paused"] is True
    assert [row["purchasable"] for row in config["tiers"]] == [False, False, False]


def test_an_existing_subscriber_can_still_reach_the_portal(client, selling, accounts):
    """Paying is closed; leaving is not. Trapping a subscriber would be worse."""
    accounts.ensure("paid-1", "paid@example.org")
    accounts.set_subscription("paid-1", Tier.BASIC, customer_id="cus_1", status="active")

    opened = client.post("/api/billing/portal", headers=SUBSCRIBER)

    assert opened.status_code == 200
    assert opened.json()["url"] == "https://portal.test/cus_1"


def test_one_variable_opens_checkout_again(client, selling, monkeypatch):
    monkeypatch.setenv("PAYMENTS_PAUSED", "false")

    started = client.post("/api/billing/checkout", json={"tier": "basic"}, headers=FREE)

    assert started.status_code == 200
    assert started.json()["url"] == "https://checkout.test/basic"
    assert client.get("/api/config").json()["payments_paused"] is False
