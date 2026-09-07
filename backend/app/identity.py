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
from urllib.parse import urlparse, urlunparse

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


def normalize_source_url(url: str) -> str:
    """Canonical form of a results URL, for keying work by page.

    Only differences that cannot change what is served are normalised: scheme
    and host casing, the default port, a trailing slash, and the fragment.
    Query strings are kept — they routinely select which election a page shows.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("source_url must not be empty")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"source_url must use http or https, got {parsed.scheme or 'no scheme'!r}")
    if not parsed.hostname:
        raise ValueError(f"source_url is not a well-formed URL: {url!r}")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    default_port = 443 if scheme == "https" else 80
    netloc = host if parsed.port in (None, default_port) else f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def source_url_key(url: str) -> str:
    """Identifier for the *page*, used to key extraction work.

    Distinct from :func:`election_hash`, which identifies the *election*. The
    election's identity is not known until the page has been read, so the two
    cannot be the same key.
    """
    payload = json.dumps(
        {"version": HASH_VERSION, "source_url": normalize_source_url(url)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
