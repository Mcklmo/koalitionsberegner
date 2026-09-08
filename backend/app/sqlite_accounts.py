"""SQLite-backed :class:`~app.accounts.AccountStore`.

Shares the database file with :class:`~app.sqlite_store.SqliteElectionStore`
and the same atomicity story: ``BEGIN IMMEDIATE`` holds the write lock across
the read-decide-write of :meth:`reserve_import`, so two imports racing for the
last unit of a month's quota cannot both win.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .accounts import Tier, UserAccount, _released, _reserved, billing_period
from .observability import io_span

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    uid                 TEXT PRIMARY KEY,
    email               TEXT,
    tier                TEXT NOT NULL DEFAULT 'free',
    period              TEXT NOT NULL DEFAULT '',
    used                INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id  TEXT,
    subscription_id     TEXT,
    subscription_status TEXT,
    created_at          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS accounts_by_customer
    ON accounts (stripe_customer_id);
"""

COLUMNS = (
    "uid, email, tier, period, used, stripe_customer_id, subscription_id, subscription_status"
)


def _tier(value: str | None) -> Tier:
    """Storage is not trusted to have preserved the enum; anything odd is free."""
    try:
        return Tier(value)
    except ValueError:
        log.warning("unknown tier %r in storage; treating the account as free", value)
        return Tier.FREE


def _account_from_row(row: sqlite3.Row) -> UserAccount:
    return UserAccount(
        uid=row["uid"],
        email=row["email"],
        tier=_tier(row["tier"]),
        period=row["period"] or "",
        used=int(row["used"] or 0),
        stripe_customer_id=row["stripe_customer_id"],
        subscription_id=row["subscription_id"],
        subscription_status=row["subscription_status"],
    )


class SqliteAccountStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._local = threading.local()
        with io_span(log, "sqlite", "migrate_accounts", path=self._path):
            self._connect().executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self):
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- reads -------------------------------------------------------------

    @staticmethod
    def _read(conn: sqlite3.Connection, uid: str) -> UserAccount | None:
        row = conn.execute("SELECT * FROM accounts WHERE uid = ?", (uid,)).fetchone()
        return _account_from_row(row) if row else None

    def get(self, uid: str) -> UserAccount | None:
        with io_span(log, "sqlite", "get_account", uid=uid[:12]) as span:
            account = self._read(self._connect(), uid)
            span["found"] = account is not None
            return account

    def find_by_customer(self, customer_id: str) -> UserAccount | None:
        with io_span(log, "sqlite", "account_by_customer", customer=customer_id[:12]) as span:
            row = self._connect().execute(
                "SELECT * FROM accounts WHERE stripe_customer_id = ? LIMIT 1", (customer_id,)
            ).fetchone()
            span["found"] = row is not None
            return _account_from_row(row) if row else None

    # --- writes ------------------------------------------------------------

    def _save(self, conn: sqlite3.Connection, account: UserAccount) -> None:
        conn.execute(
            "UPDATE accounts SET email = ?, tier = ?, period = ?, used = ?,"
            " stripe_customer_id = ?, subscription_id = ?, subscription_status = ?"
            " WHERE uid = ?",
            (
                account.email,
                account.tier.value,
                account.period,
                account.used,
                account.stripe_customer_id,
                account.subscription_id,
                account.subscription_status,
                account.uid,
            ),
        )

    def ensure(self, uid: str, email: str | None) -> UserAccount:
        # Every authenticated request passes through here. Reading first keeps
        # the steady state off SQLite's write lock, which BEGIN IMMEDIATE would
        # otherwise take — serialising every signed-in request against every
        # other. The write path below re-reads under the lock.
        existing = self.get(uid)
        if existing is not None and (not email or existing.email == email):
            return existing

        with io_span(log, "sqlite", "ensure_account", uid=uid[:12]) as span:
            with self._write() as conn:
                account = self._read(conn, uid)
                if account is None:
                    account = UserAccount(uid=uid, email=email, period=billing_period())
                    conn.execute(
                        f"INSERT INTO accounts ({COLUMNS}, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (uid, email, account.tier.value, account.period, 0,
                         None, None, None, self._clock()),
                    )
                    span["created"] = True
                    return account
                if email and account.email != email:
                    conn.execute("UPDATE accounts SET email = ? WHERE uid = ?", (email, uid))
                    account = _account_from_row(
                        conn.execute("SELECT * FROM accounts WHERE uid = ?", (uid,)).fetchone()
                    )
                span["created"] = False
                return account

    def reserve_import(self, uid: str, period: str, limit: int) -> bool:
        with io_span(log, "sqlite", "reserve_import", uid=uid[:12], period=period) as span:
            with self._write() as conn:
                account = self._read(conn, uid)
                reserved = _reserved(account, period, limit) if account else None
                if reserved is None:
                    span["granted"] = False
                    return False
                self._save(conn, reserved)
                span["granted"] = True
                span["used"] = reserved.used
                return True

    def release_import(self, uid: str, period: str) -> None:
        with io_span(log, "sqlite", "release_import", uid=uid[:12], period=period):
            with self._write() as conn:
                account = self._read(conn, uid)
                if account is not None:
                    self._save(conn, _released(account, period))

    def set_subscription(
        self,
        uid: str,
        tier: Tier,
        *,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        status: str | None = None,
    ) -> UserAccount | None:
        with io_span(log, "sqlite", "set_subscription", uid=uid[:12], tier=tier.value) as span:
            with self._write() as conn:
                account = self._read(conn, uid)
                if account is None:
                    span["found"] = False
                    return None
                updated = replace(
                    account,
                    tier=tier,
                    stripe_customer_id=customer_id or account.stripe_customer_id,
                    subscription_id=subscription_id or account.subscription_id,
                    subscription_status=status or account.subscription_status,
                )
                self._save(conn, updated)
                span["found"] = True
                return updated

    def link_customer(self, uid: str, customer_id: str) -> None:
        with io_span(log, "sqlite", "link_customer", uid=uid[:12], customer=customer_id[:12]):
            with self._write() as conn:
                conn.execute(
                    "UPDATE accounts SET stripe_customer_id = ? WHERE uid = ?",
                    (customer_id, uid),
                )
