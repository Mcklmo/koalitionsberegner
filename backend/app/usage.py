"""How the app is used, counted per day, and the reports built from the counts.

The owner wants to know what people do with the app — pick an election, import
one, ask for one — without reading logs. What is kept for that is deliberately
thin, because the code is public and the people using it are in the EU:

* **Counters, not events.** A day is a handful of numbers: how many elections
  were picked, how many imports started. Nothing says who, which election, or
  from where, so the counters are not personal data once written. There are no
  accounts, so there is nothing per person to count either.
* **Nothing new in the browser.** Page loads are counted from the config
  request the page already makes, with the language the browser already sends.

Recording can never cost a request anything: :class:`UsageRecorder` swallows
every failure, and the endpoints run it after the response has gone.

The report itself is a pure function of the counts (:func:`build_report`), so
what the owner reads can be asserted without a store, a clock or a mail server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Protocol

from .observability import scrub

log = logging.getLogger(__name__)

#: Languages the page speaks; every other browser language is counted as one.
#: A fixed list rather than whatever the header says, so a caller cannot mint a
#: new counter per request.
PAGE_LANGUAGES = ("da", "en", "de")


class UsageEvent(str, Enum):
    """Everything counted. The value is the counter's name in storage."""

    ELECTION_PICKED = "election_picked"
    LINK_OPENED = "link_opened"
    IMPORT_STARTED = "import_started"
    IMPORT_FAILED = "import_failed"
    IMPORT_SAVED_RESULT = "import_saved_result"
    IMPORT_SAVED_FORECAST = "import_saved_forecast"
    PREVIEW_DISCARDED = "preview_discarded"
    REQUEST_FILED = "request_filed"
    REQUEST_DUPLICATE = "request_duplicate"
    REQUEST_ALREADY_IMPORTED = "request_already_imported"
    OUTREACH_QUEUED = "outreach_queued"
    OUTREACH_APPROVED = "outreach_approved"
    OUTREACH_REJECTED = "outreach_rejected"
    OUTREACH_POSTED = "outreach_posted"
    OUTREACH_FAILED = "outreach_failed"
    OUTREACH_EXPIRED = "outreach_expired"
    #: Plan 3, A5's "Tracked elections" report section. Counts only, like
    #: everything else here: which election was added, refreshed or parked is
    #: `GET /api/admin/tracked`'s job, not this report's (see
    #: `doc/contribute.md`'s "Rolling out tracked elections").
    TRACKED_ADDED = "tracked_added"
    TRACKED_REFRESHED = "tracked_refreshed"
    TRACKED_FAILED = "tracked_failed"
    TRACKED_PARKED = "tracked_parked"


def language_bucket(accept_language: str | None) -> str:
    """The page language a browser prefers: ``da``, ``en``, ``de``, or ``other``."""
    first = (accept_language or "").split(",")[0].split(";")[0].strip()
    primary = first.split("-")[0].lower()
    return primary if primary in PAGE_LANGUAGES else "other"


def page_load_key(accept_language: str | None) -> str:
    """The counter a page load is added to."""
    return f"page_load_{language_bucket(accept_language)}"


#: Channels a link the owner posted may be tagged with, as `?src=` values on a
#: shared-link URL. A fixed list for the same reason as :data:`PAGE_LANGUAGES`:
#: the value arrives from the open internet, and anything not listed here must
#: not be able to mint a counter of its own. Add a channel before using it in a
#: post, not after. See `doc/research/launch-berlin-mv-2026.md`.
CAMPAIGN_SOURCES = ("reddit", "press", "hn", "mastodon", "newsletter")


def entry_key(source: str | None) -> str | None:
    """The counter a tagged entry belongs to, or None for a source we do not know.

    None is not an error: a stranger can put any `?src=` on a link they were
    given, and the only right answer to one we never posted is to ignore it.
    """
    return f"entry_{source}" if source in CAMPAIGN_SOURCES else None


#: Searches that matched no election are counted per (place, year) per day.
#: Free text from the open internet, so the key is cut down to ASCII letters
#: and a year, and a day holds at most this many distinct ones.
MISSED_PREFIX = "missed_"
MISSED_DAILY_CAP = 200
_MISSED_PLACE = re.compile(r"[a-z]+(?: [a-z]+){0,3}")
#: Letters only, joined by spaces or name punctuation; an email, a number or a sentence fails.
_PLAIN_NAME = re.compile(r"[^\W\d_]+(?:[ '.,-]+[^\W\d_]+)*")
_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "å": "a", "Å": "A", "ß": "ss"})


def missed_key(nation: str, subnation: str | None, year: int | None) -> str | None:
    """The counter for a search nobody could answer, or None if it is not a place."""
    # ponytail: non-Latin scripts fold to nothing and are dropped; transliterate if they matter.
    parts = []
    for text in (nation, subnation or ""):
        if text.strip() and not _PLAIN_NAME.fullmatch(text.strip()):
            return None
        ascii_text = unicodedata.normalize("NFKD", text.translate(_FOLD)).encode("ascii", "ignore").decode().lower()
        ascii_text = " ".join(re.sub(r"[^a-z]+", " ", ascii_text).split())
        if ascii_text and not _MISSED_PLACE.fullmatch(ascii_text):
            return None
        if ascii_text:
            parts.append(ascii_text)
    if not parts or len(" ".join(parts)) > 40:
        return None
    if year is not None and not 1900 <= year <= 2100:
        return None
    place = "__".join(part.replace(" ", "_") for part in parts)
    return MISSED_PREFIX + (f"{place}_{year}" if year else place)


class UsageStore(Protocol):
    """Storage seam for the counters. Ranges are ``[start, end)`` in UTC days."""

    def increment(self, day: date, key: str) -> None: ...

    def daily(self, start: date, end: date) -> dict[date, dict[str, int]]:
        """Every counter of every day in the range that has any."""
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
        self._reports: set[str] = set()
        self._lock = Lock()

    def increment(self, day: date, key: str) -> None:
        with self._lock:
            counts = self._counts.setdefault(day, {})
            counts[key] = counts.get(key, 0) + 1

    def daily(self, start: date, end: date) -> dict[date, dict[str, int]]:
        with self._lock:
            return {day: dict(counts) for day, counts in self._counts.items() if start <= day < end}

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

    def today(self) -> date:
        return self._clock().astimezone(timezone.utc).date()

    def record(self, event: UsageEvent | str) -> None:
        key = event.value if isinstance(event, UsageEvent) else event
        try:
            self.store.increment(self.today(), key)
        except Exception as exc:  # noqa: BLE001 - statistics must not fail a request
            log.warning("usage %s not recorded: %s", scrub(key), scrub(exc))


    def record_missed(self, key: str) -> bool:
        """Count a missed search unless the day already holds the cap of distinct ones."""
        try:
            today = self.today()
            known = self.store.daily(today, today + timedelta(days=1)).get(today, {})
            if key not in known and sum(k.startswith(MISSED_PREFIX) for k in known) >= MISSED_DAILY_CAP:
                return False
            self.store.increment(today, key)
            return True
        except Exception as exc:  # noqa: BLE001 - statistics must not fail a request
            log.warning("missed search not recorded: %s", scrub(exc))
            return False


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

    def total(self, key: str) -> int:
        return sum(counts.get(key, 0) for counts in self.daily.values())

    def on(self, day: date, *keys: str) -> int:
        counts = self.daily.get(day, {})
        return sum(counts.get(key, 0) for key in keys)


def gather(store: UsageStore, period: DateRange) -> UsageFigures:
    return UsageFigures(daily=store.daily(period.start, period.end))


E = UsageEvent

#: The report, section by section: a heading, then (label, counter) lines.
SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Visitors", (
        ("Page loads, Danish", "page_load_da"),
        ("Page loads, English", "page_load_en"),
        ("Page loads, German", "page_load_de"),
        ("Page loads, other languages", "page_load_other"),
    )),
    ("Elections", (
        ("Picked from the list", E.ELECTION_PICKED.value),
        ("Shared-link previews built (at most once per link per hour, crawlers included)",
         E.LINK_OPENED.value),
    )),
    ("Imports", (
        ("Started", E.IMPORT_STARTED.value),
        ("Saved: results", E.IMPORT_SAVED_RESULT.value),
        ("Saved: forecasts", E.IMPORT_SAVED_FORECAST.value),
        ("Previews discarded", E.PREVIEW_DISCARDED.value),
        ("Failed", E.IMPORT_FAILED.value),
    )),
    ("Requests", (
        ("Filed as a new issue", E.REQUEST_FILED.value),
        ("Already requested", E.REQUEST_DUPLICATE.value),
        ("Already imported", E.REQUEST_ALREADY_IMPORTED.value),
    )),
    ("Outreach", (
        ("Drafts queued", E.OUTREACH_QUEUED.value),
        ("Approved and sent", E.OUTREACH_APPROVED.value),
        ("Rejected", E.OUTREACH_REJECTED.value),
        ("Posted", E.OUTREACH_POSTED.value),
        ("Failed to post", E.OUTREACH_FAILED.value),
        ("Expired unapproved", E.OUTREACH_EXPIRED.value),
    )),
    ("Tagged entry links", tuple(
        (f"Opened from {source}", f"entry_{source}") for source in CAMPAIGN_SOURCES
    )),
    ("Tracked elections", (
        ("Added by the calendar scan", E.TRACKED_ADDED.value),
        ("Refresh ticks run", E.TRACKED_REFRESHED.value),
        ("Failed a tick", E.TRACKED_FAILED.value),
        ("Parked after repeated failures", E.TRACKED_PARKED.value),
    )),
)

#: Every page-load counter, so a total over them cannot quietly miss a language
#: the way the day-by-day table missed German until 2026-09-22.
PAGE_LOAD_KEYS = tuple(f"page_load_{bucket}" for bucket in (*PAGE_LANGUAGES, "other"))

LABEL_WIDTH = 34


def _line(label: str, now: int, before: int) -> str:
    change = now - before
    sign = "+" if change > 0 else ("-" if change < 0 else "±")
    return f"  {label:<{LABEL_WIDTH}}{now:>7}   ({sign}{abs(change)})"


def _missed_lines(figures: UsageFigures, top: int = 15) -> list[str]:
    totals: dict[str, int] = {}
    for counts in figures.daily.values():
        for key, count in counts.items():
            if key.startswith(MISSED_PREFIX):
                totals[key] = totals.get(key, 0) + count
    if not totals:
        return ["  Nothing."]
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return [
        f"  {key[len(MISSED_PREFIX):].replace('__', ' / ').replace('_', ' '):<{LABEL_WIDTH}}{count:>7}"
        for key, count in ranked
    ]


@dataclass(frozen=True)
class Freshness:
    """One tracked election's age: what the owner reads to decide on a refetch."""

    label: str
    held: date | None
    fetched: date | None
    parked: bool = False

    def needs_refetch(self, today: date) -> bool:
        """Held, and not read since the day it was held (or never read at all)."""
        if self.held is None or self.held > today:
            return False
        return self.parked or self.fetched is None or self.fetched <= self.held


def _freshness_lines(rows: Sequence[Freshness], today: date) -> list[str]:
    if not rows:
        return ["  Nothing tracked."]
    never = date.min
    lines = []
    for row in sorted(rows, key=lambda r: (r.fetched or never, r.label)):
        flag = "   <- refetch?" if row.needs_refetch(today) else ""
        note = " (parked)" if row.parked else ""
        lines.append(
            f"  {row.label}: held {row.held or 'date unknown'}, "
            f"fetched {row.fetched or 'never'}{note}{flag}"
        )
    return lines


def build_report(
    period: Period,
    current: DateRange,
    figures: UsageFigures,
    previous: DateRange,
    before: UsageFigures,
    freshness: Sequence[Freshness] | None = None,
    today: date | None = None,
) -> tuple[str, str]:
    """The subject and plain-text body of one report.

    ``freshness`` is given only to the report that should carry it; ``today``
    is what "held" is judged against.
    """
    subject = f"Koalitionsberegner {period.value} usage: {current.describe()}"
    lines = [
        f"Usage for {current.describe()} (UTC).",
        f"In brackets: the change since {previous.describe()}.",
    ]
    for heading, rows in SECTIONS:
        lines += ["", heading]
        lines += [_line(label, figures.total(key), before.total(key)) for label, key in rows]

    lines += ["", "Searched, not found (top 15)"]
    lines += _missed_lines(figures)

    if period is not Period.DAILY:
        lines += ["", "Day by day", f"  {'Day':<12}{'Loads':>7}{'Picked':>8}{'Imports':>9}{'Requests':>10}"]
        for day in current.days():
            lines.append(
                f"  {day.isoformat():<12}"
                f"{figures.on(day, *PAGE_LOAD_KEYS):>7}"
                f"{figures.on(day, E.ELECTION_PICKED.value):>8}"
                f"{figures.on(day, E.IMPORT_STARTED.value):>9}"
                f"{figures.on(day, E.REQUEST_FILED.value, E.REQUEST_DUPLICATE.value):>10}"
            )

    if freshness is not None:
        lines += ["", "Data freshness (oldest fetch first; nothing is refetched on its own)"]
        lines += _freshness_lines(freshness, today or current.end)

    lines += ["", "Nothing in this report identifies a person."]
    return subject, "\n".join(lines) + "\n"
