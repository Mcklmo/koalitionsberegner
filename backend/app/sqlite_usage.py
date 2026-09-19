"""SQLite-backed :class:`~app.usage.UsageStore`.

Shares the database file with the elections. Every write is a single
statement, so SQLite's own atomicity is all the locking this needs: an upsert
adds one to a counter, and ``INSERT OR IGNORE`` makes the report claim
idempotent.
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
-- Per-day active-account markers, from when there were accounts. Nothing
-- reads or writes them any more, so a database that still has them drops them.
DROP TABLE IF EXISTS usage_active;
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
