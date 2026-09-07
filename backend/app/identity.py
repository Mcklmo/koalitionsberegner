"""Election identity: one election, one hash, no duplicate variations.

An election is identified by *nation + state + election date*. ``state`` is set
only for regional elections; a national election carries ``None``. Inputs are
normalised first so that "Danmark", " danmark " and "DANMARK" collapse to one
identity, while a national and a regional election on the same date stay
distinct.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime

# Bump when the hashed representation changes, so old and new ids never collide.
HASH_VERSION = "v1"

_WHITESPACE = re.compile(r"\s+")


def normalize_place(value: str | None) -> str | None:
    """Case-fold, NFKC-normalise and collapse whitespace; empty becomes ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("place must be a string or None")
    text = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()
    return text or None


def normalize_date(value: str | date | datetime) -> date:
    """Coerce to a calendar day; time of day never affects identity."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError:
            raise ValueError(f"election_date must be an ISO 8601 date, got {value!r}") from None
    raise TypeError("election_date must be an ISO 8601 date")


def election_hash(
    nation: str, state: str | None, election_date: str | date | datetime
) -> str:
    """Return the stable identity hash for an election.

    The hashed payload is JSON so that field boundaries cannot be forged by a
    value containing the separator: ``nation="a", state="b"`` and
    ``nation="a|b", state=None`` produce different digests.
    """
    normalized_nation = normalize_place(nation)
    if normalized_nation is None:
        raise ValueError("nation must not be empty")
    payload = json.dumps(
        {
            "version": HASH_VERSION,
            "nation": normalized_nation,
            "state": normalize_place(state),
            "election_date": normalize_date(election_date).isoformat(),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
