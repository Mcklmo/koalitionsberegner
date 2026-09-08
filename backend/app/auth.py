"""Who is calling: Firebase ID token verification.

The browser signs in against Firebase and sends the resulting ID token as a
bearer token; this module turns that token into a :class:`Principal` or refuses
it. Nothing downstream ever reads an identity out of the request body, so a
caller cannot claim to be somebody else by saying so.

Verification is done locally against Google's public signing certificates
rather than by calling an identity service per request. The certificates are
cached for as long as Google's ``Cache-Control`` says they are good for, so a
verified request costs no network at all.

``AUTH_MODE`` picks the verifier — see :func:`app.config.get_verifier`. The
``off`` mode used for local runs has no notion of a visitor: every request is
the developer, unlimited and admin, which is why gating is invisible until it
is switched on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from .observability import io_span, scrub

log = logging.getLogger(__name__)

#: Google's x509 certificates for the Secure Token Service that signs ID tokens.
FIREBASE_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)

#: Floor and ceiling on how long fetched certificates are trusted.
MIN_CERT_TTL_SECONDS = 300.0
MAX_CERT_TTL_SECONDS = 24 * 3600.0

#: Tolerated clock difference between this container and Google's signer.
CLOCK_SKEW_SECONDS = 30

#: How google-auth words "I have no certificate for this token's key id". A
#: rotation, not a forgery — worth one refetch before the token is refused.
UNKNOWN_KEY_MARKERS = ("key id", "kid")


class InvalidToken(Exception):
    """The credential presented is missing, malformed, expired or not ours."""


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. Anonymous visitors are represented by ``None``."""

    uid: str
    email: str | None = None
    admin: bool = False
    unlimited: bool = False
    """Set only when auth is switched off, so local runs are never quota-limited."""


def bearer_token(header: str | None) -> str | None:
    """The token out of an ``Authorization: Bearer …`` header, if there is one."""
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal:
        """Return who signed this token, or raise :class:`InvalidToken`."""
        ...

    def anonymous(self) -> Principal | None:
        """Who a request carrying no credentials is. ``None`` means a visitor."""
        ...


class _CertificateCache:
    """Google's signing certificates, refetched only when they expire."""

    def __init__(self, url: str, *, fetch=None, clock=time.monotonic):
        self._url = url
        self._fetch = fetch or self._http_get
        self._clock = clock
        self._certs: dict[str, str] = {}
        self._expires_at = 0.0

    @staticmethod
    def _http_get(url: str) -> tuple[dict[str, str], float]:
        import httpx

        with io_span(log, "firebase", "fetch_certs") as span:
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
            certs = response.json()
            span["count"] = len(certs)
            return certs, _max_age(response.headers.get("cache-control"))

    def certs(self, *, force: bool = False) -> dict[str, str]:
        if force or not self._certs or self._clock() >= self._expires_at:
            certs, ttl = self._fetch(self._url)
            self._certs = certs
            self._expires_at = self._clock() + min(
                max(ttl, MIN_CERT_TTL_SECONDS), MAX_CERT_TTL_SECONDS
            )
        return self._certs


def _max_age(cache_control: str | None) -> float:
    """Seconds from a ``Cache-Control`` header; the floor when it says nothing."""
    for part in (cache_control or "").split(","):
        key, _, value = part.strip().partition("=")
        if key.lower() == "max-age":
            try:
                return float(value)
            except ValueError:
                break
    return MIN_CERT_TTL_SECONDS


class FirebaseTokenVerifier:
    """Verifies Firebase ID tokens for one project.

    Three things make a token ours: it is signed by a current Google
    certificate, its audience is this project, and its issuer is this project's
    Secure Token Service. Checking only the signature would accept a valid
    token minted for a *different* Firebase project.
    """

    def __init__(
        self,
        project_id: str,
        *,
        admin_emails: frozenset[str] = frozenset(),
        certs_url: str = FIREBASE_CERTS_URL,
        certs=None,
    ):
        if not project_id:
            raise ValueError("Firebase verification needs a project id")
        self._project_id = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._admin_emails = frozenset(e.strip().lower() for e in admin_emails if e.strip())
        self._certs = certs or _CertificateCache(certs_url)

    def anonymous(self) -> Principal | None:
        return None

    def _claims(self, token: str, *, force_refresh: bool) -> dict:
        from google.auth import jwt

        return jwt.decode(
            token,
            certs=self._certs.certs(force=force_refresh),
            audience=self._project_id,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )

    def verify(self, token: str) -> Principal:
        try:
            claims = self._claims(token, force_refresh=False)
        except ValueError as exc:
            # Google rotates signing keys; an unknown key id means our cache is
            # stale, not that the token is forged. Refetch once before refusing.
            message = str(exc).lower()
            if not any(marker in message for marker in UNKNOWN_KEY_MARKERS):
                raise InvalidToken(str(exc)) from None
            try:
                claims = self._claims(token, force_refresh=True)
            except ValueError as retry_exc:
                raise InvalidToken(str(retry_exc)) from None
        except Exception as exc:  # noqa: BLE001 - a cert fetch failure must not 500
            log.warning("token verification unavailable: %s", scrub(exc))
            raise InvalidToken("could not verify the credential") from None

        if claims.get("iss") != self._issuer:
            raise InvalidToken("token was issued for a different project")
        uid = claims.get("sub") or ""
        if not isinstance(uid, str) or not uid.strip():
            raise InvalidToken("token carries no subject")

        email = claims.get("email")
        email = email.strip().lower() if isinstance(email, str) and email.strip() else None
        return Principal(
            uid=uid.strip(),
            email=email,
            # A custom claim set out of band, or a configured allowlist. Never
            # anything the user can set on themselves.
            admin=bool(claims.get("admin")) or (email is not None and email in self._admin_emails),
        )


class StubTokenVerifier:
    """``AUTH_MODE=stub``: the token *is* the identity, no signature involved.

    For exercising the gated behaviour locally and in tests without a Firebase
    project. A token is ``uid``, ``uid:email`` or ``uid:email:admin``. Refusing
    to run this against a real project is the point of :func:`app.config.get_verifier`
    keeping it out of the deployed default.
    """

    def anonymous(self) -> Principal | None:
        return None

    def verify(self, token: str) -> Principal:
        uid, _, rest = token.partition(":")
        if not uid.strip():
            raise InvalidToken("stub token must start with a uid")
        email, _, role = rest.partition(":")
        return Principal(
            uid=uid.strip(),
            email=email.strip().lower() or None,
            admin=role.strip() == "admin",
        )


#: The identity every request carries when auth is switched off.
LOCAL_PRINCIPAL = Principal(
    uid="local-developer", email="local@localhost", admin=True, unlimited=True
)


class DisabledVerifier:
    """``AUTH_MODE=off``: no gating at all, for running the app without Firebase.

    Every request — with or without a header — is the local developer, so the
    app behaves exactly as it did before accounts existed.
    """

    def anonymous(self) -> Principal | None:
        return LOCAL_PRINCIPAL

    def verify(self, token: str) -> Principal:
        return LOCAL_PRINCIPAL
