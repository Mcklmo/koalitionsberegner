"""Firestore-backed :class:`~app.accounts.AccountStore`.

One document per user, keyed by Firebase uid. :meth:`reserve_import` runs in a
transaction for the same reason :meth:`~app.store.ElectionStore.claim` does:
two Cloud Run instances serving the same user's imports must not both hand out
the last unit of a month's quota.

Webhooks arrive naming a Stripe customer rather than a uid, so the customer id
is indexed — but the uid Stripe echoes back in subscription metadata is tried
first, because it needs no query at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from google.cloud import firestore

from .accounts import Tier, UserAccount, _released, _reserved, billing_period
from .observability import io_span

log = logging.getLogger(__name__)

ACCOUNTS_COLLECTION = "accounts"


def _tier(value) -> Tier:
    """Storage is not trusted to have preserved the enum; anything odd is free."""
    try:
        return Tier(value)
    except ValueError:
        log.warning("unknown tier %r in storage; treating the account as free", value)
        return Tier.FREE


def _account_from_doc(uid: str, data: dict) -> UserAccount:
    return UserAccount(
        uid=uid,
        email=data.get("email"),
        tier=_tier(data.get("tier", Tier.FREE.value)),
        period=data.get("period") or "",
        used=int(data.get("used", 0) or 0),
        stripe_customer_id=data.get("stripe_customer_id"),
        subscription_id=data.get("subscription_id"),
        subscription_status=data.get("subscription_status"),
    )


def _to_doc(account: UserAccount) -> dict:
    return {
        "email": account.email,
        "tier": account.tier.value,
        "period": account.period,
        "used": account.used,
        "stripe_customer_id": account.stripe_customer_id,
        "subscription_id": account.subscription_id,
        "subscription_status": account.subscription_status,
    }


class FirestoreAccountStore:
    def __init__(self, client: firestore.Client, *, clock=time.time):
        self._db = client
        self._clock = clock

    def _ref(self, uid: str):
        return self._db.collection(ACCOUNTS_COLLECTION).document(uid)

    def get(self, uid: str) -> UserAccount | None:
        with io_span(log, "firestore", "get_account", uid=uid[:12]) as span:
            snapshot = self._ref(uid).get()
            span["found"] = snapshot.exists
            return _account_from_doc(uid, snapshot.to_dict()) if snapshot.exists else None

    def find_by_customer(self, customer_id: str) -> UserAccount | None:
        with io_span(log, "firestore", "account_by_customer", customer=customer_id[:12]) as span:
            matches = list(
                self._db.collection(ACCOUNTS_COLLECTION)
                .where(filter=firestore.FieldFilter("stripe_customer_id", "==", customer_id))
                .limit(1)
                .stream()
            )
            span["found"] = bool(matches)
            if not matches:
                return None
            return _account_from_doc(matches[0].id, matches[0].to_dict())

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        # Every authenticated request passes through here, and almost all of
        # them find an account that needs no change. Reading first keeps the
        # steady state to one read instead of a transaction per request; the
        # transaction below re-reads, so two racing creations are still safe.
        existing = self.get(uid)
        if existing is not None and (not email or existing.email == email):
            return existing

        ref = self._ref(uid)
        clock = self._clock

        @firestore.transactional
        def _ensure(transaction) -> UserAccount:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                account = UserAccount(uid=uid, email=email, period=billing_period())
                transaction.set(ref, {**_to_doc(account), "created_at": clock()})
                return account
            account = _account_from_doc(uid, snapshot.to_dict())
            if email and account.email != email:
                account = replace(account, email=email)
                transaction.set(ref, {"email": email}, merge=True)
            return account

        with io_span(log, "firestore", "ensure_account", uid=uid[:12]):
            return _ensure(self._db.transaction())

    def reserve_import(self, uid: str, period: str, limit: int) -> bool:
        ref = self._ref(uid)

        @firestore.transactional
        def _reserve(transaction) -> bool:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            reserved = _reserved(_account_from_doc(uid, snapshot.to_dict()), period, limit)
            if reserved is None:
                return False
            # The write serialises racing reservations: whichever transaction
            # commits first makes the other's read stale, forcing a retry.
            transaction.set(ref, {"period": reserved.period, "used": reserved.used}, merge=True)
            return True

        with io_span(log, "firestore", "reserve_import", uid=uid[:12], period=period) as span:
            granted = _reserve(self._db.transaction())
            span["granted"] = granted
            return granted

    def release_import(self, uid: str, period: str) -> None:
        ref = self._ref(uid)

        @firestore.transactional
        def _release(transaction) -> None:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return
            released = _released(_account_from_doc(uid, snapshot.to_dict()), period)
            transaction.set(ref, {"period": released.period, "used": released.used}, merge=True)

        with io_span(log, "firestore", "release_import", uid=uid[:12], period=period):
            _release(self._db.transaction())

    def set_subscription(
        self,
        uid: str,
        tier: Tier,
        *,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
    ) -> UserAccount | None:
        ref = self._ref(uid)

        @firestore.transactional
        def _set(transaction) -> UserAccount | None:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            account = _account_from_doc(uid, snapshot.to_dict())
            updated = replace(
                account,
                tier=tier,
                stripe_customer_id=customer_id or account.stripe_customer_id,
                subscription_id=subscription_id or account.subscription_id,
                subscription_status=status or account.subscription_status,
            )
            transaction.set(
                ref,
                {
                    "tier": updated.tier.value,
                    "stripe_customer_id": updated.stripe_customer_id,
                    "subscription_id": updated.subscription_id,
                    "subscription_status": updated.subscription_status,
                },
                merge=True,
            )
            return updated

        with io_span(log, "firestore", "set_subscription", uid=uid[:12], tier=tier.value) as span:
            updated = _set(self._db.transaction())
            span["found"] = updated is not None
            return updated

    def link_customer(self, uid: str, customer_id: str) -> None:
        with io_span(log, "firestore", "link_customer", uid=uid[:12], customer=customer_id[:12]):
            self._ref(uid).set({"stripe_customer_id": customer_id}, merge=True)
