"""What both halves of the outreach pipeline need: one HTTP helper, one text
cleaner, one candidate, and where the files live.

The pipeline is two commands now (doc/plans/04-reddit-outreach.md, and the
plugin README): `scan.py` ingests a thread saved from Reddit and asks a local
model three questions about it, writing what it found to SQLite; `reply.py` is
run by hand, one post at a time, and is the only half that spends money. This
module is the part they share, kept free of every dependency but the standard
library so `scan.py` starts without resolving anything.

Reddit text is untrusted everywhere it appears: it is cleaned here, cut here,
and fenced as data in every prompt.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Callable

DEFAULT_SITE = "https://koalitionsberegner.moritzmarcus.com"
DEFAULT_USER_AGENT = "koalitionsberegner-outreach/0.1"

#: Everything the pipeline keeps between runs: the database and the blobs.
DEFAULT_HOME = "~/.koalitionsberegner-outreach"
DEFAULT_DB = f"{DEFAULT_HOME}/outreach.db"
DEFAULT_BLOBS = f"{DEFAULT_HOME}/posts"
#: Where saved threads are dropped: `plugins/outreach/inbox`, beside the code
#: rather than in the home directory, so the editor's file tree is where you
#: drag the saved page and `scan.py` with no argument reads exactly that.
#: Resolved from this module's own location, so it does not depend on the
#: working directory the scan was started from. Its contents are gitignored —
#: they are Reddit's text, not ours.
DEFAULT_INBOX = str(Path(__file__).resolve().parents[1] / "inbox")

#: Characters of a post handed to a model. Enough for any Reddit comment and
#: most posts; a wall of text past this is cut, not summarised.
MAX_CANDIDATE_CHARS = 4_000
#: Characters of one *comment* when the whole thread is handed to `reply.py`'s
#: model. Lower than the above because a hundred of them go in one call.
MAX_ITEM_CHARS = 1_200
#: Comments of one thread that reach the billed model. A thread longer than
#: this is a news thread, not an argument about who can govern.
MAX_THREAD_ITEMS = 120

#: A reply is short. Reddit allows far more; a helpful pointer does not need it.
MAX_REPLY_CHARS = 900
#: The footer every draft carries, appended in code so no model can drop or
#: reword it. Reddit's rules and the subreddits' own ask for exactly these two
#: disclosures: who is behind the link, and that a machine helped write it.
#: Must read exactly like ``backend/app/outreach.py``'s ``DISCLOSURE``, which
#: is what the server checks a draft against.
DISCLOSURE = (
    "\n\n---\n"
    "*I built koalitionsberegner. This reply was drafted with an LLM and "
    "read and approved by me before posting.*"
)

# --- HTTP, injectable ---------------------------------------------------------

HttpJson = Callable[..., tuple[int, object]]


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
    timeout: float = 30.0,
) -> tuple[int, object]:
    """One JSON request with the standard library. Returns (status, decoded body).

    A non-2xx answer is returned, not raised, so callers decide what a 429 or a
    404 means for them. Only a transport failure raises.
    """
    data = None
    # Name ourselves on every call. Without this urllib says "Python-urllib/3.x",
    # which Cloudflare refuses in front of our own site (error 1010).
    request_headers = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
        **(headers or {}),
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace")


# --- Text ---------------------------------------------------------------------


def clean_text(value: object) -> str:
    """Printable text only, control and direction-changing characters removed."""
    text = "" if value is None else str(value)
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith("C") or ch in "\n\t"
    )
    text = re.sub(r"[​‎‏‪-‮⁦-⁩﻿]", "", text)
    return text.strip()


def cut(text: str, limit: int = MAX_CANDIDATE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Candidates -----------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One item of a thread: the post itself, or one of its top-level comments.

    ``position`` is where it sits in the thread as the scanner walks it — 0 for
    the post, 1..n for the comments in Reddit's own order. Both halves use it
    as the item's name: the local pass records which positions it flagged, and
    the billed pass answers whichever position it picked.
    """

    kind: str  # "post" or "comment"
    id: str  # Reddit's short id, without the t3_/t1_ prefix
    thread_id: str  # the post's id, for the post its own
    subreddit: str
    title: str  # the post's title; for a comment, the parent post's title
    text: str  # selftext or comment body, already cut
    permalink: str  # absolute URL
    created_utc: float = 0.0
    position: int = 0

    @property
    def thing_id(self) -> str:
        return ("t3_" if self.kind == "post" else "t1_") + self.id

    @property
    def posted_on(self) -> str:
        return day(self.created_utc)

    def for_model(self) -> str:
        head = (f"Subreddit: r/{self.subreddit}\nPost title: {self.title}\n"
                f"Posted: {self.posted_on}\n")
        body = self.text if self.kind == "post" else f"Comment on that post:\n{self.text}"
        return head + body


def day(created_utc: float) -> str:
    """`1758326400.0` -> `2026-09-20`, or "unknown" for a missing timestamp.

    Every prompt carries this and today's date, because without them a model
    reads a year as the future: a thread titled "Alle Ergebnisse der Wahl …
    2026" was labelled an *upcoming* election the day after that election was
    held, on the strength of the year alone.
    """
    if not created_utc:
        return "unknown"
    return datetime.fromtimestamp(created_utc, UTC).date().isoformat()


def today() -> str:
    return date.today().isoformat()


def fenced(text: str) -> str:
    """Untrusted text, marked as data rather than instructions."""
    return "<document>\n" + text + "\n</document>"


# --- Configuration ----------------------------------------------------------------


def load_dotenv(path: Path, env: dict[str, str]) -> None:
    """`KEY=value` lines next to the plugin; the environment wins, as in the backend."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def environment() -> dict[str, str]:
    """The process environment with `plugins/outreach/.env` behind it."""
    env = dict(os.environ)
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", env)
    return env


def expand(path: str | Path) -> Path:
    return Path(path).expanduser()


# --- A saved thread ---------------------------------------------------------------

#: Top-level comments only, and never Reddit's "load more" placeholder: the
#: pipeline answers a thread, and a reply to a reply is a different argument.
COMMENT_KIND = "t1"


class NotAThread(ValueError):
    """The file is JSON, but not a thread listing Reddit would have served."""


def thread_from_blob(
    raw: object,
    *,
    post_limit: int = MAX_CANDIDATE_CHARS,
    comment_limit: int = MAX_CANDIDATE_CHARS,
) -> tuple[Candidate, list[Candidate]]:
    """Reddit's own `<permalink>.json` — `[{post}, {comments}]` — as candidates.

    This is the only reader of a blob: `scan.py` calls it to label the thread
    and `reply.py` calls it again, with a smaller comment limit, to hand the
    whole thread to the billed model. Both therefore agree on what position 4
    is, which is what the database stores.

    Reddit answers a scripted fetch of a thread with 403 but serves this JSON
    to a logged-in browser, so the file arrives saved by hand (the plugin
    README says how).
    """
    if not isinstance(raw, list) or len(raw) < 1:
        raise NotAThread("expected a thread listing: append .json to the post's URL")
    try:
        children = raw[0]["data"]["children"]
    except (TypeError, KeyError, IndexError):
        raise NotAThread("no post in that file — is it a thread's .json?") from None
    if not children:
        raise NotAThread("no post in that file — is it a thread's .json?")
    data = children[0].get("data") if isinstance(children[0], dict) else None
    if not isinstance(data, dict) or not clean_text(data.get("id")):
        raise NotAThread("no post in that file — is it a thread's .json?")

    post_id = clean_text(data.get("id"))
    # Reddit spells a subreddit as its owner chose ("Sverige"); everything
    # downstream — the server's allowlist and its per-subreddit cap — compares
    # lower case, so it is folded once, here.
    subreddit = clean_text(data.get("subreddit")).lower()
    permalink = clean_text(data.get("permalink"))
    post = Candidate(
        kind="post",
        id=post_id,
        thread_id=post_id,
        subreddit=subreddit,
        title=cut(clean_text(data.get("title")), 300),
        text=cut(clean_text(data.get("selftext")), post_limit),
        permalink=_absolute(permalink, ""),
        created_utc=float(data.get("created_utc") or 0),
        position=0,
    )

    comments: list[Candidate] = []
    rows = []
    if len(raw) > 1:
        try:
            rows = raw[1]["data"]["children"]
        except (TypeError, KeyError, IndexError):
            rows = []
    for child in rows:
        row = child.get("data") if isinstance(child, dict) else None
        if not isinstance(row, dict) or child.get("kind") != COMMENT_KIND:
            continue
        comment_id = clean_text(row.get("id"))
        body = cut(clean_text(row.get("body")), comment_limit)
        if not comment_id or not body or body in ("[deleted]", "[removed]"):
            continue
        comments.append(Candidate(
            kind="comment",
            id=comment_id,
            thread_id=post_id,
            subreddit=subreddit,
            title=post.title,
            text=body,
            permalink=_absolute(clean_text(row.get("permalink")), post.permalink),
            created_utc=float(row.get("created_utc") or 0),
            position=len(comments) + 1,
        ))
    return post, comments


#: The only keys of Reddit's own JSON that :func:`thread_from_blob` reads. A
#: saved thread carries about seventy per comment — awards, flair, body_html,
#: moderation fields — and one real thread was 2.7 MB of them around 3 KB of
#: argument. `scan.py --slim` keeps these and drops the rest.
POST_KEYS = ("id", "subreddit", "title", "selftext", "permalink", "created_utc", "num_comments")
COMMENT_KEYS = ("id", "body", "permalink", "created_utc")


def slim_blob(raw: object) -> list:
    """The same thread with nothing in it but what the pipeline reads.

    The shape is Reddit's own, so a slimmed blob goes back through
    :func:`thread_from_blob` and produces exactly the same post and comments —
    that equality is what the test asserts, and what lets `reply.py` read
    either kind without knowing which it got. Text is copied whole: the cut to
    a model's limit happens at read time, and an archive should not decide it.

    Rows the reader would drop anyway — Reddit's "load more", deleted and empty
    comments — are dropped here too, so positions match either way.
    """
    post, comments = thread_from_blob(raw)  # validates, and tells us what survives
    keep = {comment.id for comment in comments}
    raw_post = raw[0]["data"]["children"][0]["data"]  # type: ignore[index]
    raw_comments = raw[1]["data"]["children"] if len(raw) > 1 else []  # type: ignore[index]
    return [
        {"kind": "Listing", "data": {"children": [
            {"kind": "t3", "data": {k: raw_post.get(k) for k in POST_KEYS}},
        ]}},
        {"kind": "Listing", "data": {"children": [
            {"kind": "t1", "data": {k: child["data"].get(k) for k in COMMENT_KEYS}}
            for child in raw_comments
            if isinstance(child, dict) and child.get("kind") == COMMENT_KIND
            and isinstance(child.get("data"), dict) and child["data"].get("id") in keep
        ]}},
    ]


def _absolute(permalink: str, fallback: str) -> str:
    if permalink.startswith("/"):
        return "https://www.reddit.com" + permalink
    return permalink or fallback
