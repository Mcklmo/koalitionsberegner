"""Fetching is the app's job, not the agent's — and it must not be a SSRF hole."""

from __future__ import annotations

import httpx
import pytest

from app.fetcher import FetchError, HttpPageFetcher, html_to_text, _read

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
