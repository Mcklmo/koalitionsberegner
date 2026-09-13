"""Turning Stripe events into entitlements.

The mapping is a pure function over the JSON Stripe sends, so the cases that
matter — a lapsed card, a downgrade, a cancellation, an event we do not care
about — are all testable without Stripe, a network, or a signing secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app.accounts import Tier
from app.billing import (
    BillingUnavailable,
    DisabledBilling,
    StripeBilling,
    parse_event,
)

BASIC_PRICE = "price_basic"
PREMIUM_PRICE = "price_premium"
PRICE_TIERS = {BASIC_PRICE: Tier.BASIC, PREMIUM_PRICE: Tier.PREMIUM}


def subscription_event(
    event_type="customer.subscription.updated",
    *,
    price=PREMIUM_PRICE,
    status="active",
    customer="cus_1",
    uid="uid-1",
):
    return {
        "type": event_type,
        "data": {
            "object": {
                "id": "sub_1",
                "customer": customer,
                "status": status,
                "metadata": {"uid": uid} if uid else {},
                "items": {"data": [{"price": {"id": price}}]},
            }
        },
    }


# --- reading the events -----------------------------------------------------

def test_an_active_subscription_grants_the_tier_its_price_names():
    event = parse_event(subscription_event(price=BASIC_PRICE), PRICE_TIERS)

    assert event.tier is Tier.BASIC
    assert event.uid == "uid-1"
    assert event.customer_id == "cus_1"
    assert event.subscription_id == "sub_1"
    assert event.status == "active"


def test_a_trial_counts_as_paid():
    assert parse_event(subscription_event(status="trialing"), PRICE_TIERS).tier is Tier.PREMIUM


@pytest.mark.parametrize("status", ["past_due", "unpaid", "incomplete", "canceled", "paused"])
def test_a_subscription_that_is_not_being_paid_for_drops_to_free(status):
    """The billing change that has to take effect on its own: a card stops working."""
    assert parse_event(subscription_event(status=status), PRICE_TIERS).tier is Tier.FREE


def test_a_deleted_subscription_drops_to_free_whatever_it_says_about_status():
    event = parse_event(
        subscription_event("customer.subscription.deleted", status="active"), PRICE_TIERS
    )
    assert event.tier is Tier.FREE


def test_a_downgrade_is_just_the_new_price():
    event = parse_event(subscription_event(price=BASIC_PRICE), PRICE_TIERS)
    assert event.tier is Tier.BASIC


def test_a_price_we_do_not_recognise_grants_nothing():
    """A price added in the Stripe dashboard but not configured here must not upgrade anyone."""
    event = parse_event(subscription_event(price="price_mystery"), PRICE_TIERS)
    assert event.tier is Tier.FREE


def test_a_completed_checkout_links_the_customer_but_grants_no_tier():
    """Paying is not the same as having an active subscription; the next event decides."""
    event = parse_event(
        {
            "type": "checkout.session.completed",
            "data": {"object": {"customer": "cus_2", "client_reference_id": "uid-9"}},
        },
        PRICE_TIERS,
    )

    assert event.uid == "uid-9"
    assert event.customer_id == "cus_2"
    assert event.tier is None


def test_a_checkout_carries_the_uid_from_metadata_when_there_is_no_reference():
    event = parse_event(
        {
            "type": "checkout.session.completed",
            "data": {"object": {"customer": {"id": "cus_3"}, "metadata": {"uid": "uid-3"}}},
        },
        PRICE_TIERS,
    )
    assert (event.uid, event.customer_id) == ("uid-3", "cus_3")


def test_a_subscription_without_our_metadata_still_names_its_customer():
    """Subscriptions created before the uid was attached are found by customer."""
    event = parse_event(subscription_event(uid=None), PRICE_TIERS)
    assert event.uid is None
    assert event.customer_id == "cus_1"


@pytest.mark.parametrize(
    "event_type",
    ["invoice.paid", "payment_intent.succeeded", "customer.created", "charge.refunded"],
)
def test_events_we_do_not_act_on_are_ignored(event_type):
    assert parse_event({"type": event_type, "data": {"object": {}}}, PRICE_TIERS) is None


# --- the Stripe client ------------------------------------------------------

class FakeStripe:
    """Just enough of the SDK: records calls, and can refuse a signature."""

    def __init__(self, *, event=None, signature_error=None, retrieve_error=None):
        self.created = []
        self.portals = []
        self.verified = []
        self.retrieved = []
        self.subscriptions = {}
        self._retrieve_error = retrieve_error
        self._event = event
        self._signature_error = signature_error
        outer = self

        class Sessions:
            @staticmethod
            def create(params):
                outer.created.append(params)
                return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}

        class PortalSessions:
            @staticmethod
            def create(params):
                outer.portals.append(params)
                return {"id": "bps_1", "url": "https://portal.stripe.test/bps_1"}

        class Subscriptions:
            @staticmethod
            def retrieve(subscription_id):
                outer.retrieved.append(subscription_id)
                if outer._retrieve_error:
                    raise outer._retrieve_error
                if subscription_id in outer.subscriptions:
                    return outer.subscriptions[subscription_id]
                # Unless a test says Stripe has moved on, it still says what the
                # event said.
                return json.loads(outer.verified[-1][0])["data"]["object"]

        class Client:
            # Shaped like the real SDK: resources hang off the ``v1`` namespace,
            # which is where StripeBilling reaches for them.
            def __init__(self, api_key):
                self.api_key = api_key
                self.v1 = type(
                    "V1",
                    (),
                    {
                        "checkout": type("C", (), {"sessions": Sessions})(),
                        "billing_portal": type("B", (), {"sessions": PortalSessions})(),
                        "subscriptions": Subscriptions,
                    },
                )()

        class Webhook:
            @staticmethod
            def construct_event(payload, signature, secret):
                outer.verified.append((payload, signature, secret))
                if outer._signature_error:
                    raise outer._signature_error
                return outer._event

        self.StripeClient = Client
        self.Webhook = Webhook


def billing(**kwargs) -> tuple[StripeBilling, FakeStripe]:
    fake = FakeStripe(**kwargs)
    return (
        StripeBilling(
            "sk_test",
            prices={Tier.BASIC: BASIC_PRICE, Tier.PREMIUM: PREMIUM_PRICE},
            webhook_secret="whsec_test",
            stripe=fake,
        ),
        fake,
    )


def test_checkout_attaches_the_uid_to_the_subscription_not_just_the_session():
    """So a later subscription event names the user without any lookup."""
    stripe_billing, fake = billing()

    session = stripe_billing.checkout(
        uid="uid-1",
        tier=Tier.PREMIUM,
        email="a@example.org",
        customer_id=None,
        success_url="https://app.test/?checkout=success",
        cancel_url="https://app.test/?checkout=cancelled",
    )

    params = fake.created[0]
    assert session.url == "https://checkout.stripe.test/cs_1"
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": PREMIUM_PRICE, "quantity": 1}]
    assert params["client_reference_id"] == "uid-1"
    assert params["subscription_data"]["metadata"]["uid"] == "uid-1"
    assert params["customer_email"] == "a@example.org"


def test_a_returning_subscriber_reuses_their_stripe_customer():
    stripe_billing, fake = billing()

    stripe_billing.checkout(
        uid="uid-1", tier=Tier.BASIC, email="a@example.org", customer_id="cus_7",
        success_url="https://app.test/s", cancel_url="https://app.test/c",
    )

    assert fake.created[0]["customer"] == "cus_7"
    assert "customer_email" not in fake.created[0], "one customer per account, not per upgrade"


def test_buying_a_tier_with_no_price_is_refused():
    stripe_billing = StripeBilling(
        "sk_test", prices={Tier.BASIC: BASIC_PRICE}, webhook_secret="w", stripe=FakeStripe()
    )
    assert stripe_billing.tiers() == (Tier.BASIC,)
    with pytest.raises(BillingUnavailable):
        stripe_billing.checkout(
            uid="u", tier=Tier.PREMIUM, email=None, customer_id=None,
            success_url="https://app.test/s", cancel_url="https://app.test/c",
        )


def test_the_portal_is_opened_for_the_stored_customer():
    stripe_billing, fake = billing()

    url = stripe_billing.portal(customer_id="cus_7", return_url="https://app.test/")

    assert url == "https://portal.stripe.test/bps_1"
    assert fake.portals[0] == {"customer": "cus_7", "return_url": "https://app.test/"}


def test_a_verified_webhook_is_translated():
    stripe_billing, fake = billing(event=subscription_event())
    payload = json.dumps(subscription_event()).encode()

    event = stripe_billing.event_from_webhook(payload, "t=1,v1=abc")

    assert event.tier is Tier.PREMIUM
    assert event.uid == "uid-1"
    # The body that was parsed is the body whose signature was checked.
    assert fake.verified == [(payload, "t=1,v1=abc", "whsec_test")]


def test_an_unsigned_webhook_never_reaches_the_parser():
    stripe_billing, fake = billing(event=subscription_event())
    with pytest.raises(ValueError, match="missing Stripe signature"):
        stripe_billing.event_from_webhook(b"{}", None)
    assert fake.verified == []


def test_a_forged_webhook_is_refused():
    stripe_billing, _ = billing(signature_error=ValueError("bad signature"))
    with pytest.raises(ValueError, match="bad signature"):
        stripe_billing.event_from_webhook(b"{}", "t=1,v1=forged")


def test_billing_needs_a_key_and_at_least_one_price():
    with pytest.raises(ValueError):
        StripeBilling("", prices={Tier.BASIC: BASIC_PRICE}, stripe=FakeStripe())
    with pytest.raises(ValueError):
        StripeBilling("sk_test", prices={Tier.BASIC: ""}, stripe=FakeStripe())


def test_the_fake_is_shaped_like_the_real_sdk():
    """The fake above is only worth anything if Stripe really looks like that.

    Without this, a rename or a deprecation on Stripe's side shows up in
    production rather than here — which is exactly what a hand-written double
    is prone to hiding.
    """
    stripe = pytest.importorskip("stripe")

    client = stripe.StripeClient("sk_test_dummy").v1
    assert callable(client.checkout.sessions.create)
    assert callable(client.billing_portal.sessions.create)
    assert callable(client.subscriptions.retrieve)
    assert callable(stripe.Webhook.construct_event)


def test_a_really_signed_webhook_grants_the_tier():
    """The whole path against the installed SDK, with a genuine signature.

    The fake above hands back a plain dict, which is the one shape this code
    never has to cope with in production. That hid an SDK change that made
    every real webhook a 400: `StripeObject` stopped being a `dict` and
    `to_dict_recursive()` became private. Checkout kept succeeding and no tier
    ever changed. This test signs a body the way Stripe does and runs it
    through the real `stripe` module, so the next such change fails here.
    """
    stripe = pytest.importorskip("stripe")

    secret = "whsec_testsecret"
    payload = json.dumps(subscription_event()).encode()
    timestamp = int(time.time())
    signed = hmac.new(
        secret.encode(), b"%d.%s" % (timestamp, payload), hashlib.sha256
    ).hexdigest()

    real = StripeBilling(
        "sk_test",
        prices={Tier.BASIC: BASIC_PRICE, Tier.PREMIUM: PREMIUM_PRICE},
        webhook_secret=secret,
        stripe=stripe,
    )
    # The one call that would leave the machine: Stripe's answer, as the real
    # SDK's own object rather than a dict, is what the webhook must cope with.
    current = stripe.Subscription.construct_from(
        subscription_event()["data"]["object"], "sk_test"
    )
    real._client = type("V1", (), {"subscriptions": type(
        "S", (), {"retrieve": staticmethod(lambda subscription_id: current)}
    )})()
    event = real.event_from_webhook(payload, f"t={timestamp},v1={signed}")

    assert event.tier is Tier.PREMIUM
    assert event.uid == "uid-1"
    assert event.status == "active"

    with pytest.raises(stripe.SignatureVerificationError):
        real.event_from_webhook(payload, f"t={timestamp},v1={'0' * 64}")


def test_without_stripe_nothing_is_for_sale():
    disabled = DisabledBilling()

    assert disabled.enabled is False
    assert disabled.tiers() == ()
    with pytest.raises(BillingUnavailable):
        disabled.checkout()
    with pytest.raises(BillingUnavailable):
        disabled.portal()


def signed(event: dict) -> bytes:
    return json.dumps(event).encode()


def test_a_subscription_event_applies_the_subscription_as_it_is_now():
    """Stripe does not deliver in order: the late "incomplete" must not win."""
    stripe_billing, fake = billing()
    late = subscription_event(status="incomplete")
    fake.subscriptions[late["data"]["object"]["id"]] = subscription_event(status="active")[
        "data"
    ]["object"]

    event = stripe_billing.event_from_webhook(signed(late), "t=1,v1=sig")

    assert event.status == "active"
    assert event.tier is Tier.PREMIUM


def test_a_subscription_that_cannot_be_read_back_is_retried_not_dropped():
    stripe_billing, _ = billing(retrieve_error=RuntimeError("stripe is down"))
    with pytest.raises(BillingUnavailable):
        stripe_billing.event_from_webhook(signed(subscription_event()), "t=1,v1=sig")


def test_a_completed_checkout_is_not_read_back():
    stripe_billing, fake = billing()
    checkout = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_1", "client_reference_id": "uid-1"}},
    }
    stripe_billing.event_from_webhook(signed(checkout), "t=1,v1=sig")
    assert fake.retrieved == []
