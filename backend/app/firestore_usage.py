"""Firestore-backed :class:`~app.usage.UsageStore`.

One document per UTC day in ``usage_daily``, one field per counter, each bumped
with a server-side increment — so two instances counting at once never lose a
count, and a day costs one document however much happened in it.

Active-account markers are documents in that day's ``active`` subcollection,
named by the marker, so writing one twice is the same as writing it once. The
report claim is ``create()``, which Firestore refuses for a document that
exists: that is the whole of its atomicity.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from .observability import io_span

log = logging.getLogger(__name__)

DAILY_COLLECTION = "usage_daily"
ACTIVE_SUBCOLLECTION = "active"
REPORTS_COLLECTION = "usage_reports"

#: How many days before the cut-off a retention run looks at. The cron runs
#: daily, so one day would do; the rest catches up after runs that were missed.
FORGET_WINDOW_DAYS = 14

#: Firestore takes at most 500 writes in one batch.
BATCH_SIZE = 400


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days)]


class FirestoreUsageStore:
    def __init__(self, client: firestore.Client, *, clock=time.time):
        self._db = client
        self._clock = clock

    def _day(self, day: date):
        return self._db.collection(DAILY_COLLECTION).document(day.isoformat())

    def increment(self, day: date, key: str) -> None:
        with io_span(log, "firestore", "usage_increment", key=key):
            self._day(day).set({key: firestore.Increment(1)}, merge=True)

    def mark_active(self, day: date, marker: str) -> None:
        with io_span(log, "firestore", "usage_active"):
            self._day(day).collection(ACTIVE_SUBCOLLECTION).document(marker).set({})

    def daily(self, start: date, end: date) -> dict[date, dict[str, int]]:
        refs = [self._day(day) for day in _days(start, end)]
        counts: dict[date, dict[str, int]] = {}
        with io_span(log, "firestore", "usage_daily", days=len(refs)):
            for snapshot in self._db.get_all(refs):
                if not snapshot.exists:
                    continue
                counts[date.fromisoformat(snapshot.id)] = {
                    key: int(value)
                    for key, value in (snapshot.to_dict() or {}).items()
                    if isinstance(value, (int, float))
                }
        return counts

    def _markers(self, day: date):
        return self._day(day).collection(ACTIVE_SUBCOLLECTION)

    def active_accounts(self, start: date, end: date) -> int:
        markers: set[str] = set()
        with io_span(log, "firestore", "usage_active_accounts", start=start, end=end) as span:
            for day in _days(start, end):
                markers.update(doc.id for doc in self._markers(day).select([]).stream())
            span["accounts"] = len(markers)
        return len(markers)

    def forget_active_before(self, day: date) -> None:
        deleted = 0
        with io_span(log, "firestore", "usage_forget_active", before=day) as span:
            for old in _days(day - timedelta(days=FORGET_WINDOW_DAYS), day):
                batch, pending = self._db.batch(), 0
                for doc in self._markers(old).select([]).stream():
                    batch.delete(doc.reference)
                    pending += 1
                    if pending == BATCH_SIZE:
                        batch.commit()
                        deleted += pending
                        batch, pending = self._db.batch(), 0
                if pending:
                    batch.commit()
                    deleted += pending
            span["deleted"] = deleted

    def claim_report(self, key: str) -> bool:
        with io_span(log, "firestore", "usage_claim_report", key=key) as span:
            try:
                self._db.collection(REPORTS_COLLECTION).document(key).create(
                    {"sent_at": self._clock()}
                )
            except AlreadyExists:
                span["claimed"] = False
                return False
            span["claimed"] = True
            return True

    def release_report(self, key: str) -> None:
        with io_span(log, "firestore", "usage_release_report", key=key):
            self._db.collection(REPORTS_COLLECTION).document(key).delete()
