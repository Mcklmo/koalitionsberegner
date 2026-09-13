"""Fetching is the app's job, not the agent's — and it must not be a SSRF hole."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import fetcher
from app.fetcher import MAX_BYTES, FetchError, HttpPageFetcher, html_to_text, _read

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def response(text="<p>hi</p>", status=200, content_type="text/html"):
    return httpx.Response(
        status_code=status, headers={"content-type": content_type}, text=text,
        request=httpx.Request("GET", "https://example.org/"),
    )


def test_table_structure_survives_flattening():
    html = """
    <table>
      <tr><th>Partei</th><th>Sitze</th></tr>
      <tr><td>CDU</td><td>40</td></tr>
      <tr><td>AfD</td><td>23</td></tr>
    </table>
    """
    assert html_to_text(html).split("\n") == ["Partei\tSitze", "CDU\t40", "AfD\t23"]


def test_scripts_and_styles_are_dropped():
    html = "<style>.a{color:red}</style><script>steal()</script><p>Resultat</p>"
    assert html_to_text(html) == "Resultat"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",   # cloud instance metadata
        "http://localhost:8080/admin",
        "http://127.0.0.1/",
        "http://10.0.0.5/internal",
        "file:///etc/passwd",
        "gopher://example.org/",
    ],
)
async def test_private_and_non_http_urls_are_refused(url):
    with pytest.raises(FetchError):
        await HttpPageFetcher().fetch(url)


def test_non_html_content_is_refused():
    with pytest.raises(FetchError, match="expected an HTML page"):
        _read(response(content_type="application/pdf"))


def test_http_errors_are_reported():
    with pytest.raises(FetchError, match="HTTP 404"):
        _read(response(status=404))


def test_an_empty_page_is_refused():
    with pytest.raises(FetchError, match="no readable text"):
        _read(response(text="<html><body></body></html>"))


def test_an_oversized_page_is_refused_not_truncated():
    """A truncated results table yields a confident, wrong answer."""
    with pytest.raises(FetchError, match="too large"):
        _read(response(text="<p>" + ("word " * 200_000) + "</p>"))


async def test_a_public_page_is_fetched_and_flattened():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"},
                                       text="<table><tr><td>CDU</td><td>40</td></tr></table>")
    )
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        page = await HttpPageFetcher(client=client).fetch("https://example.org/results")
    assert page.text == "CDU\t40"


async def test_redirects_are_followed_but_re_checked():
    def handle(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>x</p>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        with pytest.raises(FetchError, match="non-public"):
            await HttpPageFetcher(client=client).fetch("https://example.org/start")


async def test_an_endless_body_is_refused_without_being_held_whole():
    """The cap is applied while reading, not after: a page cannot fill our memory."""
    chunk = b"<p>" + b"x" * 65_536
    served = 0

    async def endless():
        nonlocal served
        while True:
            served += 1
            yield chunk

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=endless())
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FetchError, match="too large"):
            await HttpPageFetcher(client=client).fetch("https://example.org/huge")
    assert served * len(chunk) <= MAX_BYTES + 2 * len(chunk)


async def test_a_page_that_never_finishes_is_given_up_on(monkeypatch):
    """A byte at a time never trips a per-read timeout; the overall one does."""
    monkeypatch.setattr(fetcher, "TOTAL_TIMEOUT_SECONDS", 0.3)

    async def drip():
        yield b"<p>"
        while True:
            await asyncio.sleep(0.05)
            yield b"x"

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=drip())
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FetchError, match="too long"):
            await HttpPageFetcher(client=client).fetch("https://example.org/slow")


async def test_a_pdf_is_refused_before_its_body_is_downloaded():
    read = False

    async def body():
        nonlocal read
        read = True
        yield b"%PDF-1.7"

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body())
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FetchError, match="expected an HTML page"):
            await HttpPageFetcher(client=client).fetch("https://example.org/results.pdf")
    assert not read
