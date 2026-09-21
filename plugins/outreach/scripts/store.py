"""What the local pass found, waiting for the billed pass to be run by hand.

One SQLite file between the two commands. It exists for one reason: the
expensive half must be a deliberate act. `scan.py` can be run on twenty threads
in an evening for nothing; `reply.py next` then spends on exactly one of them,
and the row it took is marked so the next run moves on.

Marking is reversible by design — ``reset`` clears the three columns that say
"done" and the post is claimable again, which is what reprocessing one post
with a changed prompt costs. Nothing is ever deleted here.

Written like :mod:`app.sqlite_store`: a ``SCHEMA`` constant applied with
``CREATE TABLE IF NOT EXISTS``, times as epoch floats, and ``BEGIN IMMEDIATE``
around the claim so two terminals cannot take the same post.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: What `reply.py` writes into ``posts.outcome``.
QUEUED = "queued"
NOT_WORTH = "not_worth"
NO_ELECTION = "no_election"
FAILED = "failed"

#: Outcomes a later run may try again. ``no_election`` becomes answerable once
#: the election somebody asked for is imported; ``failed`` was the network.
RETRYABLE = (NO_ELECTION, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id          TEXT PRIMARY KEY,
    subreddit        TEXT NOT NULL,
    permalink        TEXT NOT NULL,
    title            TEXT NOT NULL,
    blob_path        TEXT NOT NULL,
    created_utc      REAL NOT NULL DEFAULT 0,
    scanned_at       REAL NOT NULL,
    comments_scanned INTEGER NOT NULL DEFAULT 0,
    processed_at     REAL,
    last_attempt_at  REAL,
    outcome          TEXT,
    detail           TEXT
);
CREATE TABLE IF NOT EXISTS flags (
    post_id           TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    thing_id          TEXT NOT NULL,
    kind              TEXT NOT NULL,
    position          INTEGER NOT NULL,
    permalink         TEXT NOT NULL,
    text              TEXT NOT NULL,
    recent_election   INTEGER NOT NULL,
    upcoming_election INTEGER NOT NULL,
    multi_party       INTEGER NOT NULL,
    reason            TEXT NOT NULL,
    PRIMARY KEY (post_id, thing_id)
);
CREATE INDEX IF NOT EXISTS posts_unprocessed
    ON posts(processed_at, outcome, scanned_at);
"""


@dataclass(frozen=True)
class Flag:
    """One item the local pass answered yes to, and what it answered yes about."""

    thing_id: str
    kind: str
    position: int
    permalink: str
    text: str
    recent_election: bool
    upcoming_election: bool
    multi_party: bool
    reason: str = ""

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(name for name, on in (
            ("recent_election", self.recent_election),
            ("upcoming_election", self.upcoming_election),
            ("multi_party", self.multi_party),
        ) if on)

    def as_dict(self) -> dict:
        return {
            "thing_id": self.thing_id, "kind": self.kind, "position": self.position,
            "permalink": self.permalink, "excerpt": self.text,
            "recent_election": self.recent_election,
            "upcoming_election": self.upcoming_election,
            "multi_party": self.multi_party, "reason": self.reason,
        }


@dataclass(frozen=True)
class Post:
    """A scanned thread's row, with the items the local pass flagged."""

    post_id: str
    subreddit: str
    permalink: str
    title: str
    blob_path: str
    created_utc: float = 0.0
    scanned_at: float = 0.0
    comments_scanned: int = 0
    processed_at: float | None = None
    last_attempt_at: float | None = None
    outcome: str | None = None
    detail: str | None = None
    flags: tuple[Flag, ...] = ()

    @property
    def processed(self) -> bool:
        return self.processed_at is not None


def _post_from_row(row: sqlite3.Row, flags: tuple[Flag, ...] = ()) -> Post:
    return Post(
        post_id=row["post_id"], subreddit=row["subreddit"], permalink=row["permalink"],
        title=row["title"], blob_path=row["blob_path"], created_utc=row["created_utc"],
        scanned_at=row["scanned_at"], comments_scanned=row["comments_scanned"],
        processed_at=row["processed_at"], last_attempt_at=row["last_attempt_at"],
        outcome=row["outcome"], detail=row["detail"], flags=flags,
    )


def _flag_from_row(row: sqlite3.Row) -> Flag:
    return Flag(
        thing_id=row["thing_id"], kind=row["kind"], position=row["position"],
        permalink=row["permalink"], text=row["text"],
        recent_election=bool(row["recent_election"]),
        upcoming_election=bool(row["upcoming_election"]),
        multi_party=bool(row["multi_party"]), reason=row["reason"],
    )


class Store:
    """The scanner's memory. One connection, one thread, one file."""

    def __init__(self, path: str | Path, *, now=time.time):
        self._path = Path(path).expanduser()
        self._now = now
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _write(self):
        """One write, all of it or none of it, with SQLite's write lock held."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # --- what the local pass writes -------------------------------------------

    def record_scan(self, post: Post) -> None:
        """Store a scanned thread and its flags, replacing an earlier scan of it.

        The post's own ``processed_at``/``outcome`` are left alone: rescanning
        a thread with a better local prompt must not silently re-bill a post
        that was already answered. ``reset`` is how that is asked for.
        """
        scanned_at = post.scanned_at or self._now()
        with self._write() as db:
            db.execute(
                """
                INSERT INTO posts (post_id, subreddit, permalink, title, blob_path,
                                   created_utc, scanned_at, comments_scanned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    subreddit = excluded.subreddit,
                    permalink = excluded.permalink,
                    title = excluded.title,
                    blob_path = excluded.blob_path,
                    created_utc = excluded.created_utc,
                    scanned_at = excluded.scanned_at,
                    comments_scanned = excluded.comments_scanned
                """,
                (post.post_id, post.subreddit, post.permalink, post.title, post.blob_path,
                 post.created_utc, scanned_at, post.comments_scanned),
            )
            db.execute("DELETE FROM flags WHERE post_id = ?", (post.post_id,))
            db.executemany(
                """
                INSERT INTO flags (post_id, thing_id, kind, position, permalink, text,
                                   recent_election, upcoming_election, multi_party, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(post.post_id, f.thing_id, f.kind, f.position, f.permalink, f.text,
                  int(f.recent_election), int(f.upcoming_election), int(f.multi_party), f.reason)
                 for f in post.flags],
            )

    # --- reading ---------------------------------------------------------------

    def get(self, post_id: str) -> Post | None:
        row = self._db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        return None if row is None else _post_from_row(row, self._flags(post_id))

    def _flags(self, post_id: str) -> tuple[Flag, ...]:
        rows = self._db.execute(
            "SELECT * FROM flags WHERE post_id = ? ORDER BY position", (post_id,)
        ).fetchall()
        return tuple(_flag_from_row(row) for row in rows)

    def pending(self, *, retry: bool = False, limit: int = 50) -> list[Post]:
        """Posts the billed pass has not answered, oldest scan first."""
        rows = self._db.execute(
            f"SELECT * FROM posts WHERE {_PENDING} ORDER BY {_PENDING_ORDER} LIMIT ?",
            (limit,),
        ).fetchall() if retry else self._db.execute(
            f"SELECT * FROM posts WHERE {_FRESH} ORDER BY scanned_at LIMIT ?", (limit,)
        ).fetchall()
        return [_post_from_row(row, self._flags(row["post_id"])) for row in rows]

    def all_posts(self, *, limit: int = 200) -> list[Post]:
        rows = self._db.execute(
            "SELECT * FROM posts ORDER BY scanned_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_post_from_row(row, self._flags(row["post_id"])) for row in rows]

    # --- claiming and marking ---------------------------------------------------

    def claim_next(self, *, retry: bool = False) -> Post | None:
        """Take the next unanswered post and stamp the attempt, under the write lock.

        Stamping is what makes the claim exclusive: a row that has been handed
        out has ``last_attempt_at`` set, and the default query only offers rows
        that have never been attempted. A run that died before marking its
        outcome is therefore skipped until ``--retry`` asks for it.
        """
        where, order = (_PENDING, _PENDING_ORDER) if retry else (_FRESH, "scanned_at")
        with self._write() as db:
            row = db.execute(
                f"SELECT * FROM posts WHERE {where} ORDER BY {order} LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE posts SET last_attempt_at = ? WHERE post_id = ?",
                (self._now(), row["post_id"]),
            )
        return self.get(row["post_id"])

    def attempted(self, post_id: str) -> None:
        with self._write() as db:
            db.execute("UPDATE posts SET last_attempt_at = ? WHERE post_id = ?",
                       (self._now(), post_id))

    def mark_processed(self, post_id: str, outcome: str, detail: str = "", *,
                       processed: bool = True) -> None:
        """Record how the billed pass ended.

        ``processed`` is False for an ending that is not the post's fault — no
        election stored yet, the network — so the row stays claimable with
        ``--retry`` while still carrying what happened.
        """
        now = self._now()
        with self._write() as db:
            db.execute(
                "UPDATE posts SET processed_at = ?, last_attempt_at = ?, outcome = ?, detail = ?"
                " WHERE post_id = ?",
                (now if processed else None, now, outcome, detail or None, post_id),
            )

    def reset(self, post_id: str) -> bool:
        """Forget that the billed pass ever ran on this post. Returns whether it existed."""
        with self._write() as db:
            changed = db.execute(
                "UPDATE posts SET processed_at = NULL, last_attempt_at = NULL,"
                " outcome = NULL, detail = NULL WHERE post_id = ?",
                (post_id,),
            ).rowcount
        return changed > 0

    def reset_all(self) -> int:
        with self._write() as db:
            return db.execute(
                "UPDATE posts SET processed_at = NULL, last_attempt_at = NULL,"
                " outcome = NULL, detail = NULL"
                " WHERE processed_at IS NOT NULL OR outcome IS NOT NULL"
                "    OR last_attempt_at IS NOT NULL"
            ).rowcount


#: A post nobody has spent anything on yet: scanned, flagged, never attempted.
_FRESH = (
    "processed_at IS NULL AND outcome IS NULL AND last_attempt_at IS NULL"
    " AND EXISTS (SELECT 1 FROM flags WHERE flags.post_id = posts.post_id)"
)
#: With --retry, anything still unanswered: the above, plus the posts whose
#: last run ended in a retryable outcome or died before recording one.
_PENDING = (
    "processed_at IS NULL"
    " AND (outcome IS NULL OR outcome IN ('%s'))"
    " AND EXISTS (SELECT 1 FROM flags WHERE flags.post_id = posts.post_id)"
) % "', '".join(RETRYABLE)
_PENDING_ORDER = "COALESCE(last_attempt_at, 0), scanned_at"
