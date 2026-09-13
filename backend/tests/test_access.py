"""Who may see what, and who may spend what — driven over HTTP.

The access model in one table:

===================  ==========================  =========================
Caller               Sees                        May import
===================  ==========================  =========================
signed out           the curated selection       no
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

def test_a_visitor_sees_only_the_curated_selection(client, store, accounts):
    uid = subscribe(accounts, SUBSCRIBER)
    hidden = save(client, SUBSCRIBER)
    shown = save(client, SUBSCRIBER, OTHER_BODY)
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
    asked = [{"year": 2000 + n, "nation": "Danmark"} for n in range(BASIC_LIMIT + 2)]

    statuses = [
        client.post("/api/elections/import?wait_seconds=2",
                    json=body, headers=SUBSCRIBER).status_code
        for body in asked
    ]

    assert statuses == [200] * BASIC_LIMIT + [429, 429]
    assert parser.call_count == BASIC_LIMIT, "nothing is looked up once the quota is spent"
    assert used(accounts, uid) == BASIC_LIMIT


def test_a_spent_quota_says_when_it_comes_back(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    for n in range(BASIC_LIMIT):
        client.post("/api/elections/import?wait_seconds=2",
                    json={"year": 2000 + n, "nation": "Danmark"},
                    headers=SUBSCRIBER)

    refused = client.post("/api/elections/import", json=BODY, headers=SUBSCRIBER)

    assert refused.status_code == 429
    assert "next month" in refused.json()["detail"]


def test_an_administrator_imports_without_a_quota(client, accounts, parser):
    """On the free tier, which may import nothing, and more often than basic may."""
    for body in (BODY, OTHER_BODY, {"year": 2022, "nation": "Danmark"}):
        response = client.post("/api/elections/import?wait_seconds=2", json=body, headers=ADMIN)
        assert response.status_code == 200, response.text

    assert used(accounts, "admin-1") == 0


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


def test_failed_extractions_are_refunded_only_so_often(client, accounts, store):
    """Every failure still ran the resolver and a model over pages.

    Refunding each one without end would make the cheapest subscription an
    unlimited budget for model calls — a year that does not exist costs
    exactly as much to look for as one that does.
    """
    parser = CountingParser(fail_times=1000)
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(store, parser)
    uid = subscribe(accounts, SUBSCRIBER)
    allowed = attempt_limit(BASIC_LIMIT)

    for _ in range(allowed):
        failed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                             headers=SUBSCRIBER).json()
        assert failed["state"] == "failed"
    assert used(accounts, uid) == 0

    refused = client.post("/api/elections/import", json=BODY, headers=SUBSCRIBER)

    assert refused.status_code == 429
    assert "found no election" in refused.json()["detail"]
    assert parser.call_count == allowed
    assert client.get("/api/me", headers=SUBSCRIBER).json()["may_import"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"year": "not-a-year", "nation": "Danmark"},
        {"year": 20226, "nation": "Danmark"},
        {"year": 2026, "nation": "   "},
        {"year": 2026},
        {"year": 2026, "nation": "Danmark", "source_url": "https://x.example"},
    ],
)
def test_a_request_that_is_not_one_costs_nothing(client, accounts, parser, body):
    uid = subscribe(accounts, SUBSCRIBER)

    refused = client.post("/api/elections/import", json=body, headers=SUBSCRIBER)

    assert refused.status_code == 422
    assert parser.call_count == 0
    assert used(accounts, uid) == 0


def test_looking_up_a_request_costs_nothing_and_needs_only_an_account(client, accounts, parser):
    uid = subscribe(accounts, SUBSCRIBER)

    assert client.get("/api/elections/lookup", params=BODY,
                      headers=FREE).status_code == 200
    assert client.get("/api/elections/lookup", params=BODY,
                      headers=VISITOR).status_code == 401
    assert parser.call_count == 0
    assert used(accounts, uid) == 0


def test_confirming_a_preview_needs_its_importer_but_no_further_payment(client, accounts):
    """The extraction has already been paid for; saving what it read is not a second sale.

    Nor is it anybody else's to save: a preview its importer would have rejected
    is wrong numbers for every account.
    """
    subscribe(accounts, SUBSCRIBER)
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                            headers=SUBSCRIBER).json()
    url = f"/api/elections/imports/{previewed['request_key']}/confirm"

    assert client.post(url, headers=VISITOR).status_code == 401
    assert client.post(url, headers=FREE).status_code == 403
    assert client.get("/api/elections", headers=FREE).json() == [], "nothing was saved"

    accounts.set_subscription("paid-1", Tier.FREE, status="canceled")
    assert client.post(url, headers=SUBSCRIBER).status_code == 200


def test_an_administrator_may_confirm_anyones_preview(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                            headers=SUBSCRIBER).json()

    confirmed = client.post(
        f"/api/elections/imports/{previewed['request_key']}/confirm", headers=ADMIN
    )

    assert confirmed.status_code == 200


def test_discarding_a_preview_needs_the_account_that_started_it(client, accounts):
    """The request key follows from the year and the place, so anyone can know it.

    The preview is still not theirs to throw away: the subscriber paid for it,
    and importing again would charge them again.
    """
    subscribe(accounts, SUBSCRIBER)
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                            headers=SUBSCRIBER).json()
    key = previewed["request_key"]
    url = f"/api/elections/imports/{key}/preview"

    assert client.delete(url, headers=VISITOR).status_code == 401
    assert client.delete(url, headers=FREE).status_code == 403
    assert client.get(f"/api/elections/imports/{key}", headers=FREE).json()["state"] == "preview"
    assert client.delete(url, headers=SUBSCRIBER).status_code == 204


def test_an_administrator_may_discard_anyones_preview(client, accounts):
    subscribe(accounts, SUBSCRIBER)
    previewed = client.post("/api/elections/import?wait_seconds=2", json=BODY,
                            headers=SUBSCRIBER).json()

    discarded = client.delete(
        f"/api/elections/imports/{previewed['request_key']}/preview", headers=ADMIN
    )

    assert discarded.status_code == 204


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


def test_me_reports_no_limit_to_an_administrator(client):
    body = client.get("/api/me", headers=ADMIN).json()

    assert body["admin"] is True
    assert body["tier"] == "free", "administrator-ness is not a tier"
    assert (body["limit"], body["remaining"], body["may_import"]) == (-1, -1, True)


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


# --- an address nobody has confirmed yet ------------------------------------

def test_me_tells_an_unconfirmed_account_what_it_is_waiting_for(client):
    body = client.get("/api/me", headers=UNVERIFIED).json()

    assert body["email_verified"] is False
    assert body["may_import"] is False
    assert client.get("/api/me", headers=FREE).json()["email_verified"] is True


def test_an_unconfirmed_account_cannot_import_even_on_a_paid_tier(client, accounts, parser):
    uid = subscribe(accounts, UNVERIFIED)

    response = client.post("/api/elections/import", json=BODY, headers=UNVERIFIED)

    assert response.status_code == 403
    assert "confirm your email" in response.json()["detail"]
    assert parser.call_count == 0, "no page is fetched on an unconfirmed address's behalf"
    assert used(accounts, uid) == 0


def test_an_unconfirmed_account_cannot_look_up_or_confirm_imports(client):
    lookup = client.get("/api/elections/lookup?year=2026&nation=Danmark", headers=UNVERIFIED)
    confirm = client.post("/api/elections/imports/some-key/confirm", headers=UNVERIFIED)

    assert (lookup.status_code, confirm.status_code) == (403, 403)


def test_an_unconfirmed_account_sees_what_a_visitor_sees(client, store, accounts):
    subscribe(accounts, SUBSCRIBER)
    saved = save(client, SUBSCRIBER)
    path = f"/api/elections/{saved['election_hash']}"

    assert client.get("/api/elections", headers=UNVERIFIED).json() == []
    assert client.get(path, headers=UNVERIFIED).status_code == 403

    store.set_selected(saved["election_hash"], True)
    assert client.get(path, headers=UNVERIFIED).status_code == 200


def test_an_unconfirmed_account_cannot_pay(billing_client, fake_billing):
    response = billing_client.post(
        "/api/billing/checkout", json={"tier": "basic"}, headers=UNVERIFIED
    )

    assert response.status_code == 403
    assert fake_billing.checkouts == []


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
