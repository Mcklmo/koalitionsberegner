"""Posting an approved reply to Reddit: the outreach gate's only outbound write.

The ``outreach`` plugin finds threads and drafts replies on the owner's machine;
the owner approves each one from an emailed link (doc/plans/04-reddit-outreach.md).
This module is the last step: one comment, under one parent, with text that has
already been sanitised and approved. It decides nothing about *whether* to post
— the token, the admin secret, the caps and the sanitiser sit in front of it —
only how to post and what to say when it did not work.

The wire protocol is Reddit's script-app OAuth flow, checked against Reddit's
OAuth2 documentation (github.com/reddit-archive/reddit/wiki/OAuth2 and its
quick-start example) and the API reference for ``POST /api/comment``:

1. ``POST https://www.reddit.com/api/v1/access_token`` with the app's id and
   secret as HTTP Basic credentials and the form ``grant_type=password``,
   ``username``, ``password``. The answer is
   ``{"access_token", "token_type": "bearer", "expires_in", "scope"}``. A wrong
   login is answered ``200 {"error": "invalid_grant"}``, not with a 4xx, so the
   body is checked, not just the status.
2. ``POST https://oauth.reddit.com/api/comment`` with ``Authorization: bearer
   <token>`` and the form ``api_type=json``, ``thing_id`` (the parent's
   fullname) and ``text``. It needs the ``submit`` scope. The answer is
   ``{"json": {"errors": [...], "data": {"things": [{"kind": "t1", "data":
   {...}}]}}}`` — and a rate limit arrives as a ``RATELIMIT`` entry in that
   ``errors`` list under a 200.

Since November 2025 a new app needs Reddit's approval under its Responsible
Builder Policy before it gets API access at all, so the credentials below may
take a while to exist. Nothing else here depends on them: without all five the
poster is :class:`DisabledRedditPoster` and the rest of the gate still works.

Three things are deliberate about failure, as in :mod:`app.wishlist`:

* **Reddit's words never leave this module.** A refusal is logged by status
  and, for the in-body errors, by Reddit's error *code* (``RATELIMIT``) — never
  its message. What the caller gets is :class:`RedditUnavailable` with one of a
  handful of fixed sentences.
* **The credentials are used here and nowhere else**, never logged, and kept
  out of every ``repr``.
* **A comment Reddit accepted is never reported as a failure.** An answer that
  is odd in shape after a clean 200 still returns a :class:`PostedComment`,
  with no URL; raising would invite the owner to retry and post twice. The
  converse ambiguity — a timeout or a gateway error after the request went out —
  is raised with ``maybe_posted=True`` so the caller can say "check the thread
  before retrying" instead of offering a blind retry.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

import anyio
import httpx

from .observability import io_span, scrub

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_HOST = "https://oauth.reddit.com"
COMMENT_URL = f"{OAUTH_HOST}/api/comment"
#: Where a comment's relative permalink is resolved to an absolute URL.
PERMALINK_HOST = "https://www.reddit.com"

TIMEOUT_SECONDS = 15.0

#: Reddit's documented bearer lifetime, used when an answer leaves it out.
DEFAULT_TOKEN_SECONDS = 3600
#: A cached token is replaced this long before it expires, so a comment is
#: never sent with a token that dies in flight.
TOKEN_MARGIN_SECONDS = 60

#: The five variables the poster needs, all from Secret Manager in production.
#: Any one missing leaves posting switched off rather than half-configured.
CREDENTIAL_VARIABLES = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME",
    "REDDIT_PASSWORD",
    "REDDIT_USER_AGENT",
)

#: A reply's parent: a post (``t3_``) or a comment (``t1_``), by Reddit's
#: base-36 id. Anything else — another kind of thing, or a value carrying form
#: syntax — is refused before a request is built.
FULLNAME = re.compile(r"t[13]_[0-9a-z]{1,16}")

#: A comment's permalink as Reddit returns it: a site-relative path under
#: ``/r/``. It is stored and later shown to the owner, so anything else — an
#: absolute URL, a protocol-relative ``//host`` — is dropped, not trusted.
PERMALINK = re.compile(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9_/%\-.]+")

#: The fixed sentences a failure carries. The approval page can show any of
#: them as they are; none contains anything Reddit said.
NOT_CONFIGURED = "Posting to Reddit is not configured."
LOGIN_FAILED = "Reddit did not accept the configured login."
REFUSED = "Reddit refused the reply."
RATE_LIMITED = "Reddit asked us to slow down; try again later."
UNREACHABLE = "Reddit could not be reached."
UNCERTAIN = "Reddit did not confirm the reply; check the thread before retrying."


class RedditUnavailable(Exception):
    """The reply was not (known to be) posted. Never carries Reddit's own words.

    ``maybe_posted`` is True when the request reached Reddit and the answer
    was lost — a timeout, a dropped connection, a gateway error. The comment may
    exist, so a retry could post it twice.
    """

    def __init__(self, message: str, *, maybe_posted: bool = False):
        super().__init__(message)
        self.maybe_posted = maybe_posted


@dataclass(frozen=True)
class PostedComment:
    """What Reddit created.

    ``fullname`` is the new comment's ``t1_`` name when Reddit said it, and
    ``url`` its absolute permalink; either is ``None`` when the answer did not
    carry a sane one. The comment exists either way.
    """

    fullname: str | None
    url: str | None


class RedditPoster(Protocol):
    enabled: bool

    async def comment(self, thing_id: str, text: str) -> PostedComment: ...


@dataclass(frozen=True)
class RedditCredentials:
    """The script app and the account it posts as. Secrets stay out of ``repr``."""

    client_id: str
    client_secret: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    user_agent: str


def missing_credentials(environ: Mapping[str, str] | None = None) -> list[str]:
    """The names of the credential variables that are unset or blank."""
    env = os.environ if environ is None else environ
    return [name for name in CREDENTIAL_VARIABLES if not (env.get(name) or "").strip()]


def poster_from_env(environ: Mapping[str, str] | None = None) -> RedditPoster:
    """The real poster when all five variables are set, else a disabled one.

    Partly configured is treated as not configured, and says which names are
    missing — names only, never a value — so a deploy that lost one secret is
    easy to diagnose from the log.
    """
    env = os.environ if environ is None else environ
    missing = missing_credentials(env)
    if missing:
        if len(missing) < len(CREDENTIAL_VARIABLES):
            log.warning("reddit posting is off; missing %s", ", ".join(missing))
        return DisabledRedditPoster(missing=tuple(missing))
    return HttpxRedditPoster(
        RedditCredentials(
            client_id=env["REDDIT_CLIENT_ID"].strip(),
            client_secret=env["REDDIT_CLIENT_SECRET"].strip(),
            username=env["REDDIT_USERNAME"].strip(),
            password=env["REDDIT_PASSWORD"].strip(),
            user_agent=env["REDDIT_USER_AGENT"].strip(),
        )
    )


def _check_reply(thing_id: str, text: str) -> None:
    """Refuse a parent or a text no poster should send, before any network."""
    if not isinstance(thing_id, str) or not FULLNAME.fullmatch(thing_id):
        raise ValueError("thing_id must be a post or comment fullname (t3_… or t1_…)")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a reply needs text")


class DisabledRedditPoster:
    """No Reddit credentials, so nothing can be posted.

    Like :class:`app.wishlist.DisabledWishlist`: a deployment without this set
    up serves everything else, and the send route answers ``503``.
    """

    enabled = False

    def __init__(self, missing: tuple[str, ...] = CREDENTIAL_VARIABLES):
        self.missing = missing

    async def comment(self, thing_id: str, text: str) -> PostedComment:
        raise RedditUnavailable(NOT_CONFIGURED)


class FakeRedditPoster:
    """Posts nothing; remembers what it was asked to post.

    For tests and for a local end-to-end run of the approval flow before real
    credentials exist. ``fail_with`` makes every call raise that error instead,
    to drive the failure paths. It checks its arguments the way the real poster
    does, so a test cannot pass with a parent Reddit would never accept.
    """

    enabled = True

    def __init__(self, *, fail_with: RedditUnavailable | None = None):
        self.fail_with = fail_with
        self.posted: list[tuple[str, str]] = []

    async def comment(self, thing_id: str, text: str) -> PostedComment:
        _check_reply(thing_id, text)
        if self.fail_with is not None:
            raise self.fail_with
        self.posted.append((thing_id, text))
        comment_id = f"fake{len(self.posted)}"
        return PostedComment(
            fullname=f"t1_{comment_id}",
            url=f"{PERMALINK_HOST}/r/test/comments/{thing_id[3:]}/_/{comment_id}/",
        )


class HttpxRedditPoster:
    """Posts comments as one account through a Reddit script app.

    The bearer token is cached on the instance until shortly before it expires
    and fetched by one caller at a time, so concurrent sends share one login.
    """

    enabled = True

    def __init__(
        self,
        credentials: RedditCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._credentials = credentials
        self._client = client
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = anyio.Lock()

    def __repr__(self) -> str:
        return f"HttpxRedditPoster(username={self._credentials.username!r})"

    def _headers(self) -> dict[str, str]:
        return {"user-agent": self._credentials.user_agent}

    async def comment(self, thing_id: str, text: str) -> PostedComment:
        _check_reply(thing_id, text)
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        owns_client = self._client is None
        try:
            token = await self._bearer(client)
            response = await self._post_comment(client, token, thing_id, text)
            if response.status_code == 401:
                # Revoked or expired early. Nothing was posted under a token
                # Reddit did not accept, so one fresh login and one retry is safe.
                self._forget_token(token)
                token = await self._bearer(client)
                response = await self._post_comment(client, token, thing_id, text)
            return self._read_comment(response, thing_id)
        finally:
            if owns_client:
                await client.aclose()

    # --- the bearer token ---------------------------------------------------

    def _forget_token(self, token: str) -> None:
        if self._token == token:
            self._token = None
            self._token_expires_at = 0.0

    async def _bearer(self, client: httpx.AsyncClient) -> str:
        async with self._token_lock:
            if self._token is not None and self._clock() < self._token_expires_at:
                return self._token
            token, lifetime = await self._fetch_token(client)
            self._token = token
            self._token_expires_at = self._clock() + max(lifetime - TOKEN_MARGIN_SECONDS, 0)
            return token

    async def _fetch_token(self, client: httpx.AsyncClient) -> tuple[str, int]:
        creds = self._credentials
        try:
            with io_span(log, "reddit", "access-token") as span:
                response = await client.post(
                    TOKEN_URL,
                    auth=(creds.client_id, creds.client_secret),
                    data={
                        "grant_type": "password",
                        "username": creds.username,
                        "password": creds.password,
                    },
                    headers=self._headers(),
                )
                span["status"] = response.status_code
        except httpx.HTTPError as exc:
            # Nothing was posted yet, so this is a plain "could not reach".
            log.warning("reddit login failed: %s", type(exc).__name__)
            raise RedditUnavailable(UNREACHABLE) from None

        if response.status_code == 429:
            log.warning("reddit login rate limited: HTTP 429")
            raise RedditUnavailable(RATE_LIMITED)
        if response.status_code >= 400:
            log.warning("reddit refused the login: HTTP %s", response.status_code)
            raise RedditUnavailable(LOGIN_FAILED)
        body = _json(response)
        if not isinstance(body, dict) or "error" in body:
            # ``invalid_grant`` and friends arrive under a 200. The code is
            # Reddit's; the log keeps it, bounded, and the caller does not.
            code = body.get("error") if isinstance(body, dict) else None
            log.warning("reddit refused the login: %s", scrub(code or "unreadable answer", 40))
            raise RedditUnavailable(LOGIN_FAILED)

        token = body.get("access_token")
        token_type = body.get("token_type")
        scope = body.get("scope")
        if not isinstance(token, str) or not token or str(token_type).lower() != "bearer":
            log.warning("reddit answered the login without a bearer token")
            raise RedditUnavailable(LOGIN_FAILED)
        if isinstance(scope, str) and not ({"*", "submit"} & set(scope.split())):
            log.warning("reddit token lacks the submit scope")
            raise RedditUnavailable(LOGIN_FAILED)
        lifetime = body.get("expires_in")
        if not isinstance(lifetime, int) or isinstance(lifetime, bool) or lifetime <= 0:
            lifetime = DEFAULT_TOKEN_SECONDS
        return token, lifetime

    # --- the comment --------------------------------------------------------

    async def _post_comment(
        self, client: httpx.AsyncClient, token: str, thing_id: str, text: str
    ) -> httpx.Response:
        try:
            with io_span(log, "reddit", "comment", parent=thing_id) as span:
                response = await client.post(
                    COMMENT_URL,
                    data={"api_type": "json", "thing_id": thing_id, "text": text},
                    headers={**self._headers(), "authorization": f"bearer {token}"},
                )
                span["status"] = response.status_code
                return response
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The request never reached Reddit: certainly not posted.
            log.warning("reddit comment not sent: %s", type(exc).__name__)
            raise RedditUnavailable(UNREACHABLE) from None
        except httpx.HTTPError as exc:
            # Sent, and the answer was lost. It may have posted.
            log.warning("reddit comment unconfirmed: %s", type(exc).__name__)
            raise RedditUnavailable(UNCERTAIN, maybe_posted=True) from None

    def _read_comment(self, response: httpx.Response, thing_id: str) -> PostedComment:
        status = response.status_code
        if status == 429:
            log.warning("reddit rate limited a comment: HTTP 429")
            raise RedditUnavailable(RATE_LIMITED)
        if status >= 500:
            # A gateway error is known to arrive after the comment was made.
            log.warning("reddit comment unconfirmed: HTTP %s", status)
            raise RedditUnavailable(UNCERTAIN, maybe_posted=True)
        if status >= 400:
            log.warning("reddit refused a comment: HTTP %s", status)
            raise RedditUnavailable(REFUSED)

        body = _json(response)
        payload = body.get("json") if isinstance(body, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            codes = _error_codes(errors)
            log.warning("reddit refused a comment: %s", scrub(", ".join(codes) or "error", 80))
            raise RedditUnavailable(RATE_LIMITED if "RATELIMIT" in codes else REFUSED)

        # From here Reddit said yes. Whatever the rest looks like, the comment
        # exists, and saying otherwise would invite a second one.
        data = _first_thing(payload)
        fullname = data.get("name")
        permalink = data.get("permalink")
        posted = PostedComment(
            fullname=fullname if isinstance(fullname, str) and FULLNAME.fullmatch(fullname) else None,
            url=PERMALINK_HOST + permalink
            if isinstance(permalink, str) and PERMALINK.fullmatch(permalink)
            else None,
        )
        if posted.url is None:
            log.warning("reddit accepted a comment under %s but gave no usable permalink", thing_id)
        log.info("reddit comment posted under %s as %s", thing_id, posted.fullname or "?")
        return posted


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _error_codes(errors: object) -> list[str]:
    """Reddit's error codes (``RATELIMIT``, ``THREAD_LOCKED``), without messages.

    Each error is ``[code, message, field]``; only the code is an identifier.
    """
    codes: list[str] = []
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, list) and error and isinstance(error[0], str):
                code = error[0]
                if re.fullmatch(r"[A-Z_]{1,40}", code):
                    codes.append(code)
    return codes


def _first_thing(payload: object) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    things = data.get("things") if isinstance(data, dict) else None
    if isinstance(things, list) and things and isinstance(things[0], dict):
        inner = things[0].get("data")
        if isinstance(inner, dict):
            return inner
    return {}
