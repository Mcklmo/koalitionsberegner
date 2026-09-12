"""The Wikipedia seam: one API, one host, and only the part of an article that
states seats."""

from __future__ import annotations

import httpx
import pytest

from app.fetcher import FetchError
from app.resolver import ResolvedElection
from app.wikipedia import (
    DEFAULT_CONTACT,
    MAX_LEAD_CHARS,
    Wikipedia,
    WikipediaFetcher,
    article_of,
    article_queries,
    article_url,
    condense_article,
    relevant_titles,
    user_agent,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def resolution(**overrides) -> ResolvedElection:
    data = {
        "nation": "Germany",
        "state": "Saxony-Anhalt",
        "election_date": "2021-06-06",
        "title": "2021 Saxony-Anhalt state election",
        "sources": [],
    }
    data.update(overrides)
    return ResolvedElection.model_validate(data)


# --- which addresses are ours ----------------------------------------------

@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://en.wikipedia.org/wiki/Bundestag", ("en.wikipedia.org", "Bundestag")),
        ("https://de.wikipedia.org/wiki/Landtagswahl_in_Sachsen-Anhalt_2021",
         ("de.wikipedia.org", "Landtagswahl in Sachsen-Anhalt 2021")),
        ("https://de.m.wikipedia.org/wiki/Bundestag", ("de.m.wikipedia.org", "Bundestag")),
        ("https://wikipedia.org/wiki/Bundestag", ("wikipedia.org", "Bundestag")),
        # Percent-encoding and a section anchor are both part of an address, not
        # of a title.
        ("https://en.wikipedia.org/wiki/Sejm%20of%20Poland#Results",
         ("en.wikipedia.org", "Sejm of Poland")),
    ],
)
def test_an_article_address_is_recognised_and_reduced_to_its_title(url, expected):
    assert article_of(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.example/wiki/Bundestag",       # a look-alike domain
        "https://notwikipedia.org/wiki/Bundestag",
        "https://en.wikipedia.org.evil.test/wiki/Bundestag",
        "https://en.wikipedia.org/w/index.php?title=Bundestag",   # not an article path
        "https://en.wikipedia.org/wiki/",
        "http://169.254.169.254/wiki/Bundestag",
        "file:///wiki/Bundestag",
        "not a url at all",
    ],
)
def test_everything_else_is_left_to_the_ordinary_fetcher(url):
    assert article_of(url) is None


def test_an_article_url_is_built_back_from_its_title():
    assert article_url("en.wikipedia.org", "2021 Saxony-Anhalt state election") == (
        "https://en.wikipedia.org/wiki/2021_Saxony-Anhalt_state_election"
    )


def test_the_user_agent_always_says_who_is_calling_and_where_to_complain():
    """Wikimedia answers 403 to a client that identifies nobody, so there is
    always a contact in it — a deployment's own, or this project's address."""
    assert user_agent("ops@example.org").endswith("; ops@example.org)")
    assert DEFAULT_CONTACT in user_agent()
    assert "koalitionsberegner" in user_agent()
    assert "\n" not in user_agent("ops@example.org\nX-Injected: 1")


# --- what to search for -----------------------------------------------------

def test_the_shape_an_article_title_has_is_searched_for_first():
    """A year, a place, and the word "election" — which is how English Wikipedia
    names an election, whatever the election calls itself at home."""
    assert article_queries(resolution())[0] == "2021 Saxony-Anhalt election"


def test_the_elections_own_name_is_the_second_try():
    """Searched for on its own, "Folketingsvalget 2022" finds the polling
    districts that mention it rather than the election — but it is the right
    query when the conventional one finds nothing at all."""
    assert article_queries(resolution(title="Landtagswahl in Sachsen-Anhalt 2021")) == [
        "2021 Saxony-Anhalt election",
        "Landtagswahl in Sachsen-Anhalt 2021",
    ]


def test_another_language_is_asked_by_the_elections_own_name_first():
    """The "2021 Saxony-Anhalt election" convention is English Wikipedia's; a
    German article is called what the resolver called it."""
    assert article_queries(
        resolution(title="Landtagswahl in Sachsen-Anhalt 2021"), language="de"
    )[0] == "Landtagswahl in Sachsen-Anhalt 2021"


def test_a_national_election_is_searched_for_by_its_nation():
    queries = article_queries(resolution(state=None, title="Bundestagswahl 2025",
                                         election_date="2025-02-23"))
    assert queries[0] == "2025 Germany election"


def test_one_query_is_not_run_twice():
    assert article_queries(resolution(title="2021 Saxony-Anhalt election")) == [
        "2021 Saxony-Anhalt election"
    ]


def test_the_article_named_after_the_year_wins():
    """"Elections in Saxony-Anhalt" is the overview article, with no one table."""
    assert relevant_titles(
        ["Elections in Saxony-Anhalt", "2021 Saxony-Anhalt state election"], "2021"
    ) == ["2021 Saxony-Anhalt state election", "Elections in Saxony-Anhalt"]


def test_an_article_named_after_another_election_is_not_a_candidate():
    """A search for one election turns up its neighbours; reading one costs a
    fetch and a model call to be told it is the wrong election."""
    assert relevant_titles(
        ["2021 Saxony-Anhalt state election", "2026 Saxony-Anhalt state election",
         "2016 Saxony-Anhalt state election"], "2021"
    ) == ["2021 Saxony-Anhalt state election"]


# --- the search API ---------------------------------------------------------

def wiki(handler, **kwargs) -> Wikipedia:
    return Wikipedia(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def search_response(*titles):
    return httpx.Response(200, json={"query": {"search": [{"title": t} for t in titles]}})


async def test_the_article_for_an_election_becomes_a_candidate_url():
    found = await wiki(
        lambda request: search_response("2021 Saxony-Anhalt state election")
    ).find(resolution(), limit=2)

    assert found == ["https://en.wikipedia.org/wiki/2021_Saxony-Anhalt_state_election"]


async def test_the_search_asks_the_action_api_for_articles_only():
    seen = {}

    def handle(request):
        seen.update(dict(request.url.params))
        seen["host"] = request.url.host
        seen["user-agent"] = request.headers["user-agent"]
        return search_response("2021 Saxony-Anhalt state election")

    await wiki(handle, contact="ops@example.org").find(resolution(), limit=1)

    assert seen["host"] == "en.wikipedia.org"
    assert (seen["action"], seen["list"]) == ("query", "search")
    assert seen["srsearch"] == "2021 Saxony-Anhalt election"
    assert seen["srnamespace"] == "0", "articles, not categories or talk pages"
    assert (seen["format"], seen["formatversion"]) == ("json", "2")
    assert "ops@example.org" in seen["user-agent"]


async def test_another_language_is_another_host():
    seen = {}

    def handle(request):
        seen["host"] = request.url.host
        return search_response("Landtagswahl in Sachsen-Anhalt 2021")

    found = await wiki(handle, language="de").find(
        resolution(title="Landtagswahl in Sachsen-Anhalt 2021"), limit=1
    )

    assert seen["host"] == "de.wikipedia.org"
    assert found == ["https://de.wikipedia.org/wiki/Landtagswahl_in_Sachsen-Anhalt_2021"]


async def test_the_blunter_query_is_only_run_when_the_first_finds_nothing():
    asked = []

    def handle(request):
        asked.append(request.url.params["srsearch"])
        return search_response() if len(asked) == 1 else search_response("2021 election")

    found = await wiki(handle).find(
        resolution(title="Landtagswahl in Sachsen-Anhalt 2021"), limit=1
    )

    assert asked == ["2021 Saxony-Anhalt election", "Landtagswahl in Sachsen-Anhalt 2021"]
    assert found == ["https://en.wikipedia.org/wiki/2021_election"]


async def test_no_more_articles_are_returned_than_asked_for():
    found = await wiki(lambda request: search_response("2021 a", "2021 b", "2021 c")).find(
        resolution(), limit=2
    )
    assert len(found) == 2


async def test_asking_for_no_articles_asks_nothing():
    def handle(request):
        raise AssertionError("the API was called")

    assert await wiki(handle).find(resolution(), limit=0) == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500, text="upstream error"),
        lambda request: httpx.Response(200, json={"error": {"code": "srsearch-missing"}}),
        lambda request: httpx.Response(200, text="<html>not json</html>"),
        lambda request: httpx.Response(200, json={"query": {}}),
    ],
)
async def test_a_wikipedia_that_will_not_answer_is_not_a_failed_import(handler):
    """The resolver's own candidates are read next, exactly as before."""
    assert await wiki(handler).find(resolution(), limit=2) == []


# --- reading one article ----------------------------------------------------

ARTICLE = """
<div class="mw-parser-output">
  <div class="shortdescription">German state election</div>
  <table class="infobox vevent"><tr><th>Date</th><td>6 June 2021</td></tr></table>
  <p>The <b>2021 Saxony-Anhalt state election</b> was held on 6 June 2021 to elect
     the 97 members of the <a href="/wiki/Landtag">Landtag</a>.<sup class="reference">[1]</sup></p>
  <h2>Results</h2>
  <table class="wikitable">
    <tr><th colspan="2">Party</th><th>Votes</th><th>Seats</th></tr>
    <tr><td style="background-color:#000000"></td><td>CDU</td><td>534,000</td><td>40</td></tr>
    <tr><td style="background:#009EE0;"></td><td>AfD</td><td>270,000</td><td>23</td></tr>
  </table>
  <h2>Opinion polls</h2>
  <table class="wikitable">
    <tr><th>Pollster</th><th>CDU</th><th>AfD</th></tr>
    <tr><td>Infratest</td><td>27%</td><td>23%</td></tr>
  </table>
  <div class="reflist">1. Landeswahlleiter</div>
  <table class="navbox"><tr><td>Elections in Germany</td></tr></table>
</div>
"""


def test_the_results_table_survives_as_rows_of_cells():
    text = condense_article(ARTICLE, title="2021 Saxony-Anhalt state election")
    assert "Party\tVotes\tSeats" in text
    assert "CDU\t534,000\t40" in text


def test_a_party_keeps_the_colour_the_table_gives_it():
    """Flattening to text drops the swatch, and the agent then colours the chart
    from memory. The page's own hex codes are better than that."""
    text = condense_article(ARTICLE)
    assert "#000000" in text and "#009EE0" in text


def test_the_tables_that_are_not_results_are_left_behind():
    text = condense_article(ARTICLE)
    assert "Infratest" not in text, "an opinion poll has no seats column"
    assert "Elections in Germany" not in text, "navigation is not a result"
    assert "[1]" not in text and "Landeswahlleiter" not in text, "nor are footnotes"


def test_the_lead_comes_with_it_because_it_names_the_election():
    """The identity check downstream compares the page's own election against
    what was asked for, so the page has to say which one it is."""
    text = condense_article(ARTICLE, title="2021 Saxony-Anhalt state election")
    assert "Wikipedia article: 2021 Saxony-Anhalt state election" in text
    assert "was held on 6 June 2021" in text
    assert "Date\t6 June 2021" in text, "and the infobox says so too"


def test_a_long_lead_is_cut_back_to_the_part_that_identifies_the_election():
    article = (
        '<div class="mw-parser-output">'
        + "".join(f"<p>Campaign paragraph {n}. </p>" for n in range(200))
        + '<table class="wikitable"><tr><th>Seats</th></tr><tr><td>97</td></tr></table>'
        + "</div>"
    )
    text = condense_article(article)
    assert "Seats" in text
    assert len(text) < MAX_LEAD_CHARS + 500


@pytest.mark.parametrize(
    "html",
    [
        '<div class="mw-parser-output"><p>Sachsen-Anhalt may refer to:</p>'
        '<ul><li><a href="/wiki/X">X</a></li></ul></div>',
        '<div class="mw-parser-output"><p>A politician.</p>'
        '<table class="navbox"><tr><td>nav</td></tr></table></div>',
        "<div></div>",
    ],
)
def test_an_article_with_no_table_at_all_is_nothing_to_read(html):
    """Saying so here saves the model call that would answer "not_results"."""
    assert condense_article(html) == ""


def test_an_infobox_of_nested_tables_still_comes_out_as_rows():
    """An election infobox is a table of tables, and the inner one is where the
    seats are. Flattened as a cell it would read "Seats won 141 52" — numbers
    with nothing to say which party each belongs to."""
    nested = """
    <div class="mw-parser-output">
      <table class="infobox">
        <tr><td><table>
          <tr><th>Party</th><td>TISZA</td><td>Fidesz</td></tr>
          <tr><th>Seats won</th><td>141</td><td>52</td></tr>
        </table></td></tr>
        <tr><th>Date</th><td>12 April 2026</td></tr>
      </table>
    </div>
    """
    text = condense_article(nested)
    assert "Seats won\t141\t52" in text
    assert "Date\t12 April 2026" in text, "the outer rows too"
    assert "Seats won 141 52" not in text, "never one undelimited run of numbers"


@pytest.mark.parametrize(
    "header, party, seats",
    [
        ("Mandater", "Socialdemokratiet", "50"),      # Danish
        ("Mandátum", "TISZA", "141"),                 # Hungarian
        ("Sitze", "CDU", "40"),                       # German
        ("Escaños", "PSOE", "121"),                   # Spanish
    ],
)
def test_a_table_whose_seats_column_is_in_another_language_is_still_read(
    header, party, seats
):
    """Which table is the results table is decided by its seats column, and an
    election's article is written in the language of the place that held it."""
    html = f"""
    <div class="mw-parser-output">
      <table class="wikitable">
        <tr><th>Parti</th><th>{header}</th></tr>
        <tr><td>{party}</td><td>{seats}</td></tr>
      </table>
    </div>
    """
    assert f"{party}\t{seats}" in condense_article(html)


def test_an_unfamiliar_seats_column_falls_back_to_offering_every_table():
    """Better to hand the agent a table it can read than to drop the article:
    it answers "votes_only" when there are genuinely no seats."""
    unknown = """
    <div class="mw-parser-output">
      <table class="wikitable">
        <tr><th>党</th><th>議席</th></tr>
        <tr><td>自由民主党</td><td>259</td></tr>
      </table>
    </div>
    """
    assert "259" in condense_article(unknown)


# --- fetching one article ---------------------------------------------------

def article_response(title="2021 Saxony-Anhalt state election", html=ARTICLE):
    return httpx.Response(200, json={"parse": {"title": title, "text": html}})


async def test_an_article_is_fetched_through_the_api_and_handed_over_as_text():
    seen = {}

    def handle(request):
        seen.update(dict(request.url.params))
        return article_response()

    page = await wiki(handle).fetch_article(
        "https://en.wikipedia.org/wiki/2021_Saxony-Anhalt_state_election"
    )

    assert (seen["action"], seen["prop"]) == ("parse", "text")
    assert seen["page"] == "2021 Saxony-Anhalt state election"
    assert seen["redirects"] == "1", "a renamed election follows its redirect"
    assert "Seats" in page.text
    assert page.url == "https://en.wikipedia.org/wiki/2021_Saxony-Anhalt_state_election"


async def test_an_import_is_attributed_to_the_article_it_was_redirected_to():
    """The stored election names the page the numbers came from, and a redirect
    means that is not the title we asked for."""
    page = await wiki(lambda request: article_response(title="2021 Saxony-Anhalt election")).fetch_article(
        "https://en.wikipedia.org/wiki/Saxony-Anhalt_state_election,_2021"
    )
    assert page.url == "https://en.wikipedia.org/wiki/2021_Saxony-Anhalt_election"


@pytest.mark.parametrize(
    "handler, message",
    [
        (lambda request: httpx.Response(404, text="nope"), "HTTP 404"),
        (lambda request: httpx.Response(200, json={"error": {"code": "missingtitle"}}),
         "missingtitle"),
        (lambda request: httpx.Response(200, json={"parse": {"title": "X"}}),
         "no readable content"),
        (lambda request: article_response(html="<div><p>A politician.</p></div>"),
         "states no seat counts"),
    ],
)
async def test_an_article_that_is_not_results_is_passed_over_not_fatal(handler, message):
    """``FetchError`` is the signal the pipeline already treats as "try the next
    candidate", and a disambiguation page costs one API call to find out."""
    with pytest.raises(FetchError, match=message):
        await wiki(handler).fetch_article("https://en.wikipedia.org/wiki/X")


async def test_the_api_never_repeats_its_own_wording_back_to_a_user():
    """Only the error's code reaches the import's message — short, and from a
    vocabulary rather than free prose."""
    handler = lambda request: httpx.Response(  # noqa: E731
        200, json={"error": {"code": "ratelimited", "info": "x" * 5_000}}
    )
    with pytest.raises(FetchError) as raised:
        await wiki(handler).fetch_article("https://en.wikipedia.org/wiki/X")
    assert "x" * 100 not in str(raised.value)


# --- the fetcher seam -------------------------------------------------------

class RecordingFetcher:
    def __init__(self):
        self.urls: list[str] = []

    async def fetch(self, url):
        self.urls.append(url)
        raise FetchError("not fetched in this test")


async def test_only_wikipedia_goes_through_the_api():
    inner = RecordingFetcher()
    fetcher = WikipediaFetcher(wiki(lambda request: article_response()), inner)

    page = await fetcher.fetch("https://en.wikipedia.org/wiki/2021_Saxony-Anhalt_state_election")
    assert "Seats" in page.text
    assert inner.urls == []

    with pytest.raises(FetchError):
        await fetcher.fetch("https://wahlergebnisse.sachsen-anhalt.de/sitze.html")
    assert inner.urls == ["https://wahlergebnisse.sachsen-anhalt.de/sitze.html"]
