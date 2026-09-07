"""The parsing seam.

Turning a results URL into a validated election is a separate concern (and a
separate issue); the store only needs to know that *something* can do it. The
default implementation refuses, so a deployment without a configured parser
fails loudly instead of silently storing nothing.
"""

from __future__ import annotations

from typing import Protocol

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
