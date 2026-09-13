"""Firestore-backed :class:`~app.accounts.AccountStore`.

One document per user, keyed by Firebase uid. :meth:`reserve_import` runs in a
transaction for the same reason :meth:`~app.store.ElectionStore.claim` does:
two Cloud Run instances serving the same user's imports must not both hand out
the last unit of a month's quota.

Webhooks arrive naming a Stripe customer rather than a uid, so the customer id
is indexed — but the uid Stripe echoes back in subscription metadata is tried
first, because it needs no query at all.

``last_active_at`` is an ordinary number field, so the retention run's "longest
idle first" is a range query on Firestore's automatic single-field index. No
composite index has to be created.
"""

from __future__ import annotations

import logging
import time

from google.api_core.exceptions import NotFound
from google.cloud import firestore

from .accounts import (
    Tier,
    UserAccount,
    _deletable,
    _released,
    _reserved,
    _seen,
    _subscribed,
    billing_period,
)
from .observability import io_span

log = logging.getLogger(__name__)

ACCOUNTS_COLLECTION = "accounts"

#: Firestore takes at most this many writes in one batch.
MAX_BATCH_WRITES = 500


def _tier(value) -> Tier:
    """Storage is not trusted to have preserved the enum; anything odd is free."""
    try:
        return Tier(value)
    except ValueError:
        log.warning("unknown tier %r in storage; treating the account as free", value)
        return Tier.FREE


def _epoch(value) -> float | None:
    """A stored moment as epoch seconds. Anything that is not a number is no date."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _account_from_doc(uid: str, data: dict) -> UserAccount:
    return UserAccount(
        uid=uid,
        email=data.get("email"),
        tier=_tier(data.get("tier", Tier.FREE.value)),
        period=data.get("period") or "",
        used=int(data.get("used", 0) or 0),
        attempts=int(data.get("attempts", 0) or 0),
        stripe_customer_id=data.get("stripe_customer_id"),
        subscription_id=data.get("subscription_id"),
        subscription_status=data.get("subscription_status"),
        last_active_at=_epoch(data.get("last_active_at")),
    )


def _to_doc(account: UserAccount) -> dict:
    return {
        "email": account.email,
        "tier": account.tier.value,
        "period": account.period,
        "used": account.used,
        "attempts": account.attempts,
        "stripe_customer_id": account.stripe_customer_id,
        "subscription_id": account.subscription_id,
        "subscription_status": account.subscription_status,
        "last_active_at": account.last_active_at,
    }


def _count(query) -> int:
    """An aggregation query. Firestore counts server-side and bills it as a
    single read instead of returning every matching document."""
    result = query.count(alias="n").get()
    return int(result[0][0].value) if result and result[0] else 0


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

    def count_created(self, start: float, end: float) -> int:
        with io_span(log, "firestore", "count_created_accounts") as span:
            count = _count(
                self._db.collection(ACCOUNTS_COLLECTION)
                .where(filter=firestore.FieldFilter("created_at", ">=", start))
                .where(filter=firestore.FieldFilter("created_at", "<", end))
            )
            span["accounts"] = count
            return count

    def inactive(self, before: float, limit: int) -> list[UserAccount]:
        with io_span(log, "firestore", "inactive_accounts", limit=limit) as span:
            snapshots = list(
                self._db.collection(ACCOUNTS_COLLECTION)
                .where(filter=firestore.FieldFilter("last_active_at", "<", before))
                .order_by("last_active_at")
                .limit(limit)
                .stream()
            )
            span["accounts"] = len(snapshots)
            return [_account_from_doc(s.id, s.to_dict()) for s in snapshots]

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        # Every authenticated request passes through here, and almost all of
        # them find an account that needs no change. Reading first keeps the
        # steady state to one read instead of a transaction per request; the
        # transaction below re-reads, so two racing creations are still safe.
        # Activity is written at most daily (see accounts._seen), which is what
        # keeps that true.
        now = self._clock()
        existing = self.get(uid)
        if existing is not None and _seen(existing, email, now) == existing:
            return existing

        ref = self._ref(uid)

        @firestore.transactional
        def _ensure(transaction) -> UserAccount:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                account = UserAccount(
                    uid=uid, email=email, period=billing_period(), last_active_at=now
                )
                transaction.set(ref, {**_to_doc(account), "created_at": now})
                return account
            account = _account_from_doc(uid, snapshot.to_dict())
            seen = _seen(account, email, now)
            changed = {
                name: value
                for name, value, before in (
                    ("email", seen.email, account.email),
                    ("last_active_at", seen.last_active_at, account.last_active_at),
                )
                if value != before
            }
            if changed:
                transaction.set(ref, changed, merge=True)
            return seen

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
            transaction.set(
                ref,
                {"period": reserved.period, "used": reserved.used, "attempts": reserved.attempts},
                merge=True,
            )
            return True

        with io_span(log, "firestore", "reserve_import", uid=uid[:12], period=period) as span:
            granted = _reserve(self._db.transaction())
            span["granted"] = granted
            return granted

    def release_import(self, uid: str, period: str, *, keep_attempt: bool = False) -> None:
        ref = self._ref(uid)

        @firestore.transactional
        def _release(transaction) -> None:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return
            released = _released(
                _account_from_doc(uid, snapshot.to_dict()), period, keep_attempt=keep_attempt
            )
            transaction.set(
                ref,
                {"period": released.period, "used": released.used, "attempts": released.attempts},
                merge=True,
            )

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
            updated = _subscribed(
                account, tier, customer_id=customer_id, subscription_id=subscription_id,
                status=status,
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

    def date_undated(self, now: float, limit: int) -> int:
        collection = self._db.collection(ACCOUNTS_COLLECTION)
        with io_span(log, "firestore", "date_undated_accounts", limit=limit) as span:
            # Firestore has no query for "this field is missing": a document
            # without the field is not in that field's index at all. Two counts,
            # about a read each, tell whether any such document exists. Only
            # then is the collection scanned, which in practice means the runs
            # right after the deploy that started recording activity.
            undated = _count(collection) - _count(
                collection.where(filter=firestore.FieldFilter("last_active_at", ">=", 0))
            )
            span["undated"] = undated
            if undated <= 0:
                span["dated"] = 0
                return 0

            dated = 0
            batch = self._db.batch()
            for snapshot in collection.select(["last_active_at"]).stream():
                if dated >= limit:
                    break
                if _epoch((snapshot.to_dict() or {}).get("last_active_at")) is not None:
                    continue
                # update, not set: set would bring back a document deleted since
                # the scan read it, as a stub holding nothing but this field.
                batch.update(snapshot.reference, {"last_active_at": now})
                dated += 1
                if dated % MAX_BATCH_WRITES == 0:
                    batch.commit()
                    batch = self._db.batch()
            if dated % MAX_BATCH_WRITES:
                batch.commit()
            span["dated"] = dated
            return dated

    def mark_active(self, uid: str, now: float) -> None:
        with io_span(log, "firestore", "mark_active", uid=uid[:12]) as span:
            try:
                self._ref(uid).update({"last_active_at": now})
                span["found"] = True
            except NotFound:
                span["found"] = False

    def delete_inactive(self, uid: str, before: float) -> bool:
        ref = self._ref(uid)

        @firestore.transactional
        def _delete(transaction) -> bool:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or not _deletable(
                _account_from_doc(uid, snapshot.to_dict()), before
            ):
                return False
            transaction.delete(ref)
            return True

        with io_span(log, "firestore", "delete_inactive_account", uid=uid[:12]) as span:
            deleted = _delete(self._db.transaction())
            span["deleted"] = deleted
            return deleted
