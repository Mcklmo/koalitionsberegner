"""The parsing seam.

Turning a results URL into a validated election is a separate concern (and a
separate issue); the store only needs to know that *something* can do it. The
default implementation refuses, so a deployment without a configured parser
fails loudly instead of silently storing nothing.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from .schema import Election
from .store import ImportRequest


class ParseError(RuntimeError):
    """Raised when a results URL cannot be turned into a valid election."""


class ElectionParser(Protocol):
    async def parse(self, request: ImportRequest) -> Election:
        """Fetch and extract ``request.source_url`` into a validated election."""
        ...


class UnavailableParser:
    """Placeholder parser: every import fails until a real one is injected."""

    async def parse(self, request: ImportRequest) -> Election:
        raise ParseError("no election parser is configured")


class LlmElectionParser:
    """Fetch the page, extract it with the agent, validate the result.

    The three steps are separate objects so the agent can be swapped for a mock
    without changing the pipeline the real thing runs through.
    """

    def __init__(self, fetcher, extractor):
        self._fetcher = fetcher
        self._extractor = extractor

    async def parse(self, request: ImportRequest) -> Election:
        from .fetcher import FetchError

        try:
            page = await self._fetcher.fetch(request.source_url)
        except FetchError as exc:
            raise ParseError(str(exc)) from None

        try:
            extracted = await self._extractor.extract(page.text, request)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, never rendered
            raise ParseError(f"extraction failed: {exc}") from None

        if not any(block.parties for block in extracted.blocks):
            raise ParseError("no election results could be found on that page")

        # Identity now comes from the agent, so it is validated like any other
        # extracted field and shown to the user before anything is stored.
        try:
            return Election.model_validate(
                {
                    "nation": extracted.nation,
                    "state": extracted.state,
                    "election_date": extracted.election_date,
                    "title": extracted.title,
                    "source_url": request.source_url,
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


def _summarise(error: ValidationError, limit: int = 3) -> str:
    problems = [
        (".".join(str(p) for p in item["loc"]) or "election") + ": " + item["msg"]
        for item in error.errors()[:limit]
    ]
    more = len(error.errors()) - len(problems)
    return "; ".join(problems) + (f" (and {more} more)" if more > 0 else "")
