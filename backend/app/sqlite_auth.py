"""SQLite-backed credentials: sign-in for a run with no identity provider.

``AUTH_MODE=sqlite`` is Firebase's place in the wiring, filled by a table. It
holds the passwords itself and issues its own opaque session tokens, so a
self-hosted deployment can have real accounts — gated viewing, tiers, quotas —
with no Google project anywhere. What it does *not* do is reinterpret what an
identity means: it returns a :class:`~app.auth.Credential` and
:class:`~app.auth.PrincipalRules` decides the rest, exactly as it does for a
Firebase ID token.

Two choices worth stating. Passwords are stored as scrypt hashes with a
per-password salt, never reversibly; and the session token is stored as its
SHA-256, so the table is not a pile of working credentials if the file leaks.
The token is returned to the caller once, at sign-in, and cannot be read back
out of storage afterwards.

Sessions expire on their own clock rather than by a scheduled sweep: a row past
its ``expires_at`` is refused and deleted when it is next presented, and
sign-in clears out whatever else has lapsed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .auth import (
    BadCredentials,
    Credential,
    EmailTaken,
    InvalidToken,
    Session,
    checked_signup,
    normalize_email,
)
from .observability import io_span

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
    uid           TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    issued_at  REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_by_uid ON auth_sessions (uid);
"""

#: How long a session lasts. Long, because the only way to renew one is to type
#: the password again, and there is no refresh token to shorten it safely.
SESSION_TTL_SECONDS = 30 * 24 * 3600.0

#: scrypt cost. 16 MiB and ~50 ms per attempt on ordinary hardware: slow enough
#: that a leaked table is not a wordlist away from being passwords.
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
TOKEN_BYTES = 32


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$n$r$p$salt$hash`` — self-describing, so the cost can be raised."""
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash, at that hash's own cost."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            raise ValueError(scheme)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(digest_hex) // 2,
        )
    except (ValueError, TypeError):
        log.warning("a stored password hash is not in a form we can check")
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SqliteCredentialStore:
    """Accounts, passwords and sessions in the same file as everything else."""

    provider = "password"

    def __init__(
        self,
        path: str | Path,
        *,
        clock=time.time,
        session_ttl: float = SESSION_TTL_SECONDS,
    ):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._ttl = session_ttl
        self._local = threading.local()
        with io_span(log, "sqlite", "migrate_auth", path=self._path):
            self._connect().executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self):
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- sessions ----------------------------------------------------------

    def _issue(self, conn: sqlite3.Connection, uid: str, email: str) -> Session:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = self._clock()
        expires_at = now + self._ttl
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, uid, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (_token_hash(token), uid, now, expires_at),
        )
        return Session(token=token, uid=uid, email=email, expires_at=expires_at)

    def resolve(self, token: str) -> Credential:
        """The account behind a session token. Every gated request lands here."""
        with io_span(log, "sqlite", "resolve_session") as span:
            row = self._connect().execute(
                "SELECT s.uid AS uid, s.expires_at AS expires_at, u.email AS email"
                " FROM auth_sessions s JOIN auth_users u ON u.uid = s.uid"
                " WHERE s.token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
            if row is None:
                span["found"] = False
                raise InvalidToken("that session is not one of ours")
            if row["expires_at"] <= self._clock():
                span["expired"] = True
                # Presenting it is the last thing it is good for.
                with self._write() as conn:
                    conn.execute(
                        "DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),)
                    )
                raise InvalidToken("that session has expired")
            span["uid"] = row["uid"][:12]
            # `admin` is left to PrincipalRules: this store has no claims of its
            # own, so ADMIN_EMAILS is the only way to become one.
            return Credential(subject=row["uid"], email=row["email"])

    def sign_out(self, token: str) -> None:
        with io_span(log, "sqlite", "sign_out"):
            with self._write() as conn:
                conn.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),)
                )

    def _purge_expired(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (self._clock(),))

    # --- accounts ----------------------------------------------------------

    def register(self, email: str, password: str) -> Session:
        address, secret = checked_signup(email, password)
        # Hashing costs ~50 ms; doing it before the write lock keeps a burst of
        # sign-ups off each other's transactions.
        stored = hash_password(secret)
        with io_span(log, "sqlite", "register") as span:
            with self._write() as conn:
                taken = conn.execute(
                    "SELECT 1 FROM auth_users WHERE email = ?", (address,)
                ).fetchone()
                if taken is not None:
                    span["taken"] = True
                    raise EmailTaken("there is already an account with that address")
                uid = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO auth_users (uid, email, password_hash, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (uid, address, stored, self._clock()),
                )
                span["uid"] = uid[:12]
                return self._issue(conn, uid, address)

    def sign_in(self, email: str, password: str) -> Session:
        address = normalize_email(email)
        with io_span(log, "sqlite", "sign_in") as span:
            row = (
                self._connect().execute(
                    "SELECT uid, email, password_hash FROM auth_users WHERE email = ?", (address,)
                ).fetchone()
                if address
                else None
            )
            # An unknown address must cost the same as a wrong password, or the
            # response time answers "does this person have an account here?".
            stored = row["password_hash"] if row else hash_password(secrets.token_hex(16))
            if not verify_password(password, stored) or row is None:
                span["ok"] = False
                raise BadCredentials("wrong email address or password")
            with self._write() as conn:
                self._purge_expired(conn)
                span["uid"] = row["uid"][:12]
                return self._issue(conn, row["uid"], row["email"])
