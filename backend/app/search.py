"""Asking a search engine where an election's seats are published.

:mod:`app.resolver` usually answers this on its own — it searches as part of
working out which election was meant, and comes back with candidate URLs. This
module is the second opinion, and it is what a deployment configured with a
plain search index rather than an agent relies on entirely.

The query it is given is the election as the resolver identified it: nation,
region, date, title, and the resolver's own suggested search terms. A few dozen
characters, none of which came from a results page — no page has been read at
the point this runs — so there is nothing here for a hostile page to ride along
in.

Its only output is candidate URLs. Text that is not a URL is discarded, each URL
is fetched through the same public-address guard as any other, each page is
extracted by the same tool-less agent, and what comes back is checked against
the request before it can be staged. A search result decides what gets *read*,
never what counts as an answer.

Two providers, because they answer to different keys. ``GoogleSearch`` is the
Programmable Search JSON API — literally Google, and what to use when you want
Google's index. ``AnthropicWebSearch`` uses the search tool the Anthropic API
hosts, which needs no credential beyond the key extraction already uses.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

import httpx

from .observability import io_span

log = logging.getLogger(__name__)

#: How many search results an import may add to its candidate pages. Each one
#: it goes on to read is a fetch and a model call, and a result further down the
#: page is rarely the official one.
DEFAULT_SEARCH_LIMIT = 3

TIMEOUT_SECONDS = 15.0

GOOGLE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

SEARCH_MODEL = "claude-opus-5"
SEARCH_MAX_TOKENS = 2_000
#: The hosted search tool, capped: this agent is looking up one page, not
#: researching a topic.
SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
SEARCH_SYSTEM_PROMPT = """\
You find the web page that publishes an election's seat distribution.

Search for the election described, and answer with the URLs of pages that state
how many seats each party won — an official result, a parliament's own page on
its composition, or an encyclopedia article that gives the seat counts.

The election is identified exactly: match the date as well as the name. A
regional page of a national election is a slice of that national election, not
of the region's own — do not answer with a different election held in the same
place.

Order your answers by how official the source is: the electoral authority or
the assembly itself first. Skip pages that only report votes or percentages,
skip news commentary, and skip PDFs. Answer with one URL per line and nothing
else: no numbering, no explanation, no markdown. If you cannot find such a
page, answer with the single word NONE.\
"""

#: Bare URLs in the search agent's answer. Anything else it says is discarded.
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


class WebSearch(Protocol):
    async def find(self, query: str, *, limit: int) -> list[str]:
        """Pages that might publish the seats for ``query``, best first."""
        ...


class DisabledSearch:
    """No searching: an import reads the pages the resolver named, and no others."""

    async def find(self, query: str, *, limit: int) -> list[str]:
        return []


class GoogleSearch:
    """Google Programmable Search, via the JSON API.

    Needs an API key and the id of a search engine configured to search the
    whole web (``GOOGLE_SEARCH_API_KEY`` and ``GOOGLE_SEARCH_CX``).
    """

    def __init__(self, api_key: str, cx: str, *, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._cx = cx
        self._client = client

    async def find(self, query: str, *, limit: int) -> list[str]:
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        owns_client = self._client is None
        try:
            with io_span(log, "google", "search", chars=len(query), limit=limit) as span:
                response = await client.get(
                    GOOGLE_ENDPOINT,
                    params={
                        "key": self._api_key,
                        "cx": self._cx,
                        "q": query,
                        # A couple spare, since some results are PDFs the
                        # fetcher will turn down anyway.
                        "num": min(max(limit * 2, 1), 10),
                        "safe": "active",
                    },
                )
                span["status"] = response.status_code
                if response.status_code >= 400:
                    # A misconfigured search must not fail the import: the
                    # resolver's own candidates are still worth reading.
                    log.warning("google search returned HTTP %s", response.status_code)
                    return []
                items = response.json().get("items") or []
                found = [
                    item["link"]
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("link"), str)
                ][:limit]
                span["results"] = len(found)
            return found
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("google search failed: %s", exc)
            return []
        finally:
            if owns_client:
                await client.aclose()


class AnthropicWebSearch:
    """Search through the Anthropic API's hosted search tool.

    The one call this makes is the only place in the app where a model is given
    a tool, and the tool is a search engine whose results the model may only
    turn into URLs for us to check.
    """

    def __init__(self, client=None, *, model: str = SEARCH_MODEL):
        self._client = client
        self._model = model

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def find(self, query: str, *, limit: int) -> list[str]:
        try:
            with io_span(
                log, "anthropic", "search", model=self._model, chars=len(query)
            ) as span:
                response = await self._get_client().messages.create(
                    model=self._model,
                    max_tokens=SEARCH_MAX_TOKENS,
                    system=SEARCH_SYSTEM_PROMPT,
                    tools=[SEARCH_TOOL],
                    messages=[{"role": "user", "content": query}],
                )
                span["stop_reason"] = getattr(response, "stop_reason", None)
                found = urls_in(_answer_text(response))[:limit]
                span["results"] = len(found)
            return found
        except Exception as exc:  # noqa: BLE001 - a failed search is not a failed import
            log.warning("web search failed: %s", exc)
            return []


def _answer_text(response) -> str:
    """The model's own words, ignoring the search tool's blocks."""
    return "\n".join(
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    )


def urls_in(text: str) -> list[str]:
    """The http(s) URLs in ``text``, in order, without repeats.

    Everything that is not a URL is dropped — this is the whole of what the
    search agent is able to tell us.
    """
    found: list[str] = []
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:")
        if url not in found:
            found.append(url)
    return found


class StubSearch:
    """A search that returns what a test staged, and records what it was asked."""

    def __init__(self, results: list[str] | None = None):
        self.results = results or []
        self.queries: list[str] = []

    async def find(self, query: str, *, limit: int) -> list[str]:
        self.queries.append(query)
        return self.results[:limit]
