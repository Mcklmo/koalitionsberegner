"""Who may see what, and who may spend what — driven over HTTP.

The access model in one table:

===================  ==========================  =========================
Caller               Sees                        May import
===================  ==========================  =========================
signed out           everything stored           no
free account         everything stored           no
basic / premium      everything stored           within a monthly quota
administrator        everything stored           without any quota
===================  ==========================  =========================

The quota rule these tests exist to pin down: an import costs a unit only when
it makes the server go and read a page it has never read. Anything served out
of the store, joined to somebody else's running extraction, or that failed
outright, costs nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.accounts import InMemoryAccountStore, QuotaPolicy, Tier, attempt_limit, billing_period
from app.auth import StoreBackedVerifier, StubCredentials
from app.billing import BillingEvent, CheckoutSession, DisabledBilling
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser, make_election

#: What a user asks for: a year, a country, and sometimes a region.
BODY = {"year": 2026, "nation": "Danmark"}
OTHER_BODY = {"year": 2021, "nation": "Deutschland", "subnation": "Sachsen-Anhalt"}

BASIC_LIMIT = 2
PREMIUM_LIMIT = 5

VISITOR: dict[str, str] = {}
FREE = {"Authorization": "Bearer free-1:free@example.org"}
SUBSCRIBER = {"Authorization": "Bearer paid-1:paid@example.org"}
OTHER_SUBSCRIBER = {"Authorization": "Bearer paid-2:paid2@example.org"}
ADMIN = {"Authorization": "Bearer admin-1:admin@example.org:admin"}
#: A sign-up whose owner has not opened the confirmation link yet.
UNVERIFIED = {"Authorization": "Bearer new-1:new@example.org:unverified"}


@pytest.fixture
def parser():
    # Distinct identities per year asked for, so two saved requests are two
    # stored elections rather than one deduplicated one.
    return CountingParser(
        by_year={
            2026: make_election(nation="Danmark", election_date="2026-03-25"),
            2021: make_election(nation="Deutschland", state="Sachsen-Anhalt",
                                election_date="2021-06-06"),
        }
    )


@pytest.fixture
def store():
    return InMemoryElectionStore()


@pytest.fixture
def accounts():
    return InMemoryAccountStore()


@pytest.fixture
def policy():
    return QuotaPolicy({Tier.FREE: 0, Tier.BASIC: BASIC_LIMIT, Tier.PREMIUM: PREMIUM_LIMIT})


@pytest.fixture
def billing():
    return DisabledBilling()


@pytest.fixture
def client(parser, store, accounts, policy, billing):
    overrides = main.app.dependency_overrides
    overrides[main.get_service] = lambda: ImportService(store, parser)
    overrides[main.get_token_verifier] = lambda: StoreBackedVerifier(StubCredentials())
    overrides[main.get_account_store] = lambda: accounts
    overrides[main.get_policy] = lambda: policy
    overrides[main.get_billing_provider] = lambda: billing
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


def subscribe(accounts, headers, tier=Tier.BASIC):
    """Put an account on a paid tier the way a webhook would."""
    uid = headers["Authorization"].split()[1].split(":")[0]
    accounts.ensure(uid, None)
    accounts.set_subscription(uid, tier, customer_id=f"cus_{uid}", status="active")
    return uid


def save(client, headers, body=None):
    """Take a URL through preview and confirmation into the store."""
    previewed = client.post("/api/elections/import?wait_seconds=2", json=body or BODY,
                            headers=headers).json()
    assert previewed["state"] == "preview", previewed
    saved = client.post(
        f"/api/elections/imports/{previewed['request_key']}/confirm", headers=headers
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def used(accounts, uid):
    return accounts.get(uid).used_in(billing_period())


# --- viewing ----------------------------------------------------------------

def test_a_visitor_sees_everything_stored(client, accounts):
    """No header at all: the whole list, not a curated part of it."""
    subscribe(accounts, SUBSCRIBER)
    first = save(client, SUBSCRIBER)
    second = save(client, SUBSCRIBER, OTHER_BODY)

    listed = client.get("/api/elections", headers=VISITOR).json()

    assert {row["election_hash"] for row in listed} == {
        first["election_hash"], second["election_hash"]
    }
    assert all("selected" not in row for row in listed), "curation is gone"


def test_a_visitor_may_open_any_stored_election(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    saved = save(client, SUBSCRIBER)

    response = client.get(f"/api/elections/{saved['election_hash']}", headers=VISITOR)

    assert response.status_code == 200
    assert response.json()["election"]["title"] == "Koalitionsberegner"


def test_an_unknown_election_is_404_to_anyone(client):
    response = client.get("/api/elections/" + "0" * 64, headers=VISITOR)
    assert response.status_code == 404


# --- the account endpoint ---------------------------------------------------

def test_me_on_a_first_sign_in_creates_a_free_account(client, accounts):
    body = client.get("/api/me", headers=FREE).json()

    assert body["tier"] == "free"
    assert body["may_import"] is False
    assert body["limit"] == 0
    assert accounts.get("free-1") is not None


def test_me_reports_no_limit_to_an_administrator(client):
    body = client.get("/api/me", headers=ADMIN).json()

    assert body["admin"] is True
    assert body["tier"] == "free", "administrator-ness is not a tier"
    assert (body["limit"], body["remaining"], body["may_import"]) == (-1, -1, True)


def test_me_needs_a_credential(client):
    assert client.get("/api/me", headers=VISITOR).status_code == 401


# --- an address nobody has confirmed yet ------------------------------------

def test_me_tells_an_unconfirmed_account_what_it_is_waiting_for(client):
    body = client.get("/api/me", headers=UNVERIFIED).json()

    assert body["email_verified"] is False
    assert body["may_import"] is False
    assert client.get("/api/me", headers=FREE).json()["email_verified"] is True


def test_an_unconfirmed_account_cannot_pay(billing_client, fake_billing):
    response = billing_client.post(
        "/api/billing/checkout", json={"tier": "basic"}, headers=UNVERIFIED
    )

    assert response.status_code == 403
    assert fake_billing.checkouts == []


# --- billing changes take effect on their own -------------------------------

class FakeBilling:
    """Billing that reports whatever the test hands it, with no signature."""

    enabled = True

    def __init__(self):
        self.event: BillingEvent | None = None
        self.checkouts: list[tuple] = []

    def tiers(self):
        return (Tier.BASIC, Tier.PREMIUM)

    def checkout(self, *, uid, tier, email, customer_id, success_url, cancel_url):
        self.checkouts.append((uid, tier, customer_id))
        return CheckoutSession(url=f"https://checkout.test/{tier.value}")

    def portal(self, *, customer_id, return_url):
        return f"https://portal.test/{customer_id}"

    def event_from_webhook(self, payload, signature):
        return self.event


@pytest.fixture
def fake_billing():
    return FakeBilling()


@pytest.fixture
def billing_client(client, fake_billing, monkeypatch):
    """The same client, with billing that reports whatever the test hands it.

    A separate fixture rather than a second definition of ``billing``: a
    redefined fixture would silently apply to every test in the module,
    including the ones asserting that nothing is for sale.

    Checkout is open here. It is closed by default while payments are being
    fixed (``PAYMENTS_PAUSED``), and these cases are about what Stripe is asked
    for once it works again — the pause itself is asserted on its own below.
    """
    monkeypatch.setenv("PAYMENTS_PAUSED", "false")
    main.app.dependency_overrides[main.get_billing_provider] = lambda: fake_billing
    return client


def webhook(client):
    """Stripe posting a signed event. The body is irrelevant to the fake."""
    return client.post("/api/billing/webhook", content=b"{}").json()


def test_a_subscription_webhook_grants_the_tier(
    billing_client, fake_billing
):
    billing_client.get("/api/me", headers=FREE)  # the account exists, still free

    fake_billing.event = BillingEvent(
        customer_id="cus_9", uid="free-1", tier=Tier.PREMIUM,
        status="active", subscription_id="sub_9",
    )
    assert webhook(billing_client) == {"handled": True}

    assert billing_client.get("/api/me", headers=FREE).json()["tier"] == "premium"


def test_a_cancellation_takes_effect_without_anything_else_running(
    billing_client, accounts, fake_billing
):
    subscribe(accounts, SUBSCRIBER, Tier.PREMIUM)
    fake_billing.event = BillingEvent(
        customer_id="cus_paid-1", uid="paid-1", tier=Tier.FREE, status="canceled"
    )

    webhook(billing_client)

    assert billing_client.get("/api/me", headers=SUBSCRIBER).json()["tier"] == "free"


def test_a_webhook_that_names_only_a_customer_still_finds_the_account(
    billing_client, accounts, fake_billing
):
    """Subscriptions created before the uid was attached to their metadata."""
    subscribe(accounts, SUBSCRIBER, Tier.FREE)
    accounts.link_customer("paid-1", "cus_paid-1")
    fake_billing.event = BillingEvent(
        customer_id="cus_paid-1", uid=None, tier=Tier.BASIC, status="active"
    )

    assert webhook(billing_client) == {"handled": True}
    assert billing_client.get("/api/me", headers=SUBSCRIBER).json()["tier"] == "basic"


def test_a_webhook_for_nobody_we_know_changes_nothing(billing_client, fake_billing):
    fake_billing.event = BillingEvent(
        customer_id="cus_unknown", tier=Tier.PREMIUM, status="active"
    )
    assert webhook(billing_client) == {"handled": False}


def test_a_completed_checkout_only_links_the_customer(
    billing_client, accounts, fake_billing
):
    billing_client.get("/api/me", headers=FREE)
    fake_billing.event = BillingEvent(customer_id="cus_5", uid="free-1", tier=None)

    assert webhook(billing_client) == {"handled": True}

    assert accounts.get("free-1").stripe_customer_id == "cus_5"
    assert accounts.get("free-1").tier is Tier.FREE, "paying is not yet a subscription"


def test_the_webhook_is_the_only_way_a_tier_changes(
    billing_client, accounts, fake_billing
):
    """A client cannot buy itself an upgrade by asking for one."""
    billing_client.get("/api/me", headers=FREE)

    billing_client.post("/api/billing/checkout", json={"tier": "premium"}, headers=FREE)

    assert accounts.get("free-1").tier is Tier.FREE
    assert fake_billing.checkouts == [("free-1", Tier.PREMIUM, None)]


def test_checkout_needs_an_account_and_a_tier_that_is_for_sale(billing_client):
    anonymous = billing_client.post(
        "/api/billing/checkout", json={"tier": "premium"}, headers=VISITOR
    )
    assert anonymous.status_code == 401

    not_for_sale = billing_client.post(
        "/api/billing/checkout", json={"tier": "free"}, headers=FREE
    )
    assert not_for_sale.status_code == 400


def test_the_portal_needs_a_customer_to_open_it_for(billing_client, accounts):
    assert billing_client.post("/api/billing/portal", headers=FREE).status_code == 409

    subscribe(accounts, SUBSCRIBER)
    opened = billing_client.post("/api/billing/portal", headers=SUBSCRIBER)

    assert opened.status_code == 200
    assert opened.json()["url"] == "https://portal.test/cus_paid-1"


def test_a_subscriber_changes_tier_in_the_portal_not_in_a_second_checkout(
    billing_client, accounts, fake_billing
):
    """A second checkout is a second subscription, charged alongside the first."""
    subscribe(accounts, SUBSCRIBER)

    refused = billing_client.post(
        "/api/billing/checkout", json={"tier": "premium"}, headers=SUBSCRIBER
    )

    assert refused.status_code == 409
    assert fake_billing.checkouts == []


def test_a_lapsed_subscriber_may_check_out_again(billing_client, accounts, fake_billing):
    subscribe(accounts, SUBSCRIBER)
    accounts.set_subscription("paid-1", Tier.FREE, status="canceled")

    again = billing_client.post("/api/billing/checkout", json={"tier": "basic"}, headers=SUBSCRIBER)

    assert again.status_code == 200


def test_cancelling_a_replaced_subscription_does_not_end_the_current_one(
    billing_client, accounts, fake_billing
):
    """Stripe may report the old one's end after the new one has started."""
    billing_client.get("/api/me", headers=SUBSCRIBER)
    accounts.set_subscription("paid-1", Tier.PREMIUM, subscription_id="sub_new", status="active")

    fake_billing.event = BillingEvent(
        customer_id="cus_1", uid="paid-1", tier=Tier.FREE,
        status="canceled", subscription_id="sub_old",
    )
    webhook(billing_client)
    assert billing_client.get("/api/me", headers=SUBSCRIBER).json()["tier"] == "premium"

    fake_billing.event = BillingEvent(
        customer_id="cus_1", uid="paid-1", tier=Tier.FREE,
        status="canceled", subscription_id="sub_new",
    )
    webhook(billing_client)
    assert billing_client.get("/api/me", headers=SUBSCRIBER).json()["tier"] == "free"
