"""The outreach approval gate: the queue, the emailed link, and the send.

No test here ever posts to Reddit — every case uses `FakeRedditPoster`
(`app.reddit`), never `HttpxRedditPoster`. What is pinned down: the token's
lifecycle (works once, expires, never says which of unknown/consumed/expired
it was), the two factors a send needs, the etiquette caps enforced in code
regardless of what the plugin queued, the sanitiser re-applied to a
possibly-edited reply, and that a Reddit failure which might have gone through
anyway consumes the link instead of ever offering a blind retry.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.mailer import MailUnavailable
from app.outreach import DISCLOSURE, DraftStatus, InMemoryOutreachStore
from app.reddit import FakeRedditPoster, RedditUnavailable

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


ADMIN_SECRET = "a" * 40
ADMIN = {"x-admin-secret": ADMIN_SECRET}

LINK = "https://koalitionsberegner.moritzmarcus.com/e/" + "0" * 16
REPLY = "Here is a calculator that might help." + DISCLOSURE

DRAFT_BODY = {
    "source": "reddit",
    "subreddit": "denmark",
    "thing_id": "t3_abc123",
    "kind": "post",
    "permalink": "https://www.reddit.com/r/denmark/comments/abc123/some_thread/",
    "title": "Who will form a coalition?",
    "excerpt": "Some discussion excerpt.",
    "election_hash": "0" * 64,
    "election_title": "Denmark 2026",
    "link": LINK,
    "reply_text": REPLY,
    "verification": {
        "nation": "Denmark", "region": None, "year": 2026,
        "parties": [], "language": "en", "reason": "a real question about seats",
    },
    "classifier_reason": "mentions coalition seats",
    "created_at": "2026-09-20T12:00:00Z",
}


class FakeMailer:
    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def send(self, subject, body):
        if self.fail:
            raise MailUnavailable("could not send")
        self.sent.append((subject, body))


class DisabledFakeMailer:
    enabled = False

    def send(self, subject, body):
        raise MailUnavailable("outreach mail is not configured")


@pytest.fixture
def store():
    return InMemoryOutreachStore()


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def poster():
    return FakeRedditPoster()


@pytest.fixture
def client(store, mailer, poster, monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("OUTREACH_ALLOWED_SUBREDDITS", "denmark")
    monkeypatch.setenv("OUTREACH_SUBREDDIT_WEEKLY_CAP", "2")
    monkeypatch.setenv("OUTREACH_DAILY_CAP", "3")
    overrides = main.app.dependency_overrides
    overrides[main.get_outreach_store_provider] = lambda: store
    overrides[main.get_mailer_provider] = lambda: mailer
    overrides[main.get_reddit_poster_provider] = lambda: poster
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


def queue(client, **overrides) -> dict:
    body = {**DRAFT_BODY, **overrides}
    response = client.post("/api/admin/outreach/drafts", json=body, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def token_from(mailer: FakeMailer) -> str:
    """The approval token the last email's link carries."""
    _, body = mailer.sent[-1]
    match = re.search(r"/approve/([^\s]+)", body)
    assert match, body
    return match.group(1)


# --- queueing -----------------------------------------------------------------

def test_a_draft_is_queued_emailed_and_readable_from_the_link(client, mailer, store):
    queued = queue(client)

    assert re.fullmatch(r"draft_[0-9a-f]{16}", queued["id"])
    assert len(mailer.sent) == 1
    subject, body = mailer.sent[0]
    assert subject == "Approve a reply in r/denmark — Denmark 2026"
    assert "Who will form a coalition?" in body
    assert REPLY in body
    assert "72 hours" in body

    token = token_from(mailer)
    viewed = client.get(f"/api/outreach/approval/{token}")
    assert viewed.status_code == 200
    assert viewed.headers["cache-control"] == "no-store"
    assert viewed.headers["x-robots-tag"] == "noindex"
    payload = viewed.json()
    assert payload["subreddit"] == "denmark"
    assert payload["reply_text"] == REPLY
    assert payload["election_title"] == "Denmark 2026"
    assert payload["status"] == "pending"


def test_queueing_needs_the_admin_secret(client, mailer):
    refused = client.post("/api/admin/outreach/drafts", json=DRAFT_BODY)
    assert refused.status_code == 403
    assert mailer.sent == []


def test_a_second_draft_for_the_same_thing_id_is_already_queued(client):
    queue(client)
    body = {**DRAFT_BODY, "permalink": DRAFT_BODY["permalink"] + "?x=1"}
    duplicate = client.post("/api/admin/outreach/drafts", json=body, headers=ADMIN)
    assert duplicate.status_code == 409


def test_without_a_mailer_nothing_is_stored(client, store):
    main.app.dependency_overrides[main.get_mailer_provider] = lambda: DisabledFakeMailer()
    refused = client.post("/api/admin/outreach/drafts", json=DRAFT_BODY, headers=ADMIN)
    assert refused.status_code == 503
    assert store.list_drafts() == []


def test_a_reply_missing_its_disclosure_gets_it_restored(client, mailer):
    queue(client, reply_text="Here is a calculator that might help.")
    _, body = mailer.sent[0]
    assert DISCLOSURE in body


def test_a_reply_with_a_foreign_link_is_refused(client):
    bad = {**DRAFT_BODY, "reply_text": "See https://evil.example/spam" + DISCLOSURE}
    refused = client.post("/api/admin/outreach/drafts", json=bad, headers=ADMIN)
    assert refused.status_code == 422


def test_a_reply_over_the_length_cap_is_refused(client):
    bad = {**DRAFT_BODY, "reply_text": "x" * 1200 + DISCLOSURE}
    refused = client.post("/api/admin/outreach/drafts", json=bad, headers=ADMIN)
    assert refused.status_code == 422


def test_the_owners_queue_lists_what_was_queued(client):
    queue(client)
    refused = client.get("/api/admin/outreach/drafts")
    assert refused.status_code == 403
    listed = client.get("/api/admin/outreach/drafts", headers=ADMIN)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["subreddit"] == "denmark"


# --- viewing and the token's lifecycle -----------------------------------------

def test_an_unknown_token_is_404(client):
    assert client.get("/api/outreach/approval/nope").status_code == 404


def test_the_same_link_a_second_time_is_404(client, mailer, poster):
    queue(client)
    token = token_from(mailer)

    sent = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)
    assert sent.status_code == 200, sent.text

    replay = client.get(f"/api/outreach/approval/{token}")
    assert replay.status_code == 404
    replay_send = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)
    assert replay_send.status_code == 404


def test_a_link_older_than_72_hours_is_404(client, mailer, store):
    queued = queue(client)
    token = token_from(mailer)
    store.set_status(queued["id"], DraftStatus.PENDING, token_expires_at=time.time() - 1)

    expired = client.get(f"/api/outreach/approval/{token}")

    assert expired.status_code == 404
    assert store.get(queued["id"]).status is DraftStatus.EXPIRED


def test_sending_without_the_secret_is_refused(client, mailer, poster):
    queue(client)
    token = token_from(mailer)

    refused = client.post(f"/api/outreach/approval/{token}/send")

    assert refused.status_code == 403
    assert poster.posted == []


# --- sending --------------------------------------------------------------

def test_a_pending_draft_is_posted_and_the_result_recorded(client, mailer, poster, store):
    queued = queue(client)
    token = token_from(mailer)

    sent = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)

    assert sent.status_code == 200
    assert sent.json()["posted_url"]
    assert poster.posted == [("t3_abc123", REPLY)]
    stored = store.get(queued["id"])
    assert stored.status is DraftStatus.POSTED
    assert stored.posted_url == sent.json()["posted_url"]
    assert stored.edited is False


def test_the_owner_may_edit_the_reply_before_sending(client, mailer, poster, store):
    queued = queue(client)
    token = token_from(mailer)
    edited_text = "A shorter reply of my own."

    sent = client.post(
        f"/api/outreach/approval/{token}/send", headers=ADMIN,
        json={"reply_text": edited_text},
    )

    assert sent.status_code == 200, sent.text
    assert poster.posted == [("t3_abc123", edited_text + DISCLOSURE)]
    assert store.get(queued["id"]).edited is True


def test_an_edit_with_a_foreign_link_is_refused_at_send_too(client, mailer, poster):
    queue(client)
    token = token_from(mailer)

    refused = client.post(
        f"/api/outreach/approval/{token}/send", headers=ADMIN,
        json={"reply_text": "see https://evil.example"},
    )

    assert refused.status_code == 422
    assert poster.posted == []


def test_posting_disabled_is_a_503_that_changes_nothing(client, mailer, store):
    from app.reddit import DisabledRedditPoster

    queued = queue(client)
    token = token_from(mailer)
    main.app.dependency_overrides[main.get_reddit_poster_provider] = DisabledRedditPoster

    refused = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)

    assert refused.status_code == 503
    assert store.get(queued["id"]).status is DraftStatus.PENDING
    # The link still works: this was a configuration problem, not a decision.
    assert client.get(f"/api/outreach/approval/{token}").status_code == 200


def test_a_maybe_posted_failure_consumes_the_link_with_no_blind_retry(client, mailer, poster, store):
    queued = queue(client)
    token = token_from(mailer)
    poster.fail_with = RedditUnavailable("Reddit did not confirm the reply; check the thread before retrying.", maybe_posted=True)

    failed = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)

    assert failed.status_code == 502
    assert "check the thread" in failed.json()["detail"]
    assert store.get(queued["id"]).status is DraftStatus.FAILED
    # The token is gone: nothing offers a second click that could double-post.
    assert client.get(f"/api/outreach/approval/{token}").status_code == 404
    assert client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN).status_code == 404


def test_an_ordinary_failure_keeps_the_link_for_one_retry(client, mailer, poster, store):
    queued = queue(client)
    token = token_from(mailer)
    poster.fail_with = RedditUnavailable("Reddit refused the reply.")

    failed = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)
    assert failed.status_code == 502
    assert store.get(queued["id"]).status is DraftStatus.FAILED

    # The link still works, and a retry with the trouble cleared succeeds.
    assert client.get(f"/api/outreach/approval/{token}").status_code == 200
    poster.fail_with = None
    retried = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)
    assert retried.status_code == 200
    assert store.get(queued["id"]).status is DraftStatus.POSTED


# --- rejecting ------------------------------------------------------------

def test_rejecting_needs_the_secret_and_consumes_the_link(client, mailer, poster, store):
    queued = queue(client)
    token = token_from(mailer)

    assert client.post(f"/api/outreach/approval/{token}/reject").status_code == 403

    rejected = client.post(f"/api/outreach/approval/{token}/reject", headers=ADMIN)
    assert rejected.status_code == 200
    assert store.get(queued["id"]).status is DraftStatus.REJECTED
    assert client.get(f"/api/outreach/approval/{token}").status_code == 404
    assert poster.posted == []


# --- etiquette, enforced in code -------------------------------------------

def other_thing(client, mailer, *, thing_id: str, permalink: str, subreddit: str = "denmark") -> str:
    """Queue a second draft in a different thread and return its token."""
    queue(client, thing_id=thing_id, permalink=permalink, subreddit=subreddit)
    return token_from(mailer)


def test_a_subreddit_outside_the_allowed_list_never_posts(client, mailer, poster):
    queue(client, subreddit="worldnews")
    token = token_from(mailer)

    refused = client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN)

    assert refused.status_code == 409
    assert "allowed list" in refused.json()["detail"]
    assert poster.posted == []


def test_only_one_reply_per_thread_ever(client, mailer, poster):
    token1 = other_thing(
        client, mailer, thing_id="t3_first",
        permalink="https://www.reddit.com/r/denmark/comments/thread1/a/",
    )
    assert client.post(f"/api/outreach/approval/{token1}/send", headers=ADMIN).status_code == 200

    token2 = other_thing(
        client, mailer, thing_id="t1_second",
        permalink="https://www.reddit.com/r/denmark/comments/thread1/a/comment_xyz/",
    )
    refused = client.post(f"/api/outreach/approval/{token2}/send", headers=ADMIN)

    assert refused.status_code == 409
    assert "already been posted" in refused.json()["detail"]
    assert poster.posted == [("t3_first", REPLY)]


def test_the_weekly_cap_per_subreddit_stops_a_third_reply(client, mailer, monkeypatch):
    monkeypatch.setenv("OUTREACH_SUBREDDIT_WEEKLY_CAP", "2")
    monkeypatch.setenv("OUTREACH_DAILY_CAP", "10")
    tokens = [
        other_thing(
            client, mailer, thing_id=f"t3_w{i}",
            permalink=f"https://www.reddit.com/r/denmark/comments/w{i}/a/",
        )
        for i in range(3)
    ]
    for token in tokens[:2]:
        assert client.post(f"/api/outreach/approval/{token}/send", headers=ADMIN).status_code == 200

    refused = client.post(f"/api/outreach/approval/{tokens[2]}/send", headers=ADMIN)

    assert refused.status_code == 409
    assert "weekly" in refused.json()["detail"]


def test_the_daily_cap_stops_the_next_reply_regardless_of_subreddit(client, mailer, monkeypatch):
    monkeypatch.setenv("OUTREACH_ALLOWED_SUBREDDITS", "denmark,germany")
    monkeypatch.setenv("OUTREACH_SUBREDDIT_WEEKLY_CAP", "10")
    monkeypatch.setenv("OUTREACH_DAILY_CAP", "1")
    token1 = other_thing(
        client, mailer, thing_id="t3_d1",
        permalink="https://www.reddit.com/r/denmark/comments/d1/a/",
    )
    token2 = other_thing(
        client, mailer, thing_id="t3_d2",
        permalink="https://www.reddit.com/r/germany/comments/d2/a/", subreddit="germany",
    )
    assert client.post(f"/api/outreach/approval/{token1}/send", headers=ADMIN).status_code == 200

    refused = client.post(f"/api/outreach/approval/{token2}/send", headers=ADMIN)

    assert refused.status_code == 409
    assert "daily" in refused.json()["detail"]
