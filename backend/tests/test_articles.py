"""Three real election articles, read the way an import reads one.

The fixtures in ``test/wikipedia/`` are verbatim slices of live English Wikipedia
articles, captured 2026-09-12: the infobox, the first two lead paragraphs, the
result tables, one other wikitable as a decoy, and the article's navbox where it
had a small one. Images are dropped and nothing else is rewritten — each is a
fraction of its article, because a fixture should not be a megabyte, but every
class, inline style and footnote in it is Wikipedia's own.

Three shapes on purpose. A German federal election is a party-list result with
two seat columns and an overview table of the *previous* Bundestag sitting right
next to the real one. A Danish one puts its results table inside the infobox, and
its assembly is filled from three places at once. An Australian one groups a
coalition above its member parties, which is the shape the schema's blocks exist
for.

What these assert is the contract the extraction agent depends on: the document
it is handed names the election, states each party's seats in a row of its own,
carries the colours the page itself publishes, and has left behind the
navigation, the footnotes and the tables that are not results. The agent is not
run here — that is a model call — so the claim is about the document, not about
the extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.wikipedia import Wikipedia, condense_article

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parents[2] / "test" / "wikipedia"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass(frozen=True)
class RealArticle:
    """One captured article, and what the election actually was."""

    slug: str
    title: str
    held: str
    """The day of the election, as the page writes it."""
    assembly: str
    """The infobox line naming the assembly and the majority it takes."""
    seats: str
    """Seats in the assembly."""
    totalled: bool
    """Whether the results table sums its own seat column."""
    parties: tuple[tuple[str, str, str], ...]
    """``(colour, party, seats)`` — each in a row of the document, together."""
    not_results: tuple[str, ...]
    """Text the fixture carries that must not reach the agent."""


ARTICLES = (
    RealArticle(
        slug="german-federal-2025",
        title="2025 German federal election",
        held="23 February 2025",
        assembly="All 630 seats in the Bundestag 316 seats needed for a majority",
        seats="630",
        totalled=True,
        parties=(
            ("#151518", "Christian Democratic Union", "164"),
            ("#00A2DE", "Alternative for Germany", "152"),
            ("#E3000F", "Social Democratic Party", "120"),
            ("#BE3075", "The Left", "64"),
        ),
        not_results=(
            "2025 elections in Germany",   # the navbox
            "15.5 pp",                     # turnout by time of day, not a result
        ),
    ),
    RealArticle(
        slug="danish-general-2022",
        title="2022 Danish general election",
        held="1 November 2022",
        assembly="All 179 seats in the Folketing",
        seats="179",
        # The Danish table states no total of its own; the line above does.
        totalled=False,
        parties=(
            ("#C82518", "Social Democrats", "50"),
            ("#01438E", "Venstre", "23"),
            ("#B48CD2", "Moderates", "16"),
            ("#FF0202", "Siumut", "1"),    # one of Greenland's two Folketing seats
        ),
        not_results=("Division",),         # results by nomination district
    ),
    RealArticle(
        slug="australian-federal-2025",
        title="2025 Australian federal election",
        held="3 May 2025",
        assembly="All 150 seats in the House of Representatives 76 seats needed for a majority",
        seats="150",
        totalled=True,
        parties=(
            ("#F00011", "Labor", "94"),
            ("#06667c", "Liberal–National Coalition", "43"),
            ("#10C25B", "Greens", "1"),
            ("#B50204", "Katter's Australian", "1"),
        ),
        not_results=(
            "Incumbent Prime Minister",    # the navbox
            "As of 24 February 2025",      # the parliament before the election
        ),
    ),
)

#: A footnote marker as Wikipedia renders one, once the surrounding markup is
#: flattened: ``[1]``, ``[ 275 ]``, ``[a]``.
FOOTNOTE = re.compile(r"\[\s*(?:\d+|[a-z])\s*\]")


def markup(article: RealArticle) -> str:
    return (FIXTURES / f"{article.slug}.html").read_text(encoding="utf-8")


def document(article: RealArticle) -> str:
    """The article as the extraction agent would be handed it."""
    return condense_article(markup(article), title=article.title)


def rows_for(text: str, color: str, party: str) -> list[str]:
    """The rows a party's own colour swatch begins."""
    return [line for line in text.split("\n") if line.startswith(f"{color}\t") and party in line]


def has_cell(row: str, value: str) -> bool:
    """Whether ``value`` is a cell of ``row`` — last cell included, since a row
    whose final column is empty ends at its seat count."""
    return f"\t{value}\t" in row or row.endswith(f"\t{value}")


def cases():
    return [pytest.param(article, id=article.slug) for article in ARTICLES]


# --- the document the agent is handed ---------------------------------------

@pytest.mark.parametrize("article", cases())
def test_the_article_comes_down_to_the_part_that_states_seats(article):
    """The whole article runs to a megabyte, most of it prose and navigation —
    more than the fetcher would even accept. What survives is a few pages."""
    condensed, whole = document(article), markup(article)
    assert len(condensed) < 8_000
    assert len(condensed) < len(whole) / 4


@pytest.mark.parametrize("article", cases())
def test_the_document_names_the_election_it_reports(article):
    """``parser.is_wanted`` compares the election a page reports against the one
    that was asked for, which only works if the page says which it is."""
    condensed = document(article)
    assert f"Wikipedia article: {article.title}" in condensed
    assert article.held in condensed


@pytest.mark.parametrize("article", cases())
def test_the_size_of_the_assembly_is_stated(article):
    condensed = document(article)
    assert article.assembly in condensed
    if not article.totalled:
        return
    totals = [line for line in condensed.split("\n") if line.startswith("Total\t")]
    assert any(has_cell(line, article.seats) for line in totals), totals


@pytest.mark.parametrize("article", cases())
def test_each_party_arrives_with_its_seats_and_its_own_colour(article):
    """One row per party: the colour the page publishes, the name, the seats.

    The colour is the part flattening to text loses, and the part the agent
    would otherwise supply from memory — for a party that has never sat in a
    parliament before, memory has nothing to supply.
    """
    condensed = document(article)
    for color, party, seats in article.parties:
        rows = rows_for(condensed, color, party)
        assert rows, f"no row for {party} ({color})"
        assert any(has_cell(row, seats) for row in rows), rows


@pytest.mark.parametrize("article", cases())
def test_what_is_not_a_result_is_left_behind(article):
    """Navigation, the tables that count something else, and the footnotes.

    Not tidiness: every line of it would be read as page content by an agent
    that is told to report only what the document states.
    """
    condensed = document(article)
    for phrase in article.not_results:
        assert phrase not in condensed
    assert not FOOTNOTE.search(condensed), FOOTNOTE.search(condensed)


# --- through the pipeline ---------------------------------------------------

@pytest.mark.parametrize("article", cases())
async def test_the_article_reaches_the_pipeline_through_the_api(article):
    """The same document, fetched the way an import fetches it: one API call, and
    a page attributed to the article it was read from."""
    def handle(request):
        assert request.url.params["action"] == "parse"
        return httpx.Response(200, json={"parse": {"title": article.title, "text": markup(article)}})

    wikipedia = Wikipedia(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    page = await wikipedia.fetch_article(
        f"https://en.wikipedia.org/wiki/{article.title.replace(' ', '_')}"
    )

    assert page.url == f"https://en.wikipedia.org/wiki/{article.title.replace(' ', '_')}"
    assert page.text == document(article)
