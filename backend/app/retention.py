"""Deleting the accounts nobody uses any more.

The privacy policy promises that an account unused for
:data:`~app.accounts.INACTIVE_ACCOUNT_RETENTION_DAYS` is deleted, and this
module is what makes that true. Once a day the Worker's cron calls
``POST /api/internal/inactive-accounts``, and that endpoint runs
:func:`sweep_inactive_accounts`.

A run does three things. They are ordered so that if a run fails partway, the
next run finishes the job instead of losing track of it:

1. **Accounts without a date get today's.** Accounts stored before activity was
   recorded have no ``last_active_at``. They count as used on the day of their
   first run (:meth:`~app.accounts.AccountStore.date_undated`), so none is
   deleted for lacking a date, and none is kept forever for lacking one.
2. **The longest-idle accounts are read,** at most ``limit`` of them per run.
   A backlog is worked through over several days instead of in one request
   that times out.
3. **Each one is either kept or deleted.** An account that still has a
   subscription (:func:`~app.accounts._billed`) is kept and marked active.
   Being paid for counts as use, and marking it moves it to the back of the
   queue. Without that, enough idle subscribers would fill every run's batch
   and nothing behind them would ever be deleted. For every other account the
   sign-in is deleted first and the account record second. If the sign-in
   cannot be deleted, the record stays, and the account is back at the front
   of the queue on the next run. Deleting the record first would be the wrong
   way round: nothing would find that account again, so a sign-in that failed
   to delete, email address included, would never be removed.

What is not deleted: the Stripe customer and its invoices. Bookkeeping rules
require keeping payment records, and they are Stripe's records rather than
this app's. The usage counters hold no identities. The hashed daily
active-account markers expire after
:data:`~app.usage.ACTIVE_RETENTION_DAYS` without any help from this module.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .accounts import AccountStore, _deletable, inactive_cutoff
from .auth import IdentityRemover
from .observability import scrub

log = logging.getLogger(__name__)

#: Most accounts one run looks at. Deleting a Firebase user is a network call,
#: and a hundred of them finish well within a request's time limit.
INACTIVE_SWEEP_LIMIT = 100

#: Most undated accounts one run dates. Those are plain writes, with no call out, so more fit.
UNDATED_SWEEP_LIMIT = 500


@dataclass(frozen=True)
class InactiveSweep:
    """What one run did. Counts only: which accounts were deleted is not recorded anywhere."""

    dated: int = 0
    """Accounts that had no ``last_active_at`` and were given the run's time."""
    deleted: int = 0
    kept: int = 0
    """Idle, but a subscription was still running on them, so they were kept."""
    failed: int = 0
    """Sign-ins that could not be deleted. Their accounts are retried next run."""


def sweep_inactive_accounts(
    accounts: AccountStore,
    identities: IdentityRemover,
    *,
    now: float | None = None,
    limit: int = INACTIVE_SWEEP_LIMIT,
    date_limit: int = UNDATED_SWEEP_LIMIT,
) -> InactiveSweep:
    """Delete up to ``limit`` accounts last used longer ago than retention allows."""
    now = time.time() if now is None else now
    dated = accounts.date_undated(now, date_limit)
    before = inactive_cutoff(now)
    deleted = kept = failed = 0

    for account in accounts.inactive(before, limit):
        uid = account.uid
        if not _deletable(account, before):
            accounts.mark_active(uid, now)
            kept += 1
            continue
        try:
            identities.remove(uid)
        except Exception as exc:  # noqa: BLE001 - one stuck sign-in must not stop the rest
            log.warning(
                "could not delete the sign-in of uid=%s; its account waits for the next run: %s",
                uid[:12], scrub(exc),
            )
            failed += 1
            continue
        if accounts.delete_inactive(uid, before):
            deleted += 1
        else:
            # The user signed in or subscribed after the list was read. Their
            # sign-in is already gone, so the next sign-in fails and they sign
            # up again, onto this same account if the uid is kept (sqlite), or
            # onto a new one.
            log.warning("uid=%s was used while it was being deleted; its account is kept", uid[:12])
            kept += 1

    swept = InactiveSweep(dated=dated, deleted=deleted, kept=kept, failed=failed)
    log.info(
        "inactive accounts dated=%d deleted=%d kept=%d failed=%d",
        swept.dated, swept.deleted, swept.kept, swept.failed,
    )
    return swept
