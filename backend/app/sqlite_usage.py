"""SQLite-backed :class:`~app.usage.UsageStore`.

Shares the database file with the elections and the accounts. Every write is a
single statement, so SQLite's own atomicity is all the locking this needs:
an upsert adds one to a counter, ``INSERT OR IGNORE`` makes both the
active-account marker and the report claim idempotent.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

from .observability import io_span

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    day   TEXT NOT NULL,
    key   TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);
CREATE TABLE IF NOT EXISTS usage_active (
    day    TEXT NOT NULL,
    marker TEXT NOT NULL,
    PRIMARY KEY (day, marker)
);
CREATE TABLE IF NOT EXISTS usage_reports (
    key     TEXT PRIMARY KEY,
    sent_at REAL NOT NULL
);
"""


class SqliteUsageStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._local = threading.local()
        with io_span(log, "sqlite", "migrate_usage", path=self._path):
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

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def increment(self, day: date, key: str) -> None:
        with io_span(log, "sqlite", "usage_increment", key=key):
            self._connect().execute(
                "INSERT INTO usage_daily (day, key, count) VALUES (?, ?, 1)"
                " ON CONFLICT (day, key) DO UPDATE SET count = count + 1",
                (day.isoformat(), key),
            )

    def mark_active(self, day: date, marker: str) -> None:
        with io_span(log, "sqlite", "usage_active"):
            self._connect().execute(
                "INSERT OR IGNORE INTO usage_active (day, marker) VALUES (?, ?)",
                (day.isoformat(), marker),
            )

    def daily(self, start: date, end: date) -> dict[date, dict[str, int]]:
        with io_span(log, "sqlite", "usage_daily", start=start, end=end):
            rows = self._connect().execute(
                "SELECT day, key, count FROM usage_daily WHERE day >= ? AND day < ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        counts: dict[date, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(date.fromisoformat(row["day"]), {})[row["key"]] = int(row["count"])
        return counts

    def active_accounts(self, start: date, end: date) -> int:
        with io_span(log, "sqlite", "usage_active_accounts", start=start, end=end):
            row = self._connect().execute(
                "SELECT COUNT(DISTINCT marker) AS n FROM usage_active WHERE day >= ? AND day < ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return int(row["n"])

    def forget_active_before(self, day: date) -> None:
        with io_span(log, "sqlite", "usage_forget_active", before=day) as span:
            cursor = self._connect().execute(
                "DELETE FROM usage_active WHERE day < ?", (day.isoformat(),)
            )
            span["deleted"] = cursor.rowcount

    def claim_report(self, key: str) -> bool:
        with io_span(log, "sqlite", "usage_claim_report", key=key) as span:
            cursor = self._connect().execute(
                "INSERT OR IGNORE INTO usage_reports (key, sent_at) VALUES (?, ?)",
                (key, self._clock()),
            )
            span["claimed"] = cursor.rowcount == 1
            return cursor.rowcount == 1

    def release_report(self, key: str) -> None:
        with io_span(log, "sqlite", "usage_release_report", key=key):
            self._connect().execute("DELETE FROM usage_reports WHERE key = ?", (key,))
