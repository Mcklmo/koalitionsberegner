"""Subscriptions: Stripe Checkout in, Stripe webhooks back.

The tier a user has is never decided here or in the browser — it is whatever
Stripe last told us over a signed webhook, written to the account store. A
client that says "I am premium" is simply ignored, and a cancellation takes
effect the moment Stripe reports it, with no scheduled job in between.

Two kinds of event matter:

* ``checkout.session.completed`` ties a Stripe customer to one of our uids.
* ``customer.subscription.*`` carries the entitlement — which price, and
  whether it is currently paid for.

Both are translated into one :class:`BillingEvent` by :func:`parse_event`, a
pure function over the JSON Stripe sends, so the mapping from prices to tiers
is testable without Stripe, a network, or a signing secret.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .accounts import Tier
from .observability import io_span, scrub

log = logging.getLogger(__name__)

#: Subscription statuses that entitle the user to their tier. Anything else —
#: ``past_due``, ``unpaid``, ``canceled``, ``incomplete`` — drops them to free.
ENTITLING_STATUSES = frozenset({"active", "trialing"})

#: Events worth acting on; every other event type is acknowledged and dropped.
HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)


class BillingUnavailable(Exception):
    """Billing is not configured, so nothing can be bought."""


@dataclass(frozen=True)
class CheckoutSession:
    url: str
    session_id: str | None = None


@dataclass(frozen=True)
class BillingEvent:
    """What one webhook says about one user's entitlement.

    ``uid`` is present whenever Stripe echoed back the identity we attached at
    checkout, which is what lets a subscription event be applied even if it
    arrives before the checkout event that introduced the customer.
    """

    customer_id: str | None = None
    uid: str | None = None
    tier: Tier | None = None
    """``None`` on a pure customer/uid link, which carries no entitlement."""
    status: str | None = None
    subscription_id: str | None = None


class Billing(Protocol):
    enabled: bool

    def tiers(self) -> tuple[Tier, ...]:
        """Tiers that can actually be bought, i.e. that have a price configured."""
        ...

    def checkout(
        self,
        *,
        uid: str,
        tier: Tier,
        email: str | None,
        customer_id: str | None,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession: ...

    def portal(self, *, customer_id: str, return_url: str) -> str:
        """A Stripe-hosted page for changing or cancelling the subscription."""
        ...

    def event_from_webhook(self, payload: bytes, signature: str | None) -> BillingEvent | None:
        """Verify the signature and translate the event. ``None`` if uninteresting."""
        ...


def _first_price_id(subscription: Mapping) -> str | None:
    items = (subscription.get("items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or {}
        if price.get("id"):
            return str(price["id"])
    return None


def _customer_id(obj: Mapping) -> str | None:
    """Stripe sends the customer either as an id or as an expanded object."""
    customer = obj.get("customer")
    if isinstance(customer, str):
        return customer or None
    if isinstance(customer, Mapping):
        return str(customer.get("id")) if customer.get("id") else None
    return None


def parse_event(event: Mapping, price_tiers: Mapping[str, Tier]) -> BillingEvent | None:
    """Translate a Stripe event into an entitlement change, or ``None``."""
    event_type = event.get("type")
    if event_type not in HANDLED_EVENTS:
        return None
    obj = ((event.get("data") or {}).get("object")) or {}

    if event_type == "checkout.session.completed":
        customer_id = _customer_id(obj)
        uid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("uid")
        if not customer_id and not uid:
            return None
        # Deliberately carries no tier: the subscription events decide that, so
        # a completed checkout that never became a paid subscription grants
        # nothing.
        return BillingEvent(customer_id=customer_id, uid=str(uid) if uid else None)

    status = str(obj.get("status") or "")
    price_id = _first_price_id(obj)
    entitled = event_type != "customer.subscription.deleted" and status in ENTITLING_STATUSES
    tier = price_tiers.get(price_id or "", Tier.FREE) if entitled else Tier.FREE
    uid = (obj.get("metadata") or {}).get("uid")
    return BillingEvent(
        customer_id=_customer_id(obj),
        uid=str(uid) if uid else None,
        tier=tier,
        status=status or ("canceled" if event_type.endswith("deleted") else None),
        subscription_id=str(obj.get("id")) if obj.get("id") else None,
    )


class DisabledBilling:
    """No Stripe configured: viewing and free accounts work, nothing is for sale."""

    enabled = False

    def tiers(self) -> tuple[Tier, ...]:
        return ()

    def checkout(self, **_) -> CheckoutSession:
        raise BillingUnavailable("subscriptions are not configured on this deployment")

    def portal(self, **_) -> str:
        raise BillingUnavailable("subscriptions are not configured on this deployment")

    def event_from_webhook(self, payload: bytes, signature: str | None) -> BillingEvent | None:
        raise BillingUnavailable("subscriptions are not configured on this deployment")


class StripeBilling:
    """Stripe Checkout and the billing portal, plus signed webhook parsing.

    The uid is attached to the *subscription*, not just the checkout session, so
    every later ``customer.subscription.*`` event names the user it belongs to
    without a lookup — and without depending on the order Stripe delivers in.
    """

    enabled = True

    def __init__(
        self,
        api_key: str,
        *,
        prices: Mapping[Tier, str],
        webhook_secret: str | None = None,
        stripe=None,
    ):
        if not api_key:
            raise ValueError("Stripe billing needs an API key")
        self._prices = {tier: price for tier, price in prices.items() if price}
        if not self._prices:
            raise ValueError("Stripe billing needs at least one configured price")
        self._price_tiers = {price: tier for tier, price in self._prices.items()}
        self._webhook_secret = webhook_secret
        self._stripe = stripe or self._import_stripe()
        # The v1 namespace, not the flat one: the flat accessors still work but
        # are deprecated, and the whole point of pinning a floor is not to build
        # on something already on its way out.
        self._client = self._stripe.StripeClient(api_key).v1

    @staticmethod
    def _import_stripe():
        import stripe

        return stripe

    def tiers(self) -> tuple[Tier, ...]:
        return tuple(self._prices)

    def checkout(
        self,
        *,
        uid: str,
        tier: Tier,
        email: str | None,
        customer_id: str | None,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        price = self._prices.get(tier)
        if price is None:
            raise BillingUnavailable(f"no price configured for the {tier.value} tier")

        params = {
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": uid,
            "metadata": {"uid": uid},
            # Carried onto the subscription, so later events identify the user.
            "subscription_data": {"metadata": {"uid": uid}},
        }
        # Reusing the customer keeps one Stripe customer per account instead of
        # a new one per upgrade, which is what makes the portal show a history.
        if customer_id:
            params["customer"] = customer_id
        elif email:
            params["customer_email"] = email

        with io_span(log, "stripe", "checkout", uid=uid[:12], tier=tier.value) as span:
            session = self._client.checkout.sessions.create(params=params)
            url = self._field(session, "url")
            span["session"] = self._field(session, "id")
            if not url:
                raise BillingUnavailable("Stripe returned no checkout URL")
            return CheckoutSession(url=url, session_id=self._field(session, "id"))

    def portal(self, *, customer_id: str, return_url: str) -> str:
        with io_span(log, "stripe", "portal", customer=customer_id[:12]):
            session = self._client.billing_portal.sessions.create(
                params={"customer": customer_id, "return_url": return_url}
            )
            url = self._field(session, "url")
            if not url:
                raise BillingUnavailable("Stripe returned no portal URL")
            return url

    def event_from_webhook(self, payload: bytes, signature: str | None) -> BillingEvent | None:
        if not self._webhook_secret:
            raise BillingUnavailable("STRIPE_WEBHOOK_SECRET is not set")
        if not signature:
            raise ValueError("missing Stripe signature")
        # construct_event both verifies the HMAC and rejects a replayed
        # timestamp; an unsigned body must never reach parse_event.
        event = self._stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        as_dict = event if isinstance(event, Mapping) else event.to_dict_recursive()
        parsed = parse_event(as_dict, self._price_tiers)
        log.info(
            "stripe webhook type=%s handled=%s", scrub(as_dict.get("type")), parsed is not None
        )
        return parsed

    @staticmethod
    def _field(obj, name: str):
        """Stripe objects are dict-like; test doubles are plain dicts."""
        if isinstance(obj, Mapping):
            return obj.get(name)
        return getattr(obj, name, None)
