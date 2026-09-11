"""Sign-in with no identity provider: passwords and sessions in the local file.

The claims worth holding down are the ones a reader cannot check by looking:
that a password never reaches the disk in a readable form, that the session
token does not either, that a session ends when it says it will, and that this
store decides *identity* only — never what that identity is allowed to do,
which stays with :class:`~app.auth.PrincipalRules`.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.auth import (
    BadCredentials,
    EmailTaken,
    InvalidToken,
    PrincipalRules,
    SignUpRefused,
    StoreBackedVerifier,
)
from app.sqlite_auth import SqliteCredentialStore, hash_password, verify_password

EMAIL = "voter@example.org"
PASSWORD = "a-long-enough-password"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "elections.db"


def open_store(db, **kwargs):
    return SqliteCredentialStore(db, **kwargs)


@pytest.fixture
def store(db):
    return open_store(db)


# --- sign-up and sign-in ----------------------------------------------------

def test_registering_signs_the_new_account_straight_in(store):
    session = store.register(EMAIL, PASSWORD)

    assert store.resolve(session.token).email == EMAIL
    assert session.uid, "an account with no identifier could not own anything"


def test_the_address_is_folded_so_one_person_is_one_account(store):
    store.register("Voter@Example.ORG", PASSWORD)

    with pytest.raises(EmailTaken):
        store.register("  voter@example.org ", PASSWORD)

    assert store.sign_in("VOTER@example.org", PASSWORD).email == EMAIL


@pytest.mark.parametrize(
    "email, password, expected",
    [
        ("not-an-address", PASSWORD, "email address"),
        ("voter@example", PASSWORD, "email address"),
        ("", PASSWORD, "email address"),
        (EMAIL, "short", "at least"),
        (EMAIL, "x" * 2000, "unreasonably long"),
    ],
)
def test_an_account_we_will_not_open(store, email, password, expected):
    with pytest.raises(SignUpRefused, match=expected):
        store.register(email, password)


def test_the_wrong_password_opens_nothing(store):
    store.register(EMAIL, PASSWORD)

    with pytest.raises(BadCredentials):
        store.sign_in(EMAIL, PASSWORD + "!")


def test_an_unknown_address_is_refused_the_same_way_a_wrong_password_is(store):
    """Two different messages here would answer "does this person have an account?"."""
    store.register(EMAIL, PASSWORD)

    with pytest.raises(BadCredentials) as unknown:
        store.sign_in("nobody@example.org", PASSWORD)
    with pytest.raises(BadCredentials) as wrong:
        store.sign_in(EMAIL, "not-the-password")

    assert str(unknown.value) == str(wrong.value)


def test_signing_in_twice_gives_two_sessions_that_both_work(store):
    """A phone and a laptop are not a reason to sign the other one out."""
    first = store.register(EMAIL, PASSWORD)
    second = store.sign_in(EMAIL, PASSWORD)

    assert first.token != second.token
    assert store.resolve(first.token).subject == store.resolve(second.token).subject


# --- sessions ---------------------------------------------------------------

def test_a_token_we_never_issued_is_refused(store):
    with pytest.raises(InvalidToken):
        store.resolve("made-up")


def test_a_session_stops_working_when_it_expires(db):
    now = {"t": 1000.0}
    store = open_store(db, clock=lambda: now["t"], session_ttl=60.0)
    session = store.register(EMAIL, PASSWORD)

    now["t"] += 59.0
    assert store.resolve(session.token).email == EMAIL

    now["t"] += 2.0
    with pytest.raises(InvalidToken, match="expired"):
        store.resolve(session.token)


def test_an_expired_session_is_dropped_rather_than_kept_around(db):
    now = {"t": 1000.0}
    store = open_store(db, clock=lambda: now["t"], session_ttl=60.0)
    session = store.register(EMAIL, PASSWORD)
    now["t"] += 61.0

    with pytest.raises(InvalidToken):
        store.resolve(session.token)

    rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM auth_sessions").fetchone()
    assert rows[0] == 0


def test_signing_out_ends_that_session_and_only_that_one(store):
    laptop = store.register(EMAIL, PASSWORD)
    phone = store.sign_in(EMAIL, PASSWORD)

    store.sign_out(laptop.token)

    with pytest.raises(InvalidToken):
        store.resolve(laptop.token)
    assert store.resolve(phone.token).email == EMAIL


def test_signing_out_a_token_that_is_already_gone_is_not_an_error(store):
    store.sign_out("made-up")


def test_accounts_and_sessions_survive_a_restart(db):
    session = open_store(db).register(EMAIL, PASSWORD)

    assert open_store(db).resolve(session.token).email == EMAIL


# --- what is on disk --------------------------------------------------------

def test_the_password_is_not_stored_and_neither_is_the_token(db, store):
    session = store.register(EMAIL, PASSWORD)

    dump = "".join(
        str(row) for row in sqlite3.connect(db).iterdump()
    )

    assert PASSWORD not in dump, "a leaked file must not be a list of passwords"
    assert session.token not in dump, "nor a list of working sessions"
    assert EMAIL in dump, "the address is not a secret; it is how you sign in"


def test_a_hash_is_salted_so_two_equal_passwords_do_not_look_equal():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)

    assert first != second
    assert verify_password(PASSWORD, first) and verify_password(PASSWORD, second)
    assert not verify_password(PASSWORD + "!", first)


def test_a_hash_we_cannot_read_refuses_rather_than_crashing():
    """A row from a future scheme must lock that account out, not the process."""
    assert verify_password(PASSWORD, "argon2$something") is False
    assert verify_password(PASSWORD, "") is False


# --- the seam this store plugs into ----------------------------------------

def test_the_rules_are_applied_to_a_local_identity_exactly_as_to_a_firebase_one(store):
    verifier = StoreBackedVerifier(store, PrincipalRules.of(frozenset({"boss@example.org"})))
    ordinary = store.register(EMAIL, PASSWORD)
    boss = store.register("Boss@Example.ORG", PASSWORD)

    assert verifier.verify(ordinary.token).admin is False
    assert verifier.verify(boss.token).admin is True, "ADMIN_EMAILS, the one rule for both"
    assert verifier.anonymous() is None


def test_the_store_itself_never_claims_administrator(store):
    """There is no column for it: the allowlist is the only way in."""
    session = store.register(EMAIL, PASSWORD)

    assert store.resolve(session.token).admin is False


def test_concurrent_sign_ups_for_one_address_produce_one_account(db):
    """SQLite's write lock decides the race, not whoever read first."""
    store = open_store(db)

    def attempt(_):
        try:
            return store.register(EMAIL, PASSWORD).uid
        except EmailTaken:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))

    assert len([uid for uid in results if uid]) == 1
