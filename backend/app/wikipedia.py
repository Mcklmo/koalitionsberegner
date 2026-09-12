"""Wikipedia as the first place to look for an election's seats.

An election's official source is often the worst page to read. Electoral
authorities publish votes and percentages, put the seat allocation in a PDF, or
spread it over a frameset; and most of them only ever publish it once, in the
country's own language, at an address that stops working two elections later.
Wikipedia's article on the same election is one page, has a results table with a
seats column, names the election and the day it was held in its own lead
paragraph, and is still there years afterwards. So when there is an article for
the election, this module puts it at the front of the queue.

Two things live here, because they are the two ways an import touches Wikipedia:

- :meth:`Wikipedia.find` asks the MediaWiki search API which article covers the
  election the resolver identified, and turns the answer into article URLs. It
  is a *candidate source*, exactly like :mod:`app.search` — URLs, nothing else.
- :meth:`Wikipedia.fetch_article` reads one of those articles. It goes through
  the API rather than the ordinary fetcher (:class:`WikipediaFetcher` routes it
  there), for the plain reason that Wikimedia's servers refuse an unannounced
  client: the API is the supported way in, and it wants a ``User-Agent`` saying
  who is calling and where to complain. A request without that much is answered
  ``403 Please respect our robot policy``, so the identification is built in
  (:data:`DEFAULT_CONTACT`) rather than left to configuration — a deployment with
  its own address puts it in ``WIKIPEDIA_CONTACT``.

What comes back is put through BeautifulSoup rather than the generic flattener
in :mod:`app.fetcher`, which buys two things worth having:

- **Only the part that states seats.** The infobox, the lead, and the result
  tables; not the navboxes, footnotes, and "see also" trail that make a full
  article big enough to be refused for its size. A short document is also a
  cheaper and more accurate one to extract from.
- **The party colours.** A results table carries each party's colour as an
  inline ``background`` on its row, which flattening to text throws away —
  leaving the extraction agent to colour the chart from memory. Those swatches
  are written into the text as hex codes, so the colours come from the page.

Nothing about the safety story changes. The article is untrusted page content
like any other: it is fenced as data, read by the same tool-less extractor, its
identity checked against what was asked for in :mod:`app.parser`, and staged for
the user to confirm. The only address this module will ever request is a
``wikipedia.org`` one, and it is checked by
:func:`app.fetcher.assert_public_url` like every other address.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .fetcher import MAX_BYTES, MAX_TEXT_CHARS, FetchedPage, FetchError, assert_public_url
from .observability import io_span

log = logging.getLogger(__name__)

#: Which Wikipedia to search. English by default, and not because it is the best
#: article — often it is not — but because it is the one language that has an
#: article for an election held anywhere, and the extractor is asked for English
#: place names anyway.
DEFAULT_LANGUAGE = "en"

#: How many articles one import may queue up. An election has one article; a
#: second candidate is there for the times the first is a disambiguation page or
#: a list, and a third would just be a worse guess than the resolver's own.
DEFAULT_ARTICLE_LIMIT = 2

API_PATH = "/w/api.php"
TIMEOUT_SECONDS = 15.0

#: Wikimedia's user-agent policy: say what the client is and give an address a
#: human can follow up at. The contact half is not decoration — the API answers
#: 403 without it — so it has a default that identifies this software, and a
#: deployment that would rather be reached directly sets ``WIKIPEDIA_CONTACT``.
DEFAULT_CONTACT = "https://github.com/Mcklmo/koalitionsberegner"
USER_AGENT = "koalitionsberegner/1.0 (election results import; {contact})"

#: Hosts this module is willing to talk to: ``en.wikipedia.org``, ``de.m.…``,
#: and the bare domain. Everything else goes to the ordinary fetcher.
_WIKIPEDIA_HOST = re.compile(r"^(?:[a-z0-9-]{1,32}\.)?(?:m\.)?wikipedia\.org$", re.IGNORECASE)
#: The only path shape with an article title in it. A ``/w/index.php?title=…``
#: URL is left to the ordinary fetcher rather than unpicked here.
_ARTICLE_PATH = re.compile(r"^/wiki/([^/].*)$")

#: A column of seat counts, in the languages an election's article is likely to
#: be written in. Used to tell a results table from the other tables an article
#: carries — turnout by region, a timeline of opinion polls, a list of leaders.
_SEATS_COLUMN = re.compile(
    r"\b(?:seats?|mandat\w*|mandát\w*|мандат\w*|sitze|sièges|escaños|zetels|"
    r"seggi|miejsc|paikat|assentos|képviselő\w*)\b",
    re.IGNORECASE,
)

#: Article furniture that is never a result: navigation, citations, images,
#: maintenance notices, the edit links. Dropped before anything is read, so it
#: neither reaches the model nor counts towards the length.
_NOISE = (
    "script", "style", "noscript", "sup.reference", ".reference", ".mw-editsection",
    ".navbox", ".vertical-navbox", ".navbox-styles", ".sidebar", ".metadata", ".ambox",
    ".reflist", ".mw-references-wrap", ".refbegin", ".thumb", "figure", ".gallery",
    ".hatnote", ".shortdescription", ".noprint", ".mw-empty-elt", ".mw-jump-link",
    ".toc", "#toc", ".mbox-text", ".plainlinks",
)

#: Enough of the lead to name the election and the day it was held, which is
#: what the identity check downstream compares against. Beyond that the lead is
#: prose about the campaign.
MAX_LEAD_CHARS = 1_500
#: Infoboxes, then results tables. Both capped: an article that wants to show us
#: thirty tables is not an article about one election's results.
MAX_INFOBOXES = 2
MAX_TABLES = 8

#: A year in an article's title, which is how Wikipedia names an election.
_TITLE_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")

#: Hex colours in an inline ``style``. Only hex, because only hex survives
#: ``schema.Party.color`` — a ``background: red`` is dropped rather than guessed
#: at, and the agent falls back to the party's conventional colour.
_BACKGROUND_HEX = re.compile(
    r"background(?:-color)?\s*:\s*[^;]*?(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"
)


@dataclass(frozen=True)
class _Article:
    """One article, as the API returned it."""

    host: str
    title: str
    html: str

    @property
    def url(self) -> str:
        return article_url(self.host, self.title)


def user_agent(contact: str = "") -> str:
    """Who is calling, and where to take it up with them.

    Never contact-less: an agent that does not identify itself is what the API
    answers 403 to, so an unconfigured deployment still names this software.
    """
    return USER_AGENT.format(contact=" ".join(contact.split()) or DEFAULT_CONTACT)


def api_endpoint(host: str) -> str:
    return f"https://{host}{API_PATH}"


def article_of(url: str) -> tuple[str, str] | None:
    """The host and article title ``url`` names, or ``None`` if it names neither.

    This is the whole of the routing decision in :class:`WikipediaFetcher`, and
    the whole of the reason this module cannot be pointed at another host: a URL
    that is not an article on a ``wikipedia.org`` domain is simply not ours.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if not _WIKIPEDIA_HOST.match(parsed.hostname):
        return None
    match = _ARTICLE_PATH.match(parsed.path)
    if not match:
        return None
    title = unquote(match.group(1)).replace("_", " ").strip()
    return (parsed.hostname.lower(), title) if title else None


def article_url(host: str, title: str) -> str:
    """The canonical address of an article, which is what an import is attributed to."""
    return f"https://{host}/wiki/{quote(title.replace(' ', '_'), safe='()_,:-')}"


def article_queries(resolved, *, language: str = DEFAULT_LANGUAGE) -> list[str]:
    """What to search for, most likely first.

    Two of them, because MediaWiki's search requires every word to match and
    either query can come up empty. One is the shape an article title has — a
    year, a place, and the word "election" — and the other is the resolver's own
    name for the election. Which goes first depends on the language: the title
    convention is English Wikipedia's, and the resolver's name is often the local
    one ("Folketingsvalget 2022"), which on English Wikipedia finds the polling
    districts that mention it rather than the election itself.
    """
    year = str(resolved.election_date)[:4]
    place = _clip(resolved.state or resolved.nation, 60)
    conventional = f"{year} {place} election"
    named = _clip(resolved.title, 120)
    ordered = (conventional, named) if language == DEFAULT_LANGUAGE else (named, conventional)

    queries: list[str] = []
    for query in ordered:
        query = " ".join(query.split())
        if query and query.casefold() not in [existing.casefold() for existing in queries]:
            queries.append(query)
    return queries


def relevant_titles(titles: list[str], year: str) -> list[str]:
    """The articles that could be this election, the ones named after it first.

    An article whose title carries a *different* year is a different election —
    "2026 Saxony-Anhalt state election" is what a search for the 2021 one turns
    up second — and reading it would cost a fetch and a model call to be told so.
    What is left is ordered by whether the title names the year at all, because
    "Elections in Saxony-Anhalt" is the overview article and has no one table.
    """
    kept = [
        title for title in titles
        if not ((years := set(_TITLE_YEAR.findall(title))) and year not in years)
    ]
    return sorted(kept, key=lambda title: year not in title)


class Wikipedia:
    """The MediaWiki action API: find the article, then read it.

    One object, because it is one credential-free API and one HTTP client, and
    because a deployment configures it once — which language to search, and who
    to say we are.
    """

    def __init__(
        self,
        *,
        language: str = DEFAULT_LANGUAGE,
        contact: str = "",
        client: httpx.AsyncClient | None = None,
    ):
        self._language = language.strip().lower() or DEFAULT_LANGUAGE
        self._user_agent = user_agent(contact)
        self._client = client

    @property
    def host(self) -> str:
        return f"{self._language}.wikipedia.org"

    def handles(self, url: str) -> bool:
        """Whether this module, rather than the ordinary fetcher, serves ``url``."""
        return article_of(url) is not None

    async def find(self, resolved, *, limit: int = DEFAULT_ARTICLE_LIMIT) -> list[str]:
        """Article URLs for the election ``resolved`` describes, best first.

        A search that finds nothing, or fails, is not an import that fails: the
        resolver's own candidates are read next, exactly as they were before
        this module existed.
        """
        if limit <= 0:
            return []
        year = str(resolved.election_date)[:4]
        for query in article_queries(resolved, language=self._language):
            titles = relevant_titles(
                await self._search(query, limit=max(limit * 2, 2)), year
            )
            if titles:
                return [article_url(self.host, title) for title in titles[:limit]]
        return []

    async def fetch_article(self, url: str) -> FetchedPage:
        """One article, reduced to the part of it that states seats.

        Raises :class:`FetchError` for anything that is not a readable article,
        which is the signal :mod:`app.parser` already treats as "pass over this
        candidate" — a disambiguation page or a deleted title costs the import
        one API call and nothing else.
        """
        target = article_of(url)
        if target is None:  # unreachable through WikipediaFetcher, which checks first
            raise FetchError(f"not a Wikipedia article: {url!r}")
        host, title = target
        article = await self._article(host, title)
        text = condense_article(article.html, title=article.title)
        if not text.strip():
            raise FetchError("the article states no seat counts")
        if len(text) > MAX_TEXT_CHARS:
            # Same rule as the ordinary fetcher: never silently truncate, because
            # a cut-off results table extracts into a plausible wrong total.
            raise FetchError("the article has more text than can be read")
        return FetchedPage(url=article.url, text=text)

    async def _search(self, query: str, *, limit: int) -> list[str]:
        try:
            with io_span(
                log, "wikipedia", "search", host=self.host, chars=len(query)
            ) as span:
                payload = await self._call(
                    self.host,
                    {
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "srlimit": min(max(limit, 1), 10),
                        # Articles only: no categories, templates or talk pages.
                        "srnamespace": 0,
                    },
                )
                results = (payload.get("query") or {}).get("search") or []
                titles = [
                    item["title"]
                    for item in results
                    if isinstance(item, dict) and isinstance(item.get("title"), str)
                ]
                span["results"] = len(titles)
            return titles
        except (FetchError, httpx.HTTPError, ValueError) as exc:
            log.warning("wikipedia search failed: %s", exc)
            return []

    async def _article(self, host: str, title: str) -> _Article:
        with io_span(log, "wikipedia", "article", host=host, chars=len(title)) as span:
            payload = await self._call(
                host,
                {
                    "action": "parse",
                    "page": title,
                    "prop": "text",
                    # A renamed election follows its redirect rather than 404ing.
                    "redirects": 1,
                },
            )
            parsed = payload.get("parse") or {}
            html = parsed.get("text")
            if not isinstance(html, str) or not html:
                raise FetchError(f"the article {title!r} has no readable content")
            canonical = parsed.get("title") if isinstance(parsed.get("title"), str) else title
            span["chars"] = len(html)
        return _Article(host=host, title=canonical, html=html)

    async def _call(self, host: str, params: dict) -> dict:
        """One API call. The host is ours, the guard is the same one as always."""
        endpoint = assert_public_url(api_endpoint(host))
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        owns_client = self._client is None
        try:
            response = await client.get(
                endpoint,
                params={**params, "format": "json", "formatversion": 2},
                headers={"user-agent": self._user_agent, "accept": "application/json"},
                follow_redirects=False,
            )
            if response.status_code >= 400:
                raise FetchError(f"the Wikipedia API returned HTTP {response.status_code}")
            if len(response.content) > MAX_BYTES:
                raise FetchError("the Wikipedia API returned more than can be read")
            try:
                payload = response.json()
            except ValueError:
                raise FetchError("the Wikipedia API returned something that is not JSON") from None
            if not isinstance(payload, dict):
                raise FetchError("the Wikipedia API returned something unexpected")
            error = payload.get("error")
            if isinstance(error, dict):
                # The API's own wording would reach a user through the import's
                # error, so only its code does — a closed-ish vocabulary from a
                # source we trust, and short.
                raise FetchError(f"the Wikipedia API refused: {_clip(str(error.get('code')), 60)}")
            return payload
        except httpx.HTTPError as exc:
            raise FetchError(f"could not reach the Wikipedia API: {exc}") from None
        finally:
            if owns_client:
                await client.aclose()


class WikipediaFetcher:
    """The fetcher seam, with Wikipedia served through the API.

    A wrapper rather than a branch inside :class:`app.fetcher.HttpPageFetcher`,
    so that the ordinary path keeps its single job — fetch a public page, flatten
    it, cap it — and a deployment with ``WIKIPEDIA=off`` runs exactly that.
    """

    def __init__(self, wikipedia: Wikipedia, inner):
        self._wikipedia = wikipedia
        self._inner = inner

    async def fetch(self, url: str) -> FetchedPage:
        if self._wikipedia.handles(url):
            return await self._wikipedia.fetch_article(url)
        return await self._inner.fetch(url)


# --- Reading an article ----------------------------------------------------


def condense_article(html: str, *, title: str = "") -> str:
    """An article's HTML, reduced to the part of it that states seats.

    The title, the lead, the infobox, then the result tables: which election
    this is, and then its numbers. Returns ``""`` when the article has neither
    an infobox nor a table — which is how a disambiguation page, a redirect to a
    politician, or an article about the campaign rather than the result gets
    passed over without a model call.
    """
    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.find("div", class_="mw-parser-output")
        or soup.find(id="mw-content-text")
        or soup
    )
    for element in content.select(",".join(_NOISE)):
        element.decompose()

    infoboxes = content.find_all("table", class_="infobox")[:MAX_INFOBOXES]
    # A results table that lives *inside* the infobox — which is how Danish and
    # several other articles are built — has already been rendered with it, by
    # the row recursion in :func:`_render_table`. Rendering it again would hand
    # the agent the same seats twice.
    tables = [
        table for table in _results_tables(content)
        if not any(box in table.parents for box in infoboxes)
    ]
    if not infoboxes and not tables:
        return ""

    sections = [f"Wikipedia article: {title}" if title else "", _lead(content)]
    sections += [_render_table(table) for table in infoboxes + tables]
    return "\n\n".join(section for section in sections if section.strip())


def _results_tables(content) -> list:
    """The tables that look like a seat allocation, in the order they appear.

    Matched on having a seats column rather than on position: an article leads
    with the infobox and the result table, but also carries turnout, polling and
    previous-election tables that flatten into convincing-looking numbers.
    """
    tables = content.find_all("table", class_="wikitable")
    with_seats = [table for table in tables if _mentions_seats(table)]
    # Nothing matched: either the seats column is named in a language this
    # module does not know, or it is a nested table. Hand over what there is and
    # let the agent decide — it reports "votes_only" when there are no seats.
    return (with_seats or tables)[:MAX_TABLES]


def _mentions_seats(table) -> bool:
    """Whether any header cell of ``table`` names a column of seats."""
    return any(
        _SEATS_COLUMN.search(cell.get_text(" ", strip=True))
        for cell in table.find_all("th", limit=80)
    )


def _lead(content) -> str:
    """The opening paragraphs: what election this is, and when it was held."""
    parts: list[str] = []
    length = 0
    for paragraph in content.find_all("p", recursive=False):
        text = " ".join(paragraph.get_text(" ", strip=True).split())
        if not text:
            continue
        parts.append(text)
        length += len(text)
        if length >= MAX_LEAD_CHARS:
            break
    lead = "\n".join(parts)
    return lead if len(lead) <= MAX_LEAD_CHARS else lead[: MAX_LEAD_CHARS - 1] + "…"


def _render_table(table) -> str:
    """One table as tab-separated rows, with the row colours written in.

    Nested tables are flattened into the same sequence of rows rather than into a
    cell, because an election infobox is a table of tables and a row is where a
    party's name ends up next to its seats.

    The same shape :func:`app.fetcher.html_to_text` produces — one line per row,
    cells separated by tabs — so a Wikipedia article reads to the agent like any
    other results page, only shorter and with the colours it would otherwise
    have to remember.
    """
    lines: list[str] = []
    for row in table.find_all("tr"):
        cells = []
        for cell in row.find_all(["th", "td"], recursive=False):
            if cell.find("table"):
                # A cell wrapping a table of its own — which an election infobox
                # is made of. Its rows are reached by the loop above anyway, and
                # its text here would be one undelimited run ("Seats won 141 52
                # 6"), the one shape a seat count must not arrive in.
                continue
            text = " ".join(cell.get_text(" ", strip=True).split())
            color = _background(cell)
            if color and (cell.name == "td" or not text):
                # An empty swatch cell *is* the colour; a cell with a name keeps
                # its name and gains one. A header's own background is not a
                # party colour, so only an empty header contributes one.
                text = f"{text} {color}".strip()
            if text:
                cells.append(text)
        line = "\t".join(cells)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _background(cell) -> str | None:
    """The hex colour a results row carries for its party, if it carries one.

    Wikipedia writes it as an inline background, either on the cell itself or on
    a swatch inside it; both shapes are common and both are read here.
    """
    match = _BACKGROUND_HEX.search(cell.get("style") or "")
    if match:
        return match.group(1)
    for inner in cell.find_all(style=True, limit=8):
        match = _BACKGROUND_HEX.search(inner.get("style") or "")
        if match:
            return match.group(1)
    return None


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cap the length, as every outward string here is."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


class StubWikipedia:
    """Returns the articles a test staged, and records what it was asked."""

    def __init__(self, urls: list[str] | None = None, *, pages: dict[str, str] | None = None):
        self.urls = urls or []
        self.pages = pages or {}
        self.queries: list[str] = []

    def handles(self, url: str) -> bool:
        return url in self.pages

    async def find(self, resolved, *, limit: int = DEFAULT_ARTICLE_LIMIT) -> list[str]:
        self.queries.append(str(resolved.describe()))
        return self.urls[:limit]

    async def fetch_article(self, url: str) -> FetchedPage:
        if url not in self.pages:
            raise FetchError("the article states no seat counts")
        return FetchedPage(url=url, text=self.pages[url])
