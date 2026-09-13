"""Turning "Sachen-Anhalt 2026" into one election, and into pages that report it.

The user types a year, a nation, and — for a regional election — a region. That
is all: no URL, nothing to look up, and no obligation to spell it correctly.
Something has to turn that into *which election* they mean and *where its seats
are published*, and this module is that something.

It is a separate, narrower agent than the extraction one (:mod:`app.extractor`),
and the split is the whole safety story of this feature:

- **The resolver never reads a results page.** Its input is the few dozen
  characters the user typed, plus the date. It searches, and the only things it
  may answer with are an election's identity and a list of candidate URLs.
- **The extractor never searches.** It is handed one page at a time, with no
  tools, and reports what that page states.
- **Neither decides anything.** Every URL the resolver produces is fetched
  through the same public-address guard as any other, every page is extracted by
  the same tool-less agent, the identity that comes back is checked against what
  was asked for (:mod:`app.parser`), and nothing is stored until the user
  confirms the preview.

Being generous about spelling lives here too. "Germny", "sachsen anhalt",
"Danmark" and "Denmark" are all things a person types meaning something
unambiguous, and refusing them is a worse answer than understanding them. What
the resolver understood is shown back in the preview, which is where a
misreading gets caught — by the user, before anything is saved.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .observability import io_span
from .schema import MAX_SEATS
from .seats import AllocationMethod
from .store import ImportRequest

log = logging.getLogger(__name__)

#: No proportional system asks for more than this; a larger figure is a misreading.
MAX_THRESHOLD_PERCENT = 50.0

MODEL = "claude-opus-5"
MAX_TOKENS = 4_000

#: The hosted search tool, capped: this agent looks one election up, it does not
#: research a topic.
SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

#: How many candidate pages the resolver may name. More than a handful is not
#: better information, it is a longer list of pages to fetch and read.
MAX_SOURCES = 5
MAX_SOURCE_CHARS = 2_000
#: The search terms leave this app and go to a search engine; they are the
#: resolver's words, so they get a length like any other model output.
MAX_TERMS_CHARS = 200

SYSTEM_PROMPT = """\
You work out which election a person is asking about, and where its results are
published.

You are given a year, a nation, and sometimes a region within that nation. This
is what someone typed from memory, so read it generously:

- Correct misspellings and missing accents: "Germny" is Germany, "Sachen-Anhalt"
  is Saxony-Anhalt, "Osterreich" is Austria.
- Accept the name in any language, an abbreviation, an adjective, or a former
  name: "Danmark", "DK", "Danish", "Holland" for the Netherlands.
- A region may be given as its local or its English name, and may be a state,
  province, Land, canton, or autonomous community.

Then identify the election:

- If a region is named, the election is that region's own assembly — the
  Landtag, parliament, or council elected in that region. If no region is named,
  it is the national parliament.
- It must be an election held, or due to be held, in the year given. If that
  place holds no such election that year, say so rather than answering with a
  different year: the year is what the person asked for, and a neighbouring one
  is a different election.
- Give the nation and the region in English, and the region as null for a
  national election. Give the date the election was held as YYYY-MM-DD; for an
  election held over several days, the last of them.
- Say how the assembly's seats are allocated, as closely as one proportional
  allocation from nationwide vote shares can approximate it: assembly_seats is
  the number of seats, threshold_percent the vote share a party needs to win
  any (0 when there is no threshold), and seat_method "dhondt" or
  "sainte_lague", whichever highest-averages method is closer to the real one.
  Leave all three null for an assembly that is not elected proportionally.

An election whose seats have not been allocated yet is upcoming: one still to
be held, one being held today, and one whose votes are still being counted. Set
upcoming to true, and give as its date the day it is held or scheduled for or,
when no day has been set, the last day on which it can be held. It has no
results, so instead of a seat distribution find its opinion polls: list the
URLs of pages that publish recent polls or seat projections for this election —
a poll aggregator, a public broadcaster's poll tracker, the pollsters' own
pages, or an encyclopedia article listing the polls — the most complete and
most recently updated first.

For an election that has been held, find where its seat distribution is
published. Search for it, and list the URLs of pages that state how many seats
each party won: the electoral
authority, the assembly's own page on its composition, a public broadcaster's
results page, or an encyclopedia article that gives seat counts. Most official
sites publish this in the country's own language, so search in that language —
put those terms in search_terms as well, so they can be searched again.

Order the URLs by how official the source is. Skip pages that report only votes
or percentages, skip news commentary, skip PDFs, and skip a page about a
different election in the same place.

If you cannot get to one election, fill in unresolved_reason instead and leave
the rest as your best guess:
- "unknown_place" when the nation or region is not a place you can identify;
- "no_election" when that place holds no such election in that year;
- "ambiguous" when the request fits more than one election and nothing chooses
  between them.\
"""

#: Why a request could not be turned into one election. A closed set: the code
#: chooses which of our messages the user sees, and never writes one itself.
UnresolvedReason = Literal["unknown_place", "no_election", "ambiguous"]

#: What each code means, in the user's terms.
UNRESOLVED_MESSAGES: dict[str, str] = {
    "unknown_place": (
        "that place could not be identified — check the spelling of the country "
        "and the region"
    ),
    "no_election": (
        "no election of that kind was held there in that year — check the year, "
        "and whether it is the region's own parliament you mean"
    ),
    "ambiguous": (
        "more than one election fits that description — name the region whose "
        "own parliament you mean"
    ),
}

DEFAULT_UNRESOLVED_MESSAGE = "that election could not be identified"


def unresolved_message(reason: str | None) -> str:
    """Explain an unresolved request, falling back when the code is unknown."""
    return UNRESOLVED_MESSAGES.get(reason, DEFAULT_UNRESOLVED_MESSAGE)


class ResolvedElection(BaseModel):
    """Exactly what the resolver is allowed to say."""

    model_config = ConfigDict(extra="forbid")

    nation: str = Field(description="The nation, in English.")
    state: str | None = Field(
        default=None,
        description=(
            "The region whose own assembly was elected, in English; null for a "
            "national election."
        ),
    )
    election_date: str = Field(description="Date the election was held, as YYYY-MM-DD.")
    title: str = Field(description="Human-readable name of the election.")
    search_terms: str = Field(
        default="",
        description=(
            "What to search for to find the seat distribution, in the language "
            "the results are published in."
        ),
    )
    sources: list[str] = Field(
        default_factory=list,
        description=(
            "URLs of pages that state the seat counts, most official first — or, "
            "for an upcoming election, pages publishing its opinion polls."
        ),
    )
    upcoming: bool = Field(
        default=False,
        description="True when the election has not been held yet.",
    )
    assembly_seats: int | None = Field(
        default=None, ge=1, le=MAX_SEATS,
        description="Seats in the assembly; null if not elected proportionally.",
    )
    threshold_percent: float | None = Field(
        default=None, ge=0, le=MAX_THRESHOLD_PERCENT,
        description="Vote share in percent a party needs to win seats; 0 for none.",
    )
    seat_method: AllocationMethod | None = Field(
        default=None,
        description="The highest-averages method closest to how seats are allocated.",
    )
    unresolved_reason: UnresolvedReason | None = Field(
        default=None,
        description="Why the request could not be resolved; null when it was.",
    )

    def describe(self) -> str:
        """The election as the resolver read it, for logs and for the preview."""
        where = f"{self.nation} — {self.state}" if self.state else self.nation
        return f"{where} {self.election_date}"


class ElectionResolver(Protocol):
    async def resolve(self, request: ImportRequest) -> ResolvedElection:
        """Which election ``request`` means, and where its seats are published."""
        ...


def clean_sources(urls: list[str] | None) -> list[str]:
    """The http(s) URLs among ``urls``, in order, without repeats.

    The model's list is not taken as given: anything that is not an ordinary web
    address is dropped here, and what survives is still fetched through
    :func:`app.fetcher.assert_public_url` like any other address.
    """
    found: list[str] = []
    for url in urls or []:
        if not isinstance(url, str) or len(url) > MAX_SOURCE_CHARS:
            continue
        candidate = url.strip()
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        if parsed.scheme in ("http", "https") and parsed.hostname and candidate not in found:
            found.append(candidate)
        if len(found) >= MAX_SOURCES:
            break
    return found


def build_user_message(request: ImportRequest, today: date) -> str:
    """What was asked for, and what day it is.

    The date matters: whether an election has happened yet is the difference
    between "here are the seats" and "that vote has not been held".
    """
    lines = [
        f"Today is {today.isoformat()}.",
        "",
        f"Year: {request.year}",
        f"Nation: {request.nation}",
    ]
    if request.subnation:
        lines.append(f"Region: {request.subnation}")
    else:
        lines.append("Region: (none given — the national parliament)")
    return "\n".join(lines)


def build_request(request: ImportRequest, *, model: str, today: date) -> dict:
    """Every argument the resolution call is allowed to carry.

    Built in one place, and asserted on in the tests, because this is the only
    model call in the app that is given a tool — and the tool is a search
    engine whose results it may only turn into an election's name and a list of
    addresses for us to check.
    """
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": [SEARCH_TOOL],
        "messages": [{"role": "user", "content": build_user_message(request, today)}],
        "output_format": ResolvedElection,
    }


class AnthropicResolver:
    """Live resolution through the Anthropic API, with the hosted search tool."""

    def __init__(self, client=None, *, model: str = MODEL, clock=date.today):
        self._client = client
        self._model = model
        self._clock = clock

    def _get_client(self):
        if self._client is None:
            import anthropic

            # Key comes from ANTHROPIC_API_KEY, mounted from GCP Secret Manager.
            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def resolve(self, request: ImportRequest) -> ResolvedElection:
        from .parser import ParseError

        with io_span(
            log, "anthropic", "resolve", model=self._model, year=request.year
        ) as span:
            response = await self._get_client().messages.parse(
                **build_request(request, model=self._model, today=self._clock())
            )
            span["stop_reason"] = getattr(response, "stop_reason", None)
            usage = getattr(response, "usage", None)
            if usage is not None:
                span["input_tokens"] = getattr(usage, "input_tokens", None)
                span["output_tokens"] = getattr(usage, "output_tokens", None)

            if response.stop_reason == "refusal":
                raise ParseError("the model declined to look this election up")
            if response.parsed_output is None:
                raise ParseError("the model returned no structured result")
            resolved = response.parsed_output
            # Whatever the model listed, only real web addresses go further.
            resolved = resolved.model_copy(
                update={
                    "sources": clean_sources(resolved.sources),
                    "search_terms": resolved.search_terms[:MAX_TERMS_CHARS],
                }
            )
            span["reason"] = resolved.unresolved_reason or "resolved"
            span["sources"] = len(resolved.sources)
        return resolved


class MockResolver:
    """Reads the request back, without calling the API. The default, for now.

    Echoing the request rather than returning a fixed election is what keeps
    mock mode honest: the identity checks in :mod:`app.parser` compare what came
    back against what was asked for, and a mock that always answered
    "Saxony-Anhalt 2021" would fail all of them.
    """

    #: Somewhere harmless to point the fetcher, so mock mode still exercises the
    #: fetch and the extraction. Tests inject their own.
    DEFAULT_SOURCES = ("https://example.com/",)

    def __init__(self, sources: tuple[str, ...] | None = None, *, clock=date.today):
        self.sources = list(self.DEFAULT_SOURCES if sources is None else sources)
        self.calls: list[ImportRequest] = []
        self._clock = clock

    async def resolve(self, request: ImportRequest) -> ResolvedElection:
        self.calls.append(request)
        # Mid-year, so nothing depends on a date the mock cannot know.
        held = date(request.year, 6, 6)
        resolved = ResolvedElection(
            nation=request.nation,
            state=request.subnation,
            election_date=held.isoformat(),
            title=request.describe(),
            search_terms=request.describe(),
            sources=list(self.sources),
            # A year still to come is an upcoming election here too, so mock
            # mode walks through the forecast list as well as the result.
            upcoming=held >= self._clock(),
            # The mock's parties are Saxony-Anhalt's, so its Landtag's rules.
            assembly_seats=97,
            threshold_percent=5.0,
            seat_method="sainte_lague",
        )
        with io_span(log, "anthropic", "resolve", model="mock", year=request.year) as span:
            span["reason"] = "resolved"
            span["sources"] = len(resolved.sources)
        return resolved


class StubResolver:
    """Returns what a test staged, and records what it was asked."""

    def __init__(self, resolved: ResolvedElection | None = None, *, error: Exception | None = None):
        self.resolved = resolved
        self.error = error
        self.calls: list[ImportRequest] = []

    async def resolve(self, request: ImportRequest) -> ResolvedElection:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.resolved is None:
            raise AssertionError("StubResolver was not given anything to return")
        return self.resolved
