"""Shared fixtures: a valid election and a parser whose timing tests control."""

from __future__ import annotations

import asyncio

from app.parser import ParseError
from app.schema import Election
from app.store import ImportRequest


def make_election(**overrides) -> Election:
    data = {
        "nation": "Danmark",
        "state": None,
        "election_date": "2026-03-25",
        "title": "Koalitionsberegner",
        "source_url": "https://www.dst.dk/valg",
        "total_seats": 10,
        "majority_seats": 6,
        "blocks": [
            {"name": "Left", "parties": [{"name": "Left Party", "abbr": "L", "seats": 6, "color": "#C0392B"}]},
            {"name": "Right", "parties": [{"name": "Right Party", "abbr": "R", "seats": 4, "color": "#2980B9"}]},
        ],
    }
    data.update(overrides)
    return Election.model_validate(data)


def make_forecast(
    publisher: str = "Voxmeter", published_on: str = "2026-09-07", computed: bool = False,
    **overrides,
) -> Election:
    """One poll of an election not yet held, as the parser turns it into an election."""
    data = {
        "election_date": "2027-10-31",
        "title": f"Next Danish general election — {publisher}",
        "forecast": {"publisher": publisher, "published_on": published_on, "computed": computed},
    }
    data.update(overrides)
    return make_election(**data)


def make_request(**overrides) -> ImportRequest:
    """An import request: a year and a place, spelled however the user spelled it."""
    data = {"year": 2026, "nation": "Danmark", "subnation": None}
    data.update(overrides)
    return ImportRequest(**data)


async def wait_until(predicate, timeout: float = 2.0) -> None:
    """Yield to the event loop until ``predicate`` holds, so tests never race
    a background task that has been created but not yet scheduled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(0.01)


class CountingParser:
    """Records every call and can be held open to simulate a slow parse."""

    def __init__(self, election: Election | None = None, *, fail_times: int = 0,
                 infers: Election | None = None, by_year: dict[int, Election] | None = None):
        self._fixed = election
        self._inferred = infers or make_election()
        self._by_year = by_year or {}
        self.election = election or self._inferred
        self.calls: list[ImportRequest] = []
        self.fail_times = fail_times
        self.gate = asyncio.Event()
        self.gate.set()
        self.started = asyncio.Event()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def hold(self) -> None:
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()

    async def parse(self, request: ImportRequest) -> Election:
        self.calls.append(request)
        self.started.set()
        await self.gate.wait()
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ParseError("extraction failed")
        if request.year in self._by_year:
            return self._by_year[request.year]
        if self._fixed is not None:
            return self._fixed
        return self._inferred


class FixedExtractor:
    """An extraction agent that returns one answer, identity included.

    Unlike ``MockExtractor``, it does not echo the requested election back —
    which is what makes it the tool for testing what happens when a page
    reports something other than what was asked for.
    """

    def __init__(self, result):
        self.result = result
        self.pages: list = []

    async def extract(self, page, wanted):
        self.pages.append(page)
        return self.result
