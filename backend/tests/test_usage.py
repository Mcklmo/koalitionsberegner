"""Usage counting, and the reports built from it.

What has to hold: every interaction the owner asked about is counted where it
happens and only there; nothing points at a person; a report goes out once
however often the schedule fires; and counting can never break the request it
counts.
"""

from __future__ import annotations

import smtplib
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import SMTP_VARIABLES, ConfigError, get_mailer
from app.mailer import MailUnavailable, SmtpMailer
from app.service import ImportService
from app.sqlite_usage import SqliteUsageStore
from app.store import InMemoryElectionStore
from app.usage import (
    CAMPAIGN_SOURCES,
    DateRange,
    Freshness,
    InMemoryUsageStore,
    Period,
    UsageEvent,
    UsageFigures,
    UsageRecorder,
    build_report,
    due_periods,
    entry_key,
    language_bucket,
    period_range,
    previous_range,
)
from app.wishlist import FiledRequest
from tests.factories import CountingParser

UTC = timezone.utc
#: A Monday, at the hour the cron runs.
NOW = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
MONDAY = NOW.date()
SUNDAY = MONDAY - timedelta(days=1)


# --- periods -------------------------------------------------------------------

def test_a_daily_report_covers_yesterday():
    assert period_range(Period.DAILY, MONDAY) == DateRange(SUNDAY, MONDAY)


def test_a_weekly_report_covers_the_last_monday_to_sunday_whenever_it_is_asked():
    last_week = DateRange(date(2026, 9, 7), MONDAY)
    assert period_range(Period.WEEKLY, MONDAY) == last_week
    assert period_range(Period.WEEKLY, date(2026, 9, 17)) == last_week


def test_a_monthly_report_covers_the_last_calendar_month_across_a_new_year_and_a_leap_day():
    assert period_range(Period.MONTHLY, date(2027, 1, 1)) == DateRange(
        date(2026, 12, 1), date(2027, 1, 1)
    )
    february = period_range(Period.MONTHLY, date(2028, 3, 1))
    assert february == DateRange(date(2028, 2, 1), date(2028, 3, 1))
    assert len(february.days()) == 29


@pytest.mark.parametrize("period", list(Period))
def test_each_period_is_compared_with_the_one_just_before_it(period):
    current = period_range(period, date(2028, 3, 1))
    previous = previous_range(period, current)
    assert previous.end == current.start
    assert period_range(period, current.end) == current


def test_week_and_month_ends_add_their_reports_to_the_daily_one():
    assert due_periods(SUNDAY) == [Period.DAILY]
    assert due_periods(MONDAY) == [Period.DAILY, Period.WEEKLY]
    assert due_periods(date(2026, 10, 1)) == [Period.DAILY, Period.MONTHLY]
    assert due_periods(date(2026, 6, 1)) == [Period.DAILY, Period.WEEKLY, Period.MONTHLY]


# --- what is kept ----------------------------------------------------------------

@pytest.mark.parametrize(
    "header, bucket",
    [
        ("da-DK,da;q=0.9,en;q=0.8", "da"),
        ("en-GB", "en"),
        ("DA", "da"),
        ("de-DE,en;q=0.5", "de"),
        ("fr-FR,en;q=0.5", "other"),
        ("", "other"),
        (None, "other"),
        ("x" * 500, "other"),
    ],
)
def test_a_browser_language_lands_in_a_counter_for_a_language_the_page_speaks(header, bucket):
    assert language_bucket(header) == bucket


@pytest.fixture(params=["memory", "sqlite"])
def usage_store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryUsageStore()
        return
    store = SqliteUsageStore(tmp_path / "usage.db")
    yield store
    store.close()


def test_counters_add_up_per_day(usage_store):
    first, second = date(2026, 9, 1), date(2026, 9, 2)
    for _ in range(3):
        usage_store.increment(first, "election_picked")
    usage_store.increment(second, "election_picked")
    usage_store.increment(second, "import_started")

    assert usage_store.daily(first, second + timedelta(days=1)) == {
        first: {"election_picked": 3},
        second: {"election_picked": 1, "import_started": 1},
    }
    assert usage_store.daily(second, second + timedelta(days=1)) == {
        second: {"election_picked": 1, "import_started": 1}
    }


def test_a_report_is_claimed_once_and_can_be_given_back(usage_store):
    assert usage_store.claim_report("daily:2026-09-13") is True
    assert usage_store.claim_report("daily:2026-09-13") is False
    usage_store.release_report("daily:2026-09-13")
    assert usage_store.claim_report("daily:2026-09-13") is True


class BrokenStore(InMemoryUsageStore):
    def increment(self, day, key):
        raise RuntimeError("database down")


def test_counting_never_raises(caplog):
    recorder = UsageRecorder(BrokenStore())

    recorder.record(UsageEvent.ELECTION_PICKED)

    assert "not recorded" in caplog.text


def test_a_database_from_when_there_were_accounts_loses_its_markers(tmp_path):
    """The per-day account markers are dropped, not left behind unread."""
    import sqlite3

    path = tmp_path / "usage.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE usage_active (day TEXT, marker TEXT)")
    old.execute("INSERT INTO usage_active VALUES ('2026-09-01', 'aaaa')")
    old.commit()
    old.close()

    SqliteUsageStore(path).close()

    tables = {row[0] for row in sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    assert "usage_active" not in tables


# --- the report --------------------------------------------------------------------

def figures(daily=None) -> UsageFigures:
    return UsageFigures(daily=daily or {})


def row(body: str, label: str) -> list[str]:
    """The count and the change on the report line that starts with ``label``."""
    line = next(line for line in body.splitlines() if line.strip().startswith(label))
    return line.split()[-2:]


def test_the_report_counts_every_section_and_says_how_it_changed():
    current = period_range(Period.DAILY, MONDAY)
    previous = previous_range(Period.DAILY, current)

    subject, body = build_report(
        Period.DAILY,
        current,
        figures({SUNDAY: {"election_picked": 5, "request_filed": 2, "page_load_da": 9}}),
        previous,
        figures({previous.start: {"election_picked": 7, "page_load_da": 9}}),
    )

    assert subject == "Koalitionsberegner daily usage: 2026-09-13"
    assert row(body, "Picked from the list") == ["5", "(-2)"]
    assert row(body, "Filed as a new issue") == ["2", "(+2)"]
    assert row(body, "Page loads, Danish") == ["9", "(±0)"]
    assert "Accounts" not in body and "Paywall" not in body, "there are none of either"
    assert "Day by day" not in body, "a single day needs no table of days"


def test_the_freshness_section_flags_what_was_held_and_not_read_since():
    current = period_range(Period.DAILY, MONDAY)
    previous = previous_range(Period.DAILY, current)
    held = date(2026, 9, 1)
    rows = [
        Freshness("Never read 2026", held, None),
        Freshness("Read the day it was held 2026", held, held),
        Freshness("Read later 2026", held, date(2026, 9, 10)),
        Freshness("Parked 2026", held, date(2026, 9, 10), parked=True),
        Freshness("Upcoming 2027", date(2027, 5, 1), None),
    ]

    _, body = build_report(
        Period.DAILY, current, figures({}), previous, figures({}), rows, MONDAY
    )

    flagged = [line for line in body.splitlines() if "refetch?" in line]
    assert [line.split("  ")[1] for line in flagged] == [
        "Never read 2026", "Read the day it was held 2026", "Parked 2026",
    ]
    lines = [line.split() for line in body.splitlines()]
    assert ["Never", "read", "2026", "2026-09-01", "never", "<-", "refetch?"] in lines
    assert ["Upcoming", "2027", "2027-05-01", "never"] in lines, "not held yet, so no flag"


def test_the_report_has_no_freshness_section_unless_it_is_given_rows():
    current = period_range(Period.DAILY, MONDAY)
    previous = previous_range(Period.DAILY, current)
    _, body = build_report(Period.DAILY, current, figures({}), previous, figures({}))
    assert "Data freshness" not in body


def test_the_tracked_elections_section_counts_added_refreshed_parked_and_failed():
    """Plan 3, A5's report section, carried over from the section A pass that
    left it out: numbers only, matching GET /api/admin/tracked's own columns
    without naming a single election."""
    current = period_range(Period.DAILY, MONDAY)
    previous = previous_range(Period.DAILY, current)

    _, body = build_report(
        Period.DAILY,
        current,
        figures({SUNDAY: {
            "tracked_added": 2, "tracked_refreshed": 5, "tracked_failed": 1, "tracked_parked": 1,
        }}),
        previous,
        figures({}),
    )

    assert row(body, "Added by the calendar scan") == ["2", "(+2)"]
    assert row(body, "Refresh ticks run") == ["5", "(+5)"]
    assert row(body, "Failed a tick") == ["1", "(+1)"]
    assert row(body, "Parked after repeated failures") == ["1", "(+1)"]


def test_the_link_counter_is_labelled_by_what_it_actually_counts():
    """It counts the Worker's card fetches to the origin (at most one per link
    per hour, cached), not how many people opened a link — see
    `backend/app/main.py`'s `get_card` and `worker/index.js`'s `fetchCard`."""
    current = period_range(Period.DAILY, MONDAY)
    previous = previous_range(Period.DAILY, current)

    _, body = build_report(
        Period.DAILY, current, figures({SUNDAY: {"link_opened": 3}}), previous, figures({}),
    )

    assert row(body, "Shared-link previews built") == ["3", "(+3)"]
    assert "at most once per link per hour" in body
    assert "links opened" not in body.lower()


def test_only_a_channel_we_posted_to_mints_a_counter():
    """A `?src=` value arrives from the open internet, so the allowlist — not
    the caller — decides whether there is a counter at all."""
    assert entry_key("reddit") == "entry_reddit"
    assert entry_key("REDDIT") is None, "the tag is matched exactly, not folded"
    assert entry_key("not-a-channel-we-use") is None
    assert entry_key(None) is None
    assert set(CAMPAIGN_SOURCES) == {"reddit", "press", "hn", "mastodon", "newsletter"}


def test_the_report_has_a_line_for_every_tagged_channel():
    current = period_range(Period.DAILY, MONDAY)
    _, body = build_report(
        Period.DAILY,
        current,
        figures({SUNDAY: {"entry_reddit": 12, "entry_press": 1}}),
        previous_range(Period.DAILY, current),
        figures({previous_range(Period.DAILY, current).start: {"entry_reddit": 4}}),
    )

    assert row(body, "Opened from reddit") == ["12", "(+8)"]
    assert row(body, "Opened from press") == ["1", "(+1)"]
    for source in CAMPAIGN_SOURCES:
        assert f"Opened from {source}" in body, "a silent channel still gets its line"


def test_the_day_by_day_loads_column_counts_every_language():
    """It counted Danish, English and 'other' but not German until 2026-09-22,
    which is the language two thirds of the stored elections are read in."""
    current = period_range(Period.WEEKLY, MONDAY)
    _, body = build_report(
        Period.WEEKLY,
        current,
        figures({date(2026, 9, 9): {
            "page_load_da": 1, "page_load_en": 2, "page_load_de": 4, "page_load_other": 8,
        }}),
        previous_range(Period.WEEKLY, current),
        figures(),
    )

    table = body.split("Day by day")[1]
    assert next(line for line in table.splitlines() if "2026-09-09" in line).split()[1] == "15"


def test_a_weekly_report_adds_a_line_per_day():
    current = period_range(Period.WEEKLY, MONDAY)
    subject, body = build_report(
        Period.WEEKLY,
        current,
        figures({date(2026, 9, 9): {"import_started": 4}}),
        previous_range(Period.WEEKLY, current),
        figures(),
    )

    assert subject == "Koalitionsberegner weekly usage: 2026-09-07 to 2026-09-13"
    table = body.split("Day by day")[1].splitlines()
    days = [line.split()[0] for line in table if line.strip()[:4] == "2026"]
    assert days == [d.isoformat() for d in current.days()]
    assert next(line for line in table if "2026-09-09" in line).split() == [
        "2026-09-09", "0", "0", "4", "0"
    ]


# --- the mailer ---------------------------------------------------------------------

class FakeSmtp:
    def __init__(self, calls, host, port, timeout=None, context=None, fail=None):
        self.calls = calls
        self.fail = fail
        calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, username, password):
        if self.fail:
            raise self.fail
        self.calls.append(("login", username))

    def send_message(self, message):
        self.calls.append(("send", message["Subject"], message["To"]))


def smtp_mailer(port=587, fail=None):
    calls = []
    factory = lambda *args, **kwargs: FakeSmtp(calls, *args, fail=fail, **kwargs)  # noqa: E731
    mailer = SmtpMailer(
        "smtp.example.org", port,
        username="me@example.org", password="app-password",
        sender="me@example.org", recipients=["owner@example.org", "second@example.org"],
        smtp=factory, smtp_ssl=factory,
    )
    return mailer, calls


def test_the_password_is_sent_only_over_an_upgraded_connection():
    mailer, calls = smtp_mailer()
    mailer.send("Subject", "Body")
    assert calls == [
        ("connect", "smtp.example.org", 587),
        ("starttls",),
        ("login", "me@example.org"),
        ("send", "Subject", "owner@example.org, second@example.org"),
        ("quit",),
    ]


def test_the_report_keeps_its_columns_in_a_mail_client_with_a_proportional_font():
    mailer, _ = smtp_mailer()
    body = "Imports\n  Started                                 3   (+1)\n"

    message = mailer.message("Subject", body)

    plain = message.get_body(preferencelist=("plain",)).get_content()
    rich = message.get_body(preferencelist=("html",)).get_content()
    assert plain == body
    assert "  Started                                 3   (+1)" in rich
    assert rich.startswith("<pre") and "monospace" in rich


def test_port_465_is_encrypted_from_the_start():
    mailer, calls = smtp_mailer(port=465)
    mailer.send("Subject", "Body")
    assert ("starttls",) not in calls
    assert ("login", "me@example.org") in calls


def test_a_refusing_mail_server_says_nothing_about_itself():
    refusal = smtplib.SMTPAuthenticationError(535, b"5.7.8 Password not accepted for me@example.org")
    mailer, _ = smtp_mailer(fail=refusal)
    with pytest.raises(MailUnavailable) as raised:
        mailer.send("Subject", "Body")
    assert "Password" not in str(raised.value)
    assert "example.org" not in str(raised.value)


@pytest.fixture
def fresh_mailer(monkeypatch):
    for name in (*SMTP_VARIABLES, "SMTP_PORT", "REPORT_EMAIL_FROM"):
        monkeypatch.delenv(name, raising=False)
    get_mailer.cache_clear()
    yield
    get_mailer.cache_clear()


def test_with_no_smtp_settings_reports_are_off(fresh_mailer):
    assert get_mailer().enabled is False


def test_half_configured_email_stops_the_boot(monkeypatch, fresh_mailer):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_USERNAME", "me@example.org")
    with pytest.raises(ConfigError, match="SMTP_PASSWORD, REPORT_EMAIL_TO"):
        get_mailer()


def test_a_recipient_must_be_an_address(monkeypatch, fresh_mailer):
    for name, value in {
        "SMTP_HOST": "smtp.example.org", "SMTP_USERNAME": "me@example.org",
        "SMTP_PASSWORD": "pw", "REPORT_EMAIL_TO": "owner",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match="REPORT_EMAIL_TO"):
        get_mailer()


def test_complete_settings_send_from_the_login_unless_told_otherwise(monkeypatch, fresh_mailer):
    for name, value in {
        "SMTP_HOST": "smtp.example.org", "SMTP_USERNAME": "me@example.org",
        "SMTP_PASSWORD": "pw", "REPORT_EMAIL_TO": "a@example.org, b@example.org",
    }.items():
        monkeypatch.setenv(name, value)
    message = get_mailer().message("Subject", "Body")
    assert message["From"] == "me@example.org"
    assert message["To"] == "a@example.org, b@example.org"


# --- over HTTP --------------------------------------------------------------------

BODY = {"year": 2026, "nation": "Danmark"}
OTHER_BODY = {"year": 2021, "nation": "Deutschland", "subnation": "Sachsen-Anhalt"}
ADMIN_SECRET = "a" * 40
ADMIN = {"x-admin-secret": ADMIN_SECRET}
REPORT_SECRET = "r" * 40
REPORT = {"x-report-secret": REPORT_SECRET}
REPORTS_URL = "/api/internal/usage-reports"


class FakeMailer:
    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def send(self, subject, body):
        if self.fail:
            raise MailUnavailable("could not send the report")
        self.sent.append((subject, body))


class FakeWishlist:
    enabled = True

    def __init__(self):
        self.duplicate = False

    async def file(self, request):
        return FiledRequest(url="https://github.test/issues/1", number=1, duplicate=self.duplicate)


@pytest.fixture
def usage():
    return UsageRecorder(InMemoryUsageStore(), clock=lambda: NOW)


@pytest.fixture
def parser():
    return CountingParser()


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def wishlist():
    return FakeWishlist()


@pytest.fixture
def client(usage, parser, mailer, wishlist):
    store = InMemoryElectionStore()
    overrides = main.app.dependency_overrides
    overrides[main.get_service] = lambda: ImportService(store, parser)
    overrides[main.get_wishlist_provider] = lambda: wishlist
    overrides[main.get_usage] = lambda: usage
    overrides[main.get_mailer_provider] = lambda: mailer
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


def counted(usage) -> dict[str, int]:
    """Everything counted today."""
    return usage.store.daily(MONDAY, MONDAY + timedelta(days=1)).get(MONDAY, {})


def preview(client, body=BODY):
    response = client.post("/api/elections/import?wait_seconds=2", json=body)
    assert response.json()["state"] == "preview", response.text
    return response.json()


def save(client, body=BODY):
    saved = client.post(
        f"/api/elections/imports/{preview(client, body)['request_key']}/confirm"
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def test_a_page_load_is_counted_by_the_language_the_browser_asks_for(client, usage):
    client.get("/api/config", headers={"accept-language": "da-DK,da;q=0.9"})
    client.get("/api/config", headers={"accept-language": "de-DE"})
    client.get("/api/config", headers={"accept-language": "fr-FR"})

    assert counted(usage) == {"page_load_da": 1, "page_load_de": 1, "page_load_other": 1}


def test_an_import_is_counted_from_its_start_to_it_being_saved_and_picked(client, usage):
    saved = save(client)
    again = client.post("/api/elections/import?wait_seconds=2", json=BODY)
    assert again.json()["state"] == "ready"
    client.get(f"/api/elections/{saved['election_hash']}")

    assert counted(usage) == {
        "import_started": 1,
        "import_saved_result": 1,
        "election_picked": 1,
    }, "an election served from the store started no import"


def test_a_discarded_preview_is_counted(client, usage):
    key = preview(client, OTHER_BODY)["request_key"]
    assert client.delete(f"/api/elections/imports/{key}/preview").status_code == 204

    assert counted(usage)["preview_discarded"] == 1


def test_a_failed_import_is_counted(client, usage, parser):
    parser.fail_times = 1
    failed = client.post("/api/elections/import?wait_seconds=2", json=BODY)
    assert failed.json()["state"] == "failed"

    assert counted(usage) == {"import_started": 1, "import_failed": 1}


def test_what_a_visitor_asks_for_is_counted_by_what_came_of_it(client, usage, wishlist):
    assert client.post("/api/elections/requests", json=OTHER_BODY).status_code == 201
    wishlist.duplicate = True
    assert client.post("/api/elections/requests", json=OTHER_BODY).status_code == 200
    save(client)
    assert client.post("/api/elections/requests", json=BODY).status_code == 409

    assert {k: v for k, v in counted(usage).items() if k.startswith("request")} == {
        "request_filed": 1,
        "request_duplicate": 1,
        "request_already_imported": 1,
    }


ENTRY_URL = "/api/internal/entry"


def test_a_tagged_link_is_counted_per_channel(client, usage, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)

    assert client.post(f"{ENTRY_URL}?src=reddit", headers=REPORT).json() == {"counted": True}
    assert client.post(f"{ENTRY_URL}?src=reddit", headers=REPORT).json() == {"counted": True}
    assert client.post(f"{ENTRY_URL}?src=press", headers=REPORT).json() == {"counted": True}

    assert counted(usage) == {"entry_reddit": 2, "entry_press": 1}


def test_an_unknown_tag_is_ignored_rather_than_refused(client, usage, monkeypatch):
    """A stranger can put any `?src=` on a link they were given. Refusing it
    would make the Worker retry and log; counting it would let the open
    internet mint a counter. Neither: it is dropped."""
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)

    response = client.post(f"{ENTRY_URL}?src=whatever", headers=REPORT)

    assert response.status_code == 200
    assert response.json() == {"counted": False}
    assert counted(usage) == {}


def test_counting_an_entry_needs_the_schedule_secret(client, usage, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)

    assert client.post(f"{ENTRY_URL}?src=reddit").status_code == 403
    assert client.post(f"{ENTRY_URL}?src=reddit", headers={"x-report-secret": "r" * 39 + "x"}) \
        .status_code == 403
    assert counted(usage) == {}


def test_an_entry_with_no_tag_at_all_is_a_bad_request(client, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)

    assert client.post(ENTRY_URL, headers=REPORT).status_code == 422


def test_without_a_report_secret_there_is_no_report_endpoint(client, monkeypatch):
    monkeypatch.delenv("USAGE_REPORT_SECRET", raising=False)
    assert client.post(REPORTS_URL, headers=REPORT).status_code == 404


def test_the_schedule_has_to_prove_itself(client, mailer, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)
    assert client.post(REPORTS_URL).status_code == 403
    assert client.post(REPORTS_URL, headers={"x-report-secret": "r" * 39 + "x"}).status_code == 403
    assert mailer.sent == []


def test_a_monday_run_sends_the_daily_and_the_weekly_report_exactly_once(
    client, usage, mailer, monkeypatch
):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)
    usage.store.increment(SUNDAY, "election_picked")

    first = client.post(REPORTS_URL, headers=REPORT)
    second = client.post(REPORTS_URL, headers=REPORT)

    assert first.json() == {"sent": ["daily:2026-09-13", "weekly:2026-09-07"], "skipped": []}
    assert second.json() == {"sent": [], "skipped": ["daily:2026-09-13", "weekly:2026-09-07"]}
    assert [subject for subject, _ in mailer.sent] == [
        "Koalitionsberegner daily usage: 2026-09-13",
        "Koalitionsberegner weekly usage: 2026-09-07 to 2026-09-13",
    ]
    assert row(mailer.sent[0][1], "Picked from the list") == ["1", "(+1)"]


def test_a_report_that_could_not_be_emailed_is_sent_by_the_next_run(client, mailer, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)
    mailer.fail = True
    assert client.post(f"{REPORTS_URL}?period=daily", headers=REPORT).status_code == 502

    mailer.fail = False
    retried = client.post(f"{REPORTS_URL}?period=daily", headers=REPORT)
    assert retried.json() == {"sent": ["daily:2026-09-13"], "skipped": []}


def test_an_administrator_can_read_a_report_without_it_being_sent(
    client, usage, mailer, monkeypatch
):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    usage.store.increment(date(2026, 9, 10), "request_filed")

    response = client.get("/api/admin/usage?period=weekly&before=2026-09-14", headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["subject"] == "Koalitionsberegner weekly usage: 2026-09-07 to 2026-09-13"
    assert row(response.json()["body"], "Filed as a new issue") == ["1", "(+1)"]
    assert mailer.sent == []
    assert client.get("/api/admin/usage").status_code == 403


def test_missed_searches_are_cut_down_capped_and_reported():
    from app.usage import MISSED_DAILY_CAP, DateRange, Period, build_report, gather, missed_key

    assert missed_key("København", None, 2027) == "missed_kobenhavn_2027"
    assert missed_key("Germany", "Bavaria", None) == "missed_germany__bavaria"
    assert missed_key("a@b.com", None, None) is None
    assert missed_key("ring 0045", None, None) is None
    assert missed_key("x" * 60, None, None) is None

    recorder = UsageRecorder(InMemoryUsageStore())
    for n in range(MISSED_DAILY_CAP):
        assert recorder.record_missed(f"missed_k{n}")
    assert not recorder.record_missed("missed_one_too_many")
    assert recorder.record_missed("missed_k0"), "a key already on the day still counts"

    store = InMemoryUsageStore()
    recorder = UsageRecorder(store)
    for key in ("missed_denmark_2022",) * 3 + ("missed_norway",):
        recorder.record_missed(key)
    today = recorder.today()
    _, body = build_report(
        Period.DAILY, DateRange(today, today + timedelta(days=1)), gather(store, DateRange(today, today + timedelta(days=1))),
        DateRange(today - timedelta(days=1), today), gather(store, DateRange(today - timedelta(days=1), today)),
    )
    assert "denmark 2022" in body and "Searched, not found" in body


def test_missed_search_endpoint_counts_a_place_and_ignores_anything_else(client, usage):
    for body in ({"nation": "Fyn", "year": 2031}, {"nation": "Fyn", "year": 2031}, {"nation": "a@b.com"}):
        assert client.post("/api/elections/missed", json=body).status_code == 204
    assert {k: v for k, v in counted(usage).items() if k.startswith("missed_")} == {"missed_fyn_2031": 2}


def test_the_freshness_list_covers_every_stored_election_once_tracked_or_not():
    from app.main import _freshness
    from app.store import InMemoryElectionStore, InMemoryTrackedStore, TrackedElection, TrackedStatus
    from app.identity import request_key
    from app.service import identity_of
    from tests.factories import make_election

    elections, tracked = InMemoryElectionStore(), InMemoryTrackedStore()
    by_hand = make_election(nation="Danmark", election_date=date(2026, 3, 25))
    done = make_election(nation="Sverige", election_date=date(2026, 3, 26))
    for election in (by_hand, done):
        elections.put_election(identity_of(election), election, provenance="manual")
    tracked.add_tracked(TrackedElection(
        request_key=request_key(2026, "Sverige"), year=2026, nation="Sverige",
        status=TrackedStatus.FINAL,
    ))

    rows = _freshness(elections, tracked)

    assert [row.label for row in rows] == ["Danmark 2026"], "final is left out; by hand is not"
    assert rows[0].held == date(2026, 3, 25)


def test_a_refetch_row_carries_the_pages_own_link(monkeypatch):
    from app.main import _freshness
    from app.service import identity_of
    from app.share import share_id
    from app.store import InMemoryElectionStore, InMemoryTrackedStore
    from tests.factories import make_election

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/")
    elections = InMemoryElectionStore()
    election = make_election(nation="Danmark", election_date=date(2026, 3, 25))
    elections.put_election(identity_of(election), election, provenance="manual")

    (row,) = _freshness(elections, InMemoryTrackedStore())
    stored = elections.list_elections()[0]
    assert row.link == f"https://example.test/e/{share_id(stored.election_hash)}#refetch"

    _, body = build_report(
        Period.DAILY, period_range(Period.DAILY, MONDAY), figures({}),
        previous_range(Period.DAILY, period_range(Period.DAILY, MONDAY)), figures({}),
        [row], date(2026, 9, 1),
    )
    assert f"    {row.link}" in body.splitlines(), "flagged rows list their link under them"
