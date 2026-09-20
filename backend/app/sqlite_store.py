"""SQLite-backed :class:`~app.store.ElectionStore`.

The middle option between the in-memory store (fast, forgets everything on
restart) and Firestore (shared, needs a GCP project): a local file that survives
restarts, so a developer does not re-resolve and re-extract the same elections
every time the server comes up.

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
from .outreach import DraftStatus, NewDraft, OutreachDraft, thread_id as _draft_thread_id
from .schema import Election
from .store import (
    DEFAULT_STALE_AFTER_SECONDS,
    DISCARDABLE_STATUSES,
    Claim,
    ClaimOutcome,
    Confirmation,
    ImportRequest,
    Job,
    JobStatus,
    StoredElection,
    _decide,
    select_by_place,
)

log = logging.getLogger(__name__)

# The two import tables are keyed by *request* (a year and a place). A database
# from before that change carries ``jobs``/``pages`` keyed by URL instead; those
# rows mean nothing now, so they are left where they are rather than migrated —
# the elections they produced are in ``elections``, which is unchanged.
SCHEMA = """
CREATE TABLE IF NOT EXISTS elections (
    election_hash TEXT PRIMARY KEY,
    election      TEXT NOT NULL,
    stored_at     REAL NOT NULL,
    selected      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_jobs (
    request_key TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    query       TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL,
    attempt     INTEGER NOT NULL DEFAULT 1,
    error       TEXT,
    result      TEXT,
    forecasts   TEXT,
    owner       TEXT
);
CREATE TABLE IF NOT EXISTS import_results (
    request_key   TEXT PRIMARY KEY,
    election_hash TEXT NOT NULL,
    linked_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_drafts (
    id                TEXT PRIMARY KEY,
    thing_id          TEXT NOT NULL UNIQUE,
    thread_id         TEXT NOT NULL,
    source            TEXT NOT NULL,
    subreddit         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    permalink         TEXT NOT NULL,
    title             TEXT NOT NULL,
    excerpt           TEXT NOT NULL,
    election_hash     TEXT NOT NULL,
    election_title    TEXT NOT NULL,
    link              TEXT NOT NULL,
    reply_text        TEXT NOT NULL,
    verification      TEXT NOT NULL,
    classifier_reason TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL,
    token_hash        TEXT,
    token_expires_at  REAL,
    emailed_at        REAL,
    decided_at        REAL,
    posted_at         REAL,
    posted_url        TEXT,
    last_error        TEXT,
    edited            INTEGER NOT NULL DEFAULT 0
);
"""


def _stored_from_row(row: sqlite3.Row) -> StoredElection:
    return StoredElection(
        election_hash=row["election_hash"],
        # Re-validate on read: storage is not trusted to have preserved the schema.
        election=Election.model_validate(json.loads(row["election"])),
        stored_at=float(row["stored_at"]),
        selected=bool(row["selected"]),
    )


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        request_key=row["request_key"],
        status=JobStatus(row["status"]),
        query=row["query"] or "",
        started_at=float(row["started_at"]),
        attempt=int(row["attempt"]),
        error=row["error"],
        # A staged draft is re-validated on read like anything else from storage.
        result=Election.model_validate(json.loads(row["result"])) if row["result"] else None,
        forecasts=tuple(
            Election.model_validate(item) for item in json.loads(row["forecasts"])
        ) if row["forecasts"] else (),
        owner=row["owner"],
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
            conn = self._connect()
            conn.executescript(SCHEMA)
            self._add_missing_columns(conn)

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """Bring a database created by an earlier version up to date.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
        column added later has to be added here or every read of it fails.
        """
        for table, column, definition in (
            ("elections", "selected", "INTEGER NOT NULL DEFAULT 0"),
            ("import_jobs", "forecasts", "TEXT"),
            ("import_jobs", "owner", "TEXT"),
        ):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def get_stored(self, election_hash: str) -> StoredElection | None:
        with io_span(log, "sqlite", "get_stored", hash=election_hash[:12]) as span:
            row = self._connect().execute(
                "SELECT * FROM elections WHERE election_hash = ?", (election_hash,)
            ).fetchone()
            span["found"] = row is not None
            return _stored_from_row(row) if row else None

    def list_elections(self, *, selected_only: bool = False) -> list[StoredElection]:
        with io_span(log, "sqlite", "list_elections", selected_only=selected_only) as span:
            where = " WHERE selected = 1" if selected_only else ""
            rows = self._connect().execute(
                f"SELECT * FROM elections{where} ORDER BY stored_at"
            ).fetchall()
            span["count"] = len(rows)
            return [_stored_from_row(row) for row in rows]

    def find_by_place(
        self, year: int, nation: str, subnation: str | None = None
    ) -> StoredElection | None:
        with io_span(log, "sqlite", "find_by_place", year=year) as span:
            found = select_by_place(self.list_elections(), year, nation, subnation)
            span["found"] = found is not None
            return found

    def set_selected(self, election_hash: str, selected: bool) -> bool:
        with io_span(log, "sqlite", "set_selected", hash=election_hash[:12],
                     selected=selected) as span:
            with self._write() as conn:
                changed = conn.execute(
                    "UPDATE elections SET selected = ? WHERE election_hash = ?",
                    (1 if selected else 0, election_hash),
                ).rowcount
                span["found"] = bool(changed)
                return bool(changed)

    def replace_election(self, election_hash: str, election: Election) -> bool:
        with io_span(log, "sqlite", "replace_election", hash=election_hash[:12]) as span:
            with self._write() as conn:
                changed = conn.execute(
                    "UPDATE elections SET election = ? WHERE election_hash = ?",
                    (json.dumps(election.model_dump(mode="json")), election_hash),
                ).rowcount
                span["found"] = bool(changed)
                return bool(changed)

    def get_job(self, request_key: str) -> Job | None:
        with io_span(log, "sqlite", "get_job", request=request_key[:12]) as span:
            row = self._connect().execute(
                "SELECT * FROM import_jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
            job = _job_from_row(row) if row else None
            span["status"] = job.status.value if job else "absent"
            return job

    def resolve_request(self, request_key: str) -> str | None:
        with io_span(log, "sqlite", "resolve_request", request=request_key[:12]) as span:
            row = self._connect().execute(
                "SELECT election_hash FROM import_results WHERE request_key = ?", (request_key,)
            ).fetchone()
            resolved = row["election_hash"] if row else None
            span["hash"] = resolved[:12] if resolved else "unresolved"
            return resolved

    # --- writes ------------------------------------------------------------

    def _peek(self, conn: sqlite3.Connection, request_key: str) -> Claim:
        resolved = conn.execute(
            "SELECT election_hash FROM import_results WHERE request_key = ?",
            (request_key,),
        ).fetchone()
        election_hash = resolved["election_hash"] if resolved else None
        election = self._read_election(conn, election_hash) if election_hash else None

        row = conn.execute(
            "SELECT * FROM import_jobs WHERE request_key = ?", (request_key,)
        ).fetchone()
        job = _job_from_row(row) if row else None

        outcome = _decide(election, job, self._clock(), self._stale_after)
        if outcome is ClaimOutcome.STORED:
            return Claim(outcome, request_key, election=election,
                         election_hash=election_hash, job=job)
        return Claim(outcome, request_key, job=job)

    def peek(self, request_key: str) -> Claim:
        with io_span(log, "sqlite", "peek", request=request_key[:12]) as span:
            peeked = self._peek(self._connect(), request_key)
            span["outcome"] = peeked.outcome.value
            return peeked

    def claim(
        self, request_key: str, request: ImportRequest, owner: str | None = None
    ) -> Claim:
        with io_span(log, "sqlite", "claim", request=request_key[:12]) as span:
            with self._write() as conn:
                peeked = self._peek(conn, request_key)
                span["outcome"] = peeked.outcome.value
                if peeked.outcome is not ClaimOutcome.STARTED:
                    return peeked
                job = peeked.job

                new_job = Job(
                    request_key=request_key,
                    status=JobStatus.PENDING,
                    query=request.describe(),
                    started_at=self._clock(),
                    attempt=(job.attempt + 1) if job else 1,
                    owner=owner,
                )
                conn.execute(
                    "INSERT INTO import_jobs (request_key, status, query, started_at, attempt,"
                    " error, result, owner) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)"
                    " ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
                    " query=excluded.query, started_at=excluded.started_at,"
                    " attempt=excluded.attempt, error=NULL, result=NULL, forecasts=NULL,"
                    " owner=excluded.owner",
                    (request_key, new_job.status.value, new_job.query,
                     new_job.started_at, new_job.attempt, new_job.owner),
                )
                span["attempt"] = new_job.attempt
                return Claim(ClaimOutcome.STARTED, request_key, job=new_job)

    def stage(self, request_key: str, election: Election) -> None:
        with io_span(log, "sqlite", "stage", request=request_key[:12],
                     total_seats=election.total_seats):
            with self._write() as conn:
                row = conn.execute(
                    "SELECT query, attempt FROM import_jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO import_jobs (request_key, status, query, started_at, attempt,"
                    " error, result) VALUES (?, ?, ?, ?, ?, NULL, ?)"
                    " ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
                    # Restart the lease so the user gets a full window to confirm.
                    " started_at=excluded.started_at, error=NULL, result=excluded.result,"
                    " forecasts=NULL",
                    (request_key, JobStatus.AWAITING_CONFIRMATION.value,
                     row["query"] if row else "", self._clock(),
                     row["attempt"] if row else 1,
                     json.dumps(election.model_dump(mode="json"))),
                )

    def offer(self, request_key: str, forecasts: list[Election]) -> None:
        with io_span(log, "sqlite", "offer", request=request_key[:12],
                     forecasts=len(forecasts)):
            with self._write() as conn:
                row = conn.execute(
                    "SELECT query, attempt FROM import_jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO import_jobs (request_key, status, query, started_at, attempt,"
                    " error, result, forecasts) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)"
                    " ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
                    # Restart the lease so the user gets a full window to choose.
                    " started_at=excluded.started_at, error=NULL, result=NULL,"
                    " forecasts=excluded.forecasts",
                    (request_key, JobStatus.AWAITING_CHOICE.value,
                     row["query"] if row else "", self._clock(),
                     row["attempt"] if row else 1,
                     json.dumps([f.model_dump(mode="json") for f in forecasts])),
                )

    def confirm_forecast(
        self, request_key: str, forecast: Election, election_hash: str
    ) -> Confirmation | None:
        with io_span(log, "sqlite", "confirm_forecast", request=request_key[:12],
                     hash=election_hash[:12]) as span:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT * FROM import_jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
                job = _job_from_row(row) if row else None
                if job is None or job.status is not JobStatus.AWAITING_CHOICE \
                        or forecast not in job.forecasts:
                    span["result"] = "none"
                    return None
                already = self._read_election(conn, election_hash)
                if already is None:
                    conn.execute(
                        "INSERT INTO elections (election_hash, election, stored_at, selected)"
                        " VALUES (?, ?, ?, 0)",
                        (election_hash, json.dumps(forecast.model_dump(mode="json")),
                         self._clock()),
                    )
                span["result"] = "duplicate" if already is not None else "stored"
                return Confirmation(
                    election_hash, already or forecast, duplicate=already is not None
                )

    def confirm(self, request_key: str, election_hash: str) -> Confirmation | None:
        with io_span(log, "sqlite", "confirm", request=request_key[:12],
                     hash=election_hash[:12]) as span:
            with self._write() as conn:
                row = conn.execute(
                    "SELECT * FROM import_jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
                job = _job_from_row(row) if row else None
                already = self._read_election(conn, election_hash)

                if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION \
                        or job.result is None:
                    # Confirming twice is harmless as long as the request resolved here.
                    resolved = conn.execute(
                        "SELECT election_hash FROM import_results WHERE request_key = ?",
                        (request_key,),
                    ).fetchone()
                    if already is not None and resolved \
                            and resolved["election_hash"] == election_hash:
                        span["result"] = "already"
                        return Confirmation(election_hash, already, duplicate=False)
                    span["result"] = "none"
                    return None

                now = self._clock()
                duplicate = already is not None
                if not duplicate:
                    conn.execute(
                        "INSERT INTO elections (election_hash, election, stored_at, selected)"
                        " VALUES (?, ?, ?, 0)",
                        (election_hash, json.dumps(job.result.model_dump(mode="json")), now),
                    )
                conn.execute(
                    "INSERT INTO import_results (request_key, election_hash, linked_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(request_key) DO UPDATE SET election_hash=excluded.election_hash,"
                    " linked_at=excluded.linked_at",
                    (request_key, election_hash, now),
                )
                conn.execute(
                    "UPDATE import_jobs SET status = ?, result = NULL, owner = NULL WHERE request_key = ?",
                    (JobStatus.SUCCEEDED.value, request_key),
                )
                span["result"] = "duplicate" if duplicate else "stored"
                return Confirmation(election_hash, already or job.result, duplicate=duplicate)

    def link(self, request_key: str, election_hash: str) -> None:
        with io_span(log, "sqlite", "link", request=request_key[:12],
                     hash=election_hash[:12]) as span:
            with self._write() as conn:
                if self._read_election(conn, election_hash) is None:
                    span["linked"] = False
                    return
                now = self._clock()
                conn.execute(
                    "INSERT INTO import_results (request_key, election_hash, linked_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(request_key) DO UPDATE SET election_hash=excluded.election_hash,"
                    " linked_at=excluded.linked_at",
                    (request_key, election_hash, now),
                )
                conn.execute(
                    "INSERT INTO import_jobs (request_key, status, query, started_at, attempt,"
                    " error, result) VALUES (?, ?, '', ?, 1, NULL, NULL)"
                    " ON CONFLICT(request_key) DO UPDATE SET status=excluded.status, result=NULL,"
                    " forecasts=NULL, owner=NULL",
                    (request_key, JobStatus.SUCCEEDED.value, now),
                )
                span["linked"] = True

    def discard(self, request_key: str) -> bool:
        with io_span(log, "sqlite", "discard", request=request_key[:12]) as span:
            with self._write() as conn:
                deleted = conn.execute(
                    "DELETE FROM import_jobs WHERE request_key = ? AND status IN (?, ?)",
                    (request_key, *(status.value for status in DISCARDABLE_STATUSES)),
                ).rowcount
                span["discarded"] = bool(deleted)
                return bool(deleted)

    def fail(self, request_key: str, error: str) -> None:
        with io_span(log, "sqlite", "fail", request=request_key[:12], reason=error):
            with self._write() as conn:
                row = conn.execute(
                    "SELECT query, started_at, attempt FROM import_jobs WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO import_jobs (request_key, status, query, started_at, attempt,"
                    " error, result) VALUES (?, ?, ?, ?, ?, ?, NULL)"
                    " ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,"
                    " error=excluded.error, result=NULL, forecasts=NULL, owner=NULL",
                    (request_key, JobStatus.FAILED.value,
                     row["query"] if row else "",
                     row["started_at"] if row else self._clock(),
                     row["attempt"] if row else 1, error[:1000]),
                )


def _draft_from_row(row: sqlite3.Row) -> OutreachDraft:
    return OutreachDraft(
        id=row["id"],
        source=row["source"],
        subreddit=row["subreddit"],
        thing_id=row["thing_id"],
        thread_id=row["thread_id"],
        kind=row["kind"],
        permalink=row["permalink"],
        title=row["title"],
        excerpt=row["excerpt"],
        election_hash=row["election_hash"],
        election_title=row["election_title"],
        link=row["link"],
        reply_text=row["reply_text"],
        verification=json.loads(row["verification"]),
        classifier_reason=row["classifier_reason"],
        created_at=row["created_at"],
        status=DraftStatus(row["status"]),
        token_hash=row["token_hash"],
        token_expires_at=row["token_expires_at"],
        emailed_at=row["emailed_at"],
        decided_at=row["decided_at"],
        posted_at=row["posted_at"],
        posted_url=row["posted_url"],
        last_error=row["last_error"],
        edited=bool(row["edited"]),
    )


class SqliteOutreachStore:
    """The outreach approval queue, in the same kind of file as the elections.

    A separate connection from :class:`SqliteElectionStore` even when it is
    the same file — SQLite allows several connections to one database, and the
    two stores have no operation that must be atomic across both of them.
    """

    def __init__(self, path: str | Path, *, clock=time.time):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._local = threading.local()
        with io_span(log, "sqlite", "migrate-outreach", path=self._path):
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

    def create(
        self, draft_id: str, draft: NewDraft, *, token_hash: str, token_expires_at: float,
        emailed_at: float,
    ) -> OutreachDraft | None:
        with io_span(log, "sqlite", "outreach-create", thing_id=draft.thing_id) as span:
            with self._write() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM outreach_drafts WHERE thing_id = ?", (draft.thing_id,)
                ).fetchone()
                if existing:
                    span["duplicate"] = True
                    return None
                conn.execute(
                    "INSERT INTO outreach_drafts (id, thing_id, thread_id, source, subreddit,"
                    " kind, permalink, title, excerpt, election_hash, election_title, link,"
                    " reply_text, verification, classifier_reason, created_at, status,"
                    " token_hash, token_expires_at, emailed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        draft_id, draft.thing_id, _draft_thread_id(draft.permalink), draft.source,
                        draft.subreddit, draft.kind, draft.permalink, draft.title, draft.excerpt,
                        draft.election_hash, draft.election_title, draft.link, draft.reply_text,
                        json.dumps(draft.verification), draft.classifier_reason, draft.created_at,
                        DraftStatus.PENDING.value, token_hash, token_expires_at, emailed_at,
                    ),
                )
                span["duplicate"] = False
                return self._get(conn, draft_id)

    def _get(self, conn: sqlite3.Connection, draft_id: str) -> OutreachDraft | None:
        row = conn.execute("SELECT * FROM outreach_drafts WHERE id = ?", (draft_id,)).fetchone()
        return _draft_from_row(row) if row else None

    def get(self, draft_id: str) -> OutreachDraft | None:
        with io_span(log, "sqlite", "outreach-get", id=draft_id) as span:
            found = self._get(self._connect(), draft_id)
            span["found"] = found is not None
            return found

    def get_by_token_hash(self, token_hash: str) -> OutreachDraft | None:
        with io_span(log, "sqlite", "outreach-get-by-token") as span:
            row = self._connect().execute(
                "SELECT * FROM outreach_drafts WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            span["found"] = row is not None
            return _draft_from_row(row) if row else None

    def list_drafts(self) -> list[OutreachDraft]:
        with io_span(log, "sqlite", "outreach-list") as span:
            rows = self._connect().execute(
                "SELECT * FROM outreach_drafts ORDER BY created_at"
            ).fetchall()
            span["count"] = len(rows)
            return [_draft_from_row(row) for row in rows]

    def set_status(
        self, draft_id: str, status: DraftStatus, *, consume_token: bool = False, **fields
    ) -> None:
        with io_span(log, "sqlite", "outreach-set-status", id=draft_id, status=status.value):
            columns = dict(fields)
            columns["status"] = status.value
            if consume_token:
                columns["token_hash"] = None
                columns["token_expires_at"] = None
            assignment = ", ".join(f"{name} = ?" for name in columns)
            with self._write() as conn:
                conn.execute(
                    f"UPDATE outreach_drafts SET {assignment} WHERE id = ?",
                    (*columns.values(), draft_id),
                )

    def delete(self, draft_id: str) -> None:
        with io_span(log, "sqlite", "outreach-delete", id=draft_id):
            with self._write() as conn:
                conn.execute("DELETE FROM outreach_drafts WHERE id = ?", (draft_id,))

    def thread_posted(self, thread_id: str) -> bool:
        with io_span(log, "sqlite", "outreach-thread-posted") as span:
            row = self._connect().execute(
                "SELECT 1 FROM outreach_drafts WHERE thread_id = ? AND status = ? LIMIT 1",
                (thread_id, DraftStatus.POSTED.value),
            ).fetchone()
            span["posted"] = row is not None
            return row is not None

    def count_posted(self, subreddit: str, since: float) -> int:
        with io_span(log, "sqlite", "outreach-count-posted", subreddit=subreddit) as span:
            row = self._connect().execute(
                "SELECT COUNT(*) AS n FROM outreach_drafts"
                " WHERE subreddit = ? AND status = ? AND posted_at >= ?",
                (subreddit, DraftStatus.POSTED.value, since),
            ).fetchone()
            span["count"] = row["n"]
            return int(row["n"])

    def count_posted_total(self, since: float) -> int:
        with io_span(log, "sqlite", "outreach-count-posted-total") as span:
            row = self._connect().execute(
                "SELECT COUNT(*) AS n FROM outreach_drafts WHERE status = ? AND posted_at >= ?",
                (DraftStatus.POSTED.value, since),
            ).fetchone()
            span["count"] = row["n"]
            return int(row["n"])
