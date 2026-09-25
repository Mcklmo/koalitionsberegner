"""Asking without a year: "the latest election there".

The resolver names the previous election and the next one; code decides which
of them to offer. The next one's polls are offered beside the previous result
only when it is less than a year away. Picking asks again with a year, so
everything after that is the ordinary import, and nothing here fetches or reads
a page.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.identity import request_key
from app.parser import (
    POLLS_WINDOW_DAYS,
    ElectionChoice,
    LlmElectionParser,
    ParseError,
    choose_candidates,
)
from app.resolver import (
    LATEST_SEARCH_TOOL,
    LATEST_SYSTEM_PROMPT,
    LatestElections,
    LatestEntry,
    MockResolver,
    StubResolver,
    build_latest_request,
)
from app.service import ImportService, ImportState
from app.sqlite_store import SqliteElectionStore
from app.store import ElectionCandidate, ImportRequest, InMemoryElectionStore, JobStatus
from app.wishlist import build_issue, marker_for
from tests.factories import make_election, make_request

pytestmark = pytest.mark.anyio

TODAY = date(2026, 9, 25)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def latest(previous="2022-11-01", upcoming="2027-06-30", **overrides) -> LatestElections:
    data = {
        "nation": "Denmark",
        "state": None,
        "previous": previous and {"election_date": previous, "title": "Danish general election"},
        "upcoming": upcoming and {"election_date": upcoming, "title": "Next Danish general election"},
    }
    data.update(overrides)
    return LatestElections.model_validate(data)


def yearless(**overrides) -> ImportRequest:
    return make_request(year=None, **overrides)


class NoFetching:
    """A fetcher and extractor in one that fails the test if anything is read."""

    async def fetch(self, url):
        raise AssertionError("a yearless request reads no page")

    async def extract(self, page, wanted):
        raise AssertionError("a yearless request extracts nothing")


def llm_parser(resolver) -> LlmElectionParser:
    return LlmElectionParser(NoFetching(), NoFetching(), resolver, clock=lambda: TODAY)


# --- the request key --------------------------------------------------------

def test_a_yearless_request_has_a_key_of_its_own():
    key = request_key(None, "Danmark")
    assert key == request_key(None, " danmark ")
    for year in (2022, 2026, 2027):
        assert key != request_key(year, "Danmark"), "the latest is no one year"


def test_a_yeared_key_is_unchanged_by_the_year_becoming_optional():
    # Pinned: every job and every issue marker filed so far is keyed by it.
    assert request_key(2026, "Danmark") == request_key("2026", "danmark")


def test_a_yearless_request_describes_itself_as_the_latest():
    assert yearless().describe() == "Danmark (latest)"


# --- which elections are offered -------------------------------------------

def test_a_next_election_within_the_year_is_offered_beside_the_previous_one():
    soon = (TODAY + timedelta(days=POLLS_WINDOW_DAYS - 30)).isoformat()
    offered = choose_candidates(latest(upcoming=soon), today=TODAY)

    assert [c.which for c in offered] == ["upcoming", "previous"]
    assert [c.year for c in offered] == [int(soon[:4]), 2022]
    assert all((c.nation, c.state) == ("Denmark", None) for c in offered)


def test_a_next_election_more_than_a_year_off_is_not_offered():
    far = (TODAY + timedelta(days=POLLS_WINDOW_DAYS + 30)).isoformat()
    offered = choose_candidates(latest(upcoming=far), today=TODAY)

    assert [(c.which, c.year) for c in offered] == [("previous", 2022)]


def test_whether_its_day_is_set_does_not_matter_only_how_near_it_is():
    """The resolver gives the last legal day when no day is set; that is
    offered exactly like a day that was announced."""
    offered = choose_candidates(latest(upcoming="2026-10-31"), today=TODAY)
    assert [c.which for c in offered] == ["upcoming", "previous"]


def test_a_next_election_that_has_already_been_held_is_the_previous_one():
    offered = choose_candidates(latest(upcoming="2026-09-01"), today=TODAY)

    assert [(c.which, c.election_date) for c in offered] == [("previous", date(2026, 9, 1))]


def test_with_no_previous_election_the_next_is_all_there_is():
    offered = choose_candidates(latest(previous=None), today=TODAY)
    assert [c.which for c in offered] == ["upcoming"]


def test_with_neither_there_is_no_election():
    with pytest.raises(ParseError, match="no election"):
        choose_candidates(latest(previous=None, upcoming=None), today=TODAY)


def test_a_date_that_is_not_a_date_is_refused():
    with pytest.raises(ParseError, match="date"):
        choose_candidates(latest(previous="last autumn"), today=TODAY)


def test_a_candidates_text_is_cleaned_like_any_stored_name():
    with pytest.raises(ValidationError):
        ElectionCandidate(which="previous", year=2022, election_date=date(2022, 11, 1),
                          title="Election‮", nation="Denmark")
    with pytest.raises(ValidationError):
        ElectionCandidate(which="previous", year=2022, election_date=date(2022, 11, 1),
                          title="Election", nation="D" * 81)
    blank_region = ElectionCandidate(which="previous", year=2022,
                                     election_date=date(2022, 11, 1),
                                     title=" Election ", nation="Denmark", state=" ")
    assert (blank_region.title, blank_region.state) == ("Election", None)


# --- the parser --------------------------------------------------------------

async def test_a_yearless_request_is_answered_with_a_choice_and_reads_nothing():
    resolver = StubResolver(latest=latest())
    outcome = await llm_parser(resolver).parse(yearless())

    assert isinstance(outcome, ElectionChoice)
    assert [c.which for c in outcome.candidates] == ["upcoming", "previous"]
    assert resolver.calls == [yearless()]


async def test_a_yeared_request_never_asks_for_the_latest():
    class YearOnly(StubResolver):
        async def resolve_latest(self, request):
            raise AssertionError("a year was given")

    with pytest.raises(AssertionError, match="StubResolver was not given"):
        await llm_parser(YearOnly()).parse(make_request(year=2022))


async def test_an_unresolved_place_is_said_so():
    resolver = StubResolver(latest=latest(unresolved_reason="unknown_place"))
    with pytest.raises(ParseError, match="could not be identified"):
        await llm_parser(resolver).parse(yearless())


async def test_mock_mode_offers_both_so_the_pick_can_be_walked_through():
    outcome = await llm_parser(MockResolver(clock=lambda: TODAY)).parse(yearless())

    assert [c.which for c in outcome.candidates] == ["upcoming", "previous"]
    upcoming, previous = outcome.candidates
    assert previous.election_date < TODAY <= upcoming.election_date


def test_the_yearless_call_names_no_year_and_searches_less():
    sent = build_latest_request(yearless(), model="m", today=TODAY)

    assert sent["system"] == LATEST_SYSTEM_PROMPT
    assert sent["tools"] == [LATEST_SEARCH_TOOL]
    assert sent["output_format"] is LatestElections
    message = sent["messages"][0]["content"]
    assert "Today is 2026-09-25." in message
    assert "Year: (none given — the latest)" in message


def test_the_yearless_answer_can_hold_no_address_to_fetch():
    with pytest.raises(ValidationError):
        LatestEntry.model_validate(
            {"election_date": "2022-11-01", "title": "x", "sources": ["https://x.example"]}
        )


# --- the service ------------------------------------------------------------

class ChoosingParser:
    """Answers every request with a fixed choice; counts how often it was asked."""

    def __init__(self, *candidates: ElectionCandidate):
        self.choice = ElectionChoice(candidates)
        self.calls: list[ImportRequest] = []

    async def parse(self, request):
        self.calls.append(request)
        return self.choice


def candidate(which, when) -> ElectionCandidate:
    held = date.fromisoformat(when)
    return ElectionCandidate(which=which, year=held.year, election_date=held,
                             title=f"{which} election", nation="Denmark")


BOTH = (candidate("upcoming", "2027-06-30"), candidate("previous", "2022-11-01"))


async def test_the_service_holds_the_choice_for_the_user_to_pick():
    store = InMemoryElectionStore()
    parser = ChoosingParser(*BOTH)
    service = ImportService(store, parser, today=lambda: TODAY)

    submitted = await service.submit(yearless())
    settled = await service.wait_for(submitted.request_key, timeout=2.0)

    assert settled.state is ImportState.PICK
    assert settled.candidates == BOTH
    assert store.get_job(submitted.request_key).status is JobStatus.AWAITING_ELECTION

    again = await service.submit(yearless())
    assert (again.state, again.reused) == (ImportState.PICK, True)
    assert len(parser.calls) == 1, "a pick still on offer costs nothing to serve again"
    assert (await service.confirm(submitted.request_key)).state is ImportState.PICK


async def test_a_pick_whose_next_election_has_since_been_held_is_forgotten():
    store = InMemoryElectionStore()
    service = ImportService(store, ChoosingParser(*BOTH), today=lambda: date(2027, 7, 1))
    key = service.request_key_for(yearless())
    store.claim(key, yearless())
    store.offer_elections(key, list(BOTH))

    assert (await service.status(key)).state is ImportState.UNKNOWN
    assert await service.peek(yearless()) is None


def test_the_candidates_survive_a_restart_and_a_new_claim_clears_them(tmp_path):
    db = tmp_path / "elections.db"
    key = request_key(None, "Danmark")
    SqliteElectionStore(db).claim(key, yearless())
    SqliteElectionStore(db).offer_elections(key, list(BOTH))

    job = SqliteElectionStore(db).get_job(key)
    assert (job.status, job.candidates) == (JobStatus.AWAITING_ELECTION, BOTH)

    reopened = SqliteElectionStore(db, stale_after=0)
    reopened.claim(key, yearless())
    assert reopened.get_job(key).candidates == ()
    assert reopened.discard(key) is False, "a running import is not discardable"


def test_a_pick_can_be_discarded_in_every_store(tmp_path):
    for store in (InMemoryElectionStore(), SqliteElectionStore(tmp_path / "e.db")):
        key = request_key(None, "Danmark")
        store.claim(key, yearless())
        store.offer_elections(key, list(BOTH))
        assert store.discard(key) is True
        assert store.get_job(key) is None


# --- over HTTP --------------------------------------------------------------

@pytest.fixture
def client():
    store = InMemoryElectionStore()
    parser = ChoosingParser(*BOTH)
    main.app.dependency_overrides[main.get_service] = lambda: ImportService(store, parser)
    with TestClient(main.app) as test_client:
        test_client.store = store
        test_client.parser = parser
        yield test_client
    main.app.dependency_overrides.clear()


def test_an_import_without_a_year_offers_the_elections_to_pick(client):
    response = client.post("/api/elections/import?wait_seconds=2", json={"nation": "Danmark"})

    body = response.json()
    assert body["state"] == "pick", response.text
    assert [(c["which"], c["year"], c["nation"]) for c in body["candidates"]] == [
        ("upcoming", 2027, "Denmark"), ("previous", 2022, "Denmark"),
    ]
    assert client.parser.calls == [yearless()]


def test_an_empty_year_box_means_the_latest(client):
    response = client.post(
        "/api/elections/import?wait_seconds=2", json={"year": " ", "nation": "Danmark"}
    )
    assert response.json()["state"] == "pick"


def test_a_year_that_is_given_is_still_checked(client):
    response = client.post("/api/elections/import", json={"year": "sometime", "nation": "Danmark"})
    assert response.status_code == 422


def test_a_yearless_lookup_reports_the_pick_and_never_guesses_from_storage(client):
    from app.identity import election_hash

    held = make_election(nation="Danmark", election_date="2022-11-01")
    client.store.put_election(election_hash("Danmark", None, "2022-11-01"), held)

    unknown = client.get("/api/elections/lookup", params={"nation": "Danmark"}).json()
    assert unknown["state"] == "unknown", "the store cannot say which election is the latest"

    client.post("/api/elections/import?wait_seconds=2", json={"nation": "Danmark"})
    picked = client.get("/api/elections/lookup", params={"nation": "Danmark"}).json()
    assert picked["state"] == "pick"
    assert len(picked["candidates"]) == 2


# --- asking for it ----------------------------------------------------------

def test_a_request_for_the_latest_is_filed_as_such():
    issue = build_issue(yearless(), marker="<!-- m -->")

    assert issue["title"] == "Election request: latest Danmark"
    assert "the latest Danmark election" in issue["body"]
    assert "| Year | `latest` |" in issue["body"]
    assert marker_for(yearless()) != marker_for(make_request(year=2026))
    assert marker_for(yearless()) == marker_for(yearless(nation=" danmark"))
