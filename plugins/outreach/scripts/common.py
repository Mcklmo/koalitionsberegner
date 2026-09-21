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
from pathlib import Path
from typing import Callable

DEFAULT_SITE = "https://koalitionsberegner.moritzmarcus.com"
DEFAULT_USER_AGENT = "koalitionsberegner-outreach/0.1"

#: Everything the pipeline keeps between runs: the database and the blobs.
DEFAULT_HOME = "~/.koalitionsberegner-outreach"
DEFAULT_DB = f"{DEFAULT_HOME}/outreach.db"
DEFAULT_BLOBS = f"{DEFAULT_HOME}/posts"

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

    def for_model(self) -> str:
        head = f"Subreddit: r/{self.subreddit}\nPost title: {self.title}\n"
        body = self.text if self.kind == "post" else f"Comment on that post:\n{self.text}"
        return head + body


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


def _absolute(permalink: str, fallback: str) -> str:
    if permalink.startswith("/"):
        return "https://www.reddit.com" + permalink
    return permalink or fallback
