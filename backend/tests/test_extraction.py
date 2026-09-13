"""The import pipeline: resolve -> candidate pages -> agent -> our validator.

The user types a year and a place. Everything after that is this file: what the
resolver is asked, which pages are read, and — the part that makes a search
engine safe to build on — what happens when a page turns out to be about some
other election.
"""

from __future__ import annotations

from typing import get_args

import pytest

from app.extractor import (
    SACHSEN_ANHALT_2021,
    ExtractedBlock,
    ExtractedElection,
    ExtractedParty,
    MockExtractor,
    NoResultsReason,
    build_user_message,
)
from app.fetcher import FetchError, FetchedPage
from app.parser import NO_RESULTS_MESSAGES, LlmElectionParser, ParseError
from app.resolver import (
    UNRESOLVED_MESSAGES,
    MockResolver,
    ResolvedElection,
    StubResolver,
    UnresolvedReason,
)
from app.search import StubSearch
from app.wikipedia import StubWikipedia
from tests.factories import FixedExtractor, make_request

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


SEATS = "https://wahlergebnisse.sachsen-anhalt.de/lt26/sitze.html"
VOTES = "https://wahlergebnisse.sachsen-anhalt.de/lt26/stimmen.html"
WIKI = "https://encyclopedia.example/wiki/Landtagswahl_Sachsen-Anhalt_2026"
ARTICLE = "https://en.wikipedia.org/wiki/2026_Saxony-Anhalt_state_election"


def sachsen_anhalt_request():
    """What a user types: the year, the country, the region — all approximately."""
    return make_request(year=2026, nation="Germny", subnation="Sachen-Anhalt")


def resolution(**overrides) -> ResolvedElection:
    """What the resolver made of that: one election, and where to read it."""
    data = {
        "nation": "Germany",
        "state": "Saxony-Anhalt",
        "election_date": "2026-09-06",
        "title": "Landtagswahl in Sachsen-Anhalt 2026",
        "search_terms": "Landtagswahl Sachsen-Anhalt 2026 Sitzverteilung",
        "sources": [SEATS],
    }
    data.update(overrides)
    return ResolvedElection.model_validate(data)


def seats(**overrides) -> ExtractedElection:
    """What an extractor read off a page. Matches :func:`resolution` by default."""
    data = {
        "nation": "Germany",
        "state": "Saxony-Anhalt",
        "election_date": "2026-09-06",
        "title": "Landtagswahl in Sachsen-Anhalt 2026",
        "total_seats": 97,
        "majority_seats": 49,
        "blocks": [
            {
                "name": "Landtag",
                "parties": [
                    {"name": "Christlich Demokratische Union", "abbr": "CDU",
                     "seats": 50, "color": "#000000"},
                    {"name": "Alternative für Deutschland", "abbr": "AfD",
                     "seats": 47, "color": "#009EE0"},
                ],
            }
        ],
    }
    data.update(overrides)
    return ExtractedElection.model_validate(data)


def empty(reason=None, **overrides) -> ExtractedElection:
    """A page that was read and yielded no parties."""
    return seats(blocks=[{"name": "None", "parties": []}], no_results_reason=reason, **overrides)


class StubFetcher:
    def __init__(self, text="Partei\tSitze\nCDU\t50", error=None):
        self.text = text
        self.error = error
        self.urls: list[str] = []

    async def fetch(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return FetchedPage(url=url, text=self.text)


class SiteFetcher:
    """A little web: each URL has its own text, and anything else is a 404."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.urls: list[str] = []

    async def fetch(self, url):
        self.urls.append(url)
        if url not in self.pages:
            raise FetchError("the page returned HTTP 404")
        return FetchedPage(url=url, text=self.pages[url])


class PerPageExtractor:
    """Answers according to the page it was given, keyed by a marker in the text."""

    def __init__(self, answers: dict[str, ExtractedElection]):
        self.answers = answers
        self.pages: list[str] = []

    async def extract(self, page, wanted):
        self.pages.append(page.text)
        for marker, answer in self.answers.items():
            if marker in page.text:
                return answer
        raise AssertionError(f"no answer staged for {page.text!r}")


def parser(fetcher, extractor, resolved=None, **kwargs):
    return LlmElectionParser(
        fetcher, extractor, StubResolver(resolved or resolution()), **kwargs
    )


# --- end to end -------------------------------------------------------------

async def test_a_year_and_a_place_import_into_the_canonical_schema():
    fetcher = SiteFetcher({SEATS: "Sitzverteilung CDU 50"})
    election = await parser(fetcher, FixedExtractor(seats())).parse(sachsen_anhalt_request())

    assert election.nation == "Germany", "as resolved, not as typed"
    assert election.state == "Saxony-Anhalt"
    assert election.election_date.isoformat() == "2026-09-06"
    assert election.total_seats == 97
    assert election.majority_seats == 49
    assert sum(p.seats for b in election.blocks for p in b.parties) == 97
    assert election.source_url == SEATS, "attributed to the page the numbers came from"
    assert fetcher.urls == [SEATS], "exactly one page, the one the resolver named"


async def test_what_the_user_typed_reaches_the_resolver_untouched():
    """Misspellings are the resolver's business; nothing upstream tidies them."""
    resolver = StubResolver(resolution())
    request = sachsen_anhalt_request()
    await LlmElectionParser(
        SiteFetcher({SEATS: "Sitzverteilung"}), FixedExtractor(seats()), resolver
    ).parse(request)

    assert resolver.calls == [request]
    assert resolver.calls[0].nation == "Germny"
    assert resolver.calls[0].subnation == "Sachen-Anhalt"


async def test_a_different_shape_of_election_also_imports():
    """Two parties, two blocks, an odd seat total — nothing Folketing-specific."""
    toy = seats(
        nation="Toyland", state=None, election_date="2026-01-15", title="Toy assembly",
        total_seats=7, majority_seats=4,
        blocks=[
            {"name": "Left", "parties": [
                {"name": "Left", "abbr": "L", "seats": 4, "color": "#c0392b"}]},
            {"name": "Right", "parties": [
                {"name": "Right", "abbr": "R", "seats": 3, "color": "#2980b9"}]},
        ],
    )
    resolved = resolution(nation="Toyland", state=None, election_date="2026-01-15")
    election = await parser(
        SiteFetcher({SEATS: "toy"}), FixedExtractor(toy), resolved
    ).parse(make_request(year=2026, nation="Toyland"))

    assert election.total_seats == 7
    assert len(election.blocks) == 2


# --- a request that is not one election ------------------------------------

@pytest.mark.parametrize("reason", get_args(UnresolvedReason))
async def test_an_unresolved_request_is_explained_in_our_own_words(reason):
    fetcher = SiteFetcher({})
    with pytest.raises(ParseError) as caught:
        await parser(
            fetcher, FixedExtractor(seats()), resolution(unresolved_reason=reason)
        ).parse(sachsen_anhalt_request())

    assert str(caught.value) == UNRESOLVED_MESSAGES[reason]
    assert fetcher.urls == [], "nothing was fetched for a request that is not an election"


def test_every_unresolved_reason_has_its_own_message():
    assert set(UNRESOLVED_MESSAGES) == set(get_args(UnresolvedReason))


async def test_a_resolution_in_another_year_is_not_a_correction_to_accept():
    """Asked for 2026, answered with 2021: that means "no election in 2026",
    not "here is the one you must have meant"."""
    fetcher = SiteFetcher({SEATS: "Sitzverteilung 2021"})
    with pytest.raises(ParseError) as caught:
        await parser(
            fetcher, FixedExtractor(seats()), resolution(election_date="2021-06-06")
        ).parse(sachsen_anhalt_request())

    assert str(caught.value) == UNRESOLVED_MESSAGES["no_election"]
    assert fetcher.urls == []


async def test_a_resolution_with_no_pages_says_so_without_fetching():
    with pytest.raises(ParseError, match="no page publishing the seats"):
        await parser(
            SiteFetcher({}), FixedExtractor(seats()), resolution(sources=[])
        ).parse(sachsen_anhalt_request())


# --- the page has to be the election that was asked for --------------------

async def test_a_page_about_another_year_is_discarded_and_the_next_one_read():
    """The failure that would otherwise look like a success: a real result,
    correctly extracted, from the election before the one that was wanted."""
    fetcher = SiteFetcher({VOTES: "2021", SEATS: "2026"})
    extractor = PerPageExtractor({
        "2021": seats(election_date="2021-06-06", title="Landtagswahl 2021"),
        "2026": seats(),
    })
    election = await parser(
        fetcher, extractor, resolution(sources=[VOTES, SEATS])
    ).parse(sachsen_anhalt_request())

    assert election.election_date.isoformat() == "2026-09-06"
    assert fetcher.urls == [VOTES, SEATS]


async def test_a_page_about_another_region_is_discarded():
    fetcher = SiteFetcher({SEATS: "Thüringen"})
    with pytest.raises(ParseError) as caught:
        await parser(fetcher, FixedExtractor(seats(state="Thuringia"))).parse(
            sachsen_anhalt_request()
        )
    assert NO_RESULTS_MESSAGES["wrong_election"] in str(caught.value)


async def test_a_national_result_does_not_stand_in_for_a_regional_one():
    """A region's share of a national election is that national election, and
    the request was for the region's own parliament."""
    with pytest.raises(ParseError) as caught:
        await parser(SiteFetcher({SEATS: "Bundestag"}), FixedExtractor(seats(state=None))).parse(
            sachsen_anhalt_request()
        )
    assert NO_RESULTS_MESSAGES["wrong_election"] in str(caught.value)


async def test_a_page_about_another_nation_is_discarded():
    with pytest.raises(ParseError):
        await parser(SiteFetcher({SEATS: "Austria"}), FixedExtractor(seats(nation="Austria"))).parse(
            sachsen_anhalt_request()
        )


async def test_the_same_region_spelled_differently_is_still_the_same_region():
    """Punctuation and spacing must not reject a page that is plainly right."""
    election = await parser(
        SiteFetcher({SEATS: "Sitzverteilung"}), FixedExtractor(seats(state="Saxony Anhalt"))
    ).parse(sachsen_anhalt_request())
    assert election.state == "Saxony Anhalt"


# --- pages that were read and could not be used ----------------------------

@pytest.mark.parametrize("reason", get_args(NoResultsReason))
async def test_each_empty_reason_has_its_own_message(reason):
    assert reason in NO_RESULTS_MESSAGES, f"{reason} would fall back to the generic message"


async def test_the_reason_a_page_can_choose_is_never_text_it_wrote():
    """The agent picks a code; we own every word the user reads."""
    assert set(NO_RESULTS_MESSAGES) == set(get_args(NoResultsReason))
    with pytest.raises(ParseError) as caught:
        await parser(SiteFetcher({SEATS: "x"}), FixedExtractor(empty("not_results"))).parse(
            sachsen_anhalt_request()
        )
    assert NO_RESULTS_MESSAGES["not_results"] in str(caught.value)


async def test_a_page_with_votes_but_no_seats_moves_on_to_the_next_candidate():
    fetcher = SiteFetcher({VOTES: "Stimmen", SEATS: "Sitze"})
    extractor = PerPageExtractor({"Stimmen": empty("votes_only"), "Sitze": seats()})

    election = await parser(fetcher, extractor, resolution(sources=[VOTES, SEATS])).parse(
        sachsen_anhalt_request()
    )
    assert election.total_seats == 97
    assert fetcher.urls == [VOTES, SEATS]


async def test_the_message_says_how_many_pages_were_read():
    fetcher = SiteFetcher({VOTES: "Stimmen", SEATS: "Stimmen auch"})
    extractor = PerPageExtractor({"Stimmen": empty("votes_only")})

    with pytest.raises(ParseError) as caught:
        await parser(fetcher, extractor, resolution(sources=[VOTES, SEATS])).parse(
            sachsen_anhalt_request()
        )
    message = str(caught.value)
    assert "none of the 2 pages found" in message
    assert NO_RESULTS_MESSAGES["votes_only"] in message
    assert "Saxony-Anhalt" in message, "named as it was resolved, which is what to check"


async def test_a_candidate_that_does_not_exist_is_passed_over_quietly():
    """A stale search result 404s; that is not the error to show the user."""
    fetcher = SiteFetcher({SEATS: "Sitze"})
    election = await parser(
        fetcher, FixedExtractor(seats()), resolution(sources=["https://gone.example/x", SEATS])
    ).parse(sachsen_anhalt_request())

    assert election.total_seats == 97
    assert fetcher.urls == ["https://gone.example/x", SEATS]


async def test_when_nothing_loads_at_all_the_user_is_told_that():
    with pytest.raises(ParseError, match="no readable page could be found"):
        await parser(SiteFetcher({}), FixedExtractor(seats())).parse(sachsen_anhalt_request())


async def test_the_budget_bounds_how_many_pages_an_import_reads():
    fetcher = SiteFetcher({VOTES: "Stimmen", SEATS: "Sitze", WIKI: "Sitze"})
    extractor = PerPageExtractor({"Stimmen": empty("votes_only"), "Sitze": seats()})

    with pytest.raises(ParseError):
        await parser(
            fetcher, extractor, resolution(sources=[VOTES, SEATS, WIKI]), page_limit=1
        ).parse(sachsen_anhalt_request())
    assert fetcher.urls == [VOTES], "one page, because that is the budget"


# --- failures are reported, never rendered ---------------------------------

async def test_an_extraction_our_schema_rejects_is_reported_not_stored():
    """Seats that do not sum to the declared total must not reach the renderer."""
    with pytest.raises(ParseError, match="not valid"):
        await parser(
            SiteFetcher({SEATS: "x"}), FixedExtractor(seats(total_seats=100))
        ).parse(sachsen_anhalt_request())


async def test_a_bad_colour_from_the_agent_is_rejected():
    injected = seats(blocks=[{"name": "All", "parties": [
        {"name": "P", "abbr": "P", "seats": 97, "color": "red; background:url(x)"}]}])
    with pytest.raises(ParseError, match="not valid"):
        await parser(SiteFetcher({SEATS: "x"}), FixedExtractor(injected)).parse(
            sachsen_anhalt_request()
        )


async def test_an_agent_crash_is_contained():
    class Exploding:
        async def extract(self, page, wanted):
            raise RuntimeError("connection reset")

    # Contained, and reported without its internals: the log has the detail.
    with pytest.raises(ParseError, match="reading it failed on our side"):
        await parser(SiteFetcher({SEATS: "x"}), Exploding()).parse(sachsen_anhalt_request())


async def test_a_resolver_crash_is_not_swallowed():
    """A resolver that cannot answer is a failed import, not an empty search."""
    fetcher = SiteFetcher({SEATS: "x"})
    broken = LlmElectionParser(
        fetcher, FixedExtractor(seats()), StubResolver(error=ParseError("the model declined"))
    )
    with pytest.raises(ParseError, match="declined"):
        await broken.parse(sachsen_anhalt_request())
    assert fetcher.urls == []


# --- searching, on top of what the resolver found --------------------------

async def test_the_resolvers_own_pages_are_read_before_anything_is_searched():
    """A search costs a call; a page the resolver already named does not."""
    search = StubSearch([WIKI])
    election = await parser(
        SiteFetcher({SEATS: "Sitze"}), FixedExtractor(seats()),
        resolution(sources=[SEATS, VOTES, WIKI]), search=search
    ).parse(sachsen_anhalt_request())

    assert election.total_seats == 97
    assert search.queries == [], "the budget was already full of resolver candidates"


async def test_a_search_fills_out_a_resolution_that_found_no_pages():
    fetcher = SiteFetcher({WIKI: "Sitze"})
    election = await parser(
        fetcher, FixedExtractor(seats()), resolution(sources=[]), search=StubSearch([WIKI])
    ).parse(sachsen_anhalt_request())

    assert election.source_url == WIKI
    assert WIKI in fetcher.urls


async def test_the_search_asks_about_the_election_and_nothing_a_page_said():
    """The query is built from the resolution — which was made without reading
    any page — so nothing a hostile page wrote can write the search we run."""
    search = StubSearch([])
    resolved = resolution(sources=[])
    with pytest.raises(ParseError):
        await parser(SiteFetcher({}), FixedExtractor(seats()), resolved, search=search).parse(
            sachsen_anhalt_request()
        )

    assert len(search.queries) == 1
    query = search.queries[0]
    assert resolved.nation in query and resolved.title in query
    # The date pins the election: "Sachsen-Anhalt" alone finds the state's own
    # earlier elections as readily as this one.
    assert resolved.election_date in query
    assert resolved.search_terms in query, "in the language the results are published in"
    assert len(query) < 600


async def test_searching_can_be_turned_off():
    search = StubSearch([WIKI])
    with pytest.raises(ParseError):
        await parser(
            SiteFetcher({}), FixedExtractor(seats()), resolution(sources=[]),
            search=search, search_limit=0
        ).parse(sachsen_anhalt_request())

    assert search.queries == [], "no search engine was contacted"


async def test_a_search_result_is_read_through_the_fetcher_like_any_other_url():
    """Results come from outside the app, so they get no shortcut: they are
    fetched by the same fetcher that refuses private addresses (see
    ``test_fetcher``), never read some other way."""
    metadata = "http://169.254.169.254/latest/meta-data/"
    fetcher = SiteFetcher({})
    extractor = FixedExtractor(seats())

    with pytest.raises(ParseError):
        await parser(
            fetcher, extractor, resolution(sources=[]), search=StubSearch([metadata])
        ).parse(sachsen_anhalt_request())

    assert metadata in fetcher.urls, "offered to the fetcher, which is what refuses it"
    assert extractor.pages == [], "and never extracted"


async def test_a_failed_search_leaves_the_resolvers_candidates_alone():
    """A search engine being down is our problem, not an import that fails."""
    class Broken:
        async def find(self, query, *, limit):
            raise RuntimeError("search backend down")

    election = await parser(
        SiteFetcher({SEATS: "Sitze"}), FixedExtractor(seats()), search=Broken()
    ).parse(sachsen_anhalt_request())
    assert election.total_seats == 97


# --- Wikipedia, ahead of everything else ------------------------------------

async def test_the_article_is_read_before_the_pages_the_resolver_named():
    """An encyclopedia article states seats; an electoral authority's own site
    often states votes, percentages, or a PDF."""
    fetcher = SiteFetcher({ARTICLE: "Sitze", SEATS: "Sitze"})
    election = await parser(
        fetcher, FixedExtractor(seats()), resolution(sources=[SEATS]),
        wikipedia=StubWikipedia([ARTICLE])
    ).parse(sachsen_anhalt_request())

    assert election.source_url == ARTICLE
    assert fetcher.urls == [ARTICLE], "and the resolver's page was never needed"


async def test_an_article_that_is_not_the_results_falls_through_to_the_rest():
    """Wikipedia going first is a preference, not a dependency."""
    fetcher = SiteFetcher({SEATS: "Sitze"})
    election = await parser(
        fetcher, FixedExtractor(seats()), resolution(sources=[SEATS]),
        wikipedia=StubWikipedia([ARTICLE])
    ).parse(sachsen_anhalt_request())

    assert election.source_url == SEATS
    assert fetcher.urls == [ARTICLE, SEATS], "in that order"


async def test_an_article_found_is_one_page_the_search_budget_does_not_pay_for():
    search = StubSearch([WIKI])
    await parser(
        SiteFetcher({ARTICLE: "Sitze"}), FixedExtractor(seats()),
        resolution(sources=[SEATS, VOTES]), search=search,
        wikipedia=StubWikipedia([ARTICLE])
    ).parse(sachsen_anhalt_request())

    assert search.queries == [], "the budget was full before a search engine was asked"


async def test_a_broken_wikipedia_leaves_the_resolvers_candidates_alone():
    """Wikipedia being down is our problem, not an import that fails."""
    class Broken:
        def handles(self, url):
            return False

        async def find(self, resolved, *, limit):
            raise RuntimeError("wikipedia unreachable")

    election = await parser(
        SiteFetcher({SEATS: "Sitze"}), FixedExtractor(seats()), wikipedia=Broken()
    ).parse(sachsen_anhalt_request())
    assert election.total_seats == 97


async def test_wikipedia_can_be_turned_off():
    wikipedia = StubWikipedia([ARTICLE])
    election = await parser(
        SiteFetcher({SEATS: "Sitze"}), FixedExtractor(seats()),
        wikipedia=wikipedia, article_limit=0
    ).parse(sachsen_anhalt_request())

    assert election.source_url == SEATS
    assert wikipedia.queries == [], "no lookup was made"


async def test_an_article_is_checked_against_the_request_like_any_other_page():
    """Being an encyclopedia buys no trust: the article for the previous
    election is discarded on the same check as anything else."""
    fetcher = SiteFetcher({ARTICLE: "Sitze", SEATS: "Sitze"})
    previous = seats(election_date="2021-06-06")
    with pytest.raises(ParseError, match=NO_RESULTS_MESSAGES["wrong_election"]):
        await parser(
            fetcher, FixedExtractor(previous), resolution(sources=[SEATS]),
            wikipedia=StubWikipedia([ARTICLE])
        ).parse(sachsen_anhalt_request())


# --- the prompts ------------------------------------------------------------

def test_the_page_is_fenced_as_data_in_the_prompt():
    resolved = resolution()
    message = build_user_message(FetchedPage(url=SEATS, text="CDU\t50"), resolved)

    assert "<document>" in message and "</document>" in message
    assert SEATS in message, "the page's own address, so the agent can read it"
    assert resolved.state in message, "and which election is wanted"
    assert message.index("<document>") > message.index(resolved.state), (
        "the request precedes the untrusted document"
    )


def test_the_agent_has_no_output_channel_other_than_the_schema():
    """No tools, no state: the model can only fill in these fields."""
    assert set(ExtractedElection.model_fields) == {
        "nation", "state", "election_date", "title",
        "total_seats", "majority_seats", "blocks", "no_results_reason",
    }
    assert ExtractedElection.model_config["extra"] == "forbid"
    # The one field that feeds an error message is an enum, so it carries a
    # choice and never a sentence of the page's own.
    assert get_args(NoResultsReason) and all(
        isinstance(reason, str) for reason in get_args(NoResultsReason)
    )
    assert "source_url" not in ExtractedElection.model_fields, (
        "provenance is ours: it is the page we chose to read, not a field to fill in"
    )


def test_the_mock_result_is_internally_consistent():
    seat_sum = sum(p.seats for b in SACHSEN_ANHALT_2021.blocks for p in b.parties)
    assert seat_sum == SACHSEN_ANHALT_2021.total_seats == 97
    assert SACHSEN_ANHALT_2021.majority_seats == 49


async def test_the_mocked_pipeline_answers_whatever_it_is_asked():
    """Mock mode has to survive the identity checks for any request, or it is
    only a demo of one election."""
    request = make_request(year=2019, nation="Denmark")
    resolver = MockResolver(sources=(SEATS,))
    election = await LlmElectionParser(
        SiteFetcher({SEATS: "Sitze"}), MockExtractor(), resolver
    ).parse(request)

    assert election.nation == "Denmark"
    assert election.state is None
    assert election.election_date.year == 2019


# --- through the store's single-flight machinery ---------------------------

async def build_service(extractor=None, resolved=None):
    from app.service import ImportService
    from app.store import InMemoryElectionStore

    extractor = extractor or FixedExtractor(seats())
    store = InMemoryElectionStore()
    service = ImportService(
        store, parser(SiteFetcher({SEATS: "Sitze"}), extractor, resolved)
    )
    return store, extractor, service


async def test_a_stored_election_never_invokes_the_agent_again():
    store, extractor, service = await build_service()
    request = sachsen_anhalt_request()

    first = await service.submit(request)
    await service.wait_for(first.request_key, timeout=2.0)
    await service.confirm(first.request_key)
    assert len(extractor.pages) == 1

    second = await service.submit(request)
    assert second.state.value == "ready"
    assert second.reused is True
    assert len(extractor.pages) == 1, "a request made before must never reach the model"


async def test_a_failed_import_stores_nothing_and_stays_retryable():
    from app.service import ImportState

    extractor = FixedExtractor(seats(total_seats=100))
    store, extractor, service = await build_service(extractor)
    request = sachsen_anhalt_request()

    submitted = await service.submit(request)
    failed = await service.wait_for(submitted.request_key, timeout=2.0)

    assert failed.state is ImportState.FAILED
    assert "not valid" in failed.error, "the user is told what went wrong"
    assert store.list_elections() == []

    # A later attempt is allowed to try again.
    extractor.result = seats()
    retry = await service.submit(request)
    recovered = await service.wait_for(retry.request_key, timeout=2.0)
    assert recovered.state is ImportState.PREVIEW
    assert recovered.election.total_seats == 97
