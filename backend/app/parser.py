"""The parsing seam.

Turning a request — a year, a nation, maybe a region — into a validated election
is a separate concern (and a separate issue); the store only needs to know that
*something* can do it. The default implementation refuses, so a deployment
without a configured parser fails loudly instead of silently storing nothing.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Literal, Protocol

from pydantic import ValidationError

from .extractor import MAX_POLLS_PER_PAGE
from .fetcher import FetchedPage
from .identity import normalize_date, place_token, same_place
from .resolver import ResolvedElection, unresolved_message
from .schema import Election
from .search import DEFAULT_SEARCH_LIMIT, DisabledSearch
from .seats import allocate
from .store import ImportRequest
from .wikipedia import DEFAULT_ARTICLE_LIMIT

log = logging.getLogger(__name__)

#: How many pages one import may read. Each is a fetch and a model call, and a
#: candidate further down the list is rarely the official one.
DEFAULT_PAGE_LIMIT = 3

#: How many forecasts one upcoming election is offered with. The newest ones;
#: a list longer than this is a table to read, not a choice to make.
MAX_FORECASTS = 10


class ParseError(RuntimeError):
    """Raised when a request cannot be turned into a valid election.

    Its message is shown to the user, so it is written for them. Anything that
    is not a ParseError is reported as :data:`READ_FAILED`, never as its text.
    """


#: Why a page was unusable when reading it broke on our side — the model API
#: refused, a connection dropped. The detail is logged, not shown.
READ_FAILED = "reading it failed on our side"


class ElectionParser(Protocol):
    async def parse(self, request: ImportRequest) -> Election | list[Election]:
        """Find the election ``request`` names and extract its results.

        An election that has not been held has none, and comes back as a list
        instead: its newest polls, each one a forecast (:class:`schema.Forecast`)
        for the user to choose from. Never an empty one — no polls is an error.
        """
        ...


class UnavailableParser:
    """Placeholder parser: every import fails until a real one is injected."""

    async def parse(self, request: ImportRequest) -> Election | list[Election]:
        raise ParseError("no election parser is configured")


class LlmElectionParser:
    """Resolve the request, read the pages it points to, extract the seats.

    Five steps, each a separate object so any of them can be swapped for a mock
    without changing the pipeline the real thing runs through:

    1. :mod:`app.resolver` turns what the user typed into one election — fixing
       the spelling, finding the day it was held — and into candidate URLs;
    2. :mod:`app.wikipedia` looks the identified election up in Wikipedia, whose
       article is read first where there is one;
    3. :mod:`app.search` looks for more candidates, for the search backends that
       are a plain search engine rather than an agent;
    4. :mod:`app.fetcher` downloads one candidate at a time, through the
       public-address guard;
    5. :mod:`app.extractor` reads that one page, with no tools and no memory.

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
        wikipedia=None,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        search_limit: int = DEFAULT_SEARCH_LIMIT,
        article_limit: int = DEFAULT_ARTICLE_LIMIT,
        clock=date.today,
    ):
        self._clock = clock
        self._fetcher = fetcher
        self._extractor = extractor
        self._resolver = resolver
        self._search = search or DisabledSearch()
        # ``None``, not a disabled stand-in: there is nothing to look up in when
        # Wikipedia is off, and the candidate list is simply the one it was
        # before this seam existed.
        self._wikipedia = wikipedia
        self._page_limit = page_limit
        self._search_limit = search_limit
        self._article_limit = article_limit

    async def parse(self, request: ImportRequest) -> Election | list[Election]:
        resolved = await self._resolve(request)
        want: Literal["polls", "results"] = "polls" if self._upcoming(resolved) else "results"
        return await self.parse_resolved(resolved, request, want=want)

    async def parse_resolved(
        self, resolved: ResolvedElection, request: ImportRequest, *, want: Literal["polls", "results"]
    ) -> Election | list[Election]:
        """The rest of :meth:`parse`, given an election already resolved.

        Split out for the scheduled refresh (plan 3, A4): resolving costs a
        model call and this is what may run again and again without paying for
        one — ``want`` stands in for :meth:`_upcoming`, because election night
        is the one day the calendar and the clock disagree about what there is
        to read: the resolver still calls it "upcoming", but the day has come
        and it is a result, not a poll, that the refresh is after.
        """
        if want == "polls":
            # Wikipedia is searched for polling articles by the flag, so the
            # caller's answer is written into it.
            return await self._forecasts(resolved.model_copy(update={"upcoming": True}), request)
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

    async def peek_source(
        self, resolved: ResolvedElection, *, want: Literal["polls", "results"]
    ) -> FetchedPage | None:
        """The first candidate page, fetched but not extracted — nothing paid for yet.

        What the scheduled refresh hashes to tell an unchanged page from one
        worth a model call (plan 3, A6.1). Its text is already the condensed
        table where the candidate is a Wikipedia article — the same fetcher
        seam :meth:`_read` and :meth:`_read_polls` use — and the page as fetched
        otherwise. ``None`` when there is no candidate, or the one there is
        cannot be reached; either way the caller falls through to the ordinary
        read, which will explain why in terms a person can act on.
        """
        from .fetcher import FetchError

        candidates = (
            await self._poll_candidates(resolved)
            if want == "polls"
            else await self._candidates(resolved)
        )
        if not candidates:
            return None
        try:
            return await self._fetcher.fetch(candidates[0])
        except FetchError:
            return None

    async def _poll_candidates(self, resolved: ResolvedElection) -> list[str]:
        """Where :meth:`_forecasts` looks: the same list, without the reading."""
        candidates = await self._articles(resolved)
        for url in resolved.sources:
            if url not in candidates:
                candidates.append(url)
        return candidates

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

    def _upcoming(self, resolved: ResolvedElection) -> bool:
        """Whether this election is still to be held, and so has polls, not results.

        The resolver says so, and the date is checked as well: an election dated
        today or later has no seats to read, whatever the flag was left at — on
        its day the votes are still being cast or counted.
        """
        return resolved.upcoming or normalize_date(resolved.election_date) >= self._clock()

    async def _forecasts(
        self, resolved: ResolvedElection, request: ImportRequest
    ) -> list[Election]:
        """The newest polls for an upcoming election, as forecasts to choose from.

        The same reading loop as for a result, with two differences. Every page
        read may contribute several forecasts — a polling table is one poll per
        row — so reading carries on past the first good page until the page
        budget or :data:`MAX_FORECASTS` is reached. And no search engine is asked
        for more pages: the ones :mod:`app.search` is prompted to find are
        results, which is the one thing an upcoming election does not have.
        """
        candidates = await self._poll_candidates(resolved)
        if not candidates:
            raise ParseError(
                f"no page publishing polls for {_clip(resolved.describe(), 80)} could be found"
            )

        found: dict[tuple, Election] = {}
        read = 0
        problems: list[str] = []
        for url in candidates[: self._page_limit]:
            if len(found) >= MAX_FORECASTS:
                break
            forecasts, problem = await self._read_polls(url, resolved, request)
            if forecasts is None and problem is None:
                continue  # did not load; passed over as a result candidate is
            read += 1
            if problem is not None:
                problems.append(problem)
            for forecast in forecasts or ():
                # The same poll on two pages — an aggregator and the article
                # citing it — is one choice, not two.
                key = (place_token(forecast.forecast.publisher), forecast.forecast.published_on)
                found.setdefault(key, forecast)

        if not found:
            raise ParseError(_summarise_attempt(resolved, read, problems))
        newest_first = sorted(
            found.values(), key=lambda e: e.forecast.published_on, reverse=True
        )
        log.info(
            "found %s forecasts for %s", len(newest_first), _clip(resolved.describe(), 80)
        )
        return newest_first[:MAX_FORECASTS]

    async def _read_polls(self, url: str, resolved: ResolvedElection, request: ImportRequest):
        """Read one candidate for polls. Returns the forecasts, or why there were none.

        A poll that cannot be made into a valid forecast is dropped on its own —
        one row with a misread date says nothing about the other seven — and the
        page only counts as a failure when none of its polls survived.
        """
        from .fetcher import FetchError

        try:
            page = await self._fetcher.fetch(url)
        except FetchError as exc:
            log.info("skipped %s — %s", url, exc)
            return None, None

        try:
            extracted = await self._extractor.extract_forecasts(page, resolved)
        except ParseError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - one page's failure is not the import's
            log.warning("reading %s failed: %s: %s", url, type(exc).__name__, _clip(str(exc)))
            return None, READ_FAILED

        if not extracted.polls:
            return None, _no_polls_message(extracted.no_results_reason)
        # Checked before any poll is built, for the same reason as a result's
        # identity: the page said which election these are, and it is untrusted.
        if not polls_are_wanted(extracted, resolved, request):
            log.info(
                "discarded polls on %s — for %s %s, wanted %s",
                page.url, extracted.nation, extracted.election_year, resolved.describe(),
            )
            return None, _no_polls_message("wrong_election")

        forecasts: list[Election] = []
        rejected: list[str] = []
        for poll in extracted.polls[:MAX_POLLS_PER_PAGE]:
            try:
                forecasts.append(
                    forecast_election(poll, resolved, page.url, today=self._clock())
                )
            except ParseError as exc:
                rejected.append(str(exc))
        if rejected:
            log.info(
                "dropped %s of %s polls on %s — the first because %s",
                len(rejected), len(extracted.polls[:MAX_POLLS_PER_PAGE]), page.url, rejected[0],
            )
        if not forecasts:
            return None, rejected[0]
        return forecasts, None

    async def _candidates(self, resolved: ResolvedElection) -> list[str]:
        """Where to look, best first: Wikipedia, the resolver's own list, a search.

        Wikipedia goes first where it is configured, because an encyclopedia
        article is the one page that reliably *states seats*: one table, the
        election named in the lead, and the party colours in the markup. The
        resolver orders its own candidates by how official they are, which puts
        the electoral authority at the top — and electoral authorities publish
        votes, percentages, and PDFs at least as often as they publish a seat
        allocation. Those candidates are still read, in order, when the article
        turns out not to be one.

        The resolver has usually searched already, so the extra search is for the
        deployments whose search backend is an index rather than an agent — and
        for the times the resolver named an election it could not find a page for.
        """
        candidates = await self._articles(resolved)
        for url in resolved.sources:
            if url not in candidates:
                candidates.append(url)
        if len(candidates) < self._page_limit:
            for url in await self._find(resolved):
                if url not in candidates:
                    candidates.append(url)
        return candidates

    async def _articles(self, resolved: ResolvedElection) -> list[str]:
        """Ask Wikipedia for the article on this election.

        Like a failed search, a failed lookup is not a failed import: the
        resolver's candidates are read next, exactly as they would have been.
        """
        if self._wikipedia is None or self._article_limit <= 0:
            return []
        try:
            found = await self._wikipedia.find(resolved, limit=self._article_limit)
        except Exception as exc:  # noqa: BLE001 - reported as "we found nothing"
            log.warning(
                "looking %r up in Wikipedia failed: %s", _clip(resolved.describe(), 80), exc
            )
            return []
        if found:
            log.info("wikipedia found %s for %s", len(found), _clip(resolved.describe(), 80))
        return found

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
        except Exception as exc:  # noqa: BLE001 - one page's failure is not the import's
            log.warning("reading %s failed: %s: %s", url, type(exc).__name__, _clip(str(exc)))
            return None, READ_FAILED

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


def polls_are_wanted(extracted, resolved: ResolvedElection, request: ImportRequest) -> bool:
    """Whether a page's polls are for the upcoming election that was asked for.

    The same place check as :func:`is_wanted`. The year is checked only where
    the page stated one: a polling article is usually "for the next election",
    which is the one being asked about precisely because it has not been held.
    """
    return (
        (extracted.election_year is None or extracted.election_year == request.year)
        and same_place(extracted.nation, resolved.nation)
        and same_place(extracted.state, resolved.state)
    )


def forecast_election(poll, resolved: ResolvedElection, source_url: str, *, today: date) -> Election:
    """One poll as a forecast: seats as stated, or computed from vote shares.

    The identity is the resolver's — the page was only asked to confirm it — so
    every forecast of one election is filed under one name and one date, whoever
    published it. The seats are the page's, in one of two ways:

    - a poll in **seats** is taken as it is, and its seats are the assembly;
    - a poll in **percent** is allocated over the resolver's assembly size, with
      its threshold and method (:func:`app.seats.allocate`), and marked
      ``computed``. That is arithmetic on figures the page stated — not a model's
      estimate — but it is ours, and the forecast says so.
    """
    try:
        published_on = normalize_date(poll.published_on)
    except (TypeError, ValueError):
        raise ParseError("a poll's date could not be read") from None
    if published_on > today:
        raise ParseError("a poll was dated after today")
    if not poll.parties:
        raise ParseError("a poll listed no parties")

    if poll.unit == "seats":
        if any(not float(party.value).is_integer() for party in poll.parties):
            raise ParseError("a poll gave a party part of a seat")
        seats = [int(party.value) for party in poll.parties]
        computed = False
    else:
        if resolved.assembly_seats is None or resolved.seat_method is None:
            raise ParseError(
                "a poll gave only vote shares, and this assembly's seats cannot be "
                "computed from them"
            )
        try:
            seats = allocate(
                [party.value for party in poll.parties],
                resolved.assembly_seats,
                method=resolved.seat_method,
                threshold=resolved.threshold_percent or 0.0,
            )
        except ValueError as exc:
            raise ParseError(f"no seats could be computed from a poll: {exc}") from None
        computed = True

    # A party a poll gives no seat is not in the assembly it forecasts.
    won = [(party, count) for party, count in zip(poll.parties, seats) if count > 0]
    if not won:
        raise ParseError("a poll gave no party a seat")
    total = sum(count for _, count in won)
    try:
        return Election.model_validate(
            {
                "nation": resolved.nation,
                "state": resolved.state,
                "election_date": resolved.election_date,
                "title": f"{_clip(resolved.title, 120)} — {_clip(poll.publisher, 60)}",
                "source_url": source_url,
                "total_seats": total,
                "majority_seats": total // 2 + 1,
                "blocks": [
                    {
                        "name": _clip(resolved.title, 120),
                        "parties": [
                            {
                                "name": party.name,
                                "local_name": party.local_name,
                                "abbr": party.abbr,
                                "seats": count,
                                "color": party.color,
                            }
                            for party, count in won
                        ],
                    }
                ],
                "forecast": {
                    "publisher": poll.publisher,
                    "published_on": published_on.isoformat(),
                    "computed": computed,
                },
            }
        )
    except ValidationError as exc:
        raise ParseError(f"a poll is not valid: {_summarise(exc)}") from None


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
                                "local_name": party.local_name,
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


#: The same, for a page that was read for polls.
NO_POLLS_MESSAGES = {
    "wrong_election": "its polls were for a different election",
    "no_polls": "it published no polls for that election",
}

DEFAULT_NO_POLLS_MESSAGE = "no polls could be found on it"


def _no_polls_message(reason: str | None) -> str:
    return NO_POLLS_MESSAGES.get(reason, DEFAULT_NO_POLLS_MESSAGE)


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
