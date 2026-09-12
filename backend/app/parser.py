"""The parsing seam.

Turning a request — a year, a nation, maybe a region — into a validated election
is a separate concern (and a separate issue); the store only needs to know that
*something* can do it. The default implementation refuses, so a deployment
without a configured parser fails loudly instead of silently storing nothing.
"""

from __future__ import annotations

import logging
from typing import Protocol

from pydantic import ValidationError

from .identity import normalize_date, same_place
from .resolver import ResolvedElection, unresolved_message
from .schema import Election
from .search import DEFAULT_SEARCH_LIMIT, DisabledSearch
from .store import ImportRequest

log = logging.getLogger(__name__)

#: How many pages one import may read. Each is a fetch and a model call, and a
#: candidate further down the list is rarely the official one.
DEFAULT_PAGE_LIMIT = 3


class ParseError(RuntimeError):
    """Raised when a request cannot be turned into a valid election."""


class ElectionParser(Protocol):
    async def parse(self, request: ImportRequest) -> Election:
        """Find the election ``request`` names and extract its results."""
        ...


class UnavailableParser:
    """Placeholder parser: every import fails until a real one is injected."""

    async def parse(self, request: ImportRequest) -> Election:
        raise ParseError("no election parser is configured")


class LlmElectionParser:
    """Resolve the request, read the pages it points to, extract the seats.

    Four steps, each a separate object so any of them can be swapped for a mock
    without changing the pipeline the real thing runs through:

    1. :mod:`app.resolver` turns what the user typed into one election — fixing
       the spelling, finding the day it was held — and into candidate URLs;
    2. :mod:`app.search` looks for more candidates, for the search backends that
       are a plain search engine rather than an agent;
    3. :mod:`app.fetcher` downloads one candidate at a time, through the
       public-address guard;
    4. :mod:`app.extractor` reads that one page, with no tools and no memory.

    What comes back is then checked *here*, in code, against what was asked for:
    a page reporting some other year or some other region is skipped, however
    confidently it was extracted. That check is why this pipeline can be pointed
    at a search engine at all — the model chooses what to read, never what
    counts as an answer.
    """

    def __init__(
        self,
        fetcher,
        extractor,
        resolver,
        *,
        search=None,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        search_limit: int = DEFAULT_SEARCH_LIMIT,
    ):
        self._fetcher = fetcher
        self._extractor = extractor
        self._resolver = resolver
        self._search = search or DisabledSearch()
        self._page_limit = page_limit
        self._search_limit = search_limit

    async def parse(self, request: ImportRequest) -> Election:
        resolved = await self._resolve(request)
        candidates = await self._candidates(resolved)
        if not candidates:
            raise ParseError(
                f"no page publishing the seats for {_clip(resolved.describe(), 80)} "
                "could be found"
            )

        read = 0
        problems: list[str] = []
        for url in candidates[: self._page_limit]:
            election, problem = await self._read(url, resolved, request)
            if election is not None:
                return election
            if problem is not None:
                read += 1
                problems.append(problem)

        raise ParseError(_summarise_attempt(resolved, read, problems))

    async def _resolve(self, request: ImportRequest) -> ResolvedElection:
        """Work out which election this is, and refuse if it is not one.

        The year is the user's, not the resolver's, so a resolution that lands
        in another year is not a correction to accept — it is the resolver
        saying this place held no such election in the year that was asked for.
        """
        resolved = await self._resolver.resolve(request)
        if resolved.unresolved_reason:
            log.info(
                "unresolved %r reason=%s", request.describe(), resolved.unresolved_reason
            )
            raise ParseError(unresolved_message(resolved.unresolved_reason))
        try:
            resolved_year = normalize_date(resolved.election_date).year
        except (TypeError, ValueError):
            raise ParseError("the election's date could not be read") from None
        if resolved_year != request.year:
            log.info(
                "resolution left the requested year %r -> %s",
                request.describe(), resolved.election_date,
            )
            raise ParseError(unresolved_message("no_election"))
        log.info("resolved %r as %s", request.describe(), resolved.describe())
        return resolved

    async def _candidates(self, resolved: ResolvedElection) -> list[str]:
        """Where to look, best first: the resolver's own list, then a search.

        The resolver has usually searched already, so the extra search is for
        the deployments whose search backend is an index rather than an agent —
        and for the times the resolver named an election it could not find a
        page for.
        """
        candidates = list(resolved.sources)
        if len(candidates) < self._page_limit:
            for url in await self._find(resolved):
                if url not in candidates:
                    candidates.append(url)
        return candidates

    async def _find(self, resolved: ResolvedElection) -> list[str]:
        """Ask the web where this election's seats are published.

        A search that fails is not an import that fails: whatever the resolver
        already found is still worth reading.
        """
        if self._search_limit <= 0:
            return []
        try:
            return await self._search.find(search_query(resolved), limit=self._search_limit)
        except Exception as exc:  # noqa: BLE001 - reported as "we found nothing"
            log.warning("searching for %r failed: %s", _clip(resolved.describe(), 80), exc)
            return []

    async def _read(self, url: str, resolved: ResolvedElection, request: ImportRequest):
        """Read one candidate. Returns the election, or why this page was not it.

        A candidate that will not load is passed over in silence and does not
        count as a page read: a stale search result is expected to 404, and that
        is not the failure the user needs to hear about. A page that loads and
        says something useless *is* reported — it is an answer, and a wrong one.
        """
        from .fetcher import FetchError

        try:
            page = await self._fetcher.fetch(url)
        except FetchError as exc:
            log.info("skipped %s — %s", url, exc)
            return None, None

        try:
            extracted = await self._extractor.extract(page, resolved)
        except ParseError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, never rendered
            return None, f"extraction failed: {_clip(str(exc))}"

        if not any(block.parties for block in extracted.blocks):
            return None, _no_results_message(extracted.no_results_reason)

        try:
            election = _validate(extracted, page.url)
        except ParseError as exc:
            return None, str(exc)

        # The identity came from an untrusted page, so it is checked against the
        # request before it is allowed to decide anything. A page that reports a
        # different election is the one failure that would otherwise look like a
        # success. Checked after validation, so the comparison is against clean,
        # parsed values rather than whatever the page talked the agent into.
        if not is_wanted(election, resolved, request):
            log.info(
                "discarded %s — reports %s %s, wanted %s",
                page.url, election.nation, election.election_date, resolved.describe(),
            )
            return None, _no_results_message("wrong_election")

        return election, None


def is_wanted(election: Election, resolved: ResolvedElection, request: ImportRequest) -> bool:
    """Whether what a page reported is the election that was asked for.

    Three things have to agree, and each catches a different way a search goes
    wrong: the *year* is the user's own input, so a page about the previous
    election fails it; the *nation* catches a same-named region elsewhere; and
    the *region* — where ``None`` means "this was the national election" —
    catches both a neighbouring region and a national result standing in for a
    regional one.

    Names are compared as :func:`identity.place_token` reduces them, so
    punctuation and spacing do not matter. Both agents are told to name places
    in English, and the extractor to reuse the names the request gives it, so
    the two sides of this comparison do not drift apart over spelling.
    """
    return (
        election.election_date.year == request.year
        and same_place(election.nation, resolved.nation)
        and same_place(election.state, resolved.state)
    )


def search_query(resolved: ResolvedElection) -> str:
    """What to search for: the election, as the resolver identified it.

    Built from allowlisted fields, each clipped. Nothing a results page said
    reaches this string — the resolver never read one — and it leaves the app,
    so it gets a length like any other model output.
    """
    named = " ".join(
        _clip(part, 60)
        for part in (resolved.title, resolved.nation, resolved.state, resolved.election_date)
        if part
    )
    query = (
        f"Election: {named}\n"
        "Find a page that states how many seats each party won."
    )
    if resolved.search_terms:
        query += f"\nSearch terms: {_clip(resolved.search_terms, 200)}"
    return query


def _validate(extracted, source_url: str) -> Election:
    """Put the extraction through the schema. Whatever fails it is not stored."""
    try:
        return Election.model_validate(
            {
                "nation": extracted.nation,
                "state": extracted.state,
                "election_date": extracted.election_date,
                "title": extracted.title,
                # The page the numbers were actually read from, which is one the
                # resolver or the search found — the user gave us no address.
                "source_url": source_url,
                "total_seats": extracted.total_seats,
                "majority_seats": extracted.majority_seats,
                "blocks": [
                    {
                        "name": block.name,
                        "parties": [
                            {
                                "name": party.name,
                                "abbr": party.abbr,
                                "seats": party.seats,
                                "color": party.color,
                            }
                            for party in block.parties
                        ],
                    }
                    for block in extracted.blocks
                ],
            }
        )
    except ValidationError as exc:
        # The agent produced something our schema rejects. Report it; store nothing.
        raise ParseError(f"the extracted results are not valid: {_summarise(exc)}") from None


#: What an empty or unusable extraction means, in the user's terms. The agent
#: picks one of these codes; the wording is ours, so an untrusted page cannot
#: write the message even though it decides which one appears.
NO_RESULTS_MESSAGES = {
    "wrong_election": "it reported a different election",
    "votes_only": "it reported votes, not seats",
    "identity_unclear": "it had seat counts, but nothing saying which election they are from",
    "not_results": "it was not a set of election results",
}

DEFAULT_NO_RESULTS_MESSAGE = "no election results could be found on it"


def _no_results_message(reason: str | None) -> str:
    """Explain one unusable page, falling back when the agent said nothing."""
    return NO_RESULTS_MESSAGES.get(reason, DEFAULT_NO_RESULTS_MESSAGE)


def _summarise_attempt(
    resolved: ResolvedElection, read: int, problems: list[str]
) -> str:
    """Say what was looked at and what was wrong with it.

    The election is named as the resolver understood it, because a request that
    was read as the wrong election is the most likely reason for ending up here
    and the user cannot see that anywhere else.
    """
    election = _clip(resolved.describe(), 80)
    if not read:
        return f"no readable page could be found for {election}"
    lead = (
        f"the one page found for {election} could not be used"
        if read == 1
        else f"none of the {read} pages found for {election} could be used"
    )
    distinct = list(dict.fromkeys(problems))
    if len(distinct) == 1:
        return f"{lead}: {distinct[0]}"
    return f"{lead}; the first of them because {distinct[0]}"


#: Errors are shown to the user, and validation messages quote the value that
#: failed — which came from an untrusted page. Inert in the DOM either way, but
#: a page should not be able to write an essay into our error surface.
MAX_PROBLEM_CHARS = 200


def _clip(text: str, limit: int = MAX_PROBLEM_CHARS) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _summarise(error: ValidationError, limit: int = 3) -> str:
    problems = [
        _clip((".".join(str(p) for p in item["loc"]) or "election") + ": " + item["msg"])
        for item in error.errors()[:limit]
    ]
    more = len(error.errors()) - len(problems)
    return "; ".join(problems) + (f" (and {more} more)" if more > 0 else "")
