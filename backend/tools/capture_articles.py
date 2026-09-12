"""Refresh the real-article fixtures in ``test/wikipedia/``.

    cd backend && uv run python tools/capture_articles.py

Each fixture is a slice of a live English Wikipedia article: the infobox, the
first two lead paragraphs, the result tables, one other wikitable as a decoy, and
a small navbox where the article has one. Images are dropped and the rest is
copied verbatim — classes, inline styles and footnote markup included, since that
markup is exactly what :func:`app.wikipedia.condense_article` is written against.
A whole article is a megabyte and has no business in a test suite; these are tens
of kilobytes and still nobody's hand-written idea of what Wikipedia looks like.

Wikipedia is edited continuously, so a refresh *will* produce a diff. Read it:
`backend/tests/test_articles.py` states what each election actually was, and a
fixture that has quietly lost its seats column should fail those tests rather
than be committed alongside them.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.wikipedia import _SEATS_COLUMN, api_endpoint, condense_article, user_agent  # noqa: E402

#: Fixture name -> article title. Three shapes, not three of a kind: a
#: party-list result beside an overview of the outgoing parliament, a results
#: table that lives inside the infobox, and a coalition grouped above its member
#: parties.
ARTICLES = {
    "german-federal-2025": "2025 German federal election",
    "danish-general-2022": "2022 Danish general election",
    "australian-federal-2025": "2025 Australian federal election",
}

OUT = pathlib.Path(__file__).resolve().parents[2] / "test" / "wikipedia"

#: Tables bigger than this are the ones that list every constituency. One is
#: enough of a fixture without them.
MAX_TABLE_CHARS = 25_000
MAX_NAVBOX_CHARS = 8_000
#: Markup that carries no text for a model to read, and most of the bytes.
WEIGHT = ("img", "svg", "map", "audio", "video", "style", "link")


async def article(client: httpx.AsyncClient, title: str) -> dict:
    response = await client.get(
        api_endpoint("en.wikipedia.org"),
        params={
            "action": "parse", "page": title, "prop": "text", "redirects": 1,
            "format": "json", "formatversion": 2,
        },
        headers={"user-agent": user_agent(), "accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()["parse"]


def states_seats(table) -> bool:
    return any(
        _SEATS_COLUMN.search(cell.get_text(" ", strip=True))
        for cell in table.find_all("th", limit=80)
    )


def slice_article(html: str) -> str:
    """The part of an article worth keeping, in the article's own markup."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("div", class_="mw-parser-output")
    for tag in content.find_all(WEIGHT):
        tag.decompose()

    parts = []
    infobox = content.find("table", class_="infobox")
    if infobox:
        parts.append(infobox)
    parts += [p for p in content.find_all("p", recursive=False) if p.get_text(strip=True)][:2]

    # A table already inside the infobox comes with it; saving it again would put
    # the same seats in the fixture twice.
    tables = [
        table for table in content.find_all("table", class_="wikitable")
        if not table.find_parent("table", class_="infobox")
        and len(str(table)) < MAX_TABLE_CHARS
    ]
    results = [table for table in tables if states_seats(table)]
    others = sorted((t for t in tables if t not in results), key=lambda t: len(str(t)))
    parts += results[:2] + others[:1]

    navbox = content.find(class_="navbox")
    if navbox and len(str(navbox)) < MAX_NAVBOX_CHARS:
        parts.append(navbox)

    return '<div class="mw-parser-output">\n' + "\n".join(str(p) for p in parts) + "\n</div>\n"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=40.0) as client:
        for slug, title in ARTICLES.items():
            parsed = await article(client, title)
            fixture = slice_article(parsed["text"])
            (OUT / f"{slug}.html").write_text(fixture, encoding="utf-8")
            condensed = condense_article(fixture, title=parsed["title"])
            print(
                f"{slug:26} article={len(parsed['text']):>9,}  "
                f"fixture={len(fixture):>7,}  condensed={len(condensed):>6,}"
            )


if __name__ == "__main__":
    asyncio.run(main())
