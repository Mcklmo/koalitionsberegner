"""The outreach approval queue: what the ``outreach`` plugin queues, and how a
draft moves from "queued" to "posted" (or rejected, or failed).

``plugins/outreach/scripts/scan.py`` runs on the owner's machine, reads Reddit,
drafts a reply and posts the JSON below to ``POST /api/admin/outreach/drafts``
(see ``doc/plans/04-reddit-outreach.md``). Everything after that is this
module and the routes in :mod:`app.main`: store the draft, email the owner a
one-time link, let them view it with the link alone and send it only with the
link *and* ``ADMIN_SECRET``, and never post the same thread twice or more than
the etiquette limits allow.

Two things are deliberately re-checked here even though the plugin already did
them, because nothing the client sends is trusted twice:

* **The reply text.** :func:`validate_reply_text` re-applies the plugin's own
  rules — bounded length, no URL but the site's own, the disclosure footer —
  because an owner may edit the text before sending, and the plugin's
  sanitising never reaches the server.
* **The thread.** :func:`thread_id` reads the post id out of the permalink
  Reddit gave, not out of anything the plugin computed, so "one reply per
  thread" cannot be defeated by two drafts that disagree about which thread
  they are in.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from threading import Lock
from typing import Protocol
from urllib.parse import urlsplit

#: The footer every posted reply carries. Must read exactly like
#: ``plugins/outreach/scripts/scan.py``'s ``DISCLOSURE`` — this is what a
#: dropped-by-editing footer is compared against and re-appended to.
DISCLOSURE = (
    "\n\n---\n"
    "*I built koalitionsberegner. This reply was drafted with an LLM and "
    "read and approved by me before posting.*"
)

#: A reply is short even where Reddit allows much more (doc/plans/04, section 2).
MAX_REPLY_CHARS = 1200

#: Reused for the same reason the plugin has it: no URL may reach Reddit except
#: the site's own link.
URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

#: The post id inside a Reddit permalink — ``/r/<sub>/comments/<id>/...`` —
#: whether the permalink names the post itself or a comment under it, and
#: whether or not a trailing slash follows the id (a permalink can end right
#: there, with nothing after it). This is what "one reply per thread" is keyed
#: on, not the field the plugin never sends.
_THREAD_ID = re.compile(r"/comments/([A-Za-z0-9]+)(?=/|\?|#|$)")

#: How long an emailed approval link works, in seconds.
TOKEN_LIFETIME_SECONDS = 72 * 3600

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
#: Same, but excluding ``\n`` and ``\t`` -- the two control characters real
#: Reddit text carries (``plugins/outreach/scripts/scan.py``'s ``clean_text``
#: keeps exactly these). Used for the one field that is Reddit's own prose
#: rather than something typed into a form.
_CONTROL_CHARS_ALLOW_NEWLINES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CONFUSABLE_CHARS = re.compile(r"[­​‎‏‪-‮  ⁦-⁩﻿]")


def clean(value: str, *, field: str, max_len: int, allow_newlines: bool = False) -> str:
    """Like :func:`app.schema.clean_text`, but with a caller-chosen length.

    Reddit text is longer than anything :mod:`app.schema` bounds to 200
    characters, so this repeats its rules rather than reusing it: normalised,
    no control characters, no invisible or direction-changing ones, and never
    empty. ``allow_newlines`` permits newlines and tabs, for the one field
    that carries real Reddit prose (an excerpt) rather than a single line
    typed into a form.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    control_chars = _CONTROL_CHARS_ALLOW_NEWLINES if allow_newlines else _CONTROL_CHARS
    if control_chars.search(value):
        raise ValueError(f"{field} must not contain control characters")
    if _CONFUSABLE_CHARS.search(value):
        raise ValueError(f"{field} must not contain invisible or direction-changing characters")
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return text


def thread_id(permalink: str) -> str:
    """The thread a permalink belongs to, for the one-reply-per-thread rule.

    Falls back to the whole permalink when it does not look like Reddit's own
    shape — still a stable, distinct key, just not a shared one.
    """
    match = _THREAD_ID.search(permalink)
    return match.group(1) if match else permalink


def _host(url: str) -> str | None:
    """The parsed, lowercased host of a URL -- whether or not it carries a
    scheme, since :data:`URL_PATTERN` also matches bare ``www.`` mentions.

    Parsing the host, rather than checking whether one URL's text starts with
    another's, is what keeps a lookalike host (``https://<our host>.evil.example/...``)
    or one that smuggles the real host into userinfo
    (``https://<our host>@evil.example/...``) from being accepted: both parse
    to a *different* host than the real one, where a plain ``str.startswith``
    would have matched them.
    """
    parsed = urlsplit(url if "//" in url else "//" + url)
    return parsed.hostname


def validate_reply_text(text: str, *, own_link: str) -> None:
    """Refuse a reply no poster should send. Raises :class:`ValueError`.

    Re-checked here even though the plugin already sanitised the draft,
    because an owner may have edited it and nothing the client sends is
    trusted twice (doc/plans/04-reddit-outreach.md, section 2, guard 5).
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a reply needs text")
    if len(text) > MAX_REPLY_CHARS:
        raise ValueError(f"a reply must be at most {MAX_REPLY_CHARS} characters")
    if not text.endswith(DISCLOSURE):
        raise ValueError("a reply must end with the disclosure")
    allowed_host = _host(own_link)
    for match in URL_PATTERN.finditer(text):
        if _host(match.group(0)) != allowed_host:
            raise ValueError("a reply may carry no URL but the site's own")


def finalize_reply_text(text: str, *, own_link: str) -> str:
    """The text as it will be posted: the disclosure restored if editing dropped it.

    Validated either way, so an edit that also broke the length or added a
    foreign URL is still refused.
    """
    candidate = text if text.endswith(DISCLOSURE) else text.rstrip() + DISCLOSURE
    validate_reply_text(candidate, own_link=own_link)
    return candidate


def new_token() -> tuple[str, str]:
    """A fresh approval token and the hash that is stored in its place."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


#: Statuses from which a send may still be attempted: a fresh draft, or one
#: whose last attempt did not confirm a post (never one Reddit accepted).
RETRYABLE_STATUSES = (DraftStatus.PENDING, DraftStatus.FAILED)

#: Statuses from which a reject is still accepted. Everything retryable, plus
#: ``APPROVED`` -- a draft a `claim` moved there but whose send then hit
#: something other than a clean :class:`RedditUnavailable` (a client
#: disconnect, a cancelled request, a pod restart, an unhandled bug) never
#: gets a further status update, so without this an owner would find both
#: ``/send`` and ``/reject`` refusing forever with no way to close it out. This
#: does not reopen it to a blind retry: ``/send`` still only accepts
#: ``RETRYABLE_STATUSES``, so the one thing an owner can do with a stranded
#: ``APPROVED`` draft is reject it, never resend it.
REJECTABLE_STATUSES = RETRYABLE_STATUSES + (DraftStatus.APPROVED,)


@dataclass(frozen=True)
class NewDraft:
    """What the plugin posts, already shaped: see the field table in
    doc/plans/04-reddit-outreach.md, "What already exists"."""

    source: str
    subreddit: str
    thing_id: str
    kind: str
    permalink: str
    title: str
    excerpt: str
    election_hash: str
    election_title: str
    link: str
    reply_text: str
    verification: dict
    classifier_reason: str
    created_at: str


@dataclass(frozen=True)
class OutreachDraft:
    id: str
    source: str
    subreddit: str
    thing_id: str
    thread_id: str
    kind: str
    permalink: str
    title: str
    excerpt: str
    election_hash: str
    election_title: str
    link: str
    reply_text: str
    verification: dict
    classifier_reason: str
    created_at: str
    status: DraftStatus = DraftStatus.PENDING
    token_hash: str | None = None
    token_expires_at: float | None = None
    emailed_at: float | None = None
    decided_at: float | None = None
    posted_at: float | None = None
    posted_url: str | None = None
    last_error: str | None = None
    edited: bool = False


class OutreachStore(Protocol):
    """Storage seam for the approval queue."""

    def create(
        self, draft_id: str, draft: NewDraft, *, token_hash: str, token_expires_at: float,
        emailed_at: float,
    ) -> OutreachDraft | None:
        """Store a new draft with a fresh token. ``None`` when ``thing_id`` is
        already queued — the caller's ``409``."""
        ...

    def get(self, draft_id: str) -> OutreachDraft | None: ...

    def get_by_token_hash(self, token_hash: str) -> OutreachDraft | None:
        """The draft this token names, or ``None`` for an unknown or already
        consumed one. Callers still check ``token_expires_at`` themselves."""
        ...

    def list_drafts(self) -> list[OutreachDraft]:
        """Every draft, oldest first."""
        ...

    def set_status(
        self, draft_id: str, status: DraftStatus, *, consume_token: bool = False, **fields
    ) -> None:
        """Move a draft to ``status``, and set any of the timestamp/result
        fields the caller passes. ``consume_token`` clears the stored hash so
        the link cannot be replayed."""
        ...

    def claim(
        self, draft_id: str, *, decided_at: float, edited: bool = False
    ) -> OutreachDraft | None:
        """Atomically move a draft from a retryable status to ``APPROVED``, or
        do nothing. ``None`` when the draft was not found or was not in
        ``RETRYABLE_STATUSES`` at the moment this ran -- the caller's ``409``.

        This is the compare-and-swap that makes two overlapping ``/send``
        requests for the same token safe: whichever wins the race is the only
        one that ever reaches ``poster.comment``. Implementations must make
        the read-and-write one atomic operation (sqlite's ``BEGIN IMMEDIATE``,
        a Firestore transaction), not a read followed by a separate write.
        """
        ...

    def delete(self, draft_id: str) -> None:
        """Remove a draft that could not be made reachable (the email failed).
        Never called once a draft could possibly have been emailed."""
        ...

    def thread_posted(self, thread_id: str) -> bool:
        """Whether a reply has already gone out in this thread."""
        ...

    def count_posted(self, subreddit: str, since: float) -> int: ...

    def count_posted_total(self, since: float) -> int: ...


class InMemoryOutreachStore:
    """Process-local queue, for tests and for a local run without a database."""

    def __init__(self, *, clock=time.time):
        self._drafts: dict[str, OutreachDraft] = {}
        self._by_thing_id: dict[str, str] = {}
        self._lock = Lock()
        self._clock = clock

    def create(
        self, draft_id: str, draft: NewDraft, *, token_hash: str, token_expires_at: float,
        emailed_at: float,
    ) -> OutreachDraft | None:
        with self._lock:
            if draft.thing_id in self._by_thing_id:
                return None
            stored = OutreachDraft(
                id=draft_id,
                source=draft.source,
                subreddit=draft.subreddit,
                thing_id=draft.thing_id,
                thread_id=thread_id(draft.permalink),
                kind=draft.kind,
                permalink=draft.permalink,
                title=draft.title,
                excerpt=draft.excerpt,
                election_hash=draft.election_hash,
                election_title=draft.election_title,
                link=draft.link,
                reply_text=draft.reply_text,
                verification=draft.verification,
                classifier_reason=draft.classifier_reason,
                created_at=draft.created_at,
                token_hash=token_hash,
                token_expires_at=token_expires_at,
                emailed_at=emailed_at,
            )
            self._drafts[draft_id] = stored
            self._by_thing_id[draft.thing_id] = draft_id
            return stored

    def get(self, draft_id: str) -> OutreachDraft | None:
        with self._lock:
            return self._drafts.get(draft_id)

    def get_by_token_hash(self, token_hash: str) -> OutreachDraft | None:
        with self._lock:
            for draft in self._drafts.values():
                if draft.token_hash is not None and draft.token_hash == token_hash:
                    return draft
            return None

    def list_drafts(self) -> list[OutreachDraft]:
        with self._lock:
            return sorted(self._drafts.values(), key=lambda d: d.created_at)

    def set_status(
        self, draft_id: str, status: DraftStatus, *, consume_token: bool = False, **fields
    ) -> None:
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None:
                return
            updates = dict(fields)
            updates["status"] = status
            if consume_token:
                updates["token_hash"] = None
                updates["token_expires_at"] = None
            self._drafts[draft_id] = replace(draft, **updates)

    def claim(
        self, draft_id: str, *, decided_at: float, edited: bool = False
    ) -> OutreachDraft | None:
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None or draft.status not in RETRYABLE_STATUSES:
                return None
            updated = replace(
                draft, status=DraftStatus.APPROVED, decided_at=decided_at, edited=edited
            )
            self._drafts[draft_id] = updated
            return updated

    def delete(self, draft_id: str) -> None:
        with self._lock:
            draft = self._drafts.pop(draft_id, None)
            if draft is not None:
                self._by_thing_id.pop(draft.thing_id, None)

    def thread_posted(self, thread_id: str) -> bool:
        with self._lock:
            return any(
                d.thread_id == thread_id and d.status is DraftStatus.POSTED
                for d in self._drafts.values()
            )

    def count_posted(self, subreddit: str, since: float) -> int:
        with self._lock:
            return sum(
                1 for d in self._drafts.values()
                if d.subreddit == subreddit and d.status is DraftStatus.POSTED
                and d.posted_at is not None and d.posted_at >= since
            )

    def count_posted_total(self, since: float) -> int:
        with self._lock:
            return sum(
                1 for d in self._drafts.values()
                if d.status is DraftStatus.POSTED
                and d.posted_at is not None and d.posted_at >= since
            )
