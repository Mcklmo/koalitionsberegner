"""Elections people asked for and we do not hold: filed as GitHub issues.

Importing is what is sold (see :mod:`app.main`), so an account without a
subscription reaches the end of the import form and can go no further. The
election it wanted is still worth knowing about — it is the one signal that says
which elections are missing — so instead of a refusal it is written down, and
the place it is written down is this repository's issue tracker. They are
imported by hand from there, a batch at a time.

The tracker *is* the queue: there is no second copy of it here. That is what
decides how a duplicate is caught — by asking GitHub for the open requests and
matching on the request key, rather than by keeping a table that could disagree
with the issues somebody has meanwhile closed. It costs one extra GET per
filing and cannot drift.

Two things are deliberate about the failure modes:

* Nothing here lets GitHub's own error body reach the caller. The status is
  kept for the log; the caller is told only that the request could not be
  filed. A mis-scoped or expired token cannot describe itself into a browser.
* The token is used in this module and nowhere else.

The wire format is a pure function (:func:`build_issue`), so what an issue
actually says can be asserted without a network in the picture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from .identity import request_key
from .observability import io_span, scrub
from .store import ImportRequest

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

TIMEOUT_SECONDS = 15.0

#: The label every filed request carries. GitHub creates it on first use, so
#: nothing has to exist in the repository up front. ASCII and URL-safe because
#: it is what the import queue is read back by: ``label:election-request``.
LABEL = "election-request"

#: How many pages of open requests are searched before a duplicate is given up
#: on. 100 per page, so this covers a backlog far larger than one person
#: imports by hand; past it a second issue for the same election is the
#: acceptable outcome.
MAX_SEARCH_PAGES = 3

#: Hidden in an HTML comment so the issue reads as prose, and matched exactly
#: when looking for one already filed. The request key — not the words typed —
#: is what makes two requests the same, so "Danmark 2022" and " danmark  2022 "
#: are one issue.
MARKER_PREFIX = "<!-- request-key:"


class WishlistUnavailable(Exception):
    """The request could not be filed. Never carries GitHub's own words."""


@dataclass(frozen=True)
class FiledRequest:
    """Where the request ended up.

    ``duplicate`` is True when somebody had already asked for this election and
    the issue was left as it was — the caller is pointed at the existing one
    rather than told nothing happened.
    """

    url: str
    number: int
    duplicate: bool = False


class Wishlist(Protocol):
    enabled: bool

    async def file(self, request: ImportRequest, *, requester: str | None) -> FiledRequest: ...


class DisabledWishlist:
    """No GitHub token configured, so there is nowhere to file a request.

    Billing is optional the same way (:class:`app.billing.DisabledBilling`): a
    deployment that has not set this up should serve the rest of the app rather
    than fail to start, and the page asks ``/api/config`` whether to offer it.
    """

    enabled = False

    async def file(self, request: ImportRequest, *, requester: str | None) -> FiledRequest:
        raise WishlistUnavailable("election requests are not configured")


def marker_for(request: ImportRequest) -> str:
    """The line that identifies which election an issue is about."""
    key = request_key(request.year, request.nation, request.subnation)
    return f"{MARKER_PREFIX} {key} -->"


#: Characters removed from anything interpolated into the issue body. A
#: backtick would close the code span and let the rest render as markdown; a
#: pipe is a cell separator, so one inside a value adds columns to the table it
#: sits in. Whitespace (newlines included) is collapsed by ``split``. None of
#: the three belongs in a place name or an email address, so dropping them
#: loses nothing real.
_MARKDOWN_BREAKERS = str.maketrans({"`": None, "|": None})


def _as_code(value: object) -> str:
    """A value rendered inside a table cell's code span, safe to put in one."""
    return "`" + " ".join(str(value).translate(_MARKDOWN_BREAKERS).split()) + "`"


def build_issue(
    request: ImportRequest, *, requester: str | None, marker: str
) -> dict[str, object]:
    """The exact JSON body posted to GitHub, as a pure function of the request.

    Everything interpolated here was typed by a user. It reaches the issue as
    markdown, so it is confined to code spans and to a single-line title —
    which is the same reason the extraction path treats a fetched page as data:
    text from outside decides nothing about the shape of what surrounds it.
    """
    where = f"{request.nation} — {request.subnation}" if request.subnation else request.nation
    lines = [
        f"Someone asked for **{request.year} {where}**, which is not in the store.",
        "",
        "| | |",
        "| --- | --- |",
        f"| Year | {_as_code(request.year)} |",
        f"| Nation | {_as_code(request.nation)} |",
    ]
    if request.subnation:
        lines.append(f"| Region | {_as_code(request.subnation)} |")
    if requester:
        # Worth having: it is who to tell once the election is in, and it is
        # the account that asked, not a name typed into a field.
        lines.append(f"| Asked by | {_as_code(requester)} |")
    lines += [
        "",
        "Import it with the year and the place above, then close this issue.",
        "",
        marker,
    ]
    return {
        # Single-line by construction: a title is one line, and a pasted
        # multi-line place name would otherwise arrive with newlines in it.
        "title": f"Election request: {request.year} {' '.join(where.split())}",
        "body": "\n".join(lines),
        "labels": [LABEL],
    }


class GithubWishlist:
    """Files requests as issues on one repository with a fine-grained PAT.

    The token needs "Issues: write" on that repository and nothing else.
    """

    enabled = True

    def __init__(
        self, token: str, *, owner: str, repo: str, client: httpx.AsyncClient | None = None
    ):
        self._token = token
        self._owner = owner
        self._repo = repo
        self._client = client

    @property
    def _issues_url(self) -> str:
        return f"{GITHUB_API}/repos/{self._owner}/{self._repo}/issues"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            # GitHub refuses a request that does not say who is calling.
            "user-agent": "koalitionsberegner-wishlist",
        }

    async def file(self, request: ImportRequest, *, requester: str | None) -> FiledRequest:
        marker = marker_for(request)
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        owns_client = self._client is None
        try:
            existing = await self._find_open(client, marker)
            if existing is not None:
                return existing
            return await self._create(client, request, requester=requester, marker=marker)
        except httpx.HTTPError as exc:
            # The address and the token are in this module; what reaches the
            # caller is that it did not work.
            log.warning("filing an election request failed: %s", scrub(exc))
            raise WishlistUnavailable("could not file the request") from None
        finally:
            if owns_client:
                await client.aclose()

    async def _find_open(
        self, client: httpx.AsyncClient, marker: str
    ) -> FiledRequest | None:
        """The open request for this election, if somebody already asked.

        A failure to *search* is not a failure to file: the search is there to
        avoid a duplicate, and a duplicate issue is a far better outcome than
        refusing a user who asked for something reasonable. So this answers
        None on a bad status and lets the filing go ahead.
        """
        for page in range(1, MAX_SEARCH_PAGES + 1):
            with io_span(log, "github", "list-issues", page=page) as span:
                response = await client.get(
                    self._issues_url,
                    headers=self._headers(),
                    params={
                        "labels": LABEL,
                        "state": "open",
                        "per_page": 100,
                        "page": page,
                    },
                )
                span["status"] = response.status_code
            if response.status_code >= 400:
                log.warning("could not read existing requests: HTTP %s", response.status_code)
                return None
            issues = response.json()
            if not isinstance(issues, list) or not issues:
                return None
            for issue in issues:
                if not isinstance(issue, dict) or marker not in (issue.get("body") or ""):
                    continue
                url, number = issue.get("html_url"), issue.get("number")
                if isinstance(url, str) and isinstance(number, int):
                    return FiledRequest(url=url, number=number, duplicate=True)
            if len(issues) < 100:
                return None
        return None

    async def _create(
        self,
        client: httpx.AsyncClient,
        request: ImportRequest,
        *,
        requester: str | None,
        marker: str,
    ) -> FiledRequest:
        payload = build_issue(request, requester=requester, marker=marker)
        with io_span(log, "github", "create-issue", year=request.year) as span:
            response = await client.post(
                self._issues_url, headers=self._headers(), json=payload
            )
            span["status"] = response.status_code
        if response.status_code >= 400:
            log.warning("github refused an election request: HTTP %s", response.status_code)
            raise WishlistUnavailable("could not file the request")
        created = response.json()
        url = created.get("html_url") if isinstance(created, dict) else None
        number = created.get("number") if isinstance(created, dict) else None
        if not isinstance(url, str) or not isinstance(number, int):
            log.warning("github accepted an issue but described it oddly")
            raise WishlistUnavailable("could not file the request")
        log.info("filed election request #%s", number)
        return FiledRequest(url=url, number=number, duplicate=False)
