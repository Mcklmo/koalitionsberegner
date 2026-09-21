"""Offline tests for store.py: claiming one post, marking it, taking it back."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store as store_module  # noqa: E402
from store import FAILED, NO_ELECTION, QUEUED, Flag, Post, Store  # noqa: E402


def flag(position: int = 1, thing_id: str = "t1_a") -> Flag:
    return Flag(thing_id=thing_id, kind="comment", position=position, permalink="https://r/x",
                text="who governs?", recent_election=False, upcoming_election=True,
                multi_party=True, reason="two blocs")


def post(post_id: str = "p1", *, flags=(flag(),), scanned_at: float = 1.0) -> Post:
    return Post(post_id=post_id, subreddit="denmark", permalink=f"https://r/{post_id}",
                title="t", blob_path=f"/blobs/{post_id}.json", scanned_at=scanned_at,
                comments_scanned=6, flags=tuple(flags))


def fresh(tmp_path) -> Store:
    return Store(tmp_path / "outreach.db")


def test_record_and_read_back(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post())
    row = db.get("p1")
    assert row.comments_scanned == 6
    assert row.flags[0].labels == ("upcoming_election", "multi_party")
    assert not row.processed


def test_rescan_replaces_flags_but_keeps_the_outcome(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post())
    db.mark_processed("p1", QUEUED, "queued as 3")
    db.record_scan(post(flags=(flag(position=4, thing_id="t1_b"),)))
    row = db.get("p1")
    assert [f.position for f in row.flags] == [4]
    # A better local prompt must not silently re-bill a post already answered.
    assert row.outcome == QUEUED and row.processed


def test_claim_hands_out_each_post_once(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1", scanned_at=1.0))
    db.record_scan(post("p2", scanned_at=2.0))
    assert db.claim_next().post_id == "p1"
    assert db.claim_next().post_id == "p2"
    assert db.claim_next() is None


def test_a_post_without_flags_is_never_claimed(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1", flags=()))
    assert db.claim_next() is None
    assert db.pending(retry=True) == []


def test_retry_takes_back_a_miss_but_not_a_queued_draft(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1"))
    db.record_scan(post("p2", scanned_at=2.0))
    db.claim_next()
    db.mark_processed("p1", NO_ELECTION, "asked for: issue 12", processed=False)
    db.claim_next()
    db.mark_processed("p2", QUEUED, "queued as 3")

    assert db.claim_next() is None  # neither is fresh any more
    assert [p.post_id for p in db.pending(retry=True)] == ["p1"]
    assert db.claim_next(retry=True).post_id == "p1"


def test_a_run_that_died_is_only_offered_again_with_retry(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1"))
    db.claim_next()  # ... and the process dies here, marking nothing
    assert db.claim_next() is None
    assert db.claim_next(retry=True).post_id == "p1"


def test_reset_makes_a_processed_post_claimable_again(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1"))
    db.claim_next()
    db.mark_processed("p1", QUEUED, "queued as 3")
    assert db.claim_next() is None

    assert db.reset("p1") is True
    again = db.claim_next()
    assert again.post_id == "p1" and again.outcome is None and not again.processed
    assert db.reset("nope") is False


def test_reset_all_counts_what_it_took_back(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1"))
    db.record_scan(post("p2", scanned_at=2.0))
    db.mark_processed("p1", FAILED, "no network", processed=False)
    db.mark_processed("p2", QUEUED, "queued as 3")
    assert db.reset_all() == 2
    assert db.reset_all() == 0


def test_flags_go_when_the_post_does(tmp_path):
    db = fresh(tmp_path)
    db.record_scan(post("p1"))
    db._db.execute("DELETE FROM posts WHERE post_id = 'p1'")
    assert db._db.execute("SELECT count(*) FROM flags").fetchone()[0] == 0


def test_the_schema_is_applied_twice_without_complaint(tmp_path):
    fresh(tmp_path).close()
    db = fresh(tmp_path)
    assert db.get("p1") is None
    assert store_module.RETRYABLE == (NO_ELECTION, FAILED)
