"""Accounts, subscription tiers and the monthly import quota.

Viewing is open; *importing* is what costs money, because an import is what
runs a page fetch and a model call. So the quota counts exactly one thing: an
import that started a new extraction. A page already stored, or one somebody
else is extracting right now, is served without touching the quota — see
:meth:`~app.store.ElectionStore.claim`, which is what decides that.

Quotas reset by calendar month with no scheduled job: a period label is stored
alongside the counter, and a counter belonging to a past period reads as zero
(:meth:`UserAccount.used_in`). The first import of a new month overwrites it.
"""

from __future__ import annotations

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

#: Tiers a subscription can grant. Free is what an account starts and ends on.
PAID_TIERS = (Tier.BASIC, Tier.PREMIUM)


def billing_period(when: date | datetime | None = None) -> str:
    """The quota window a moment falls in: ``"2026-09"``.

    UTC, so a user does not get a second allowance by changing timezone.
    """
    moment = when or datetime.now(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


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
    stripe_customer_id: str | None = None
    subscription_id: str | None = None
    subscription_status: str | None = None
    """Stripe's own word for the subscription: ``active``, ``past_due``, ``canceled``…"""

    def used_in(self, period: str) -> int:
        """Imports consumed in ``period`` — zero once the month has rolled over."""
        return self.used if self.period == period else 0

    def remaining(self, policy: QuotaPolicy, period: str | None = None) -> int:
        window = period or billing_period()
        return max(0, policy.limit(self.tier) - self.used_in(window))

    def may_import(self, policy: QuotaPolicy, period: str | None = None) -> bool:
        return self.remaining(policy, period) > 0


class AccountStore(Protocol):
    """Storage seam for accounts. ``reserve_import`` must be atomic."""

    def get(self, uid: str) -> UserAccount | None: ...

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        """Return the account, creating a free one on first sight of ``uid``."""
        ...

    def reserve_import(self, uid: str, period: str, limit: int) -> bool:
        """Take one import from this month's allowance. False when it is spent.

        Atomic, because two imports racing must not both see the last unit.
        """
        ...

    def release_import(self, uid: str, period: str) -> None:
        """Give a reserved import back — the extraction never started."""
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


def _reserved(account: UserAccount, period: str, limit: int) -> UserAccount | None:
    """The reserve rule, shared by every implementation so they cannot drift."""
    used = account.used_in(period)
    if used >= limit:
        return None
    return replace(account, period=period, used=used + 1)


def _released(account: UserAccount, period: str) -> UserAccount:
    """The release rule. A refund for a past month would resurrect a spent counter."""
    if account.period != period or account.used <= 0:
        return account
    return replace(account, used=account.used - 1)


class InMemoryAccountStore:
    """Process-local accounts, for tests and for runs without a real database."""

    def __init__(self, accounts: dict[str, UserAccount] | None = None):
        self._accounts: dict[str, UserAccount] = dict(accounts or {})
        self._lock = Lock()

    def get(self, uid: str) -> UserAccount | None:
        with self._lock:
            return self._accounts.get(uid)

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                account = UserAccount(uid=uid, email=email, period=billing_period())
            elif email and account.email != email:
                account = replace(account, email=email)
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

    def release_import(self, uid: str, period: str) -> None:
        with self._lock:
            account = self._accounts.get(uid)
            if account is not None:
                self._accounts[uid] = _released(account, period)

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
            updated = replace(
                account,
                tier=tier,
                stripe_customer_id=customer_id or account.stripe_customer_id,
                subscription_id=subscription_id or account.subscription_id,
                subscription_status=status or account.subscription_status,
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
