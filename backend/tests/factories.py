"""Shared fixtures: a valid election and a parser whose timing tests control."""

from __future__ import annotations

import asyncio

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


def make_request(**overrides) -> ImportRequest:
    """An import request is now just a URL; identity comes from the agent."""
    data = {"source_url": "https://www.dst.dk/valg"}
    # Identity kwargs are accepted so a caller can vary what the agent will infer.
    data.update({k: v for k, v in overrides.items() if k == "source_url"})
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
                 infers: Election | None = None, by_url: dict[str, Election] | None = None):
        self._fixed = election
        self._inferred = infers or make_election()
        self._by_url = by_url or {}
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
            raise RuntimeError("extraction failed")
        if request.source_url in self._by_url:
            return self._by_url[request.source_url]
        if self._fixed is not None:
            return self._fixed
        return self._inferred
