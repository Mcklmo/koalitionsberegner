"""Offline tests for scan.py: a saved thread in, a blob and flags out.

Nothing here touches the network, Reddit, Ollama or Claude.

Run: uv run --with pytest --with pydantic --with anthropic pytest plugins/outreach/scripts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan  # noqa: E402
from common import NotAThread, thread_from_blob  # noqa: E402
from scan import (  # noqa: E402
    BATCH,
    Classification,
    GaveUp,
    KeywordClassifier,
    OllamaClassifier,
    OpenAiCompatibleClassifier,
    classification_from,
    expand_sources,
    ingest,
    scan_thread,
)
from store import Store  # noqa: E402

POLITICS = "Kan rød blok danne regering med de radikale, eller mangler de mandater?"


def thread(comments: list[str], *, title: str = "Smørrebrød i Aarhus?", selftext: str = "hvor?") -> list:
    """Reddit's own two-element listing for one thread."""
    return [
        {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {
            "id": "p1", "subreddit": "Denmark", "title": title, "selftext": selftext,
            "permalink": "/r/denmark/comments/p1/x/", "created_utc": 100, "num_comments": len(comments),
        }}]}},
        {"kind": "Listing", "data": {"children": [
            {"kind": "t1", "data": {"id": f"c{i}", "body": body,
                                    "permalink": f"/r/denmark/comments/p1/x/c{i}/",
                                    "created_utc": 100 + i}}
            for i, body in enumerate(comments)
        ] + [{"kind": "more", "data": {"id": "zzz", "children": ["a", "b"]}}]}},
    ]


class CountingClassifier:
    """Answers from a table and remembers exactly which items it was asked about."""

    def __init__(self, positives: set[int], *, errors: set[int] = frozenset()):
        self.positives = positives
        self.errors = errors
        self.asked: list[int] = []

    def classify(self, candidate) -> Classification:
        self.asked.append(candidate.position)
        if candidate.position in self.errors:
            raise RuntimeError("ollama is not running")
        if candidate.position in self.positives:
            return Classification(upcoming_election=True, multi_party=True, reason="blocs")
        return Classification(reason="food")


# --- reading a saved thread ---------------------------------------------------


def test_a_saved_thread_becomes_a_post_and_its_comments():
    post, comments = thread_from_blob(thread(["first", "second"]))
    assert (post.kind, post.id, post.position, post.thing_id) == ("post", "p1", 0, "t3_p1")
    # Reddit spells it "Denmark"; everything downstream compares lower case.
    assert post.subreddit == "denmark"
    assert post.permalink == "https://www.reddit.com/r/denmark/comments/p1/x/"
    assert [(c.position, c.text, c.thing_id) for c in comments] == [
        (1, "first", "t1_c0"), (2, "second", "t1_c1")]
    assert comments[0].permalink.endswith("/c0/")


def test_deleted_and_load_more_rows_are_dropped():
    raw = thread(["kept", "[deleted]", "[removed]", ""])
    _post, comments = thread_from_blob(raw)
    assert [c.text for c in comments] == ["kept"]


def test_a_comment_is_cut_to_the_limit_it_is_read_with():
    _post, comments = thread_from_blob(thread(["x" * 3000]), comment_limit=100)
    assert len(comments[0].text) == 100


def test_a_file_that_is_not_a_thread_says_so():
    with pytest.raises(NotAThread):
        thread_from_blob({"posts": []})
    with pytest.raises(NotAThread):
        thread_from_blob([{"data": {"children": []}}])


# --- what the local model answers ----------------------------------------------


def test_the_schema_is_required():
    assert classification_from('{"recent_election": true, "upcoming_election": false,'
                               ' "multi_party": false, "reason": "r"}').recent_election
    with pytest.raises(ValueError):
        classification_from('{"political": true}')


def test_any_yes_is_a_yes():
    assert not Classification().positive
    assert Classification(multi_party=True).positive
    assert Classification(recent_election=True).labels == ("recent_election",)


def test_the_keyword_classifier_answers_all_three():
    post, comments = thread_from_blob(thread([POLITICS]))
    label = KeywordClassifier().classify(comments[0])
    assert label.multi_party and label.positive
    assert not KeywordClassifier().classify(post).positive


def test_the_model_is_told_when_it_was_posted_and_when_now_is():
    """The bug this prevents: "Alle Ergebnisse der Wahl … 2026", posted the day
    after that election, labelled upcoming on the strength of the year."""
    seen = {}

    def http(method, url, *, body=None, **kw):
        seen.update(body=body)
        return 200, {"message": {"content": json.dumps(
            {"recent_election": True, "upcoming_election": False,
             "multi_party": False, "reason": "results"})}}

    post, _comments = thread_from_blob(thread([], title="Alle Ergebnisse der Wahl 2026"))
    OllamaClassifier(url="http://x", model="m", http=http, now="2026-09-21").classify(post)
    asked = seen["body"]["messages"][1]["content"]
    assert asked.startswith("Today is 2026-09-21.")
    assert "Posted: 1970-01-01" in asked  # created_utc 100, the fixture's own clock
    # Ours is stated outside the markers; only Reddit's text goes inside them.
    assert asked.index("Today is") < asked.index("<document>")


def test_ollama_is_asked_with_the_schema():
    seen = {}

    def http(method, url, *, body=None, **kw):
        seen.update(method=method, url=url, body=body)
        return 200, {"message": {"content": json.dumps(
            {"recent_election": False, "upcoming_election": True,
             "multi_party": False, "reason": "in March"})}}

    _post, comments = thread_from_blob(thread([POLITICS]))
    label = OllamaClassifier(url="http://localhost:11434/", model="qwen3:8b", http=http).classify(comments[0])
    assert label.upcoming_election and not label.multi_party
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["format"] == scan.CLASSIFIER_SCHEMA
    assert seen["body"]["options"]["temperature"] == 0
    # The comment reaches the model fenced as data, never as instructions.
    assert "<document>" in seen["body"]["messages"][1]["content"]


def test_an_openai_compatible_server_gets_a_strict_json_schema():
    def http(method, url, *, body=None, **kw):
        assert url.endswith("/v1/chat/completions")
        assert body["response_format"]["json_schema"]["strict"] is True
        return 200, {"choices": [{"message": {"content": {
            "recent_election": True, "upcoming_election": False,
            "multi_party": True, "reason": "r"}}}]}

    _post, comments = thread_from_blob(thread([POLITICS]))
    label = OpenAiCompatibleClassifier(url="http://x", model="m", http=http).classify(comments[0])
    assert label.recent_election and label.multi_party


def test_a_model_server_that_refuses_is_an_error():
    with pytest.raises(RuntimeError):
        OllamaClassifier(url="http://x", model="m",
                         http=lambda *a, **k: (500, None)).classify(
            thread_from_blob(thread(["hi"]))[1][0])


# --- the early stop --------------------------------------------------------------


def test_a_positive_post_stops_before_any_comment_is_read():
    post, comments = thread_from_blob(thread(["a", "b", "c", "d"]))
    classifier = CountingClassifier({0})
    flags, scanned = scan_thread(post, comments, classifier)
    assert classifier.asked == [0]
    assert scanned == 0
    assert [f.thing_id for f in flags] == ["t3_p1"]


def test_the_scan_stops_after_the_batch_that_found_something():
    post, comments = thread_from_blob(thread([f"c{i}" for i in range(9)]))
    classifier = CountingClassifier({5})  # the second batch of three
    flags, scanned = scan_thread(post, comments, classifier)
    assert scanned == 6
    # Everything past that batch is a call the local model never had to make.
    assert sorted(classifier.asked) == [0, 1, 2, 3, 4, 5, 6]
    assert [f.position for f in flags] == [5]


def test_every_positive_of_the_stopping_batch_is_flagged():
    post, comments = thread_from_blob(thread([f"c{i}" for i in range(6)]))
    flags, scanned = scan_thread(post, comments, CountingClassifier({1, 3}))
    assert [f.position for f in flags] == [1, 3]
    assert scanned == BATCH


def test_a_thread_with_nothing_in_it_is_read_to_the_end():
    post, comments = thread_from_blob(thread([f"c{i}" for i in range(5)]))
    classifier = CountingClassifier(set())
    flags, scanned = scan_thread(post, comments, classifier)
    assert flags == [] and scanned == 5
    assert len(classifier.asked) == 6


def test_one_bad_answer_does_not_end_the_scan():
    post, comments = thread_from_blob(thread([f"c{i}" for i in range(4)]))
    errors: list[str] = []
    flags, scanned = scan_thread(post, comments, CountingClassifier({4}, errors={2}), errors=errors)
    assert [f.position for f in flags] == [4]
    assert errors and "classify t1_c1" in errors[0]


def test_a_model_that_keeps_failing_is_given_up_on():
    post, comments = thread_from_blob(thread([f"c{i}" for i in range(9)]))
    classifier = CountingClassifier(set(), errors={0, 1, 2, 3})
    with pytest.raises(GaveUp):
        scan_thread(post, comments, classifier)


# --- ingest ------------------------------------------------------------------------


def write(tmp_path: Path, raw: list, name: str = "saved.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_ingest_keeps_the_blob_verbatim_and_records_the_row(tmp_path):
    raw = thread(["food", POLITICS])
    path = write(tmp_path, raw)
    store = Store(tmp_path / "db.sqlite")
    result = ingest(str(path), blobs=tmp_path / "blobs", store=store,
                    classifier=KeywordClassifier(), rescan=False)

    blob = Path(result["blob"])
    assert blob == tmp_path / "blobs" / "denmark" / "p1.json"
    assert json.loads(blob.read_text(encoding="utf-8")) == raw
    row = store.get("p1")
    assert row.blob_path == str(blob) and row.comments_scanned == 2
    assert [f.position for f in row.flags] == [2]
    assert result["flagged"][0]["multi_party"] is True


def test_a_thread_already_scanned_is_left_alone_unless_asked(tmp_path):
    path = write(tmp_path, thread(["food", POLITICS]))
    store = Store(tmp_path / "db.sqlite")
    kwargs = dict(blobs=tmp_path / "blobs", store=store, classifier=KeywordClassifier())
    ingest(str(path), rescan=False, **kwargs)

    again = ingest(str(path), rescan=False, **kwargs)
    assert again["skipped"] == "already scanned"

    rescanned = ingest(str(path), rescan=True, **kwargs)
    assert "skipped" not in rescanned and rescanned["comments_scanned"] == 2


def test_a_file_that_is_not_json_says_what_to_do(tmp_path):
    path = tmp_path / "saved.json"
    path.write_text("<html>not json</html>", encoding="utf-8")
    with pytest.raises(NotAThread, match="not JSON"):
        ingest(str(path), blobs=tmp_path, store=Store(tmp_path / "db.sqlite"),
               classifier=KeywordClassifier(), rescan=False)


def test_the_command_line_scans_a_file_and_prints_the_blob_and_the_flags(tmp_path, capsys):
    path = write(tmp_path, thread(["food", POLITICS]))
    code = scan.main([str(path), "--classifier", "keyword",
                      "--db", str(tmp_path / "db.sqlite"), "--blobs", str(tmp_path / "blobs")])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    post = printed["posts"][0]
    assert post["blob"].endswith("/denmark/p1.json")
    assert [f["thing_id"] for f in post["flagged"]] == ["t1_c1"]


def test_the_command_line_survives_a_bad_file(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    code = scan.main([str(bad), "--classifier", "keyword",
                      "--db", str(tmp_path / "db.sqlite"), "--blobs", str(tmp_path / "blobs")])
    printed = json.loads(capsys.readouterr().out)
    assert code == 1 and printed["errors"]


# --- where the files come from ------------------------------------------------------


def test_a_directory_stands_for_the_json_files_in_it(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("b.json", "a.json", "notes.txt"):
        (inbox / name).write_text("{}", encoding="utf-8")
    found = expand_sources([str(inbox)], inbox=tmp_path / "unused")
    assert [Path(f).name for f in found] == ["a.json", "b.json"]


def test_naming_nothing_reads_the_inbox_and_makes_it(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    write(inbox, thread(["one"]), "saved.json")
    assert [Path(f).name for f in expand_sources([], inbox=inbox)] == ["saved.json"]

    empty = tmp_path / "fresh"
    assert expand_sources([], inbox=empty) == []
    assert empty.is_dir()  # made, so there is somewhere to drop the next one


def test_a_file_named_directly_is_left_alone(tmp_path):
    path = write(tmp_path, thread(["one"]))
    assert expand_sources([str(path), "-"], inbox=tmp_path) == [str(path), "-"]


def test_the_command_line_with_no_file_scans_the_inbox(tmp_path, capsys, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    write(inbox, thread(["food", POLITICS]), "saved.json")
    monkeypatch.setenv("OUTREACH_INBOX", str(inbox))
    code = scan.main(["--classifier", "keyword", "--db", str(tmp_path / "db.sqlite"),
                      "--blobs", str(tmp_path / "blobs")])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and printed["posts"][0]["post_id"] == "p1"


# --- --slim ---------------------------------------------------------------------


def cluttered() -> list:
    """A thread as Reddit really serves it: the fields we read, buried."""
    raw = thread(["kept", "[deleted]"])
    raw[0]["data"]["children"][0]["data"].update(
        all_awardings=[{"name": "Gold"}], body_html="<div>…</div>", author_flair_richtext=[],
        mod_reports=[], gildings={"gid_1": 3}, selftext_html="<p>hvor?</p>")
    for child in raw[1]["data"]["children"]:
        if child["kind"] == "t1":
            child["data"].update(all_awardings=[], body_html="<div>…</div>", ups=41,
                                 author="someone", gildings={}, collapsed_reason_code=None)
    return raw


def test_slimming_keeps_only_what_the_pipeline_reads():
    slim = scan.slim_blob(cluttered())
    assert set(slim[0]["data"]["children"][0]["data"]) == set(
        ("id", "subreddit", "title", "selftext", "permalink", "created_utc", "num_comments"))
    comment = slim[1]["data"]["children"][0]["data"]
    assert set(comment) == {"id", "body", "permalink", "created_utc"}
    assert "author" not in comment and "ups" not in comment


def test_a_slimmed_thread_reads_back_identically():
    raw = cluttered()
    # The point of the shape: reply.py cannot tell which kind of blob it got.
    assert thread_from_blob(scan.slim_blob(raw)) == thread_from_blob(raw)


def test_slimming_does_not_cut_a_long_comment():
    raw = thread(["x" * 9000])
    kept = scan.slim_blob(raw)[1]["data"]["children"][0]["data"]["body"]
    assert len(kept) == 9000  # the cut belongs to whoever reads it, not to the archive


def test_slim_writes_a_smaller_blob_that_still_scans(tmp_path):
    path = write(tmp_path, cluttered())
    store = Store(tmp_path / "db.sqlite")
    result = ingest(str(path), blobs=tmp_path / "blobs", store=store,
                    classifier=KeywordClassifier(), rescan=False, slim=True)
    blob = Path(result["blob"])
    assert blob.stat().st_size < path.stat().st_size
    assert thread_from_blob(json.loads(blob.read_text(encoding="utf-8")))[1][0].text == "kept"
