"""SQLite-backed :class:`~app.store.ElectionStore`.

The middle option between the in-memory store (fast, forgets everything on
restart) and Firestore (shared, needs a GCP project): a local file that survives
restarts, so a developer does not re-fetch and re-extract the same pages every
time the server comes up.

Atomicity comes from ``BEGIN IMMEDIATE``, which takes SQLite's write lock for
the whole read-decide-write of :meth:`claim`. That is the same guarantee the
Firestore transaction provides, and the only property single-flight depends on.
Connections are per-thread because the service calls the store from a thread
pool and SQLite connections are not shareable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .observability import io_span
from .schema import Election
from .store import (
    DEFAULT_STALE_AFTER_SECONDS,
    Claim,
    ClaimOutcome,
    Confirmation,
    ImportRequest,
    Job,
    JobStatus,
    StoredElection,
    _decide,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS elections (
    election_hash TEXT PRIMARY KEY,
    election      TEXT NOT NULL,
    stored_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    page_key   TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    attempt    INTEGER NOT NULL DEFAULT 1,
    error      TEXT,
    result     TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    page_key      TEXT PRIMARY KEY,
    election_hash TEXT NOT NULL,
    linked_at     REAL NOT NULL
);
"""


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        page_key=row["page_key"],
        status=JobStatus(row["status"]),
        source_url=row["source_url"] or "",
        started_at=float(row["started_at"]),
        attempt=int(row["attempt"]),
        error=row["error"],
        # A staged draft is re-validated on read like anything else from storage.
        result=Election.model_validate(json.loads(row["result"])) if row["result"] else None,
    )


class SqliteElectionStore:
    def __init__(
        self,
        path: str | Path,
        *,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        clock=time.time,
    ):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._stale_after = stale_after
        self._clock = clock
        self._local = threading.local()
        with io_span(log, "sqlite", "migrate", path=self._path):
            self._connect().executescript(SCHEMA)

    # --- connection handling ----------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """One connection per thread; SQLite locks between them for us."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # isolation_level=None turns off implicit transactions so BEGIN
            # IMMEDIATE below means exactly what it says.
            conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")   # readers do not block the writer
            conn.execute("PRAGMA busy_timeout=30000")  # wait for the lock, do not fail
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self):
        """A write transaction that holds SQLite's write lock from the first read."""
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

    def _read_election(self, conn: sqlite3.Connection, election_hash: str) -> Election | None:
        row = conn.execute(
            "SELECT election FROM elections WHERE election_hash = ?", (election_hash,)
        ).fetchone()
        # Re-validate on read: storage is not trusted to have preserved the schema.
        return Election.model_validate(json.loads(row["election"])) if row else None

    def get_election(self, election_hash: str) -> Election | None:
        with io_span(log, "sqlite", "get_election", hash=election_hash[:12]) as span:
            election = self._read_election(self._connect(), election_hash)
            span["found"] = election is not None
            return election

    def list_elections(self) -> list[StoredElection]:
        with io_span(log, "sqlite", "list_elections") as span:
            rows = self._connect().execute(
                "SELECT election_hash, election, stored_at FROM elections ORDER BY stored_at"
            ).fetchall()
            span["count"] = len(rows)
            return [
                StoredElection(
                    election_hash=row["election_hash"],
                    election=Election.model_validate(json.loads(row["election"])),
                    stored_at=float(row["stored_at"]),
                )
                for row in rows
            ]

    def get_job(self, page_key: str) -> Job | None:
        with io_span(log, "sqlite", "get_job", page=page_key[:12]) as span:
            row = self._connect().execute(
                "SELECT * FROM jobs WHERE page_key = ?", (page_key,)
            ).fetchone()
            job = _job_from_row(row) if row else None
            span["status"] = job.status.value if job else "absent"
            return job

    def resolve_page(self, page_key: str) -> str | None:
        with io_span(log, "sqlite", "resolve_page", page=page_key[:12]) as span:
            row = self._connect().execute(
                "SELECT election_hash FROM pages WHERE page_key = ?", (page_key,)
            ).fetchone()
            resolved = row["election_hash"] if row else None
            span["hash"] = resolved[:12] if resolved else "unresolved"
            return resolved

    # --- writes ------------------------------------------------------------

    def claim(self, page_key: str, request: ImportRequest) -> Claim:
        with io_span(log, "sqlite", "claim", page=page_key[:12]) as span:
            with self._write() as conn:
                page = conn.execute(
                    "SELECT election_hash FROM pages WHERE page_key = ?", (page_key,)
                ).fetchone()
                election_hash = page["election_hash"] if page else None
                election = self._read_election(conn, election_hash) if election_hash else None

                row = conn.execute(
                    "SELECT * FROM jobs WHERE page_key = ?", (page_key,)
                ).fetchone()
                job = _job_from_row(row) if row else None

                outcome = _decide(election, job, self._clock(), self._stale_after)
                span["outcome"] = outcome.value
                if outcome is ClaimOutcome.STORED:
                    return Claim(outcome, page_key, election=election,
                                 election_hash=election_hash, job=job)
                if outcome is ClaimOutcome.ATTACHED:
                    return Claim(outcome, page_key, job=job)

                new_job = Job(
                    page_key=page_key,
                    status=JobStatus.PENDING,
                    source_url=request.source_url,
                    started_at=self._clock(),
                    attempt=(job.attempt + 1) if job else 1,
                )
                conn.execute(
                    "INSERT INTO jobs (page_key, status, source_url, started_at, attempt,"
                    " error, result) VALUES (?, ?, ?, ?, ?, NULL, NULL)"
                    " ON CONFLICT(page_key) DO UPDATE SET status=excluded.status,"
                    " source_url=excluded.source_url, started_at=excluded.started_at,"
                    " attempt=excluded.attempt, error=NULL, result=NULL",
                    (page_key, new_job.status.value, new_job.source_url,
                     new_job.started_at, new_job.attempt),
                )
                span["attempt"] = new_job.attempt
                return Claim(outcome, page_key, job=new_job)

    def stage(self, page_key: str, election: Election) -> None:
        with io_span(log, "sqlite", "stage", page=page_key[:12],
                     total_seats=election.total_seats):
            with self._write() as conn:
                row = conn.execute(
                    "SELECT source_url, attempt FROM jobs WHERE page_key = ?", (page_key,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO jobs (page_key, status, source_url, started_at, attempt,"
                    " error, result) VALUES (?, ?, ?, ?, ?, NULL, ?)"
                    " ON CONFLICT(page_key) DO UPDATE SET status=excluded.status,"
                    # Restart the lease so the user gets a full window to confirm.
                    " started_at=excluded.started_at, error=NULL, result=excluded.result",
                    (page_key, JobStatus.AWAITING_CONFIRMATION.value,
                     row["source_url"] if row else "", self._clock(),
                     row["attempt"] if row else 1,
                     json.dumps(election.model_dump(mode="json"))),
                )

    def confirm(self, page_key: str, election_hash: str) -> Confirmation | None:
        with io_span(log, "sqlite", "confirm", page=page_key[:12],
                     hash=election_hash[:12]) as span:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE page_key = ?", (page_key,)
                ).fetchone()
                job = _job_from_row(row) if row else None
                already = self._read_election(conn, election_hash)

                if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION \
                        or job.result is None:
                    # Confirming twice is harmless as long as the page resolved here.
                    page = conn.execute(
                        "SELECT election_hash FROM pages WHERE page_key = ?", (page_key,)
                    ).fetchone()
                    if already is not None and page and page["election_hash"] == election_hash:
                        span["result"] = "already"
                        return Confirmation(election_hash, already, duplicate=False)
                    span["result"] = "none"
                    return None

                now = self._clock()
                duplicate = already is not None
                if not duplicate:
                    conn.execute(
                        "INSERT INTO elections (election_hash, election, stored_at)"
                        " VALUES (?, ?, ?)",
                        (election_hash, json.dumps(job.result.model_dump(mode="json")), now),
                    )
                conn.execute(
                    "INSERT INTO pages (page_key, election_hash, linked_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(page_key) DO UPDATE SET election_hash=excluded.election_hash,"
                    " linked_at=excluded.linked_at",
                    (page_key, election_hash, now),
                )
                conn.execute(
                    "UPDATE jobs SET status = ?, result = NULL WHERE page_key = ?",
                    (JobStatus.SUCCEEDED.value, page_key),
                )
                span["result"] = "duplicate" if duplicate else "stored"
                return Confirmation(election_hash, already or job.result, duplicate=duplicate)

    def link(self, page_key: str, election_hash: str) -> None:
        with io_span(log, "sqlite", "link", page=page_key[:12],
                     hash=election_hash[:12]) as span:
            with self._write() as conn:
                if self._read_election(conn, election_hash) is None:
                    span["linked"] = False
                    return
                now = self._clock()
                conn.execute(
                    "INSERT INTO pages (page_key, election_hash, linked_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(page_key) DO UPDATE SET election_hash=excluded.election_hash,"
                    " linked_at=excluded.linked_at",
                    (page_key, election_hash, now),
                )
                conn.execute(
                    "INSERT INTO jobs (page_key, status, source_url, started_at, attempt,"
                    " error, result) VALUES (?, ?, '', ?, 1, NULL, NULL)"
                    " ON CONFLICT(page_key) DO UPDATE SET status=excluded.status, result=NULL",
                    (page_key, JobStatus.SUCCEEDED.value, now),
                )
                span["linked"] = True

    def discard(self, page_key: str) -> bool:
        with io_span(log, "sqlite", "discard", page=page_key[:12]) as span:
            with self._write() as conn:
                deleted = conn.execute(
                    "DELETE FROM jobs WHERE page_key = ? AND status = ?",
                    (page_key, JobStatus.AWAITING_CONFIRMATION.value),
                ).rowcount
                span["discarded"] = bool(deleted)
                return bool(deleted)

    def fail(self, page_key: str, error: str) -> None:
        with io_span(log, "sqlite", "fail", page=page_key[:12], reason=error):
            with self._write() as conn:
                row = conn.execute(
                    "SELECT source_url, started_at, attempt FROM jobs WHERE page_key = ?",
                    (page_key,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO jobs (page_key, status, source_url, started_at, attempt,"
                    " error, result) VALUES (?, ?, ?, ?, ?, ?, NULL)"
                    " ON CONFLICT(page_key) DO UPDATE SET status=excluded.status,"
                    " error=excluded.error, result=NULL",
                    (page_key, JobStatus.FAILED.value,
                     row["source_url"] if row else "",
                     row["started_at"] if row else self._clock(),
                     row["attempt"] if row else 1, error[:1000]),
                )
