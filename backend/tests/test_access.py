"""Who may see what, and who may spend what — driven over HTTP.

The access model in one table:

===================  ==========================  =========================
Caller               Sees                        May import
===================  ==========================  =========================
signed out           the curated selection       no
free account         everything stored           no
basic / premium      everything stored           within a monthly quota
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
from app.accounts import InMemoryAccountStore, QuotaPolicy, Tier, billing_period
from app.auth import StubTokenVerifier
from app.billing import BillingEvent, CheckoutSession, DisabledBilling
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser, make_election

URL = "https://wahlergebnisse.sachsen-anhalt.de/"
OTHER_URL = "https://mirror.example.net/sachsen-anhalt"
BODY = {"source_url": URL}

BASIC_LIMIT = 2
PREMIUM_LIMIT = 5

VISITOR: dict[str, str] = {}
FREE = {"Authorization": "Bearer free-1:free@example.org"}
SUBSCRIBER = {"Authorization": "Bearer paid-1:paid@example.org"}
OTHER_SUBSCRIBER = {"Authorization": "Bearer paid-2:paid2@example.org"}
ADMIN = {"Authorization": "Bearer admin-1:admin@example.org:admin"}


@pytest.fixture
def parser():
    # Distinct identities per URL, so two saved pages are two stored elections
    # rather than one deduplicated one.
    return CountingParser(
        by_url={
            URL: make_election(nation="Deutschland", state="Sachsen-Anhalt",
                               election_date="2021-06-06"),
            OTHER_URL: make_election(nation="Danmark", election_date="2026-03-25"),
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
    overrides[main.get_token_verifier] = StubTokenVerifier
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
        f"/api/elections/pages/{previewed['page_key']}/confirm", headers=headers
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def used(accounts, uid):
    return accounts.get(uid).used_in(billing_period())


# --- viewing ----------------------------------------------------------------

def test_a_visitor_sees_only_the_curated_selection(client, store, accounts):
    uid = subscribe(accounts, SUBSCRIBER)
    hidden = save(client, SUBSCRIBER)
    shown = save(client, SUBSCRIBER, {"source_url": OTHER_URL})
    assert used(accounts, uid) <= BASIC_LIMIT
    store.set_selected(shown["election_hash"], True)

    listed = client.get("/api/elections", headers=VISITOR).json()

    assert [row["election_hash"] for row in listed] == [shown["election_hash"]]
    assert hidden["election_hash"] not in [row["election_hash"] for row in listed]


def test_an_account_sees_everything_stored(client, store, accounts):
    subscribe(accounts, SUBSCRIBER)
    save(client, SUBSCRIBER)

    assert client.get("/api/elections", headers=VISITOR).json() == []
    assert len(client.get("/api/elections", headers=FREE).json()) == 1


def test_a_visitor_may_open_a_selected_election_but_not_an_unselected_one(
    client, store, accounts
):
    subscribe(accounts, SUBSCRIBER)
    saved = save(client, SUBSCRIBER)
    election_hash = saved["election_hash"]

    refused = client.get(f"/api/elections/{election_hash}", headers=VISITOR)
    assert refused.status_code == 401
    assert "sign in" in refused.json()["detail"]

    store.set_selected(election_hash, True)
    assert client.get(f"/api/elections/{election_hash}", headers=VISITOR).status_code == 200


def test_a_free_account_may_open_any_stored_election(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    saved = save(client, SUBSCRIBER)

    response = client.get(f"/api/elections/{saved['election_hash']}", headers=FREE)

    assert response.status_code == 200
    assert response.json()["election"]["title"] == "Koalitionsberegner"


def test_a_forged_credential_is_refused_rather_than_treated_as_a_visitor(client):
    response = client.get("/api/elections", headers={"Authorization": "Bearer :nonsense"})
    assert response.status_code == 401


# --- importing --------------------------------------------------------------

def test_a_visitor_cannot_import(client, parser):
    response = client.post("/api/elections/import", json=BODY, headers=VISITOR)

    assert response.status_code == 401
    assert parser.call_count == 0, "no page is fetched for an unauthenticated caller"


def test_a_free_account_cannot_import(client, parser):
    response = client.post("/api/elections/import", json=BODY, headers=FREE)

    assert response.status_code == 402, "an account is free; importing is what is sold"
    assert "subscribe" in response.json()["detail"]
    assert parser.call_count == 0


def test_a_subscriber_imports_within_the_monthly_quota(client, accounts, parser):
    uid = subscribe(accounts, SUBSCRIBER)

    body = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                       headers=SUBSCRIBER).json()

    assert body["state"] == "preview"
    assert parser.call_count == 1
    assert used(accounts, uid) == 1


def test_the_quota_is_enforced_on_the_server_not_the_client(client, accounts, parser):
    uid = subscribe(accounts, SUBSCRIBER)
    urls = [f"https://results.example.org/{n}" for n in range(BASIC_LIMIT + 2)]

    statuses = [
        client.post("/api/elections/import?wait_seconds=2",
                    json={"source_url": url}, headers=SUBSCRIBER).status_code
        for url in urls
    ]

    assert statuses == [200] * BASIC_LIMIT + [429, 429]
    assert parser.call_count == BASIC_LIMIT, "nothing is fetched once the quota is spent"
    assert used(accounts, uid) == BASIC_LIMIT


def test_a_spent_quota_says_when_it_comes_back(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    for n in range(BASIC_LIMIT):
        client.post("/api/elections/import?wait_seconds=2",
                    json={"source_url": f"https://results.example.org/{n}"},
                    headers=SUBSCRIBER)

    refused = client.post("/api/elections/import", json=BODY, headers=SUBSCRIBER)

    assert refused.status_code == 429
    assert "next month" in refused.json()["detail"]


def test_premium_gets_a_larger_allowance_than_basic(client, accounts):
    basic = subscribe(accounts, SUBSCRIBER, Tier.BASIC)
    premium = subscribe(accounts, OTHER_SUBSCRIBER, Tier.PREMIUM)

    assert client.get("/api/me", headers=SUBSCRIBER).json()["limit"] == BASIC_LIMIT
    assert client.get("/api/me", headers=OTHER_SUBSCRIBER).json()["limit"] == PREMIUM_LIMIT
    assert used(accounts, basic) == used(accounts, premium) == 0


def test_one_quota_is_not_another_users(client, accounts):
    first = subscribe(accounts, SUBSCRIBER)
    second = subscribe(accounts, OTHER_SUBSCRIBER)

    client.post("/api/elections/import?wait_seconds=2", json=BODY, headers=SUBSCRIBER)

    assert used(accounts, first) == 1
    assert used(accounts, second) == 0


def test_the_allowance_comes_back_the_next_month(client, accounts):
    """No scheduled job resets anything; last month's counter simply stops counting."""
    uid = subscribe(accounts, SUBSCRIBER)
    accounts.reserve_import(uid, "2020-01", BASIC_LIMIT)
    accounts.reserve_import(uid, "2020-01", BASIC_LIMIT)

    response = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                           headers=SUBSCRIBER)

    assert response.status_code == 200
    assert used(accounts, uid) == 1


# --- what does not cost quota ----------------------------------------------

def test_an_election_already_stored_costs_nothing(client, accounts, parser):
    """The point of a shared store: the second person to want an election pays nothing."""
    owner = subscribe(accounts, SUBSCRIBER)
    save(client, SUBSCRIBER)
    calls_after_first = parser.call_count
    reader = subscribe(accounts, OTHER_SUBSCRIBER)

    reused = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                         headers=OTHER_SUBSCRIBER).json()

    assert reused["state"] == "ready"
    assert reused["reused"] is True
    assert parser.call_count == calls_after_first, "nothing was fetched or read"
    assert used(accounts, reader) == 0, "a stored election is free"
    assert used(accounts, owner) == 1, "the person who paid to read it still paid"


def test_joining_an_extraction_somebody_else_started_costs_nothing(client, accounts, parser):
    first = subscribe(accounts, SUBSCRIBER)
    second = subscribe(accounts, OTHER_SUBSCRIBER)
    parser.hold()
    client.post("/api/elections/import", json=BODY, headers=SUBSCRIBER)

    attached = client.post("/api/elections/import", json=BODY, headers=OTHER_SUBSCRIBER).json()
    parser.release()

    assert attached["reused"] is True
    assert parser.call_count == 1, "one extraction, however many callers want it"
    assert used(accounts, first) == 1
    assert used(accounts, second) == 0


def test_a_failed_extraction_is_refunded(client, accounts, store):
    """An unreadable page produced nothing; charging for it would punish bad luck."""
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(
        store, CountingParser(fail_times=1)
    )
    uid = subscribe(accounts, SUBSCRIBER)

    failed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                         headers=SUBSCRIBER).json()

    assert failed["state"] == "failed"
    assert used(accounts, uid) == 0, "the quota is back by the time the failure is visible"


def test_a_malformed_url_costs_nothing(client, accounts, parser):
    uid = subscribe(accounts, SUBSCRIBER)

    refused = client.post("/api/elections/import",
                          json={"source_url": "javascript:alert(1)"}, headers=SUBSCRIBER)

    assert refused.status_code == 422
    assert parser.call_count == 0
    assert used(accounts, uid) == 0


def test_looking_up_a_page_costs_nothing_and_needs_only_an_account(client, accounts, parser):
    uid = subscribe(accounts, SUBSCRIBER)

    assert client.get("/api/elections/lookup", params={"source_url": URL},
                      headers=FREE).status_code == 200
    assert client.get("/api/elections/lookup", params={"source_url": URL},
                      headers=VISITOR).status_code == 401
    assert parser.call_count == 0
    assert used(accounts, uid) == 0


def test_confirming_a_preview_needs_an_account_but_no_further_payment(client, accounts):
    """The extraction has already been paid for; saving what it read is not a second sale."""
    subscribe(accounts, SUBSCRIBER)
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                            headers=SUBSCRIBER).json()
    page = previewed["page_key"]

    assert client.post(f"/api/elections/pages/{page}/confirm", headers=VISITOR).status_code == 401
    assert client.post(f"/api/elections/pages/{page}/confirm", headers=FREE).status_code == 200


def test_discarding_a_preview_needs_an_account(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                            headers=SUBSCRIBER).json()
    page = previewed["page_key"]

    assert client.delete(f"/api/elections/pages/{page}/preview",
                         headers=VISITOR).status_code == 401
    assert client.delete(f"/api/elections/pages/{page}/preview",
                         headers=FREE).status_code == 204


# --- the account endpoint ---------------------------------------------------

def test_me_reports_the_tier_and_what_is_left(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    client.post("/api/elections/import?wait_seconds=2", json=BODY, headers=SUBSCRIBER)

    body = client.get("/api/me", headers=SUBSCRIBER).json()

    assert body["tier"] == "basic"
    assert body["email"] == "paid@example.org"
    assert (body["used"], body["limit"], body["remaining"]) == (1, BASIC_LIMIT, BASIC_LIMIT - 1)
    assert body["may_import"] is True
    assert body["period"] == billing_period()


def test_me_on_a_first_sign_in_creates_a_free_account(client, accounts):
    body = client.get("/api/me", headers=FREE).json()

    assert body["tier"] == "free"
    assert body["may_import"] is False
    assert body["limit"] == 0
    assert accounts.get("free-1") is not None


def test_me_needs_a_credential(client):
    assert client.get("/api/me", headers=VISITOR).status_code == 401


def test_the_public_config_is_readable_signed_out(client):
    """The page has to know how to sign in before anyone has."""
    body = client.get("/api/config", headers=VISITOR).json()

    assert body["auth_required"] is True
    assert body["billing_enabled"] is False
    assert {t["tier"]: t["monthly_imports"] for t in body["tiers"]} == {
        "free": 0, "basic": BASIC_LIMIT, "premium": PREMIUM_LIMIT
    }


# --- curation ---------------------------------------------------------------

def test_only_an_administrator_may_curate_the_selection(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    saved = save(client, SUBSCRIBER)
    path = f"/api/elections/{saved['election_hash']}/selected"

    assert client.put(path, json={"selected": True}, headers=VISITOR).status_code == 401
    assert client.put(path, json={"selected": True}, headers=FREE).status_code == 403
    assert client.put(path, json={"selected": True}, headers=SUBSCRIBER).status_code == 403

    promoted = client.put(path, json={"selected": True}, headers=ADMIN)
    assert promoted.status_code == 200
    assert promoted.json()["selected"] is True


def test_curation_can_be_undone(client, accounts, store):
    subscribe(accounts, SUBSCRIBER)
    saved = save(client, SUBSCRIBER)
    path = f"/api/elections/{saved['election_hash']}/selected"

    client.put(path, json={"selected": True}, headers=ADMIN)
    client.put(path, json={"selected": False}, headers=ADMIN)

    assert client.get("/api/elections", headers=VISITOR).json() == []


def test_curating_an_unknown_election_is_404(client):
    response = client.put(
        "/api/elections/" + "0" * 64 + "/selected", json={"selected": True}, headers=ADMIN
    )
    assert response.status_code == 404


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
def billing_client(client, fake_billing):
    """The same client, with billing that reports whatever the test hands it.

    A separate fixture rather than a second definition of ``billing``: a
    redefined fixture would silently apply to every test in the module,
    including the ones asserting that nothing is for sale.
    """
    main.app.dependency_overrides[main.get_billing_provider] = lambda: fake_billing
    return client


def webhook(client):
    """Stripe posting a signed event. The body is irrelevant to the fake."""
    return client.post("/api/billing/webhook", content=b"{}").json()


def test_a_subscription_webhook_grants_the_tier_and_the_quota_with_it(
    billing_client, fake_billing
):
    billing_client.get("/api/me", headers=FREE)  # the account exists, still free
    refused = billing_client.post("/api/elections/import", json=BODY, headers=FREE)
    assert refused.status_code == 402

    fake_billing.event = BillingEvent(
        customer_id="cus_9", uid="free-1", tier=Tier.PREMIUM,
        status="active", subscription_id="sub_9",
    )
    assert webhook(billing_client) == {"handled": True}

    assert billing_client.get("/api/me", headers=FREE).json()["tier"] == "premium"
    allowed = billing_client.post(
        "/api/elections/import?wait_seconds=2", json=BODY, headers=FREE
    )
    assert allowed.status_code == 200, "the quota arrives with the tier"


def test_a_cancellation_takes_effect_without_anything_else_running(
    billing_client, accounts, fake_billing
):
    subscribe(accounts, SUBSCRIBER, Tier.PREMIUM)
    fake_billing.event = BillingEvent(
        customer_id="cus_paid-1", uid="paid-1", tier=Tier.FREE, status="canceled"
    )

    webhook(billing_client)

    assert billing_client.get("/api/me", headers=SUBSCRIBER).json()["tier"] == "free"
    refused = billing_client.post("/api/elections/import", json=BODY, headers=SUBSCRIBER)
    assert refused.status_code == 402


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
