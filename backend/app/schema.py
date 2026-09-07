"""Canonical election schema — the Python mirror of ``js/election.js``.

Same contract, same rules: allowlisted fields only, seats summing to the
declared total, a majority consistent with the assembly size, real dates,
absolute http(s) URLs and hex-only colours. Anything a parser produces must
pass through here before it is stored or served.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TEXT = 200
MAX_BLOCKS = 50
MAX_PARTIES_PER_BLOCK = 200
MAX_SEATS = 100_000

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value: str, *, field: str) -> str:
    """Normalise and reject text that has no business reaching a browser."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{field} must not contain control characters")
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > MAX_TEXT:
        raise ValueError(f"{field} must be at most {MAX_TEXT} characters")
    return text


class Strict(BaseModel):
    """Base for every schema model.

    ``extra="forbid"`` makes unknown fields an error rather than something to
    ignore, and ``strict=True`` stops Pydantic coercing ``"10"`` into ``10`` —
    both matching what ``js/election.js`` enforces on the client.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Party(Strict):
    name: str
    abbr: str
    seats: int = Field(ge=0, le=MAX_SEATS)
    color: str

    @field_validator("name", "abbr")
    @classmethod
    def _text(cls, v: str, info) -> str:
        return clean_text(v, field=info.field_name)

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not isinstance(v, str) or not HEX_COLOR.match(v):
            raise ValueError('color must be a hex colour like "#1a2b3c"')
        return v


class Block(Strict):
    name: str
    parties: list[Party] = Field(min_length=1, max_length=MAX_PARTIES_PER_BLOCK)

    @field_validator("name")
    @classmethod
    def _text(cls, v: str) -> str:
        return clean_text(v, field="name")


class Election(Strict):
    """A validated election, ready to store and to render."""

    nation: str
    state: str | None = None
    election_date: date
    title: str
    source_url: str
    total_seats: int = Field(ge=1, le=MAX_SEATS)
    majority_seats: int = Field(ge=1, le=MAX_SEATS)
    blocks: list[Block] = Field(min_length=1, max_length=MAX_BLOCKS)

    @field_validator("nation", "title")
    @classmethod
    def _text(cls, v: str, info) -> str:
        return clean_text(v, field=info.field_name)

    @field_validator("state")
    @classmethod
    def _optional_text(cls, v: str | None) -> str | None:
        return None if v is None else clean_text(v, field="state")

    @field_validator("election_date", mode="before")
    @classmethod
    def _date(cls, v):
        # Accept ISO dates and date-times; identity only ever depends on the day.
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v).date()
            except ValueError:
                raise ValueError(f"election_date must be an ISO 8601 date, got {v!r}") from None
        raise ValueError("election_date must be an ISO 8601 date")

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("source_url must be a string")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"source_url must use http or https, got {parsed.scheme or 'no scheme'!r}"
            )
        if not parsed.netloc:
            raise ValueError(f"source_url is not a well-formed URL: {v!r}")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "Election":
        # A majority is more than half the assembly and no more than all of it.
        if not (self.total_seats / 2 < self.majority_seats <= self.total_seats):
            raise ValueError(
                f"majority_seats {self.majority_seats} is not a majority of "
                f"{self.total_seats} seats (expected {self.total_seats // 2 + 1} "
                f"to {self.total_seats})"
            )
        seat_sum = sum(p.seats for b in self.blocks for p in b.parties)
        if seat_sum != self.total_seats:
            raise ValueError(
                f"party seats sum to {seat_sum}, but total_seats is {self.total_seats}"
            )
        return self
