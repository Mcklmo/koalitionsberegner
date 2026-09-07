"""Server-side fetching of the pasted results URL.

The backend fetches the page itself and hands the text to the model as data.
The model never browses: it gets exactly one document, from exactly the URL the
user pasted, and has no way to ask for another.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse, urlunparse

import httpx

from .observability import io_span

log = logging.getLogger(__name__)

MAX_BYTES = 5_000_000
MAX_TEXT_CHARS = 400_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 20.0
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

# Tags whose contents are never page text.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
# Tags that imply a line break, so table rows and list items stay separable.
_BREAK_TAGS = {
    "br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "section", "article",
}


class FetchError(RuntimeError):
    """The page could not be fetched, or is not something we will hand to a model."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    text: str


class _TextExtractor(HTMLParser):
    """Flattens HTML to text, keeping cell and row boundaries.

    Election results are almost always tables; collapsing them into one run of
    words loses the party-to-seat association the model needs.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BREAK_TAGS:
            self._parts.append("\n")
        elif tag in ("td", "th"):
            self._parts.append("\t")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        lines = []
        for raw_line in "".join(self._parts).split("\n"):
            cells = [" ".join(cell.split()) for cell in raw_line.split("\t")]
            line = "\t".join(c for c in cells if c)
            if line:
                lines.append(line)
        return "\n".join(lines)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _assert_public_url(url: str) -> str:
    """Reject anything that is not a public http(s) address.

    Without this the pasted URL is a server-side request forgery primitive:
    ``http://169.254.169.254/`` would hand the model instance metadata.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"only http and https URLs can be imported, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise FetchError(f"not a well-formed URL: {url!r}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise FetchError(f"could not resolve {parsed.hostname!r}: {exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise FetchError(f"{parsed.hostname!r} resolves to a non-public address")
    return urlunparse(parsed)


class HttpPageFetcher:
    """Fetches one page, following only redirects that are themselves public."""

    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self._client = client

    async def fetch(self, url: str) -> FetchedPage:
        client = self._client or httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"user-agent": "koalitionsberegner/1.0 (+election results import)"},
        )
        owns_client = self._client is None
        try:
            current = url
            for hop in range(MAX_REDIRECTS + 1):
                current = _assert_public_url(current)
                with io_span(log, "page", "get", url=current, hop=hop) as span:
                    response = await client.get(current)
                    span["status"] = response.status_code
                    span["bytes"] = len(response.content)
                    span["type"] = response.headers.get("content-type", "").split(";")[0]
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("the server sent a redirect with no destination")
                        span["redirect_to"] = location
                        current = str(response.url.join(location))
                        continue
                    text = _read(response)
                    span["chars"] = len(text)
                return FetchedPage(url=current, text=text)
            raise FetchError("too many redirects")
        except httpx.HTTPError as exc:
            raise FetchError(f"could not fetch the page: {exc}") from None
        finally:
            if owns_client:
                await client.aclose()


def _read(response: httpx.Response) -> str:
    if response.status_code >= 400:
        raise FetchError(f"the page returned HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type not in ALLOWED_CONTENT_TYPES:
        raise FetchError(f"expected an HTML page, got {content_type!r}")
    if len(response.content) > MAX_BYTES:
        raise FetchError("the page is too large to import")

    text = html_to_text(response.text) if content_type != "text/plain" else response.text
    if not text.strip():
        raise FetchError("the page has no readable text")
    if len(text) > MAX_TEXT_CHARS:
        # Never silently truncate: a cut-off page yields a plausible but wrong result.
        raise FetchError("the page is too large to import; link the results table directly")
    return text
