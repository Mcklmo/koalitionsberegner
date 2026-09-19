"""A shared link's selection, and the card that link unfurls into.

A link to a coalition is ``/e/<id>?c=<parties>&s=<seats>`` (see
``doc/plans/02-share-links.md``): ``<id>`` a prefix of the election hash, ``c``
the selected parties as positions in the order the page lists them, block by
block, and ``s`` the seat total when the link was made. Everything here is pure:
it reads the two query parameters under the same rules ``js/share.js`` applies,
and words the card that both the preview image (:mod:`app.og_image`) and the
page's ``<meta>`` tags are made from, so the two never disagree.

The card is in English whatever language the page is in: crawlers have none.

``c`` and ``s`` are the only inputs a stranger controls, so they are bounded
before anything is done with them: a selection can name no more parties than
the schema allows an election to have, and a malformed one counts as nothing
selected rather than as an error — the election still renders.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .schema import MAX_BLOCKS, MAX_PARTIES_PER_BLOCK, MAX_SEATS, Election, Party

#: How many parties a selection can name: as many as an election can hold.
MAX_SELECTION = MAX_BLOCKS * MAX_PARTIES_PER_BLOCK

#: Characters of the election hash a link carries. Lookups accept any prefix
#: from :data:`MIN_ID_LENGTH` up; 16 hex characters will not collide in a store
#: of tens of elections.
ID_LENGTH = 16
MIN_ID_LENGTH = 12
_ID = re.compile(r"[0-9a-f]{%d,64}" % MIN_ID_LENGTH)

# ASCII digits only: ``\d`` would also match Arabic-Indic and other digits,
# which ``int()`` accepts and ``js/share.js`` would not.
_INDEX = re.compile(r"[0-9]{1,%d}" % len(str(MAX_SELECTION - 1)))
_SEATS = re.compile(r"[0-9]{1,%d}" % len(str(MAX_SEATS)))

#: Longest ``c`` worth splitting: every index at its widest, plus the commas.
_MAX_C_LENGTH = MAX_SELECTION * (len(str(MAX_SELECTION - 1)) + 1)

#: How many parties the description names before it says ``+N more``.
NAMED_PARTIES = 8
#: Longest description; platforms cut ``og:description`` near here anyway.
MAX_DESCRIPTION = 200

#: Bumped whenever :mod:`app.og_image` draws differently, so a changed layout
#: is not hidden behind images cached under the old one's ETag.
IMAGE_VERSION = 1

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

Verdict = Literal["short", "majority", "large"]


def valid_id(value: str) -> bool:
    """Whether ``value`` can name an election in a link: 12 to 64 lowercase hex."""
    return _ID.fullmatch(value) is not None


def share_id(election_hash: str) -> str:
    """The id a new link carries for this election."""
    return election_hash[:ID_LENGTH]


def flatten(election: Election) -> list[Party]:
    """Every party, in the order the page lists them: block by block."""
    return [party for block in election.blocks for party in block.parties]


def parse_selection(c: str | None, election: Election) -> list[int]:
    """The selected positions ``c`` names, ascending and unique.

    Missing or empty means nothing selected. So does a malformed ``c`` — a
    token that is not a plain non-negative integer, an empty token, or more
    tokens than any election has parties — because a mangled link should still
    open the election. An index past the last party is dropped on its own.
    """
    if not c or len(c) > _MAX_C_LENGTH:
        return []
    tokens = c.split(",")
    if len(tokens) > MAX_SELECTION or not all(_INDEX.fullmatch(t) for t in tokens):
        return []
    count = len(flatten(election))
    return sorted({i for i in map(int, tokens) if i < count})


def parse_seats(s: str | None) -> int | None:
    """The seat total ``s`` claims, or None when missing or malformed."""
    if not s or not _SEATS.fullmatch(s):
        return None
    seats = int(s)
    return seats if seats <= MAX_SEATS else None


def verdict(total: int, election: Election) -> Verdict:
    """What the page would say about ``total``: short, a majority, or a large one.

    A large majority is more than two thirds of the assembly, exactly as
    ``js/app.js`` draws the line.
    """
    if total < election.majority_seats:
        return "short"
    return "large" if total > election.total_seats * 2 // 3 else "majority"


@dataclass(frozen=True)
class Card:
    """What a shared link shows before it is opened: its tags and its image."""

    title: str
    subtitle: str
    """``Final result · 25 Mar 2026``, or the forecast's publisher and date."""
    description: str
    image_path: str
    page_path: str
    total: int
    total_seats: int
    majority_seats: int
    majority: bool
    verdict: Verdict
    stale: bool
    """The link claimed a different seat total than this election now gives."""
    parties: tuple[tuple[str, int, str], ...]
    """The selected parties as ``(abbr, seats, color)``, in list order."""


def card(
    election: Election,
    election_hash: str,
    selection: list[int],
    seats_claimed: int | None,
) -> Card:
    """The card for this election with these parties selected.

    ``selection`` is what :func:`parse_selection` returned; it is normalised
    again here so a caller cannot produce a card that disagrees with its link.
    """
    parties = flatten(election)
    indices = sorted({i for i in selection if 0 <= i < len(parties)})
    chosen = [parties[i] for i in indices]
    total = sum(p.seats for p in chosen)
    query = _query(indices, total)
    ident = share_id(election_hash)
    kind = verdict(total, election)
    return Card(
        title=election.title,
        subtitle=" · ".join(_source(election)),
        description=_description(election, chosen, total, kind),
        image_path=f"/api/og/{ident}.png{query}",
        page_path=f"/e/{ident}{query}",
        total=total,
        total_seats=election.total_seats,
        majority_seats=election.majority_seats,
        majority=kind != "short",
        verdict=kind,
        stale=seats_claimed is not None and seats_claimed != total,
        parties=tuple((p.abbr, p.seats, p.color) for p in chosen),
    )


def image_etag(election_hash: str, selection: list[int], seats_claimed: int | None,
               stored_at: float) -> str:
    """A strong ETag for the preview image of this link.

    Built from what the image depends on: the full hash (an id prefix can
    resolve to a different election later), the selection as parsed (so
    ``c=2,0`` and ``c=0,2`` share it), the claimed seats, the moment the
    election was stored (a correction replaces it) and the layout version.
    """
    key = "|".join((
        election_hash,
        ",".join(map(str, selection)),
        "" if seats_claimed is None else str(seats_claimed),
        repr(stored_at),
        str(IMAGE_VERSION),
    ))
    return '"' + hashlib.sha256(key.encode()).hexdigest()[:32] + '"'


def format_date(day: date) -> str:
    """``25 Mar 2026``: English, and the same wherever the server runs."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _query(indices: list[int], total: int) -> str:
    if not indices:
        return ""
    return f"?c={','.join(map(str, indices))}&s={total}"


def _source(election: Election) -> list[str]:
    """Where the numbers come from, as the parts of one line."""
    forecast = election.forecast
    if forecast is None:
        return ["Final result", format_date(election.election_date)]
    parts = ["Forecast", forecast.publisher, format_date(forecast.published_on)]
    if forecast.computed:
        parts.append("seats computed")
    return parts


def _description(election: Election, chosen: list[Party], total: int, kind: Verdict) -> str:
    # "Final result, 25 Mar 2026." or "Forecast: Voxmeter, 12 Sep 2026, seats computed."
    head, *rest = _source(election)
    source_sentence = f"{head}{', ' if election.forecast is None else ': '}{', '.join(rest)}."
    if not chosen:
        return _fit(
            f"Pick parties and see whether they reach the {election.majority_seats} seats "
            f"a majority needs. {source_sentence}"
        )

    if kind == "short":
        outcome = f"{election.majority_seats - total} short of a majority"
    else:
        size = "a large majority" if kind == "large" else "a majority"
        outcome = f"{size} (+{total - election.majority_seats})"
    tail = f": {total} of {election.total_seats} seats, {outcome}. {source_sentence}"

    # Name up to eight parties; name fewer when long abbreviations would push
    # the line past the limit, and cut it only when even one is too long.
    for named in range(min(NAMED_PARTIES, len(chosen)), 0, -1):
        names = " + ".join(p.abbr for p in chosen[:named])
        if named < len(chosen):
            names += f" +{len(chosen) - named} more"
        text = names + tail
        if len(text) <= MAX_DESCRIPTION:
            return text
    return _fit(text)


def _fit(text: str) -> str:
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 1].rstrip() + "…"
