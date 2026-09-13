"""How the app is used, counted per day, and the reports built from the counts.

The owner wants to know what people do with the app — pick an election, import
one, ask for one — without reading logs. What is kept for that is deliberately
thin, because the code is public and the people using it are in the EU:

* **Counters, not events.** A day is a handful of numbers: how many elections
  were picked, how many imports started. Nothing says who, which election, or
  from where, so the counters are not personal data once written.
* **One exception, and it expires.** Counting *distinct* active accounts over a
  week or a month needs to know that Monday's account is Tuesday's. That is a
  marker per account per day — a truncated hash of the uid, never the uid or
  the address — and it is deleted after :data:`ACTIVE_RETENTION_DAYS`, the
  longest span a report compares. It is still pseudonymous data, which is why
  the page's privacy section names it.
* **Nothing new in the browser.** Page loads are counted from the config
  request the page already makes, with the language the browser already sends.

Recording can never cost a request anything: :class:`UsageRecorder` swallows
every failure, and the endpoints run it after the response has gone.

The report itself is a pure function of the counts (:func:`build_report`), so
what the owner reads can be asserted without a store, a clock or a mail server.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Protocol

from .observability import scrub

log = logging.getLogger(__name__)

#: How long an active-account marker is kept. A monthly report compares the
#: month just ended with the one before it, and that is at most 62 days.
ACTIVE_RETENTION_DAYS = 62

#: Languages the page speaks; every other browser language is counted as one.
#: A fixed list rather than whatever the header says, so a caller cannot mint a
#: new counter per request.
PAGE_LANGUAGES = ("da", "en")


class UsageEvent(str, Enum):
    """Everything counted. The value is the counter's name in storage."""

    ELECTION_PICKED = "election_picked"
    IMPORT_STARTED = "import_started"
    IMPORT_FAILED = "import_failed"
    IMPORT_SAVED_RESULT = "import_saved_result"
    IMPORT_SAVED_FORECAST = "import_saved_forecast"
    PREVIEW_DISCARDED = "preview_discarded"
    REQUEST_FILED = "request_filed"
    REQUEST_DUPLICATE = "request_duplicate"
    REQUEST_ALREADY_IMPORTED = "request_already_imported"
    REFUSED_NO_SUBSCRIPTION = "refused_no_subscription"
    REFUSED_LIMIT_REACHED = "refused_limit_reached"
    CHECKOUT_STARTED = "checkout_started"
    SUBSCRIPTION_STARTED = "subscription_started"
    SUBSCRIPTION_ENDED = "subscription_ended"


def language_bucket(accept_language: str | None) -> str:
    """The page language a browser prefers: ``da``, ``en``, or ``other``."""
    first = (accept_language or "").split(",")[0].split(";")[0].strip()
    primary = first.split("-")[0].lower()
    return primary if primary in PAGE_LANGUAGES else "other"


def page_load_key(accept_language: str | None) -> str:
    """The counter a page load is added to."""
    return f"page_load_{language_bucket(accept_language)}"


def account_marker(uid: str) -> str:
    """What stands for an account in the active-account count.

    Enough bits that two accounts of a small app do not collide, too few to be
    worth anything but telling one account's days apart from another's.
    """
    return hashlib.sha256(uid.encode()).hexdigest()[:16]


def marker_expiry(day: date) -> datetime:
    """The moment the marker for ``day`` is past its retention.

    The same moment :meth:`UsageStore.forget_active_before` would first delete
    it: the run on ``day + ACTIVE_RETENTION_DAYS + 1`` forgets every day before
    ``day + 1``. Firestore's TTL policy deletes on this, which is what keeps the
    promise when the daily schedule does not run.
    """
    return datetime.combine(
        day + timedelta(days=ACTIVE_RETENTION_DAYS + 1), time(), tzinfo=timezone.utc
    )


class UsageStore(Protocol):
    """Storage seam for the counters. Ranges are ``[start, end)`` in UTC days."""

    def increment(self, day: date, key: str) -> None: ...

    def mark_active(self, day: date, marker: str) -> None:
        """Note that an account was active on ``day``. Idempotent."""
        ...

    def daily(self, start: date, end: date) -> dict[date, dict[str, int]]:
        """Every counter of every day in the range that has any."""
        ...

    def active_accounts(self, start: date, end: date) -> int:
        """Distinct accounts active on any day in the range."""
        ...

    def forget_active_before(self, day: date) -> None:
        """Delete the active-account markers of every day before ``day``."""
        ...

    def claim_report(self, key: str) -> bool:
        """Mark a report as being sent. False when it already was — atomic."""
        ...

    def release_report(self, key: str) -> None:
        """Undo a claim whose report could not be sent, so the next run retries."""
        ...


class InMemoryUsageStore:
    """Process-local counters, for tests and for runs without a real database."""

    def __init__(self):
        self._counts: dict[date, dict[str, int]] = {}
        self._active: dict[date, set[str]] = {}
        self._reports: set[str] = set()
        self._lock = Lock()

    def increment(self, day: date, key: str) -> None:
        with self._lock:
            counts = self._counts.setdefault(day, {})
            counts[key] = counts.get(key, 0) + 1

    def mark_active(self, day: date, marker: str) -> None:
        with self._lock:
            self._active.setdefault(day, set()).add(marker)

    def daily(self, start: date, end: date) -> dict[date, dict[str, int]]:
        with self._lock:
            return {day: dict(counts) for day, counts in self._counts.items() if start <= day < end}

    def active_accounts(self, start: date, end: date) -> int:
        with self._lock:
            markers = set()
            for day, seen in self._active.items():
                if start <= day < end:
                    markers |= seen
            return len(markers)

    def forget_active_before(self, day: date) -> None:
        with self._lock:
            for old in [d for d in self._active if d < day]:
                del self._active[old]

    def claim_report(self, key: str) -> bool:
        with self._lock:
            if key in self._reports:
                return False
            self._reports.add(key)
            return True

    def release_report(self, key: str) -> None:
        with self._lock:
            self._reports.discard(key)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UsageRecorder:
    """The one thing request handlers talk to. Never raises.

    A counter that could not be written is a report that is off by one; a
    request that failed because of it would be an outage caused by statistics.
    """

    def __init__(self, store: UsageStore, *, clock: Callable[[], datetime] = utc_now):
        self.store = store
        self._clock = clock
        # Every signed-in page load reports its account. One write per account
        # per day per instance is enough; the store deduplicates the rest.
        self._seen_day: date | None = None
        self._seen: set[str] = set()
        self._lock = Lock()

    def today(self) -> date:
        return self._clock().astimezone(timezone.utc).date()

    def record(self, event: UsageEvent | str) -> None:
        key = event.value if isinstance(event, UsageEvent) else event
        try:
            self.store.increment(self.today(), key)
        except Exception as exc:  # noqa: BLE001 - statistics must not fail a request
            log.warning("usage %s not recorded: %s", scrub(key), scrub(exc))

    def active(self, uid: str) -> None:
        day, marker = self.today(), account_marker(uid)
        with self._lock:
            if self._seen_day != day:
                self._seen_day, self._seen = day, set()
            if marker in self._seen:
                return
        try:
            self.store.mark_active(day, marker)
        except Exception as exc:  # noqa: BLE001
            log.warning("active account not recorded: %s", scrub(exc))
            return
        with self._lock:
            if self._seen_day == day:
                self._seen.add(marker)


# --- periods ----------------------------------------------------------------

class Period(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class DateRange:
    """UTC days from ``start`` up to, not including, ``end``."""

    start: date
    end: date

    @property
    def last(self) -> date:
        return self.end - timedelta(days=1)

    def days(self) -> list[date]:
        return [self.start + timedelta(days=n) for n in range((self.end - self.start).days)]

    def timestamps(self) -> tuple[float, float]:
        """The range as epoch seconds, which is how account creation is stored."""
        start = datetime.combine(self.start, time(), tzinfo=timezone.utc)
        end = datetime.combine(self.end, time(), tzinfo=timezone.utc)
        return start.timestamp(), end.timestamp()

    def describe(self) -> str:
        if self.end - self.start == timedelta(days=1):
            return self.start.isoformat()
        return f"{self.start.isoformat()} to {self.last.isoformat()}"


def period_range(period: Period, today: date) -> DateRange:
    """The last *complete* period before ``today``: yesterday, last week, last month.

    Weeks run Monday to Sunday.
    """
    if period is Period.DAILY:
        return DateRange(today - timedelta(days=1), today)
    if period is Period.WEEKLY:
        monday = today - timedelta(days=today.weekday())
        return DateRange(monday - timedelta(days=7), monday)
    first = today.replace(day=1)
    return DateRange((first - timedelta(days=1)).replace(day=1), first)


def previous_range(period: Period, current: DateRange) -> DateRange:
    """The period before ``current``, of the same kind."""
    # A period starts on its own first day, so the last complete one before
    # that day is exactly the one before it.
    return period_range(period, current.start)


def due_periods(today: date) -> list[Period]:
    """Which reports a run on ``today`` sends: daily always, then week and month ends."""
    due = [Period.DAILY]
    if today.weekday() == 0:
        due.append(Period.WEEKLY)
    if today.day == 1:
        due.append(Period.MONTHLY)
    return due


def report_key(period: Period, current: DateRange) -> str:
    """What makes a report the same report, so a retried run does not send it twice."""
    return f"{period.value}:{current.start.isoformat()}"


# --- the report ---------------------------------------------------------------

@dataclass(frozen=True)
class UsageFigures:
    """Everything one period's report says, before it is worded."""

    daily: Mapping[date, Mapping[str, int]]
    active_accounts: int
    new_accounts: int

    def total(self, key: str) -> int:
        return sum(counts.get(key, 0) for counts in self.daily.values())

    def on(self, day: date, *keys: str) -> int:
        counts = self.daily.get(day, {})
        return sum(counts.get(key, 0) for key in keys)


class CountsCreated(Protocol):
    def count_created(self, start: float, end: float) -> int: ...


def gather(store: UsageStore, accounts: CountsCreated, period: DateRange) -> UsageFigures:
    return UsageFigures(
        daily=store.daily(period.start, period.end),
        active_accounts=store.active_accounts(period.start, period.end),
        new_accounts=accounts.count_created(*period.timestamps()),
    )


E = UsageEvent

#: The report, section by section: a heading, then (label, counter) lines.
SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Visitors", (
        ("Page loads, Danish", "page_load_da"),
        ("Page loads, English", "page_load_en"),
        ("Page loads, other languages", "page_load_other"),
    )),
    ("Elections", (
        ("Picked from the list", E.ELECTION_PICKED.value),
    )),
    ("Imports", (
        ("Started", E.IMPORT_STARTED.value),
        ("Saved: results", E.IMPORT_SAVED_RESULT.value),
        ("Saved: forecasts", E.IMPORT_SAVED_FORECAST.value),
        ("Previews discarded", E.PREVIEW_DISCARDED.value),
        ("Failed", E.IMPORT_FAILED.value),
    )),
    ("Requests from accounts without a subscription", (
        ("Filed as a new issue", E.REQUEST_FILED.value),
        ("Already requested", E.REQUEST_DUPLICATE.value),
        ("Already imported", E.REQUEST_ALREADY_IMPORTED.value),
    )),
    ("Paywall", (
        ("Import refused: no subscription", E.REFUSED_NO_SUBSCRIPTION.value),
        ("Import refused: monthly limit", E.REFUSED_LIMIT_REACHED.value),
        ("Checkouts started", E.CHECKOUT_STARTED.value),
        ("Subscriptions started", E.SUBSCRIPTION_STARTED.value),
        ("Subscriptions ended", E.SUBSCRIPTION_ENDED.value),
    )),
)

LABEL_WIDTH = 34


def _line(label: str, now: int, before: int) -> str:
    change = now - before
    sign = "+" if change > 0 else ("-" if change < 0 else "±")
    return f"  {label:<{LABEL_WIDTH}}{now:>7}   ({sign}{abs(change)})"


def build_report(
    period: Period,
    current: DateRange,
    figures: UsageFigures,
    previous: DateRange,
    before: UsageFigures,
) -> tuple[str, str]:
    """The subject and plain-text body of one report."""
    subject = f"Koalitionsberegner {period.value} usage: {current.describe()}"
    lines = [
        f"Usage for {current.describe()} (UTC).",
        f"In brackets: the change since {previous.describe()}.",
    ]
    for heading, rows in SECTIONS:
        lines += ["", heading]
        lines += [_line(label, figures.total(key), before.total(key)) for label, key in rows]
    lines += [
        "",
        "Accounts",
        _line("New", figures.new_accounts, before.new_accounts),
        _line("Active (distinct)", figures.active_accounts, before.active_accounts),
    ]

    if period is not Period.DAILY:
        lines += ["", "Day by day", f"  {'Day':<12}{'Loads':>7}{'Picked':>8}{'Imports':>9}{'Requests':>10}"]
        for day in current.days():
            lines.append(
                f"  {day.isoformat():<12}"
                f"{figures.on(day, 'page_load_da', 'page_load_en', 'page_load_other'):>7}"
                f"{figures.on(day, E.ELECTION_PICKED.value):>8}"
                f"{figures.on(day, E.IMPORT_STARTED.value):>9}"
                f"{figures.on(day, E.REQUEST_FILED.value, E.REQUEST_DUPLICATE.value):>10}"
            )

    lines += [
        "",
        "Counts only: nothing in this report identifies a person or an election.",
    ]
    return subject, "\n".join(lines) + "\n"
