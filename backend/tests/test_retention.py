"""Accounts nobody uses are deleted.

The privacy policy promises this, so these properties have to hold: an account
unused for two years is deleted, and its sign-in with it; an account with a
subscription still running is never deleted; an account is never deleted just
because it has no date; a sign-in that could not be deleted is tried again on
the next run instead of being forgotten; and recording activity adds no write
to almost any request.

Store tests run against both backends that can persist. Firestore's
transactions cannot be faked faithfully, so its tests below cover only the
parts that run outside a transaction: dating accounts that have no date, and
listing idle accounts.
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound

from app import firestore_accounts, main
from app.accounts import (
    INACTIVE_ACCOUNT_RETENTION_DAYS,
    InMemoryAccountStore,
    Tier,
    UserAccount,
    _deletable,
    inactive_cutoff,
)
from app.auth import (
    FIREBASE_DELETE_USER_URL,
    BadCredentials,
    FirebaseIdentities,
    IdentityRemovalFailed,
    InvalidToken,
    NoIdentities,
)
from app.firestore_accounts import FirestoreAccountStore
from app.retention import sweep_inactive_accounts
from app.sqlite_accounts import SqliteAccountStore
from app.sqlite_auth import SqliteCredentialStore

DAY = 24 * 3600.0
NOW = 2_000_000_000.0
RETENTION = INACTIVE_ACCOUNT_RETENTION_DAYS * DAY
LONG_AGO = NOW - RETENTION - DAY
RECENTLY = NOW - RETENTION + DAY


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeIdentities:
    """Records whose sign-in was deleted; fails for the uids in ``refuse``."""

    def __init__(self):
        self.removed: list[str] = []
        self.refuse: set[str] = set()

    def remove(self, uid: str) -> None:
        if uid in self.refuse:
            raise IdentityRemovalFailed("HTTP 403 PERMISSION_DENIED")
        self.removed.append(uid)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def identities():
    return FakeIdentities()


@pytest.fixture(params=["memory", "sqlite"])
def accounts(request, tmp_path, clock):
    if request.param == "memory":
        yield InMemoryAccountStore(clock=clock)
        return
    store = SqliteAccountStore(tmp_path / "elections.db", clock=clock)
    yield store
    store.close()


def used_at(accounts, clock, uid: str, when: float, **subscription) -> None:
    """An account whose last sign-in was at ``when``, subscribed as ``subscription`` says."""
    clock.now = when
    accounts.ensure(uid, f"{uid}@example.org")
    if subscription:
        accounts.set_subscription(uid, **subscription)
    clock.now = NOW


def add_undated(accounts, uid: str) -> None:
    """An account stored before activity was recorded."""
    if isinstance(accounts, SqliteAccountStore):
        accounts._connect().execute("INSERT INTO accounts (uid) VALUES (?)", (uid,))
    else:
        accounts._accounts[uid] = UserAccount(uid=uid)


def sweep(accounts, identities, now: float = NOW, **limits):
    return sweep_inactive_accounts(accounts, identities, now=now, **limits)


# --- recording activity -----------------------------------------------------

def test_a_new_account_is_active_from_the_moment_it_is_created(accounts):
    account = accounts.ensure("u1", "u1@example.org")

    assert account.last_active_at == NOW
    assert accounts.get("u1").last_active_at == NOW


def test_activity_moves_forward_at_most_once_a_day(accounts, clock):
    accounts.ensure("u1", "u1@example.org")

    clock.now = NOW + DAY - 1
    assert accounts.ensure("u1", "u1@example.org").last_active_at == NOW
    clock.now = NOW + DAY
    assert accounts.ensure("u1", "u1@example.org").last_active_at == NOW + DAY
    assert accounts.get("u1").last_active_at == NOW + DAY


def test_a_new_address_is_saved_the_same_day_without_moving_the_date(accounts, clock):
    accounts.ensure("u1", "old@example.org")

    clock.now = NOW + 3600
    again = accounts.ensure("u1", "new@example.org")

    assert (again.email, again.last_active_at) == ("new@example.org", NOW)
    assert accounts.get("u1") == again


def test_a_request_within_the_day_writes_nothing(tmp_path, clock):
    """ensure runs on every signed-in request; activity must not make each one a write."""
    store = SqliteAccountStore(tmp_path / "elections.db", clock=clock)
    store.ensure("u1", "u1@example.org")
    writes = []
    take_lock = store._write

    def counting_write():
        writes.append(clock.now)
        return take_lock()

    store._write = counting_write
    try:
        clock.now = NOW + 3600
        for _ in range(5):
            store.ensure("u1", "u1@example.org")
        assert writes == []

        clock.now = NOW + DAY
        store.ensure("u1", "u1@example.org")
        assert writes == [NOW + DAY]
    finally:
        store.close()


def test_signing_in_dates_an_account_that_had_no_date(accounts):
    add_undated(accounts, "old")

    assert accounts.get("old").last_active_at is None
    assert accounts.ensure("old", "old@example.org").last_active_at == NOW


def test_an_accounts_table_from_before_activity_is_migrated_undated(tmp_path, clock):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (
            uid TEXT PRIMARY KEY, email TEXT, tier TEXT NOT NULL DEFAULT 'free',
            period TEXT NOT NULL DEFAULT '', used INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id TEXT, subscription_id TEXT, subscription_status TEXT,
            created_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO accounts (uid, email) VALUES ('old', 'old@example.org');
        """
    )
    conn.commit()
    conn.close()

    store = SqliteAccountStore(path, clock=clock)
    try:
        assert store.get("old").last_active_at is None
        assert sweep(store, FakeIdentities()).dated == 1
        assert store.get("old").last_active_at == NOW
    finally:
        store.close()


# --- the rule ---------------------------------------------------------------

@pytest.mark.parametrize(
    "account, deletable",
    [
        (UserAccount(uid="u", last_active_at=LONG_AGO), True),
        (UserAccount(uid="u", last_active_at=RECENTLY), False),
        (UserAccount(uid="u", last_active_at=inactive_cutoff(NOW)), False),
        (UserAccount(uid="u", last_active_at=None), False),
        (UserAccount(uid="u", tier=Tier.BASIC, subscription_status="active", last_active_at=LONG_AGO), False),
        (UserAccount(uid="u", tier=Tier.PREMIUM, subscription_status="trialing", last_active_at=LONG_AGO), False),
        (UserAccount(uid="u", subscription_status="past_due", last_active_at=LONG_AGO), False),
        (UserAccount(uid="u", subscription_status="unpaid", last_active_at=LONG_AGO), False),
        (UserAccount(uid="u", tier=Tier.BASIC, last_active_at=LONG_AGO), False),
        (UserAccount(uid="u", subscription_status="canceled", last_active_at=LONG_AGO), True),
        (UserAccount(uid="u", subscription_status="incomplete_expired", last_active_at=LONG_AGO), True),
    ],
    ids=[
        "idle-free", "recently-used", "exactly-at-the-cutoff", "undated", "paying", "trialing",
        "card-being-retried", "unpaid", "paid-tier-without-status", "cancelled", "never-completed",
    ],
)
def test_the_retention_rule(account, deletable):
    assert _deletable(account, inactive_cutoff(NOW)) is deletable


def test_retention_is_two_years():
    assert inactive_cutoff(NOW) == NOW - 730 * DAY


# --- a run --------------------------------------------------------------------

def test_an_unused_free_account_is_deleted_with_its_sign_in(accounts, clock, identities):
    used_at(accounts, clock, "idle", LONG_AGO)
    used_at(accounts, clock, "recent", RECENTLY)

    swept = sweep(accounts, identities)

    assert (swept.deleted, swept.kept, swept.failed) == (1, 0, 0)
    assert identities.removed == ["idle"]
    assert accounts.get("idle") is None
    assert accounts.get("recent") is not None


def test_an_unused_account_with_a_running_subscription_is_kept(accounts, clock, identities):
    used_at(accounts, clock, "payer", LONG_AGO, tier=Tier.BASIC, status="active")
    used_at(accounts, clock, "retrying", LONG_AGO, tier=Tier.FREE, status="past_due")

    swept = sweep(accounts, identities)

    assert (swept.deleted, swept.kept) == (0, 2)
    assert identities.removed == [], "never delete the sign-in of somebody who pays"
    assert accounts.get("payer").tier is Tier.BASIC
    assert accounts.get("payer").last_active_at == NOW, "being paid for counts as use"


def test_a_subscription_that_has_ended_keeps_nothing(accounts, clock, identities):
    used_at(accounts, clock, "former", LONG_AGO, tier=Tier.BASIC, status="active")
    accounts.set_subscription("former", Tier.FREE, status="canceled")

    assert sweep(accounts, identities).deleted == 1
    assert accounts.get("former") is None


def test_idle_subscribers_do_not_hold_up_the_accounts_behind_them(accounts, clock, identities):
    used_at(accounts, clock, "payer-1", LONG_AGO - 3 * DAY, tier=Tier.BASIC, status="active")
    used_at(accounts, clock, "payer-2", LONG_AGO - 2 * DAY, tier=Tier.PREMIUM, status="active")
    used_at(accounts, clock, "idle", LONG_AGO)

    first = sweep(accounts, identities, limit=2)
    second = sweep(accounts, identities, limit=2)

    assert (first.kept, first.deleted) == (2, 0)
    assert (second.kept, second.deleted) == (0, 1)
    assert identities.removed == ["idle"]


def test_an_account_with_no_date_is_dated_rather_than_deleted(accounts, identities):
    add_undated(accounts, "old")

    first = sweep(accounts, identities)
    assert (first.dated, first.deleted) == (1, 0)
    assert accounts.get("old").last_active_at == NOW

    assert sweep(accounts, identities, now=NOW + RETENTION - DAY).deleted == 0
    assert sweep(accounts, identities, now=NOW + RETENTION + DAY).deleted == 1
    assert identities.removed == ["old"]


def test_a_sign_in_that_could_not_be_deleted_is_retried_by_the_next_run(accounts, clock, identities):
    used_at(accounts, clock, "stuck", LONG_AGO)
    used_at(accounts, clock, "idle", LONG_AGO - DAY)
    identities.refuse = {"stuck"}

    first = sweep(accounts, identities)
    assert (first.deleted, first.failed) == (1, 1), "one failure does not stop the rest"
    assert accounts.get("stuck") is not None, "the record stays so that the next run finds it"

    identities.refuse = set()
    second = sweep(accounts, identities)
    assert (second.deleted, second.failed) == (1, 0)
    assert accounts.get("stuck") is None


def test_a_run_deletes_at_most_its_limit_longest_idle_first(accounts, clock, identities):
    for age in range(5):
        used_at(accounts, clock, f"idle-{age}", LONG_AGO - age * DAY)

    assert sweep(accounts, identities, limit=2).deleted == 2
    assert identities.removed == ["idle-4", "idle-3"]
    assert sweep(accounts, identities, limit=2).deleted == 2
    assert sweep(accounts, identities, limit=2).deleted == 1
    assert sweep(accounts, identities, limit=2).deleted == 0


def test_undated_accounts_are_dated_a_batch_at_a_time(accounts, identities):
    for n in range(3):
        add_undated(accounts, f"old-{n}")

    assert [sweep(accounts, identities, date_limit=2).dated for _ in range(3)] == [2, 1, 0]


def test_an_account_used_while_it_was_being_deleted_is_kept(accounts, clock):
    used_at(accounts, clock, "returning", LONG_AGO)

    class SignsInMeanwhile:
        def remove(self, uid):
            accounts.ensure(uid, f"{uid}@example.org")

    swept = sweep(accounts, SignsInMeanwhile())

    assert (swept.deleted, swept.kept) == (0, 1)
    assert accounts.get("returning").last_active_at == NOW


# --- deleting the sign-in ---------------------------------------------------

def test_deleting_a_password_sign_in_removes_the_user_and_every_session(tmp_path):
    store = SqliteCredentialStore(tmp_path / "elections.db")
    try:
        first = store.register("voter@example.org", "a-long-enough-password")
        second = store.sign_in("voter@example.org", "a-long-enough-password")

        store.remove(first.uid)

        for session in (first, second):
            with pytest.raises(InvalidToken):
                store.resolve(session.token)
        with pytest.raises(BadCredentials):
            store.sign_in("voter@example.org", "a-long-enough-password")
        store.remove(first.uid)  # already gone: not an error
        assert store.register("voter@example.org", "a-long-enough-password").uid != first.uid
    finally:
        store.close()


class FakeCredentials:
    def __init__(self):
        self.valid = False
        self.token = None
        self.refreshed = 0

    def refresh(self, request):
        self.refreshed += 1
        self.valid = True
        self.token = f"token-{self.refreshed}"


def firebase(*answers):
    """Firebase identities answering each deletion with the next of ``answers``."""
    calls = []
    queue = list(answers)

    def post(url, payload, token):
        calls.append((url, payload, token))
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return FirebaseIdentities("demo-project", credentials=FakeCredentials(), post=post), calls


def test_a_firebase_user_is_deleted_by_uid_as_the_service_account():
    identities, calls = firebase((200, {}), (200, {}))

    identities.remove("uid-1")
    identities.remove("uid-2")

    url = FIREBASE_DELETE_USER_URL.format(project="demo-project")
    assert calls == [
        (url, {"localId": "uid-1"}, "token-1"),
        (url, {"localId": "uid-2"}, "token-1"),
    ], "a token is fetched once and reused while it is valid"


@pytest.mark.parametrize(
    "answer",
    [(400, {"error": {"code": 400, "message": "USER_NOT_FOUND"}}), (404, {})],
    ids=["user-not-found", "http-404"],
)
def test_a_firebase_user_that_is_already_gone_counts_as_deleted(answer):
    identities, _ = firebase(answer)
    identities.remove("uid-1")


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"error": {"code": 403, "message": "PERMISSION_DENIED: Caller does not have permission"}}),
        (503, {}),
        ConnectionError("connection reset"),
    ],
    ids=["missing-role", "unavailable", "network"],
)
def test_a_firebase_deletion_that_did_not_happen_raises(answer):
    identities, _ = firebase(answer)
    with pytest.raises(IdentityRemovalFailed):
        identities.remove("uid-1")


def test_modes_without_stored_sign_ins_have_nothing_to_delete():
    assert NoIdentities().remove("uid-1") is None


@pytest.mark.parametrize(
    "mode, kind",
    [("off", NoIdentities), ("stub", NoIdentities), ("sqlite", SqliteCredentialStore),
     ("firebase", FirebaseIdentities)],
)
def test_sign_ins_are_deleted_wherever_auth_mode_keeps_them(mode, kind, monkeypatch, tmp_path):
    from app import config

    monkeypatch.setenv("AUTH_MODE", mode)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "elections.db"))
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-project")
    config.get_identity_remover.cache_clear()
    config.get_password_store.cache_clear()
    try:
        assert isinstance(config.get_identity_remover(), kind)
    finally:
        config.get_identity_remover.cache_clear()
        config.get_password_store.cache_clear()


# --- Firestore, outside its transactions --------------------------------------

_COMPARE = {"<": float.__lt__, ">=": float.__ge__}


class FakeSnapshot:
    def __init__(self, reference, data):
        self.reference = reference
        self.id = reference.id
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class FakeDocument:
    def __init__(self, db, doc_id):
        self._db = db
        self.id = doc_id

    def get(self, transaction=None):
        return FakeSnapshot(self, self._db.docs.get(self.id))

    def update(self, data):
        if self.id not in self._db.docs:
            raise NotFound("no document to update")
        self._db.docs[self.id].update(data)


class FakeQuery:
    """Enough of a Firestore query for numeric filters, one ordering, limits and counts."""

    def __init__(self, db, filters=(), order=None, limit=None, fields=None):
        self._db = db
        self._filters = filters
        self._order = order
        self._limit = limit
        self._fields = fields

    def _with(self, **changes):
        state = dict(filters=self._filters, order=self._order, limit=self._limit, fields=self._fields)
        return FakeQuery(self._db, **{**state, **changes})

    def where(self, *, filter):
        return self._with(filters=self._filters + (filter,))

    def order_by(self, field):
        return self._with(order=field)

    def limit(self, n):
        return self._with(limit=n)

    def select(self, fields):
        return self._with(fields=list(fields))

    def document(self, doc_id):
        return FakeDocument(self._db, doc_id)

    def _matches(self, data):
        for f in self._filters:
            value = data.get(f.field_path)
            # Like Firestore: a range filter on numbers only sees numbers.
            if not isinstance(value, (int, float)) or not _COMPARE[f.op_string](float(value), f.value):
                return False
        return True

    def stream(self):
        self._db.scans += 1
        matched = [(i, d) for i, d in sorted(self._db.docs.items()) if self._matches(d)]
        if self._order:
            matched.sort(key=lambda item: item[1][self._order])
        for doc_id, data in matched[: self._limit]:
            if self._fields is not None:
                data = {k: v for k, v in data.items() if k in self._fields}
            yield FakeSnapshot(FakeDocument(self._db, doc_id), data)

    def count(self, alias):
        n = sum(1 for data in self._db.docs.values() if self._matches(data))
        return SimpleNamespace(get=lambda: [[SimpleNamespace(value=n)]])


class FakeBatch:
    def __init__(self, db):
        self._db = db
        self._updates = []

    def update(self, reference, data):
        self._updates.append((reference, data))

    def commit(self):
        for reference, data in self._updates:
            reference.update(data)
        self._db.commits += 1


class FakeFirestore:
    def __init__(self, docs):
        self.docs = docs
        self.scans = 0
        self.commits = 0

    def collection(self, name):
        assert name == firestore_accounts.ACCOUNTS_COLLECTION
        return FakeQuery(self)

    def batch(self):
        return FakeBatch(self)


def test_firestore_dates_the_accounts_without_a_date_and_only_those():
    db = FakeFirestore({
        "missing": {"email": "a@example.org", "tier": "free"},
        "null": {"last_active_at": None},
        "dated": {"last_active_at": RECENTLY},
    })

    assert FirestoreAccountStore(db).date_undated(NOW, 10) == 2
    assert db.docs["missing"]["last_active_at"] == NOW
    assert db.docs["null"]["last_active_at"] == NOW
    assert db.docs["dated"]["last_active_at"] == RECENTLY


def test_firestore_scans_nothing_once_every_account_has_a_date():
    db = FakeFirestore({"a": {"last_active_at": RECENTLY}, "b": {"last_active_at": NOW}})

    assert FirestoreAccountStore(db).date_undated(NOW, 10) == 0
    assert db.scans == 0, "the counts alone answer it"


def test_firestore_dates_up_to_the_limit_in_batches_it_may_commit(monkeypatch):
    monkeypatch.setattr(firestore_accounts, "MAX_BATCH_WRITES", 2)
    db = FakeFirestore({f"old-{n}": {} for n in range(5)})
    store = FirestoreAccountStore(db)

    assert store.date_undated(NOW, 4) == 4
    assert db.commits == 2
    assert store.date_undated(NOW, 4) == 1
    assert all(doc["last_active_at"] == NOW for doc in db.docs.values())


def test_firestore_lists_the_longest_idle_first():
    db = FakeFirestore({
        "recent": {"last_active_at": RECENTLY},
        "idle": {"last_active_at": LONG_AGO, "tier": "free"},
        "idler": {"last_active_at": int(LONG_AGO - DAY)},
        "undated": {},
    })

    idle = FirestoreAccountStore(db).inactive(inactive_cutoff(NOW), 10)

    assert [(a.uid, a.last_active_at) for a in idle] == [
        ("idler", LONG_AGO - DAY),
        ("idle", LONG_AGO),
    ]


def test_firestore_marking_an_unknown_account_active_is_not_an_error():
    db = FakeFirestore({"a": {"last_active_at": LONG_AGO}})
    store = FirestoreAccountStore(db)

    store.mark_active("gone", NOW)
    store.mark_active("a", NOW)

    assert db.docs == {"a": {"last_active_at": NOW}}


# --- over HTTP --------------------------------------------------------------------

SECRET = "r" * 40
SCHEDULE = {"x-report-secret": SECRET}
URL = "/api/internal/inactive-accounts"


@pytest.fixture
def swept_client(identities):
    now = time.time()
    store = InMemoryAccountStore({
        "idle": UserAccount(uid="idle", last_active_at=now - RETENTION - DAY),
        "payer": UserAccount(
            uid="payer", tier=Tier.BASIC, subscription_status="active",
            last_active_at=now - RETENTION - DAY,
        ),
        "recent": UserAccount(uid="recent", last_active_at=now - DAY),
    })
    overrides = main.app.dependency_overrides
    overrides[main.get_account_store] = lambda: store
    overrides[main.get_identity_remover_provider] = lambda: identities
    with TestClient(main.app) as test_client:
        yield test_client, store
    overrides.clear()


def test_without_a_schedule_secret_there_is_no_deletion_endpoint(swept_client, identities, monkeypatch):
    client, _ = swept_client
    monkeypatch.delenv("USAGE_REPORT_SECRET", raising=False)

    assert client.post(URL, headers=SCHEDULE).status_code == 404
    assert identities.removed == []


def test_only_the_schedule_may_delete_accounts(swept_client, identities, monkeypatch):
    client, _ = swept_client
    monkeypatch.setenv("USAGE_REPORT_SECRET", SECRET)

    assert client.post(URL).status_code == 403
    assert client.post(URL, headers={"x-report-secret": "r" * 39 + "x"}).status_code == 403
    assert identities.removed == []


def test_the_daily_run_deletes_the_unused_account_and_says_how_many(swept_client, identities, monkeypatch):
    client, store = swept_client
    monkeypatch.setenv("USAGE_REPORT_SECRET", SECRET)

    response = client.post(URL, headers=SCHEDULE)

    assert response.status_code == 200, response.text
    assert response.json() == {"dated": 0, "deleted": 1, "kept": 1, "failed": 0}
    assert identities.removed == ["idle"]
    assert store.get("idle") is None
    assert store.get("payer") is not None and store.get("recent") is not None


def test_a_run_whose_sign_in_deletions_failed_shows_as_failed_and_is_retried(
    swept_client, identities, monkeypatch
):
    client, store = swept_client
    monkeypatch.setenv("USAGE_REPORT_SECRET", SECRET)
    identities.refuse = {"idle"}

    failed = client.post(URL, headers=SCHEDULE)
    assert failed.status_code == 502
    assert store.get("idle") is not None

    identities.refuse = set()
    assert client.post(URL, headers=SCHEDULE).json()["deleted"] == 1
    assert store.get("idle") is None
