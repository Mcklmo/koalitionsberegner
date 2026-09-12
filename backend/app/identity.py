"""Election identity: one election, one hash, no duplicate variations.

An election is identified by *nation + state + election date*. ``state`` is set
only for regional elections; a national election carries ``None``. Inputs are
normalised first so that "Danmark", " danmark " and "DANMARK" collapse to one
identity, while a national and a regional election on the same date stay
distinct.

The other key here is the *request* key: what a user asked for — a year, a
nation, and optionally the region within it. It is not the same thing as an
election's identity, because the exact date of the election is not known until
the results have been found. See :func:`request_key`.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime

# Bump when the hashed representation changes, so old and new ids never collide.
HASH_VERSION = "v1"

#: The years an election may be asked for. Deliberately wide and fixed: which
#: years actually held an election is not something this module can know, and a
#: range that moved with the clock would make the key unstable.
MIN_YEAR = 1800
MAX_YEAR = 2100

_WHITESPACE = re.compile(r"\s+")
#: Characters that are not part of a place name, for comparison purposes only.
_NOT_ALNUM = re.compile(r"[^0-9a-z]+")
#: Slips a hand makes on a number row: a letter that is really a digit.
_YEAR_TYPOS = str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1", "i": "1"})


def normalize_place(value: str | None) -> str | None:
    """Case-fold, NFKC-normalise and collapse whitespace; empty becomes ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("place must be a string or None")
    text = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()
    return text or None


def place_token(value: str | None) -> str | None:
    """A place name reduced to letters and digits, for *comparing* two names.

    Deliberately blunter than :func:`normalize_place`: accents, spaces, hyphens
    and full stops all disappear, so "Saxony-Anhalt", "saxony anhalt" and
    "SaxonyAnhalt" are one token. Used to check that the results we found are
    the place that was asked for — never to build an identity, because it
    throws away differences a real name may depend on.
    """
    normalized = normalize_place(value)
    if normalized is None:
        return None
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", normalized) if not unicodedata.combining(ch)
    )
    token = _NOT_ALNUM.sub("", stripped)
    return token or None


def same_place(left: str | None, right: str | None) -> bool:
    """Whether two names, from two different sources, mean the same place.

    ``None`` is a place too — it means "no region, this was a national
    election" — so two ``None``s match and a ``None`` never matches a name.
    """
    return place_token(left) == place_token(right)


def normalize_year(value: int | str) -> int:
    """The year as a number, forgiving the way a year gets mistyped.

    A digit row is easy to miss: ``"2o26"`` and ``"2026 "`` are meant as 2026,
    and reading them that way is cheaper than a round trip that says "not a
    number". Anything still not a plausible year is refused — guessing at
    ``"20226"`` would only produce an import for an election that never was.
    """
    if isinstance(value, bool):  # bool is an int; a year it is not
        raise TypeError("year must be a number")
    if isinstance(value, int):
        year = value
    elif isinstance(value, str):
        text = value.strip().translate(_YEAR_TYPOS)
        if not text.isdigit():
            raise ValueError(f"year must be a four-digit year, got {value!r}")
        year = int(text)
    else:
        raise TypeError("year must be a number")
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}, got {year}")
    return year


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


def request_key(year: int | str, nation: str, subnation: str | None = None) -> str:
    """Identifier for *what was asked for*, used to key the work of answering it.

    Distinct from :func:`election_hash`, which identifies the election that was
    found: the day it was held is part of that identity and is not known until
    the results have been read.

    Two people who type the same thing share one import, and one who mistypes
    the nation gets their own — which costs an extra resolution, not an extra
    stored election, because a request that resolves to an election already held
    is linked to it rather than stored again.
    """
    normalized_nation = normalize_place(nation)
    if normalized_nation is None:
        raise ValueError("nation must not be empty")
    payload = json.dumps(
        {
            "version": HASH_VERSION,
            "year": normalize_year(year),
            "nation": normalized_nation,
            "subnation": normalize_place(subnation),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
