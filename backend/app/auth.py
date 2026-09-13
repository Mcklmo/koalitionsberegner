"""Who is calling: turning a bearer token back into a :class:`Principal`.

Two things happen to every credential, and they are deliberately separate:

* a **credential store** says which account a token belongs to
  (:class:`CredentialStore`) — that is where the mechanism lives, whether it is
  a Firebase ID token verified against Google's certificates or a session row
  in SQLite;
* the **rules** then decide what that account may claim
  (:class:`PrincipalRules`) — a subject is mandatory, an email is folded so
  allowlists match, and administrator-ness comes from the token or from
  ``ADMIN_EMAILS``, never from anything the user can set on themselves.

:class:`StoreBackedVerifier` is the only thing that joins them, so a new store
cannot accidentally bring its own interpretation of the rules with it, and the
rules cannot grow a dependency on any one store. Nothing downstream ever reads
an identity out of the request body, so a caller cannot claim to be somebody
else by saying so.

``AUTH_MODE`` picks the store — see :func:`app.config.get_verifier`. The ``off``
mode used for local runs has no notion of a visitor: every request is the
developer, unlimited and admin, which is why gating is invisible until it is
switched on.
"""

from __future__ import annotations

import logging
import threading
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
    email_verified: bool = False
    """Whether the address is known to be the caller's, not just typed by them.

    False unless something says otherwise, so a principal built somewhere new
    trusts nothing about its address. :class:`PrincipalRules` is what sets it.
    """

    @property
    def unmetered(self) -> bool:
        """Imports cost this caller nothing: gating is off, or they administer the app."""
        return self.unlimited or self.admin


@dataclass(frozen=True)
class Credential:
    """What a store found behind a token, before any rule has been applied.

    Deliberately dumb: a subject, whatever the store knows about the address,
    and whether the *store itself* says this is an administrator (a Firebase
    custom claim; a local store has no such notion). Everything else — folding,
    validation, the admin allowlist — belongs to :class:`PrincipalRules`.
    """

    subject: str
    email: str | None = None
    admin: bool = False
    email_verified: bool | None = None
    """``True`` confirmed, ``False`` not yet, ``None`` when the store has no way
    of confirming an address at all."""


def bearer_token(header: str | None) -> str | None:
    """The token out of an ``Authorization: Bearer …`` header, if there is one."""
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def normalize_email(value: object) -> str | None:
    """Fold an address so an allowlist comparison means what it looks like."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


class CredentialStore(Protocol):
    """Where a token is turned back into the account it was issued to.

    The one thing every store must get right: raise :class:`InvalidToken` for
    anything it cannot vouch for. Returning a :class:`Credential` is a claim
    that this token really was issued to this subject.
    """

    #: Which sign-in flow the browser should run: ``firebase``, ``password`` or
    #: ``none`` when the page cannot obtain a token by itself.
    provider: str

    def resolve(self, token: str) -> Credential: ...


class TokenVerifier(Protocol):
    provider: str

    def verify(self, token: str) -> Principal:
        """Return who signed this token, or raise :class:`InvalidToken`."""
        ...

    def anonymous(self) -> Principal | None:
        """Who a request carrying no credentials is. ``None`` means a visitor."""
        ...


@dataclass(frozen=True)
class PrincipalRules:
    """What a resolved credential is allowed to claim. One copy, every store.

    Administrator-ness has exactly two sources: a claim the store vouches for
    (set out of band — a Firebase custom claim is not something a user can mint
    for themselves) and a configured allowlist of addresses. A store cannot
    grant it by returning a different shape of credential.
    """

    admin_emails: frozenset[str] = frozenset()

    @classmethod
    def of(cls, admin_emails: frozenset[str] = frozenset()) -> "PrincipalRules":
        return cls(frozenset(e.strip().lower() for e in admin_emails if e.strip()))

    def principal(self, credential: Credential) -> Principal:
        uid = credential.subject.strip() if isinstance(credential.subject, str) else ""
        if not uid:
            raise InvalidToken("token carries no subject")
        email = normalize_email(credential.email)
        # A store that cannot confirm addresses (SQLite, stub) never promised
        # to, and the docs say so; one that can and has not is taken at its word.
        verified = credential.email_verified is not False
        return Principal(
            uid=uid,
            email=email,
            # The allowlist names addresses, so it may only match one known to be
            # the caller's — otherwise signing up *as* the admin address would be
            # enough to become the admin.
            admin=credential.admin
            or (verified and email is not None and email in self.admin_emails),
            email_verified=verified,
        )


class StoreBackedVerifier:
    """A :class:`TokenVerifier` assembled from a store and the shared rules.

    The whole point of this class is that it is boring: everything mechanism-
    specific is in the injected store, everything policy-specific is in the
    injected rules, and swapping Firebase for SQLite changes neither.
    """

    def __init__(self, store: CredentialStore, rules: PrincipalRules | None = None):
        self._store = store
        self._rules = rules or PrincipalRules()

    @property
    def provider(self) -> str:
        return self._store.provider

    @property
    def store(self) -> CredentialStore:
        return self._store

    def anonymous(self) -> Principal | None:
        # Every real store gates: no credential means a signed-out visitor.
        return None

    def verify(self, token: str) -> Principal:
        return self._rules.principal(self._store.resolve(token))


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


class FirebaseCredentials:
    """``AUTH_MODE=firebase``: Firebase ID tokens, verified for one project.

    Verification is done locally against Google's public signing certificates
    rather than by calling an identity service per request. The certificates are
    cached for as long as Google's ``Cache-Control`` says they are good for, so
    a verified request costs no network at all.

    Three things make a token ours: it is signed by a current Google
    certificate, its audience is this project, and its issuer is this project's
    Secure Token Service. Checking only the signature would accept a valid
    token minted for a *different* Firebase project.
    """

    provider = "firebase"

    def __init__(self, project_id: str, *, certs_url: str = FIREBASE_CERTS_URL, certs=None):
        if not project_id:
            raise ValueError("Firebase verification needs a project id")
        self._project_id = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._certs = certs or _CertificateCache(certs_url)

    def _claims(self, token: str, *, force_refresh: bool) -> dict:
        from google.auth import jwt

        return jwt.decode(
            token,
            certs=self._certs.certs(force=force_refresh),
            audience=self._project_id,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )

    def resolve(self, token: str) -> Credential:
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
        subject = claims.get("sub")
        return Credential(
            subject=subject if isinstance(subject, str) else "",
            email=claims.get("email"),
            # A custom claim set out of band. Never anything the user can set
            # on themselves.
            admin=bool(claims.get("admin")),
            # Set by Firebase once the user has opened the link it mailed them.
            # Anything but a literal true, absent included, is not confirmed.
            email_verified=claims.get("email_verified") is True,
        )


class StubCredentials:
    """``AUTH_MODE=stub``: the token *is* the identity, no signature involved.

    For exercising the gated behaviour locally and in tests without a Firebase
    project. A token is ``uid``, ``uid:email``, ``uid:email:admin`` or
    ``uid:email:unverified`` — the last one a Firebase account whose address has
    not been confirmed yet. Refusing to run this against a real project is the
    point of :func:`app.config.get_verifier` keeping it out of the deployed default.
    """

    #: The browser cannot mint these, so the page offers no sign-in flow.
    provider = "none"

    def resolve(self, token: str) -> Credential:
        uid, _, rest = token.partition(":")
        if not uid.strip():
            raise InvalidToken("stub token must start with a uid")
        email, _, role = rest.partition(":")
        role = role.strip()
        return Credential(
            subject=uid,
            email=email,
            admin=role == "admin",
            email_verified=False if role == "unverified" else None,
        )


#: The identity every request carries when auth is switched off.
LOCAL_PRINCIPAL = Principal(
    uid="local-developer",
    email="local@localhost",
    admin=True,
    unlimited=True,
    email_verified=True,
)


class DisabledVerifier:
    """``AUTH_MODE=off``: no gating at all, for running the app without Firebase.

    Every request — with or without a header — is the local developer, so the
    app behaves exactly as it did before accounts existed. It is a verifier
    rather than a store because it never looks at the token: there is nothing
    to resolve and no rule to apply.
    """

    provider = "none"

    def anonymous(self) -> Principal | None:
        return LOCAL_PRINCIPAL

    def verify(self, token: str) -> Principal:
        return LOCAL_PRINCIPAL


# --- stores that hold passwords themselves ----------------------------------
#
# Firebase keeps the passwords and hands the browser a token; a self-hosted run
# has nowhere to send the user, so the store below issues its own sessions.
# Everything a *caller* needs is declared here rather than in the SQLite module,
# so the API layer never imports one particular implementation.

#: Exactly what Firebase enforces, so the page can state one rule and be right
#: in either mode rather than guessing which backend is behind it.
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 1024


class SignUpRefused(Exception):
    """The address or the password is not something we will accept."""


class EmailTaken(SignUpRefused):
    """Somebody already registered that address."""


class BadCredentials(Exception):
    """That address and password pair does not open anything."""


@dataclass(frozen=True)
class Session:
    """A freshly issued session. ``token`` is shown once and never stored raw."""

    token: str
    uid: str
    email: str
    expires_at: float


class PasswordCredentialStore(Protocol):
    """A :class:`CredentialStore` that also registers and signs people in."""

    provider: str

    def resolve(self, token: str) -> Credential: ...

    def register(self, email: str, password: str) -> Session:
        """Create an account and sign it in. Raises :class:`SignUpRefused`."""
        ...

    def sign_in(self, email: str, password: str) -> Session:
        """Open a session. Raises :class:`BadCredentials`."""
        ...

    def sign_out(self, token: str) -> None:
        """End this session. Unknown tokens are not an error — they are gone."""
        ...

    def remove(self, uid: str) -> None:
        """Delete the user and its sessions. This store is also the :class:`IdentityRemover`."""
        ...


def checked_signup(email: str, password: str) -> tuple[str, str]:
    """The rules a new account must satisfy, wherever it is being stored.

    Here rather than in the store for the same reason :class:`PrincipalRules`
    is: a second implementation must not get to have a different opinion about
    what an acceptable password is.
    """
    address = normalize_email(email)
    if address is None or "@" not in address or "." not in address.rpartition("@")[2]:
        raise SignUpRefused("that does not look like an email address")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SignUpRefused(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Not a policy — a hash of unbounded input is a free way to burn CPU.
        raise SignUpRefused("that password is unreasonably long")
    return address, password


# --- deleting a sign-in ------------------------------------------------------
#
# When an account is deleted for being unused (see app.retention), deleting the
# account record is not enough. The sign-in behind it, with its email address,
# is personal data too, and while it exists signing in would create a new
# account. The seam is declared here, beside the stores that verify sign-ins,
# for the same reason PasswordCredentialStore is: the caller never imports a
# particular implementation.

#: Identity Toolkit's admin endpoint for deleting a user. firebase-admin's
#: ``auth.delete_user`` calls this same endpoint.
FIREBASE_DELETE_USER_URL = (
    "https://identitytoolkit.googleapis.com/v1/projects/{project}/accounts:delete"
)

#: The OAuth scope the service account's token is requested with.
FIREBASE_ADMIN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: How Identity Toolkit words "there is no such user".
USER_NOT_FOUND = "USER_NOT_FOUND"


class IdentityRemovalFailed(Exception):
    """The sign-in is still there. Nothing is lost by trying again later."""


class IdentityRemover(Protocol):
    """Deletes the sign-in belonging to a uid.

    A sign-in that is already gone counts as deleted. Retention retries a
    failed removal on the next run, and a retry after an earlier success, or
    after someone deleted the user by hand, must not count as a failure.
    """

    def remove(self, uid: str) -> None:
        """Raise when the sign-in could not be deleted."""
        ...


class NoIdentities:
    """``AUTH_MODE=off`` and ``stub``: no sign-in is stored anywhere, so there is nothing to delete."""

    def remove(self, uid: str) -> None:
        return None


def _post_json(url: str, payload: dict, token: str) -> tuple[int, dict]:
    import httpx

    response = httpx.post(
        url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=10.0
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def _error_message(body: dict) -> str:
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else ""


class FirebaseIdentities:
    """Deletes Firebase Authentication users, as the service account this runs as.

    Calls Identity Toolkit's REST API directly, with Application Default
    Credentials (on Cloud Run, the attached service account). That avoids
    adding firebase-admin for a single call: google-auth is already installed
    for token verification. The service account needs a role that can delete
    users, such as ``roles/firebaseauth.admin``; until it has one, every
    removal fails with 403 and the accounts stay until the next run.

    Credentials are not looked up until the first removal, so a deployment
    that never deletes anyone does not need them to start.
    """

    def __init__(self, project_id: str, *, credentials=None, post=_post_json):
        if not project_id:
            raise ValueError("deleting Firebase users needs a project id")
        self._url = FIREBASE_DELETE_USER_URL.format(project=project_id)
        self._credentials = credentials
        self._post = post
        self._lock = threading.Lock()

    def _token(self) -> str:
        import google.auth
        from google.auth.transport.requests import Request

        with self._lock:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(scopes=[FIREBASE_ADMIN_SCOPE])
            if not self._credentials.valid:
                self._credentials.refresh(Request())
            return self._credentials.token

    def remove(self, uid: str) -> None:
        with io_span(log, "firebase", "delete_user", uid=uid[:12]) as span:
            try:
                status, body = self._post(self._url, {"localId": uid}, self._token())
            except Exception as exc:  # noqa: BLE001 - credentials or network: retry next run
                raise IdentityRemovalFailed(
                    f"could not ask Firebase to delete the user: {scrub(exc)}"
                ) from None
            span["status"] = status
            if status == 200:
                return
            message = _error_message(body)
            if status == 404 or message.startswith(USER_NOT_FOUND):
                span["gone"] = True
                return
            raise IdentityRemovalFailed(
                f"Firebase refused to delete the user: HTTP {status} {scrub(message)}".rstrip()
            )
