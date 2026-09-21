"""Offline tests for reply.py: one scanned post, one draft, no network anywhere.

Claude is a canned answer, the site is a dict. What is actually asserted is the
part no model decides: which item is answered, which link is built, what the
reply may contain, and what the database says afterwards.

Run: uv run --with pytest --with pydantic --with anthropic pytest plugins/outreach/scripts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reply  # noqa: E402
from common import DISCLOSURE, MAX_REPLY_CHARS  # noqa: E402
from reply import (  # noqa: E402
    Composition,
    ElectionIndex,
    Identification,
    PrintingSink,
    ServerSink,
    StoredElection,
    build_request,
    coalition_link,
    fetch_election,
    file_election_request,
    run_next,
    sanitize_reply,
    thread_document,
)
from store import FAILED, NO_ELECTION, NOT_WORTH, QUEUED, Store  # noqa: E402
from test_scan import thread  # noqa: E402

SITE = "https://koalitionsberegner.example"

SUMMARIES = [
    {"election_hash": "a" * 64, "nation": "Danmark", "state": None, "election_date": "2026-03-25",
     "title": "Denmark — 2026", "forecast": None},
    {"election_hash": "b" * 64, "nation": "Germany", "state": None, "election_date": "2029-09-23",
     "title": "Germany — 2029 (Forsa)", "forecast": {"publisher": "Forsa", "published_on": "2026-09-01"}},
    {"election_hash": "c" * 64, "nation": "Germany", "state": None, "election_date": "2029-09-23",
     "title": "Germany — 2029 (INSA)", "forecast": {"publisher": "INSA", "published_on": "2026-09-10"}},
    {"election_hash": "d" * 64, "nation": "Germany", "state": "Sachsen-Anhalt",
     "election_date": "2026-09-06", "title": "Sachsen-Anhalt — 2026", "forecast": None},
]

ELECTION = {
    "election_hash": "a" * 64,
    "election": {
        "title": "Denmark — 2026", "total_seats": 179, "majority_seats": 90,
        "blocks": [
            {"name": "Rød blok", "parties": [
                {"name": "Social Democrats", "local_name": "Socialdemokratiet", "abbr": "A", "seats": 50},
                {"name": "Green Left", "local_name": "SF", "abbr": "F", "seats": 15},
            ]},
            {"name": "Blå blok", "parties": [
                {"name": "Liberals", "local_name": "Venstre", "abbr": "V", "seats": 23},
                {"name": "Denmark Democrats", "local_name": None, "abbr": "Æ", "seats": 14},
            ]},
        ],
    },
}


class FakeHttp:
    """The site, as far as reply.py can tell. Records every call."""

    def __init__(self, *, requests_status: int = 201):
        self.calls: list[tuple] = []
        self.requests_status = requests_status

    def __call__(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url, body))
        if url.endswith("/api/elections"):
            return 200, SUMMARIES
        if "/api/elections/requests" in url:
            if self.requests_status == 409:
                return 409, {"detail": "already imported"}
            return self.requests_status, {"url": "https://github.com/x/y/issues/12",
                                          "number": 12, "duplicate": False}
        if "/api/elections/" in url:
            return 200, ELECTION
        if url.endswith("/api/admin/outreach/drafts"):
            return 201, {"id": "draft-7"}
        raise AssertionError(f"unexpected call to {url}")


class FakeWriter:
    """Claude, with its two answers written down in advance."""

    def __init__(self, identified: Identification, composed: Composition | None = None):
        self._identified = identified
        self._composed = composed
        self.documents: list[str] = []
        self.composed_with: list[StoredElection] = []

    def identify(self, document: str) -> Identification:
        self.documents.append(document)
        return self._identified

    def compose(self, document: str, election: StoredElection) -> Composition:
        self.documents.append(document)
        self.composed_with.append(election)
        assert self._composed is not None, "compose should not have been called"
        return self._composed


DANISH = Identification(about_election=True, worth_replying=True, nation="Danmark",
                        region=None, year=2026, parties=("A", "V"), language="da",
                        reason="they are counting seats")


def scanned(tmp_path, comments: list[str], *, flagged_position: int = 1) -> tuple[Store, str]:
    """A post in the database with its blob on disk, as scan.py would leave it."""
    raw = thread(comments)
    blob = tmp_path / "blob.json"
    blob.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    store = Store(tmp_path / "db.sqlite")
    from store import Flag, Post

    store.record_scan(Post(
        post_id="p1", subreddit="denmark", permalink="https://www.reddit.com/r/denmark/comments/p1/x/",
        title="Smørrebrød i Aarhus?", blob_path=str(blob), comments_scanned=3,
        flags=(Flag(thing_id=f"t1_c{flagged_position - 1}", kind="comment",
                    position=flagged_position, permalink="https://r/x", text=comments[flagged_position - 1],
                    recent_election=False, upcoming_election=True, multi_party=True,
                    reason="two blocs"),),
    ))
    return store, str(blob)


# --- what the model is shown ------------------------------------------------------


def test_the_model_sees_every_comment_not_only_the_flagged_one():
    from common import thread_from_blob

    post, comments = thread_from_blob(thread(["first", "second", "third"]))
    document = thread_document(post, comments, {2: "upcoming_election"})
    for text in ("first", "second", "third"):
        assert text in document
    assert "[3] comment" in document
    # The flag is named as a hint, and said to be one.
    assert "hint only" in document and "[2] upcoming_election" in document


def test_a_very_long_thread_is_cut_and_says_so(monkeypatch):
    from common import thread_from_blob

    monkeypatch.setattr(reply, "MAX_THREAD_ITEMS", 2)
    post, comments = thread_from_blob(thread(["a", "b", "c", "d"]))
    document = thread_document(post, comments, {})
    assert "2 further comments are not shown" in document
    assert "[3] comment" not in document


def test_a_request_carries_no_tools_and_fences_the_thread():
    request = build_request("system words", "thread words", model="claude-opus-5")
    assert request["model"] == "claude-opus-5" and "tools" not in request
    assert request["messages"][0]["content"].startswith("<document>")
    assert "thread words" in request["messages"][0]["content"]


# --- the link ----------------------------------------------------------------------


def test_the_election_is_flattened_block_by_block():
    election = fetch_election(SITE, "a" * 64, http=FakeHttp())
    assert [(p.position, p.abbr, p.seats) for p in election.parties] == [
        (0, "A", 50), (1, "F", 15), (2, "V", 23), (3, "Æ", 14)]
    assert election.majority_seats == 90


def test_the_link_names_a_coalition_not_the_front_page():
    election = fetch_election(SITE, "a" * 64, http=FakeHttp())
    # Out of order and repeated on the way in; ascending and unique on the way
    # out, exactly as js/share.js encodes a selection.
    link = coalition_link(SITE, election, [2, 0, 2], 73)
    assert link == f"{SITE}/e/{'a' * 16}?c=0,2&s=73"


# --- the reply text -----------------------------------------------------------------


def test_the_numbers_are_ours_and_the_link_is_ours():
    text = sanitize_reply(
        "{seats} af 179, altså {short_by} fra {majority}. Prøv selv: {link}",
        "https://site/e/abc?c=0,2&s=73", seats=73, majority=90)
    assert text.startswith("73 af 179, altså 17 fra 90.")
    assert "https://site/e/abc?c=0,2&s=73" in text
    assert text.endswith(DISCLOSURE)


def test_no_url_but_ours_survives():
    text = sanitize_reply("see https://example.com/spam and www.evil.dk {link}",
                          "https://site/e/abc", seats=1, majority=2)
    assert "example.com" not in text and "evil.dk" not in text
    assert text.count("https://site/e/abc") == 1


def test_a_draft_without_the_placeholder_still_carries_the_link():
    text = sanitize_reply("de når aldrig 90.", "https://site/e/abc", seats=1, majority=90)
    assert text.startswith("de når aldrig 90.") and "https://site/e/abc" in text


def test_a_long_draft_is_cut_and_keeps_both_link_and_footer():
    text = sanitize_reply("x" * 4000 + " {link}", "https://site/e/abc", seats=1, majority=2)
    assert len(text) <= MAX_REPLY_CHARS
    assert "https://site/e/abc" in text and text.endswith(DISCLOSURE)


# --- one post, end to end ------------------------------------------------------------


def test_the_answer_may_be_a_comment_the_local_pass_never_saw(tmp_path):
    store, _ = scanned(tmp_path, ["flagged one", "another", "the best opening"])
    http = FakeHttp()
    writer = FakeWriter(DANISH, Composition(target_position=3, coalition=(0, 2),
                                            reply_draft="A og V når {seats}. {link}",
                                            language="da", reason="sharpest"))
    result = run_next(store, post_id="p1", retry=False, writer=writer,
                      index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                      sink=ServerSink(site=SITE, admin_secret="s", http=http), http=http)

    assert result["outcome"] == QUEUED and result["detail"] == "queued as draft-7"
    assert result["answered"] == {"position": 3, "thing_id": "t1_c2",
                                  "permalink": "https://www.reddit.com/r/denmark/comments/p1/x/c2/",
                                  "was_flagged": False}
    assert result["seats"] == 73 and result["coalition"] == ["A", "V"]
    assert result["link"] == f"{SITE}/e/{'a' * 16}?c=0,2&s=73"

    draft = [body for method, url, body in http.calls if url.endswith("/drafts")][0]
    assert draft["thing_id"] == "t1_c2" and draft["kind"] == "comment"
    assert draft["excerpt"] == "the best opening"
    assert draft["reply_text"].startswith("A og V når 73.") and draft["reply_text"].endswith(DISCLOSURE)
    assert store.get("p1").processed and store.get("p1").outcome == QUEUED


def test_a_position_the_thread_does_not_have_falls_back_to_the_post(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(DANISH, Composition(target_position=99, coalition=(0, 1),
                                            reply_draft="{link}"))
    result = run_next(store, post_id="p1", retry=False, writer=writer,
                      index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                      sink=ServerSink(site=SITE, admin_secret="s", http=http), http=http)
    assert result["answered"]["thing_id"] == "t3_p1"
    assert result["outcome"] == QUEUED


def test_nothing_worth_answering_never_reaches_the_second_call(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(Identification(about_election=True, worth_replying=False,
                                       reason="a joke thread"))
    result = run_next(store, post_id="p1", retry=False, writer=writer,
                      index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                      sink=ServerSink(site=SITE, admin_secret="s", http=http), http=http)
    assert result["outcome"] == NOT_WORTH and store.get("p1").processed
    assert writer.composed_with == []
    assert not any(url.endswith("/drafts") for _m, url, _b in http.calls)


def test_an_election_we_do_not_hold_is_asked_for_and_the_post_kept(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(Identification(about_election=True, worth_replying=True,
                                       nation="Portugal", year=2026, reason="seats"))
    result = run_next(store, post_id="p1", retry=False, writer=writer,
                      index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                      sink=ServerSink(site=SITE, admin_secret="s", http=http), http=http)

    filed = [body for _m, url, body in http.calls if url.endswith("/api/elections/requests")]
    assert filed == [{"year": 2026, "nation": "Portugal", "subnation": None}]
    assert result["outcome"] == NO_ELECTION and result["processed"] is False
    assert "issues/12" in store.get("p1").detail
    # Kept, not finished: once the election is imported, --retry takes it again.
    assert not store.get("p1").processed
    assert store.claim_next(retry=True).post_id == "p1"


def test_a_missing_election_with_no_year_is_not_filed(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(Identification(about_election=True, worth_replying=True,
                                       nation=None, year=None, reason="unclear"))
    result = run_next(store, post_id="p1", retry=False, writer=writer,
                      index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                      sink=PrintingSink(), http=http)
    assert result["outcome"] == NO_ELECTION
    assert not any("requests" in url for _m, url, _b in http.calls)


def test_a_coalition_of_one_party_is_refused(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(DANISH, Composition(target_position=1, coalition=(0, 99),
                                            reply_draft="{link}"))
    result = run_next(store, post_id="p1", retry=False, writer=writer,
                      index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                      sink=ServerSink(site=SITE, admin_secret="s", http=http), http=http)
    assert result["outcome"] == FAILED and result["processed"] is False
    assert "fewer than two parties" in store.get("p1").detail


def test_reset_lets_the_same_post_be_answered_again(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(DANISH, Composition(target_position=1, coalition=(0, 2),
                                            reply_draft="{link}"))
    common = dict(retry=False, writer=writer,
                  index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                  sink=ServerSink(site=SITE, admin_secret="s", http=http), http=http)
    run_next(store, post_id=None, **common)
    assert run_next(store, post_id=None, **common)["nothing_to_do"]

    store.reset("p1")
    assert run_next(store, post_id=None, **common)["outcome"] == QUEUED


def test_a_dry_run_prints_the_draft_and_queues_nothing(tmp_path, capsys):
    store, _ = scanned(tmp_path, ["one", "two"])
    http = FakeHttp()
    writer = FakeWriter(DANISH, Composition(target_position=1, coalition=(0, 2),
                                            reply_draft="A og V. {link}"))
    run_next(store, post_id="p1", retry=False, writer=writer,
             index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
             sink=PrintingSink(), http=http)
    printed = json.loads(capsys.readouterr().out)
    assert printed["draft"]["reply_text"].startswith("A og V.")
    assert not any(url.endswith("/drafts") for _m, url, _b in http.calls)


def test_a_refused_admin_secret_stops_the_run(tmp_path):
    store, _ = scanned(tmp_path, ["one", "two"])

    def http(method, url, *, headers=None, body=None, timeout=30.0):
        if url.endswith("/drafts"):
            return 403, {"detail": "no"}
        return FakeHttp()(method, url, headers=headers, body=body, timeout=timeout)

    writer = FakeWriter(DANISH, Composition(target_position=1, coalition=(0, 2),
                                            reply_draft="{link}"))
    with pytest.raises(PermissionError):
        run_next(store, post_id="p1", retry=False, writer=writer,
                 index_of=lambda: ElectionIndex.from_summaries(SUMMARIES), site=SITE,
                 sink=ServerSink(site=SITE, admin_secret="wrong", http=http), http=http)
    assert store.get("p1").outcome == FAILED


def test_an_already_imported_election_is_not_filed_twice(tmp_path):
    detail, url = file_election_request(SITE, nation="Denmark", region=None, year=2026,
                                        http=FakeHttp(requests_status=409))
    assert "already imported" in detail and url == ""


# --- picking which stored election to link -----------------------------------------


def test_a_result_wins_over_a_poll_and_the_newest_poll_over_an_older_one():
    index = ElectionIndex.from_summaries(SUMMARIES)
    assert index.match("Danmark", None, 2026).election_hash == "a" * 64
    assert index.match("Germany", None, 2029).election_hash == "c" * 64


def test_a_national_election_never_stands_in_for_a_regional_one():
    index = ElectionIndex.from_summaries(SUMMARIES)
    assert index.match("Germany", "Bayern", 2026) is None
    assert index.match("Germany", "Sachsen-Anhalt", 2026).election_hash == "d" * 64
    assert index.match(None, None, 2026) is None


def test_the_election_is_fetched_by_the_hash_the_index_gave():
    http = FakeHttp()
    fetch_election(SITE, "a" * 64, http=http)
    assert http.calls[0][1] == f"{SITE}/api/elections/{'a' * 64}"
