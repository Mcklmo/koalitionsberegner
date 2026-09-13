"""Accounts, subscription tiers and the monthly import quota.

Viewing is open; *importing* is what costs money, because an import is what
runs a page fetch and a model call. So the quota counts exactly one thing: an
import that started a new extraction. A page already stored, or one somebody
else is extracting right now, is served without touching the quota — see
:meth:`~app.store.ElectionStore.claim`, which is what decides that.

Quotas reset by calendar month with no scheduled job: a period label is stored
alongside the counter, and a counter belonging to a past period reads as zero
(:meth:`UserAccount.used_in`). The first import of a new month overwrites it.

Accounts nobody uses are deleted. Each one carries ``last_active_at``, which
:meth:`AccountStore.ensure` — the call every signed-in request makes — moves
forward at most once a day. Once a day :mod:`app.retention` deletes an account
whose last activity is older than :data:`INACTIVE_ACCOUNT_RETENTION_DAYS` and
that has no subscription still running (:func:`_deletable`). Like the quota
rules, that rule is one function every store calls, so the stores cannot
disagree about who gets deleted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from collections.abc import Mapping
from enum import Enum
from threading import Lock
from typing import Protocol


class Tier(str, Enum):
    FREE = "free"        # account, no imports
    BASIC = "basic"      # a handful of imports a month
    PREMIUM = "premium"  # a working budget, for journalists and analysts


#: Imports per calendar month. Free accounts view everything and import nothing.
DEFAULT_MONTHLY_IMPORTS: Mapping[Tier, int] = {
    Tier.FREE: 0,
    Tier.BASIC: 10,
    Tier.PREMIUM: 200,
}

#: Failed imports are refunded — a misremembered year should not cost an import
#: — but each one still ran the resolver and read pages with a model. So failures
#: are refunded up to as many again as the allowance itself, and never fewer than
#: this; past that, the month's imports are spent.
MIN_REFUNDED_FAILURES = 5


def attempt_limit(limit: int) -> int:
    """How many imports may be *started* in a month on an allowance of ``limit``."""
    return limit + max(limit, MIN_REFUNDED_FAILURES) if limit > 0 else 0


#: Tiers a subscription can grant. Free is what an account starts and ends on.
PAID_TIERS = (Tier.BASIC, Tier.PREMIUM)

#: Subscription statuses that entitle the user to their tier. Anything else —
#: ``past_due``, ``unpaid``, ``canceled``, ``incomplete`` — drops them to free.
ENTITLING_STATUSES = frozenset({"active", "trialing"})

#: Stripe statuses after which a subscription is over for good. Every other
#: status is a subscription Stripe may still charge for — ``past_due`` while it
#: retries a card, say — and an account with one of those is never deleted.
ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})

#: An account nobody has used for this long is deleted, sign-in and all. The
#: privacy policy states this number, so change both together.
INACTIVE_ACCOUNT_RETENTION_DAYS = 730

#: ``ensure`` writes ``last_active_at`` only once the stored value is at least
#: this old. Retention is measured in years, so a day's precision loses nothing,
#: and it keeps the request path at one read: otherwise every signed-in request
#: would also be a write.
ACTIVITY_RESOLUTION_SECONDS = 24 * 3600.0


def billing_period(when: date | datetime | None = None) -> str:
    """The quota window a moment falls in: ``"2026-09"``.

    UTC, so a user does not get a second allowance by changing timezone.
    """
    moment = when or datetime.now(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def inactive_cutoff(now: float) -> float:
    """The moment, in epoch seconds, that an account's last activity must be before for it to be deleted."""
    return now - INACTIVE_ACCOUNT_RETENTION_DAYS * 24 * 3600.0


@dataclass(frozen=True)
class QuotaPolicy:
    """How many imports each tier gets per month."""

    monthly_imports: Mapping[Tier, int] = field(
        default_factory=lambda: dict(DEFAULT_MONTHLY_IMPORTS)
    )

    def limit(self, tier: Tier) -> int:
        return int(self.monthly_imports.get(tier, 0))


@dataclass(frozen=True)
class UserAccount:
    uid: str
    """The Firebase user id — the only identifier the API trusts."""
    email: str | None = None
    tier: Tier = Tier.FREE
    period: str = ""
    """The month ``used`` was counted in. A stale label means the counter is spent."""
    used: int = 0
    attempts: int = 0
    """Imports started in ``period``, the refunded failures among them."""
    stripe_customer_id: str | None = None
    subscription_id: str | None = None
    subscription_status: str | None = None
    """Stripe's own word for the subscription: ``active``, ``past_due``, ``canceled``…"""
    last_active_at: float | None = None
    """When the account was last used, in epoch seconds, accurate to a day.

    ``None`` for an account stored before this was recorded. That alone never
    makes an account deletable: the next request dates it, or failing that the
    next retention run does (:meth:`AccountStore.date_undated`), and from then
    on it has two years like any other account.
    """

    def used_in(self, period: str) -> int:
        """Imports consumed in ``period`` — zero once the month has rolled over."""
        return self.used if self.period == period else 0

    def attempts_in(self, period: str) -> int:
        """Imports started in ``period``, whether or not they were refunded."""
        return self.attempts if self.period == period else 0

    def remaining(self, policy: QuotaPolicy, period: str | None = None) -> int:
        window = period or billing_period()
        return max(0, policy.limit(self.tier) - self.used_in(window))

    def may_import(self, policy: QuotaPolicy, period: str | None = None) -> bool:
        window = period or billing_period()
        return self.remaining(policy, window) > 0 and self.attempts_in(window) < attempt_limit(
            policy.limit(self.tier)
        )


class AccountStore(Protocol):
    """Storage seam for accounts. ``reserve_import`` must be atomic."""

    def get(self, uid: str) -> UserAccount | None: ...

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        """Return the account, creating a free one on first sight of ``uid``.

        Also where an account counts as used: ``last_active_at`` is set on
        creation and moved forward once it is a day old (:func:`_seen`).
        """
        ...

    def reserve_import(self, uid: str, period: str, limit: int) -> bool:
        """Take one import from this month's allowance. False when it is spent.

        Atomic, because two imports racing must not both see the last unit.
        """
        ...

    def release_import(self, uid: str, period: str, *, keep_attempt: bool = False) -> None:
        """Give a reserved import back.

        ``keep_attempt`` when the import did its work and failed: the allowance
        comes back, the attempt still counts toward :func:`attempt_limit`.
        """
        ...

    def set_subscription(
        self,
        uid: str,
        tier: Tier,
        *,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
    ) -> UserAccount | None:
        """Apply what billing says this user is entitled to. Unknown uid: ``None``."""
        ...

    def link_customer(self, uid: str, customer_id: str) -> None:
        """Record the Stripe customer, so webhooks can find the account again."""
        ...

    def find_by_customer(self, customer_id: str) -> UserAccount | None:
        """Webhooks identify the user by Stripe customer, not by uid."""
        ...

    def count_created(self, start: float, end: float) -> int:
        """How many accounts were created in ``[start, end)``, in epoch seconds.

        A number for the usage report, answered from the creation time the
        store keeps anyway — not a list of who.
        """
        ...

    def date_undated(self, now: float, limit: int) -> int:
        """Set ``last_active_at`` to ``now`` on up to ``limit`` accounts that have none.

        This is how accounts stored before activity was recorded come under the
        rule: they count as used today. None is deleted for lacking a date, and
        none is kept forever for lacking one either. Returns how many it dated.
        """
        ...

    def inactive(self, before: float, limit: int) -> list[UserAccount]:
        """Up to ``limit`` accounts last active before ``before``, longest idle first.

        Includes the ones still billed; the caller applies :func:`_deletable`.
        """
        ...

    def mark_active(self, uid: str, now: float) -> None:
        """Record the account as in use at ``now``. An unknown uid is not an error."""
        ...

    def delete_inactive(self, uid: str, before: float) -> bool:
        """Delete the account if :func:`_deletable` still holds for it, atomically.

        Checked again here rather than trusted from :meth:`inactive`, because
        the user may have signed in or subscribed since that list was read.
        """
        ...


def _reserved(account: UserAccount, period: str, limit: int) -> UserAccount | None:
    """The reserve rule, shared by every implementation so they cannot drift."""
    used = account.used_in(period)
    attempts = account.attempts_in(period)
    if used >= limit or attempts >= attempt_limit(limit):
        return None
    return replace(account, period=period, used=used + 1, attempts=attempts + 1)


def _released(account: UserAccount, period: str, *, keep_attempt: bool = False) -> UserAccount:
    """The release rule. A refund for a past month would resurrect a spent counter."""
    if account.period != period or account.used <= 0:
        return account
    attempts = account.attempts if keep_attempt else max(0, account.attempts - 1)
    return replace(account, used=account.used - 1, attempts=attempts)


def _subscribed(
    account: UserAccount,
    tier: Tier,
    *,
    customer_id: str | None,
    subscription_id: str | None,
    status: str | None,
) -> UserAccount:
    """The webhook rule, shared by every implementation so they cannot drift.

    An account pays through one subscription. Another one may take its place
    only by being paid for: an old duplicate being cancelled, or a late event
    about a subscription the user has since replaced, says nothing about the one
    they pay for now — and applied blindly, it would drop a paying subscriber
    to free.
    """
    paying_now = account.tier is not Tier.FREE and account.subscription_status in ENTITLING_STATUSES
    if (
        paying_now
        and tier is Tier.FREE
        and subscription_id
        and account.subscription_id
        and subscription_id != account.subscription_id
    ):
        return account
    return replace(
        account,
        tier=tier,
        stripe_customer_id=customer_id or account.stripe_customer_id,
        subscription_id=subscription_id or account.subscription_id,
        subscription_status=status or account.subscription_status,
    )


def _seen(account: UserAccount, email: str | None, now: float) -> UserAccount:
    """What ``ensure`` makes of an account that already exists.

    The current email address, and ``last_active_at`` moved to ``now`` when it
    is missing or at least :data:`ACTIVITY_RESOLUTION_SECONDS` old. When the
    result equals the account passed in, nothing needs writing, which is what
    lets the stores answer most requests with a single read.
    """
    if email and account.email != email:
        account = replace(account, email=email)
    if account.last_active_at is None or now - account.last_active_at >= ACTIVITY_RESOLUTION_SECONDS:
        account = replace(account, last_active_at=now)
    return account


def _billed(account: UserAccount) -> bool:
    """Whether a subscription may still be running on this account.

    Deliberately broader than being entitled to a tier. A ``past_due``
    subscription grants no imports, but Stripe is still retrying the card, and
    deleting the account would leave a subscription with no account for its
    webhooks to reach. A paid tier with no recorded status is kept as well:
    nothing says that subscription has ended.
    """
    if account.tier is not Tier.FREE:
        return True
    status = account.subscription_status
    return status is not None and status not in ENDED_STATUSES


def _deletable(account: UserAccount, before: float) -> bool:
    """The retention rule, shared by every implementation so they cannot drift.

    Last used before ``before``, and nothing billed. An account with no date is
    never deletable. Deletion is only ever done on purpose, and a missing date
    means the account has not been looked at yet, not that it is old.
    """
    return (
        account.last_active_at is not None
        and account.last_active_at < before
        and not _billed(account)
    )


class InMemoryAccountStore:
    """Process-local accounts, for tests and for runs without a real database."""

    def __init__(self, accounts: dict[str, UserAccount] | None = None, *, clock=time.time):
        self._accounts: dict[str, UserAccount] = dict(accounts or {})
        # Only accounts this store created have a creation time; ones handed in
        # already existed, and are not new to anyone.
        self._created: dict[str, float] = {}
        self._clock = clock
        self._lock = Lock()

    def get(self, uid: str) -> UserAccount | None:
        with self._lock:
            return self._accounts.get(uid)

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        with self._lock:
            now = self._clock()
            account = self._accounts.get(uid)
            if account is None:
                account = UserAccount(
                    uid=uid, email=email, period=billing_period(), last_active_at=now
                )
                self._created[uid] = now
            else:
                account = _seen(account, email, now)
            self._accounts[uid] = account
            return account

    def reserve_import(self, uid: str, period: str, limit: int) -> bool:
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                return False
            reserved = _reserved(account, period, limit)
            if reserved is None:
                return False
            self._accounts[uid] = reserved
            return True

    def release_import(self, uid: str, period: str, *, keep_attempt: bool = False) -> None:
        with self._lock:
            account = self._accounts.get(uid)
            if account is not None:
                self._accounts[uid] = _released(account, period, keep_attempt=keep_attempt)

    def set_subscription(
        self,
        uid: str,
        tier: Tier,
        *,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
    ) -> UserAccount | None:
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                return None
            updated = _subscribed(
                account, tier, customer_id=customer_id, subscription_id=subscription_id,
                status=status,
            )
            self._accounts[uid] = updated
            return updated

    def link_customer(self, uid: str, customer_id: str) -> None:
        with self._lock:
            account = self._accounts.get(uid)
            if account is not None:
                self._accounts[uid] = replace(account, stripe_customer_id=customer_id)

    def find_by_customer(self, customer_id: str) -> UserAccount | None:
        with self._lock:
            for account in self._accounts.values():
                if account.stripe_customer_id == customer_id:
                    return account
            return None

    def count_created(self, start: float, end: float) -> int:
        with self._lock:
            return sum(1 for created in self._created.values() if start <= created < end)

    def date_undated(self, now: float, limit: int) -> int:
        with self._lock:
            undated = [uid for uid, a in self._accounts.items() if a.last_active_at is None][:limit]
            for uid in undated:
                self._accounts[uid] = replace(self._accounts[uid], last_active_at=now)
            return len(undated)

    def inactive(self, before: float, limit: int) -> list[UserAccount]:
        with self._lock:
            idle = [
                a for a in self._accounts.values()
                if a.last_active_at is not None and a.last_active_at < before
            ]
            return sorted(idle, key=lambda a: a.last_active_at)[:limit]

    def mark_active(self, uid: str, now: float) -> None:
        with self._lock:
            account = self._accounts.get(uid)
            if account is not None:
                self._accounts[uid] = replace(account, last_active_at=now)

    def delete_inactive(self, uid: str, before: float) -> bool:
        with self._lock:
            account = self._accounts.get(uid)
            if account is None or not _deletable(account, before):
                return False
            del self._accounts[uid]
            self._created.pop(uid, None)
            return True
