"""The extraction pipeline: fetch -> agent -> our validator -> stored election."""

from __future__ import annotations

import pytest

from app.extractor import (
    SACHSEN_ANHALT_2021,
    ExtractedBlock,
    ExtractedElection,
    ExtractedParty,
    MockExtractor,
    build_user_message,
)
from app.fetcher import FetchError, FetchedPage
from app.parser import LlmElectionParser, ParseError
from tests.factories import make_request

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class StubFetcher:
    def __init__(self, text="Partei\tSitze\nCDU\t40", error=None):
        self.text = text
        self.error = error
        self.urls: list[str] = []

    async def fetch(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return FetchedPage(url=url, text=self.text)


def sachsen_anhalt_request():
    """Just a URL — the agent works out which election this is."""
    return make_request(source_url="https://wahlergebnisse.sachsen-anhalt.de/")


# --- end-to-end through the mocked agent -----------------------------------

async def test_sachsen_anhalt_imports_into_the_canonical_schema():
    fetcher, extractor = StubFetcher(), MockExtractor()
    election = await LlmElectionParser(fetcher, extractor).parse(sachsen_anhalt_request())

    assert election.nation == "Germany", "inferred by the agent, not supplied"
    assert election.state == "Saxony-Anhalt"
    assert election.election_date.isoformat() == "2021-06-06"
    assert election.total_seats == 97
    assert election.majority_seats == 49
    assert [p.abbr for b in election.blocks for p in b.parties] == [
        "CDU", "AfD", "Linke", "SPD", "FDP", "Grüne"
    ]
    assert sum(p.seats for b in election.blocks for p in b.parties) == 97


async def test_the_page_the_user_pasted_is_the_page_that_is_fetched():
    fetcher, extractor = StubFetcher(), MockExtractor()
    request = sachsen_anhalt_request()
    await LlmElectionParser(fetcher, extractor).parse(request)

    assert fetcher.urls == [request.source_url], "exactly one page, exactly that URL"
    page_text, seen_request = extractor.calls[0]
    assert page_text == fetcher.text, "the agent receives the page as data"
    assert seen_request == request


async def test_a_different_shape_of_election_also_imports():
    """Two parties, two blocks, an odd seat total — nothing Folketing-specific."""
    toy = ExtractedElection(
        nation="Toyland",
        election_date="2026-01-15",
        title="Toy assembly",
        total_seats=7,
        majority_seats=4,
        blocks=[
            ExtractedBlock(name="Left", parties=[ExtractedParty(name="Left", abbr="L", seats=4, color="#c0392b")]),
            ExtractedBlock(name="Right", parties=[ExtractedParty(name="Right", abbr="R", seats=3, color="#2980b9")]),
        ],
    )
    election = await LlmElectionParser(StubFetcher(), MockExtractor(toy)).parse(make_request())
    assert election.total_seats == 7
    assert len(election.blocks) == 2


# --- identity cannot be influenced by the page -----------------------------

async def test_the_agent_infers_identity_but_not_the_source_url():
    """The agent decides which election this is; only the URL is beyond its reach,
    because that is the one fact the user actually supplied."""
    request = sachsen_anhalt_request()
    election = await LlmElectionParser(StubFetcher(), MockExtractor()).parse(request)

    assert election.source_url == request.source_url
    assert "source_url" not in ExtractedElection.model_fields
    assert {"nation", "state", "election_date"} <= set(ExtractedElection.model_fields)


async def test_an_identity_our_schema_rejects_is_reported_not_stored():
    """A page cannot smuggle a malformed identity past validation."""
    bad_date = ExtractedElection(
        nation="Nowhere", election_date="not-a-date", title="X",
        total_seats=1, majority_seats=1,
        blocks=[ExtractedBlock(name="All", parties=[
            ExtractedParty(name="P", abbr="P", seats=1, color="#000000")
        ])],
    )
    with pytest.raises(ParseError, match="not valid"):
        await LlmElectionParser(StubFetcher(), MockExtractor(bad_date)).parse(make_request())


# --- failures are reported, never rendered ---------------------------------

async def test_a_fetch_failure_becomes_a_user_visible_parse_error():
    fetcher = StubFetcher(error=FetchError("the page returned HTTP 404"))
    with pytest.raises(ParseError, match="HTTP 404"):
        await LlmElectionParser(fetcher, MockExtractor()).parse(make_request())


async def test_an_agent_result_our_schema_rejects_is_reported_not_stored():
    """Seats that do not sum to the declared total must not reach the renderer."""
    inconsistent = ExtractedElection(
        nation="Nowhere", election_date="2026-01-01", title="Bad extraction",
        total_seats=100,
        majority_seats=51,
        blocks=[ExtractedBlock(name="All", parties=[
            ExtractedParty(name="Only Party", abbr="OP", seats=40, color="#000000")
        ])],
    )
    with pytest.raises(ParseError, match="not valid"):
        await LlmElectionParser(StubFetcher(), MockExtractor(inconsistent)).parse(make_request())


async def test_a_bad_colour_from_the_agent_is_rejected():
    injected = ExtractedElection(
        nation="Nowhere", election_date="2026-01-01", title="Injected",
        total_seats=1,
        majority_seats=1,
        blocks=[ExtractedBlock(name="All", parties=[
            ExtractedParty(name="P", abbr="P", seats=1, color="red; background:url(x)")
        ])],
    )
    with pytest.raises(ParseError, match="not valid"):
        await LlmElectionParser(StubFetcher(), MockExtractor(injected)).parse(make_request())


async def test_an_empty_extraction_is_reported_rather_than_stored():
    empty = ExtractedElection(
        nation="Nowhere", election_date="2026-01-01", title="Not an election",
        total_seats=1, majority_seats=1,
        blocks=[ExtractedBlock(name="None", parties=[])],
    )
    # An empty party list cannot even be constructed past our schema, so the
    # pipeline must catch it before validation and say something useful.
    with pytest.raises(ParseError, match="no election results"):
        await LlmElectionParser(StubFetcher(), MockExtractor(empty)).parse(make_request())


async def test_an_agent_crash_is_contained():
    class Exploding:
        async def extract(self, page_text, request):
            raise RuntimeError("connection reset")

    with pytest.raises(ParseError, match="connection reset"):
        await LlmElectionParser(StubFetcher(), Exploding()).parse(make_request())


# --- the prompt ------------------------------------------------------------

def test_the_page_is_fenced_as_data_in_the_prompt():
    request = sachsen_anhalt_request()
    message = build_user_message("CDU\t40", request)
    assert "<document>" in message and "</document>" in message
    assert request.source_url in message
    assert message.index("<document>") > message.index(request.source_url), (
        "the URL precedes the untrusted document"
    )


def test_the_agent_has_no_output_channel_other_than_the_schema():
    """No tools, no state: the model can only fill in these four fields."""
    assert set(ExtractedElection.model_fields) == {
        "nation", "state", "election_date", "title",
        "total_seats", "majority_seats", "blocks",
    }
    assert ExtractedElection.model_config["extra"] == "forbid"


def test_the_mock_result_is_internally_consistent():
    seats = sum(p.seats for b in SACHSEN_ANHALT_2021.blocks for p in b.parties)
    assert seats == SACHSEN_ANHALT_2021.total_seats == 97
    assert SACHSEN_ANHALT_2021.majority_seats == 49


# --- through the store's single-flight machinery ---------------------------

class CountingExtractor(MockExtractor):
    """A mock agent that records how many times it would have called the model."""


async def build_service():
    from app.service import ImportService
    from app.store import InMemoryElectionStore

    extractor = CountingExtractor()
    store = InMemoryElectionStore()
    return store, extractor, ImportService(store, LlmElectionParser(StubFetcher(), extractor))


async def test_a_stored_election_never_invokes_the_agent_again():
    store, extractor, service = await build_service()
    request = sachsen_anhalt_request()

    first = await service.submit(request)
    await service.wait_for(first.page_key, timeout=2.0)
    await service.confirm(first.page_key)
    assert len(extractor.calls) == 1

    second = await service.submit(request)
    assert second.state.value == "ready"
    assert second.reused is True
    assert len(extractor.calls) == 1, "a known page must never reach the model"


async def test_a_failed_extraction_stores_nothing_and_stays_retryable():
    from app.service import ImportState, ImportService
    from app.store import InMemoryElectionStore

    broken = ExtractedElection(
        nation="Nowhere", election_date="2026-01-01", title="Bad",
        total_seats=100, majority_seats=51,
        blocks=[ExtractedBlock(name="All", parties=[
            ExtractedParty(name="P", abbr="P", seats=1, color="#000000")
        ])],
    )
    store = InMemoryElectionStore()
    extractor = MockExtractor(broken)
    service = ImportService(store, LlmElectionParser(StubFetcher(), extractor))
    request = sachsen_anhalt_request()

    submitted = await service.submit(request)
    failed = await service.wait_for(submitted.page_key, timeout=2.0)

    assert failed.state is ImportState.FAILED
    assert "not valid" in failed.error, "the user is told what went wrong"
    assert store.list_elections() == []

    # A later attempt is allowed to try again.
    extractor.result = SACHSEN_ANHALT_2021
    retry = await service.submit(request)
    recovered = await service.wait_for(retry.page_key, timeout=2.0)
    assert recovered.state is ImportState.PREVIEW
    assert recovered.election.total_seats == 97
