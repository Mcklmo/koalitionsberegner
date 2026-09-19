"""Usage counting, and the reports built from it.

What has to hold: every interaction the owner asked about is counted where it
happens and only there; nothing that points at a person outlives its retention;
a report goes out once however often the schedule fires; and counting can never
break the request it counts.
"""

from __future__ import annotations

import smtplib
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.accounts import InMemoryAccountStore, QuotaPolicy, Tier, billing_period
from app.auth import StoreBackedVerifier, StubCredentials
from app.billing import BillingEvent, DisabledBilling
from app.config import SMTP_VARIABLES, ConfigError, get_mailer
from app.firestore_usage import FirestoreUsageStore
from app.mailer import MailUnavailable, SmtpMailer
from app.service import ImportService
from app.sqlite_usage import SqliteUsageStore
from app.store import InMemoryElectionStore
from app.usage import (
    ACTIVE_RETENTION_DAYS,
    DateRange,
    InMemoryUsageStore,
    Period,
    UsageEvent,
    UsageFigures,
    UsageRecorder,
    account_marker,
    build_report,
    due_periods,
    marker_expiry,
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


@pytest.mark.parametrize("today", [date(2026, 9, 1), date(2027, 1, 1), date(2028, 3, 1)])
def test_retention_keeps_every_day_a_monthly_report_compares(today):
    """September 1st is the worst case: July and August are 62 days together."""
    current = period_range(Period.MONTHLY, today)
    previous = previous_range(Period.MONTHLY, current)
    assert previous.start >= today - timedelta(days=ACTIVE_RETENTION_DAYS)


# --- what is kept ----------------------------------------------------------------

@pytest.mark.parametrize(
    "header, bucket",
    [
        ("da-DK,da;q=0.9,en;q=0.8", "da"),
        ("en-GB", "en"),
        ("DA", "da"),
        ("de-DE,en;q=0.5", "other"),
        ("", "other"),
        (None, "other"),
        ("x" * 500, "other"),
    ],
)
def test_a_browser_language_lands_in_one_of_three_counters(header, bucket):
    assert language_bucket(header) == bucket


def test_an_account_marker_is_stable_and_is_not_the_account_id():
    marker = account_marker("firebase-uid-123")
    assert marker == account_marker("firebase-uid-123")
    assert marker != account_marker("firebase-uid-124")
    assert "firebase-uid-123" not in marker
    assert len(marker) == 16


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


def test_an_account_active_on_several_days_is_one_account(usage_store):
    for day in (date(2026, 9, 1), date(2026, 9, 2)):
        usage_store.mark_active(day, "aaaa")
        usage_store.mark_active(day, "aaaa")
    usage_store.mark_active(date(2026, 9, 2), "bbbb")

    assert usage_store.active_accounts(date(2026, 9, 1), date(2026, 9, 3)) == 2
    assert usage_store.active_accounts(date(2026, 9, 1), date(2026, 9, 2)) == 1


def test_retention_forgets_old_markers_and_nothing_else(usage_store):
    old, kept = date(2026, 7, 1), date(2026, 7, 2)
    usage_store.mark_active(old, "aaaa")
    usage_store.mark_active(kept, "bbbb")
    usage_store.increment(old, "election_picked")

    usage_store.forget_active_before(kept)

    assert usage_store.active_accounts(old, kept) == 0
    assert usage_store.active_accounts(kept, kept + timedelta(days=1)) == 1
    assert usage_store.daily(old, kept) == {old: {"election_picked": 1}}, "counters are kept"


def test_a_marker_expires_on_the_day_retention_would_first_forget_it():
    day = date(2026, 7, 1)
    expires = marker_expiry(day)
    assert expires.tzinfo is UTC and expires.time() == time()

    store = InMemoryUsageStore()
    store.mark_active(day, "aaaa")
    run = lambda today: store.forget_active_before(today - timedelta(days=ACTIVE_RETENTION_DAYS))

    run(expires.date() - timedelta(days=1))
    assert store.active_accounts(day, day + timedelta(days=1)) == 1, "the run the day before keeps it"
    run(expires.date())
    assert store.active_accounts(day, day + timedelta(days=1)) == 0


class FakeFirestoreRef:
    """Just enough of a Firestore client to see what a document is written with."""

    def __init__(self, writes, path=()):
        self._writes, self._path = writes, path

    def collection(self, name):
        return FakeFirestoreRef(self._writes, (*self._path, name))

    document = collection

    def set(self, data, merge=False):
        self._writes["/".join(self._path)] = data


def test_a_firestore_marker_carries_the_expiry_its_ttl_policy_deletes_on():
    """Retention must not hang on the daily schedule running."""
    writes = {}
    FirestoreUsageStore(FakeFirestoreRef(writes)).mark_active(date(2026, 7, 1), "aaaa")

    assert writes == {
        "usage_daily/2026-07-01/active/aaaa": {"expire_at": marker_expiry(date(2026, 7, 1))}
    }


def test_a_report_is_claimed_once_and_can_be_given_back(usage_store):
    assert usage_store.claim_report("daily:2026-09-13") is True
    assert usage_store.claim_report("daily:2026-09-13") is False
    usage_store.release_report("daily:2026-09-13")
    assert usage_store.claim_report("daily:2026-09-13") is True


class BrokenStore(InMemoryUsageStore):
    def increment(self, day, key):
        raise RuntimeError("database down")

    def mark_active(self, day, marker):
        raise RuntimeError("database down")


def test_counting_never_raises(caplog):
    recorder = UsageRecorder(BrokenStore())

    recorder.record(UsageEvent.ELECTION_PICKED)
    recorder.active("user-1")

    assert "not recorded" in caplog.text


class MarkCountingStore(InMemoryUsageStore):
    def __init__(self):
        super().__init__()
        self.marks = 0

    def mark_active(self, day, marker):
        self.marks += 1
        super().mark_active(day, marker)


def test_an_account_is_written_down_once_a_day_per_process():
    store = MarkCountingStore()
    now = [datetime(2026, 9, 13, 23, 59, tzinfo=UTC)]
    recorder = UsageRecorder(store, clock=lambda: now[0])

    recorder.active("user-1")
    recorder.active("user-1")
    assert store.marks == 1

    now[0] += timedelta(minutes=2)
    recorder.active("user-1")
    assert store.marks == 2, "a new day is a new mark"
    assert store.active_accounts(date(2026, 9, 13), date(2026, 9, 15)) == 1


# --- the report --------------------------------------------------------------------

def figures(daily=None, *, active=0, new=0) -> UsageFigures:
    return UsageFigures(daily=daily or {}, active_accounts=active, new_accounts=new)


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
        figures(
            {SUNDAY: {"election_picked": 5, "request_filed": 2, "page_load_da": 9}},
            active=3,
            new=1,
        ),
        previous,
        figures({previous.start: {"election_picked": 7, "page_load_da": 9}}, active=3),
    )

    assert subject == "Koalitionsberegner daily usage: 2026-09-13"
    assert row(body, "Picked from the list") == ["5", "(-2)"]
    assert row(body, "Filed as a new issue") == ["2", "(+2)"]
    assert row(body, "Page loads, Danish") == ["9", "(±0)"]
    assert row(body, "New") == ["1", "(+1)"]
    assert row(body, "Active (distinct)") == ["3", "(±0)"]
    assert "Day by day" not in body, "a single day needs no table of days"


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
FREE = {"Authorization": "Bearer free-1:free@example.org"}
SUBSCRIBER = {"Authorization": "Bearer paid-1:paid@example.org"}
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


class FakeBilling(DisabledBilling):
    """Hands the webhook whatever event the test puts here."""

    def __init__(self):
        self.event: BillingEvent | None = None

    def event_from_webhook(self, payload, signature):
        return self.event


@pytest.fixture
def usage():
    return UsageRecorder(InMemoryUsageStore(), clock=lambda: NOW)


@pytest.fixture
def accounts():
    store = InMemoryAccountStore()
    store.ensure("paid-1", "paid@example.org")
    store.set_subscription("paid-1", Tier.BASIC, status="active")
    return store


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
def billing():
    return FakeBilling()


@pytest.fixture
def client(usage, accounts, parser, mailer, wishlist, billing):
    store = InMemoryElectionStore()
    overrides = main.app.dependency_overrides
    overrides[main.get_service] = lambda: ImportService(store, parser)
    overrides[main.get_token_verifier] = lambda: StoreBackedVerifier(StubCredentials())
    overrides[main.get_account_store] = lambda: accounts
    overrides[main.get_policy] = lambda: QuotaPolicy({Tier.FREE: 0, Tier.BASIC: 5})
    overrides[main.get_billing_provider] = lambda: billing
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
    response = client.post("/api/elections/import?wait_seconds=2", json=body, headers=SUBSCRIBER)
    assert response.json()["state"] == "preview", response.text
    return response.json()


def save(client, body=BODY):
    saved = client.post(
        f"/api/elections/imports/{preview(client, body)['request_key']}/confirm",
        headers=SUBSCRIBER,
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def test_a_page_load_is_counted_by_the_language_the_browser_asks_for(client, usage):
    client.get("/api/config", headers={"accept-language": "da-DK,da;q=0.9"})
    client.get("/api/config", headers={"accept-language": "de-DE"})

    assert counted(usage) == {"page_load_da": 1, "page_load_other": 1}


def test_an_import_is_counted_from_its_start_to_it_being_saved_and_picked(client, usage):
    saved = save(client)
    again = client.post("/api/elections/import?wait_seconds=2", json=BODY, headers=SUBSCRIBER)
    assert again.json()["state"] == "ready"
    client.get(f"/api/elections/{saved['election_hash']}", headers=FREE)

    assert counted(usage) == {
        "import_started": 1,
        "import_saved_result": 1,
        "election_picked": 1,
    }, "an election served from the store started no import"


def test_a_discarded_preview_is_counted(client, usage):
    key = preview(client, OTHER_BODY)["request_key"]
    assert client.delete(f"/api/elections/imports/{key}/preview", headers=SUBSCRIBER).status_code == 204

    assert counted(usage)["preview_discarded"] == 1


def test_a_failed_import_is_counted(client, usage, parser):
    parser.fail_times = 1
    failed = client.post("/api/elections/import?wait_seconds=2", json=BODY, headers=SUBSCRIBER)
    assert failed.json()["state"] == "failed"

    assert counted(usage) == {"import_started": 1, "import_failed": 1}


def test_what_a_free_account_asks_for_is_counted_by_what_came_of_it(client, usage, wishlist):
    assert client.post("/api/elections/requests", json=OTHER_BODY, headers=FREE).status_code == 201
    wishlist.duplicate = True
    assert client.post("/api/elections/requests", json=OTHER_BODY, headers=FREE).status_code == 200
    save(client)
    assert client.post("/api/elections/requests", json=BODY, headers=FREE).status_code == 409

    assert {k: v for k, v in counted(usage).items() if k.startswith("request")} == {
        "request_filed": 1,
        "request_duplicate": 1,
        "request_already_imported": 1,
    }


def test_an_account_is_active_once_a_day_however_often_it_loads_the_page(client, usage):
    for headers in (SUBSCRIBER, SUBSCRIBER, FREE):
        assert client.get("/api/me", headers=headers).status_code == 200

    assert usage.store.active_accounts(MONDAY, MONDAY + timedelta(days=1)) == 2


def test_a_subscription_counts_when_it_starts_and_when_it_ends_not_on_every_update(
    client, usage, accounts, billing
):
    accounts.ensure("free-1", "free@example.org")

    def webhook(tier, status):
        billing.event = BillingEvent(
            uid="free-1", tier=tier, status=status, customer_id="cus_1", subscription_id="sub_1"
        )
        assert client.post("/api/billing/webhook", content=b"{}").status_code == 200

    webhook(Tier.BASIC, "active")
    webhook(Tier.BASIC, "active")
    webhook(Tier.FREE, "canceled")

    assert counted(usage) == {"subscription_started": 1, "subscription_ended": 1}


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


def test_every_run_forgets_active_accounts_older_than_retention(client, usage, mailer, monkeypatch):
    monkeypatch.setenv("USAGE_REPORT_SECRET", REPORT_SECRET)
    mailer.fail = True  # retention does not wait for the email to work
    expired = MONDAY - timedelta(days=ACTIVE_RETENTION_DAYS + 1)
    kept = MONDAY - timedelta(days=ACTIVE_RETENTION_DAYS)
    usage.store.mark_active(expired, "aaaa")
    usage.store.mark_active(kept, "bbbb")

    client.post(f"{REPORTS_URL}?period=daily", headers=REPORT)

    assert usage.store.active_accounts(expired, kept) == 0
    assert usage.store.active_accounts(kept, kept + timedelta(days=1)) == 1


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
    assert client.get("/api/admin/usage", headers=FREE).status_code == 403
