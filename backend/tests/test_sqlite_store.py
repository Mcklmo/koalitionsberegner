"""What SQLite adds over the in-memory store: it remembers.

The shared contract is covered in test_store.py, which runs against both.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.sqlite_store import SqliteElectionStore
from app.store import ClaimOutcome, JobStatus
from tests.factories import make_election, make_request

HASH = "e" * 64
PAGE = "p" * 64


@pytest.fixture
def db(tmp_path):
    return tmp_path / "elections.db"


def open_store(db, **kwargs):
    return SqliteElectionStore(db, **kwargs)


def save(store, page=PAGE, election_hash=HASH, election=None):
    store.claim(page, make_request())
    store.stage(page, election or make_election())
    return store.confirm(page, election_hash)


def test_a_stored_election_survives_a_restart(db):
    """The whole point: a second run does not re-fetch and re-extract."""
    election = make_election()
    first = open_store(db)
    save(first, election=election)
    first.close()

    second = open_store(db)
    assert second.get_election(HASH) == election
    assert second.resolve_page(PAGE) == HASH
    assert [s.election_hash for s in second.list_elections()] == [HASH]
    assert second.claim(PAGE, make_request()).outcome is ClaimOutcome.STORED, (
        "the page is recognised across runs, so nothing is extracted again"
    )
    second.close()


def test_the_database_file_is_created_on_first_use(tmp_path):
    db = tmp_path / "nested" / "dir" / "elections.db"
    store = open_store(db)
    assert db.exists(), "the parent directory is created too"
    store.close()


def test_reopening_an_existing_database_does_not_wipe_it(db):
    first = open_store(db)
    save(first)
    first.close()

    for _ in range(3):
        store = open_store(db)
        assert len(store.list_elections()) == 1, "migrations are idempotent"
        store.close()


def test_an_unconfirmed_preview_survives_a_restart(db):
    """A draft is a job row, not an election, and must stay that way."""
    first = open_store(db)
    first.claim(PAGE, make_request())
    first.stage(PAGE, make_election())
    first.close()

    second = open_store(db)
    job = second.get_job(PAGE)
    assert job.status is JobStatus.AWAITING_CONFIRMATION
    assert job.result is not None, "the extracted draft is still there"
    assert second.list_elections() == [], "and still not stored"
    second.close()


def test_a_failed_job_survives_a_restart_and_stays_retryable(db):
    first = open_store(db)
    first.claim(PAGE, make_request())
    first.fail(PAGE, "extraction failed")
    first.close()

    second = open_store(db)
    assert second.get_job(PAGE).error == "extraction failed"
    assert second.claim(PAGE, make_request()).outcome is ClaimOutcome.STARTED
    assert second.get_job(PAGE).attempt == 2, "the attempt count carries over"
    second.close()


def test_separate_connections_to_one_file_still_single_flight(db):
    """Two store instances stand in for two processes sharing the file."""
    a, b = open_store(db), open_store(db)
    outcomes = [a.claim(PAGE, make_request()).outcome,
                b.claim(PAGE, make_request()).outcome]
    assert outcomes == [ClaimOutcome.STARTED, ClaimOutcome.ATTACHED]
    a.close()
    b.close()


def test_racing_threads_on_one_file_produce_one_started_claim(db):
    """Each thread gets its own connection; SQLite's write lock serialises them."""
    store = open_store(db)
    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: store.claim(PAGE, make_request()).outcome, range(48)))

    assert outcomes.count(ClaimOutcome.STARTED) == 1
    assert outcomes.count(ClaimOutcome.ATTACHED) == 47
    store.close()


def test_racing_confirmations_across_threads_store_one_election(db):
    store = open_store(db)
    store.claim(PAGE, make_request())
    store.stage(PAGE, make_election())

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.confirm(PAGE, HASH), range(32)))

    assert all(r is not None for r in results)
    assert len(store.list_elections()) == 1
    store.close()


def test_stored_elections_are_revalidated_on_read(db):
    """Rows are re-checked against the schema; storage is not trusted."""
    import json
    import sqlite3

    store = open_store(db)
    save(store)
    store.close()

    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT election FROM elections").fetchone()
        corrupted = json.loads(row[0])
        corrupted["total_seats"] = 999  # no longer matches the seat sum
        conn.execute("UPDATE elections SET election = ?", (json.dumps(corrupted),))

    reopened = open_store(db)
    with pytest.raises(Exception):
        reopened.get_election(HASH)
    reopened.close()


def test_config_selects_the_sqlite_backend(tmp_path, monkeypatch):
    from app.config import get_store

    monkeypatch.setenv("ELECTION_STORE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "configured.db"))
    get_store.cache_clear()
    try:
        assert isinstance(get_store(), SqliteElectionStore)
        assert (tmp_path / "configured.db").exists()
    finally:
        get_store.cache_clear()


def test_a_database_from_before_curation_gains_the_column(db):
    """CREATE TABLE IF NOT EXISTS leaves an old table alone, so the column is added."""
    db.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(db)
    old.executescript(
        """
        CREATE TABLE elections (
            election_hash TEXT PRIMARY KEY,
            election      TEXT NOT NULL,
            stored_at     REAL NOT NULL
        );
        CREATE TABLE jobs (
            page_key   TEXT PRIMARY KEY,
            status     TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            attempt    INTEGER NOT NULL DEFAULT 1,
            error      TEXT,
            result     TEXT
        );
        CREATE TABLE pages (
            page_key      TEXT PRIMARY KEY,
            election_hash TEXT NOT NULL,
            linked_at     REAL NOT NULL
        );
        """
    )
    old.execute(
        "INSERT INTO elections (election_hash, election, stored_at) VALUES (?, ?, ?)",
        (HASH, json.dumps(make_election().model_dump(mode="json")), 1.0),
    )
    old.commit()
    old.close()

    store = open_store(db)
    try:
        stored = store.get_stored(HASH)
        assert stored is not None, "the election survives the upgrade"
        assert stored.selected is False, "nothing becomes public by being migrated"
        assert store.list_elections(selected_only=True) == []
        assert store.set_selected(HASH, True) is True
        assert [s.election_hash for s in store.list_elections(selected_only=True)] == [HASH]
    finally:
        store.close()
