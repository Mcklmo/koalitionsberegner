"""Accounts, tiers and the monthly import allowance.

The quota is the only thing standing between a subscription and an unbounded
bill, so the properties that matter are: it is spent atomically, it is given
back when nothing was consumed, and it comes back by itself every month.

Every test runs against both persistent-capable backends, because the API above
them cannot tell which store it was handed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from app.accounts import (
    DEFAULT_MONTHLY_IMPORTS,
    InMemoryAccountStore,
    QuotaPolicy,
    Tier,
    UserAccount,
    billing_period,
)
from app.sqlite_accounts import SqliteAccountStore

UID = "user-1"
THIS_MONTH = "2026-09"
NEXT_MONTH = "2026-10"


@pytest.fixture(params=["memory", "sqlite"])
def accounts(request, tmp_path):
    if request.param == "memory":
        yield InMemoryAccountStore()
        return
    store = SqliteAccountStore(tmp_path / "accounts.db")
    yield store
    store.close()


# --- the quota model --------------------------------------------------------

def test_a_free_account_may_not_import():
    policy = QuotaPolicy()
    account = UserAccount(uid=UID, tier=Tier.FREE, period=THIS_MONTH)
    assert policy.limit(Tier.FREE) == 0
    assert account.may_import(policy, THIS_MONTH) is False


def test_paid_tiers_differ_and_premium_is_the_larger_one():
    policy = QuotaPolicy()
    assert 0 < policy.limit(Tier.BASIC) < policy.limit(Tier.PREMIUM)
    assert policy.limit(Tier.BASIC) == DEFAULT_MONTHLY_IMPORTS[Tier.BASIC]


def test_a_counter_from_a_past_month_reads_as_spent_nothing():
    """The reset, with no scheduled job anywhere: the label goes stale."""
    account = UserAccount(uid=UID, tier=Tier.BASIC, period=THIS_MONTH, used=99)
    assert account.used_in(THIS_MONTH) == 99
    assert account.used_in(NEXT_MONTH) == 0
    assert account.may_import(QuotaPolicy(), NEXT_MONTH) is True


def test_the_period_label_is_the_calendar_month_in_utc():
    assert billing_period(date(2026, 9, 7)) == "2026-09"
    assert billing_period(date(2026, 12, 31)) == "2026-12"
    assert billing_period(date(2027, 1, 1)) == "2027-01"


# --- the stores -------------------------------------------------------------

def test_an_account_is_created_free_on_first_sight(accounts):
    account = accounts.ensure(UID, "a@example.org")

    assert account.uid == UID
    assert account.tier is Tier.FREE, "accounts are free; importing is what is sold"
    assert account.subscription_status is None
    assert accounts.get(UID) == account


def test_ensuring_twice_returns_the_same_account_and_refreshes_the_email(accounts):
    accounts.ensure(UID, "old@example.org")
    accounts.set_subscription(UID, Tier.PREMIUM)

    again = accounts.ensure(UID, "new@example.org")

    assert again.tier is Tier.PREMIUM, "a second sign-in must not reset the tier"
    assert again.email == "new@example.org"


def test_an_unknown_account_has_nothing_to_reserve(accounts):
    assert accounts.get("nobody") is None
    assert accounts.reserve_import("nobody", THIS_MONTH, 10) is False


def test_reserving_spends_the_allowance_and_stops_at_the_limit(accounts):
    accounts.ensure(UID, None)
    accounts.set_subscription(UID, Tier.BASIC)

    granted = [accounts.reserve_import(UID, THIS_MONTH, 3) for _ in range(5)]

    assert granted == [True, True, True, False, False]
    assert accounts.get(UID).used_in(THIS_MONTH) == 3


def test_releasing_gives_the_unit_back(accounts):
    accounts.ensure(UID, None)
    assert accounts.reserve_import(UID, THIS_MONTH, 1) is True
    assert accounts.reserve_import(UID, THIS_MONTH, 1) is False

    accounts.release_import(UID, THIS_MONTH)

    assert accounts.get(UID).used_in(THIS_MONTH) == 0
    assert accounts.reserve_import(UID, THIS_MONTH, 1) is True


def test_releasing_more_than_was_taken_cannot_go_negative(accounts):
    accounts.ensure(UID, None)
    for _ in range(3):
        accounts.release_import(UID, THIS_MONTH)
    assert accounts.get(UID).used_in(THIS_MONTH) == 0


def test_a_refund_for_a_past_month_does_not_revive_a_spent_counter(accounts):
    """A late failure from September must not hand out an October import."""
    accounts.ensure(UID, None)
    accounts.reserve_import(UID, NEXT_MONTH, 5)

    accounts.release_import(UID, THIS_MONTH)

    assert accounts.get(UID).used_in(NEXT_MONTH) == 1


def test_the_allowance_returns_by_itself_the_next_month(accounts):
    accounts.ensure(UID, None)
    assert accounts.reserve_import(UID, THIS_MONTH, 1) is True
    assert accounts.reserve_import(UID, THIS_MONTH, 1) is False

    assert accounts.reserve_import(UID, NEXT_MONTH, 1) is True
    assert accounts.get(UID).used_in(NEXT_MONTH) == 1


def test_racing_reservations_never_hand_out_more_than_the_limit(accounts):
    """The property the whole quota rests on; anything less over-serves."""
    accounts.ensure(UID, None)
    limit = 5

    with ThreadPoolExecutor(max_workers=16) as pool:
        granted = list(pool.map(lambda _: accounts.reserve_import(UID, THIS_MONTH, limit), range(64)))

    assert granted.count(True) == limit
    assert accounts.get(UID).used_in(THIS_MONTH) == limit


def test_a_subscription_sets_the_tier_and_records_what_stripe_said(accounts):
    accounts.ensure(UID, None)

    updated = accounts.set_subscription(
        UID, Tier.PREMIUM, customer_id="cus_1", subscription_id="sub_1", status="active"
    )

    assert updated.tier is Tier.PREMIUM
    assert updated.stripe_customer_id == "cus_1"
    assert updated.subscription_status == "active"
    assert accounts.get(UID).tier is Tier.PREMIUM


def test_a_subscription_for_an_unknown_account_changes_nothing(accounts):
    assert accounts.set_subscription("nobody", Tier.PREMIUM) is None


def test_dropping_to_free_leaves_the_customer_linked(accounts):
    """Cancelling must not orphan the customer, or a resubscribe makes a second one."""
    accounts.ensure(UID, None)
    accounts.set_subscription(UID, Tier.BASIC, customer_id="cus_1", status="active")

    accounts.set_subscription(UID, Tier.FREE, status="canceled")

    account = accounts.get(UID)
    assert account.tier is Tier.FREE
    assert account.stripe_customer_id == "cus_1"


def test_an_account_can_be_found_by_its_stripe_customer(accounts):
    """How a webhook that names only a customer reaches the right account."""
    accounts.ensure(UID, None)
    accounts.link_customer(UID, "cus_9")

    assert accounts.find_by_customer("cus_9").uid == UID
    assert accounts.find_by_customer("cus_absent") is None


def test_sqlite_accounts_survive_a_restart(tmp_path):
    path = tmp_path / "accounts.db"
    first = SqliteAccountStore(path)
    first.ensure(UID, "a@example.org")
    first.set_subscription(UID, Tier.BASIC, customer_id="cus_1", status="active")
    first.reserve_import(UID, THIS_MONTH, 10)
    first.close()

    second = SqliteAccountStore(path)
    account = second.get(UID)
    second.close()

    assert account.tier is Tier.BASIC
    assert account.used_in(THIS_MONTH) == 1, "a restart must not refill the allowance"
    assert account.stripe_customer_id == "cus_1"
