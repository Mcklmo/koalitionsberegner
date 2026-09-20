"""``SqliteOutreachStore``: the approval queue remembers across a restart.

The shared behaviour every ``OutreachStore`` implementation must have is
exercised through the API in ``test_outreach.py``, against the in-memory one.
This file is what SQLite adds over that: the file survives a restart, and two
connections to it agree — the same relationship ``test_sqlite_store.py`` pins
down for the elections.
"""

from __future__ import annotations

import time

import pytest

from app.outreach import DraftStatus, NewDraft
from app.sqlite_store import SqliteOutreachStore

THING_ID = "t3_abc123"


def make_draft(**overrides) -> NewDraft:
    data = dict(
        source="reddit",
        subreddit="denmark",
        thing_id=THING_ID,
        kind="post",
        permalink="https://www.reddit.com/r/denmark/comments/abc123/thread/",
        title="Who forms a coalition?",
        excerpt="An excerpt.",
        election_hash="0" * 64,
        election_title="Denmark 2026",
        link="https://koalitionsberegner.moritzmarcus.com/e/" + "0" * 16,
        reply_text="A reply.",
        verification={"nation": "Denmark"},
        classifier_reason="mentions seats",
        created_at="2026-09-20T12:00:00Z",
    )
    data.update(overrides)
    return NewDraft(**data)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "outreach.db"


def open_store(db, **kwargs):
    return SqliteOutreachStore(db, **kwargs)


def test_a_draft_survives_a_restart(db):
    first = open_store(db)
    created = first.create(
        "draft_1", make_draft(), token_hash="h" * 64,
        token_expires_at=1000.0, emailed_at=500.0,
    )
    assert created is not None
    first.close()

    second = open_store(db)
    stored = second.get("draft_1")
    assert stored.thing_id == THING_ID
    assert stored.thread_id == "abc123"
    assert stored.status is DraftStatus.PENDING
    assert stored.verification == {"nation": "Denmark"}
    second.close()


def test_a_second_draft_for_the_same_thing_id_is_refused(db):
    store = open_store(db)
    assert store.create("draft_1", make_draft(), token_hash="h", token_expires_at=1, emailed_at=1)
    again = store.create(
        "draft_2", make_draft(permalink="https://www.reddit.com/r/denmark/comments/abc123/other/"),
        token_hash="h2", token_expires_at=1, emailed_at=1,
    )
    assert again is None
    assert len(store.list_drafts()) == 1


def test_get_by_token_hash_finds_only_an_unconsumed_token(db):
    store = open_store(db)
    store.create("draft_1", make_draft(), token_hash="abc", token_expires_at=1, emailed_at=1)

    assert store.get_by_token_hash("abc").id == "draft_1"
    assert store.get_by_token_hash("nope") is None

    store.set_status("draft_1", DraftStatus.REJECTED, consume_token=True, decided_at=2.0)
    assert store.get_by_token_hash("abc") is None
    assert store.get("draft_1").status is DraftStatus.REJECTED
    assert store.get("draft_1").token_hash is None


def test_list_drafts_is_oldest_first(db):
    store = open_store(db)
    store.create(
        "draft_b", make_draft(thing_id="t3_b", created_at="2026-09-20T12:00:02Z"),
        token_hash="b", token_expires_at=1, emailed_at=1,
    )
    store.create(
        "draft_a", make_draft(thing_id="t3_a", created_at="2026-09-20T12:00:01Z"),
        token_hash="a", token_expires_at=1, emailed_at=1,
    )

    assert [d.id for d in store.list_drafts()] == ["draft_a", "draft_b"]


def test_set_status_can_carry_arbitrary_result_fields(db):
    store = open_store(db)
    store.create("draft_1", make_draft(), token_hash="h", token_expires_at=1, emailed_at=1)

    store.set_status(
        "draft_1", DraftStatus.FAILED, last_error="Reddit refused the reply.", decided_at=42.0,
    )

    stored = store.get("draft_1")
    assert stored.status is DraftStatus.FAILED
    assert stored.last_error == "Reddit refused the reply."
    assert stored.decided_at == 42.0
    assert stored.token_hash == "h", "an ordinary failure does not consume the link"


def test_delete_removes_a_draft_that_could_not_be_emailed(db):
    store = open_store(db)
    store.create("draft_1", make_draft(), token_hash="h", token_expires_at=1, emailed_at=1)

    store.delete("draft_1")

    assert store.get("draft_1") is None
    assert store.list_drafts() == []


def test_thread_posted_is_true_only_once_a_reply_in_it_is_posted(db):
    store = open_store(db)
    store.create("draft_1", make_draft(), token_hash="h", token_expires_at=1, emailed_at=1)
    assert store.thread_posted("abc123") is False

    store.set_status("draft_1", DraftStatus.POSTED, consume_token=True, posted_at=time.time())

    assert store.thread_posted("abc123") is True
    assert store.thread_posted("some-other-thread") is False


def test_count_posted_is_windowed_by_subreddit_and_time(db):
    store = open_store(db)
    now = time.time()
    for i in range(3):
        store.create(
            f"draft_{i}", make_draft(thing_id=f"t3_{i}", permalink=f"https://reddit.com/r/denmark/comments/t{i}/a/"),
            token_hash=f"h{i}", token_expires_at=1, emailed_at=1,
        )
    store.set_status("draft_0", DraftStatus.POSTED, consume_token=True, posted_at=now - 10)
    store.set_status("draft_1", DraftStatus.POSTED, consume_token=True, posted_at=now - (8 * 24 * 3600))
    # draft_2 stays pending: not posted, so it never counts.

    assert store.count_posted("denmark", since=now - 3600) == 1
    assert store.count_posted("denmark", since=now - (9 * 24 * 3600)) == 2
    assert store.count_posted("germany", since=now - 3600) == 0
    assert store.count_posted_total(since=now - 3600) == 1
    assert store.count_posted_total(since=now - (9 * 24 * 3600)) == 2
