"""Token verification: what makes a credential ours, and what does not.

The tokens here are really signed and really verified — a self-signed
certificate stands in for Google's, so the signature check is the production
one rather than a stub. That matters because the failures worth catching are
the ones that still *look* like valid tokens: signed by the wrong key, minted
for a different Firebase project, or expired.

Two layers are under test and the split is the point: a *store* says which
account a token belongs to, and :class:`~app.auth.PrincipalRules` decides what
that account may claim. The rules tests below use a store made of a dict,
because they must hold for every store — including ones that do not exist yet.
"""

from __future__ import annotations

import datetime
import json
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

from app.auth import (
    MIN_CERT_TTL_SECONDS,
    Credential,
    DisabledVerifier,
    FirebaseCredentials,
    InvalidToken,
    Principal,
    PrincipalRules,
    StoreBackedVerifier,
    StubCredentials,
    _CertificateCache,
    _max_age,
    bearer_token,
)

PROJECT = "koalitionsberegner-test"
ISSUER = f"https://securetoken.google.com/{PROJECT}"
KID = "test-key-1"


def _keypair():
    """A signing key plus the x509 certificate a verifier would fetch for it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "securetoken.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    signer = crypt.RSASigner.from_string(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        KID,
    )
    return signer, pem


SIGNER, CERTIFICATE = _keypair()
OTHER_SIGNER, OTHER_CERTIFICATE = _keypair()


def make_token(signer=SIGNER, *, audience=PROJECT, issuer=ISSUER, expired=False, **claims):
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "uid-123",
        "iat": now - 3600 if expired else now - 10,
        "exp": now - 1800 if expired else now + 3600,
        **claims,
    }
    return jwt.encode(signer, payload).decode()


class FakeCerts:
    """Stands in for the cache, counting how often it is asked to refetch."""

    def __init__(self, certs=None):
        self.mapping = certs if certs is not None else {KID: CERTIFICATE}
        self.forced = 0

    def certs(self, *, force: bool = False):
        if force:
            self.forced += 1
            self.mapping = {KID: CERTIFICATE}
        return self.mapping


def verifier(certs=None, admin_emails=frozenset()) -> StoreBackedVerifier:
    """The production wiring: the Firebase store, behind the shared rules."""
    return StoreBackedVerifier(
        FirebaseCredentials(PROJECT, certs=certs or FakeCerts()),
        PrincipalRules.of(admin_emails),
    )


# --- the Authorization header ----------------------------------------------

@pytest.mark.parametrize(
    "header, expected",
    [
        ("Bearer abc.def", "abc.def"),
        ("bearer abc.def", "abc.def"),   # the scheme is case-insensitive
        ("  Bearer   abc  ", "abc"),
        (None, None),
        ("", None),
        ("abc.def", None),               # a bare token is not a credential
        ("Basic dXNlcjpwdw==", None),
        ("Bearer", None),
        ("Bearer   ", None),
    ],
)
def test_only_a_bearer_header_yields_a_token(header, expected):
    assert bearer_token(header) == expected


# --- real signature verification -------------------------------------------

def test_a_properly_signed_token_identifies_its_subject():
    principal = verifier().verify(make_token(sub="uid-abc", email="A@Example.ORG"))

    assert principal.uid == "uid-abc"
    assert principal.email == "a@example.org", "email is folded so allowlists match"
    assert principal.admin is False
    assert principal.unlimited is False


def test_a_token_signed_by_the_wrong_key_is_refused():
    with pytest.raises(InvalidToken):
        verifier().verify(make_token(OTHER_SIGNER))


def test_a_token_for_another_firebase_project_is_refused():
    """Valid, current, Google-signed — and still not ours."""
    with pytest.raises(InvalidToken):
        verifier().verify(make_token(audience="someone-elses-project"))


def test_a_token_whose_issuer_is_not_this_project_is_refused():
    with pytest.raises(InvalidToken, match="different project"):
        verifier().verify(make_token(issuer="https://securetoken.google.com/evil"))


def test_an_expired_token_is_refused():
    with pytest.raises(InvalidToken):
        verifier().verify(make_token(expired=True))


def test_a_token_without_a_subject_is_refused():
    with pytest.raises(InvalidToken, match="no subject"):
        verifier().verify(make_token(sub=""))


def test_garbage_is_refused_rather_than_raising_something_else():
    with pytest.raises(InvalidToken):
        verifier().verify("not-a-token")


def test_an_admin_claim_is_honoured_but_only_from_the_token():
    assert verifier().verify(make_token(admin=True)).admin is True
    assert verifier().verify(make_token(admin=False)).admin is False


def test_an_allowlisted_email_is_an_admin():
    listed = verifier(admin_emails=frozenset({"boss@example.org"}))
    assert listed.verify(make_token(email="boss@example.org", email_verified=True)).admin is True
    assert listed.verify(make_token(email="other@example.org", email_verified=True)).admin is False


def test_the_token_says_whether_the_address_is_confirmed():
    def confirmed(**claims):
        return verifier().verify(make_token(email="a@example.org", **claims)).email_verified

    assert confirmed(email_verified=True) is True
    assert confirmed(email_verified=False) is False
    assert confirmed() is False, "a token that does not say is not confirmed"
    assert confirmed(email_verified="true") is False, "only Firebase's own boolean counts"


def test_an_unconfirmed_allowlisted_address_is_not_an_admin():
    """Otherwise signing up *as* the admin's address would be enough to become one."""
    listed = verifier(admin_emails=frozenset({"boss@example.org"}))

    assert listed.verify(make_token(email="boss@example.org", email_verified=False)).admin is False
    assert listed.verify(make_token(email="boss@example.org")).admin is False


def test_an_unknown_key_id_refetches_the_certificates_once_before_refusing():
    """Google rotates signing keys; a stale cache must not lock everyone out."""
    stale = FakeCerts({"some-other-kid": OTHER_CERTIFICATE})

    principal = verifier(stale).verify(make_token())

    assert principal.uid == "uid-123"
    assert stale.forced == 1


def test_a_certificate_fetch_failure_is_a_refusal_not_a_crash():
    class Broken:
        def certs(self, *, force: bool = False):
            raise ConnectionError("securetoken unreachable")

    with pytest.raises(InvalidToken, match="could not verify"):
        verifier(Broken()).verify(make_token())


def test_firebase_verification_needs_a_project():
    with pytest.raises(ValueError):
        FirebaseCredentials("")


def test_a_request_with_no_credential_is_a_visitor():
    assert verifier().anonymous() is None


# --- certificate caching ----------------------------------------------------

def test_certificates_are_fetched_once_and_reused_until_they_expire():
    clock = {"now": 0.0}
    fetches = []

    def fetch(url):
        fetches.append(url)
        return {KID: CERTIFICATE}, 600.0

    cache = _CertificateCache("https://certs.test", fetch=fetch, clock=lambda: clock["now"])

    cache.certs()
    clock["now"] = 599.0
    cache.certs()
    assert len(fetches) == 1, "a verified request must not cost a network call"

    clock["now"] = 601.0
    cache.certs()
    assert len(fetches) == 2


@pytest.mark.parametrize(
    "header, expected",
    [
        ("public, max-age=1800", 1800.0),
        ("max-age=60", 60.0),
        ("no-cache", MIN_CERT_TTL_SECONDS),
        (None, MIN_CERT_TTL_SECONDS),
        ("max-age=nonsense", MIN_CERT_TTL_SECONDS),
    ],
)
def test_the_cache_lifetime_comes_from_the_response(header, expected):
    assert _max_age(header) == expected


def test_a_short_lifetime_is_raised_to_the_floor():
    cache = _CertificateCache(
        "https://certs.test", fetch=lambda url: ({}, 1.0), clock=lambda: 0.0
    )
    cache.certs()
    assert cache._expires_at >= MIN_CERT_TTL_SECONDS


# --- the rules, which hold whatever the store is ---------------------------

class DictStore:
    """A credential store made of a dict — the smallest thing that is one."""

    provider = "password"

    def __init__(self, **credentials: Credential):
        self.credentials = credentials

    def resolve(self, token: str) -> Credential:
        if token not in self.credentials:
            raise InvalidToken("no such session")
        return self.credentials[token]


def test_the_rules_are_the_same_ones_whatever_store_resolved_the_token():
    """The reason the split exists: a second store cannot relax the first's rules."""
    rules = PrincipalRules.of(frozenset({"boss@example.org"}))
    from_firebase = rules.principal(Credential(subject="u1", email="Boss@Example.ORG"))
    from_elsewhere = StoreBackedVerifier(
        DictStore(tok=Credential(subject="u1", email="Boss@Example.ORG")), rules
    ).verify("tok")

    assert from_firebase == from_elsewhere
    assert from_firebase.admin is True, "the allowlist does not care who issued the token"
    assert from_firebase.email == "boss@example.org", "folded once, in the rules"


def test_a_store_that_cannot_confirm_addresses_is_not_one_that_said_no():
    """SQLite has no mail to confirm with; Firebase does, and may not have yet."""
    rules = PrincipalRules.of(frozenset({"boss@example.org"}))

    cannot = rules.principal(Credential(subject="u1", email="boss@example.org"))
    said_no = rules.principal(
        Credential(subject="u1", email="boss@example.org", email_verified=False)
    )

    assert (cannot.email_verified, cannot.admin) == (True, True)
    assert (said_no.email_verified, said_no.admin) == (False, False)


def test_a_store_cannot_hand_out_an_identity_without_a_subject():
    with pytest.raises(InvalidToken, match="no subject"):
        StoreBackedVerifier(DictStore(tok=Credential(subject="  "))).verify("tok")


def test_an_unknown_token_is_refused_rather_than_treated_as_a_visitor():
    store = StoreBackedVerifier(DictStore())
    with pytest.raises(InvalidToken):
        store.verify("tok")
    assert store.anonymous() is None


def test_the_page_is_told_which_sign_in_flow_to_run():
    """`/api/config` reads this off the verifier, so the two cannot disagree."""
    assert verifier().provider == "firebase"
    assert StoreBackedVerifier(DictStore()).provider == "password"
    assert StoreBackedVerifier(StubCredentials()).provider == "none"
    assert DisabledVerifier().provider == "none"


# --- the non-production store ----------------------------------------------

@pytest.mark.parametrize(
    "token, expected",
    [
        ("u1", Principal(uid="u1", email_verified=True)),
        ("u1:a@example.org", Principal(uid="u1", email="a@example.org", email_verified=True)),
        (
            "u1:a@example.org:admin",
            Principal(uid="u1", email="a@example.org", admin=True, email_verified=True),
        ),
        ("u1::admin", Principal(uid="u1", admin=True, email_verified=True)),
        ("u1:a@example.org:unverified", Principal(uid="u1", email="a@example.org")),
    ],
)
def test_the_stub_store_reads_the_identity_straight_out_of_the_token(token, expected):
    assert StoreBackedVerifier(StubCredentials()).verify(token) == expected


def test_the_stub_store_still_needs_a_uid():
    with pytest.raises(InvalidToken):
        StoreBackedVerifier(StubCredentials()).verify(":a@example.org")


def test_with_auth_off_every_request_is_the_local_developer():
    disabled = DisabledVerifier()
    anonymous = disabled.anonymous()

    assert anonymous is not None, "auth off means no gating, not everyone anonymous"
    assert anonymous.unlimited is True
    assert anonymous.admin is True
    assert anonymous.email_verified is True
    assert disabled.verify("anything") == anonymous


def test_the_serialised_form_of_a_token_is_never_logged(caplog):
    """Tokens are credentials; a log line holding one is a credential leak."""
    token = make_token()
    with caplog.at_level("DEBUG", logger="app"):
        with pytest.raises(InvalidToken):
            verifier().verify(make_token(OTHER_SIGNER))
        verifier().verify(token)

    assert not any(token[:40] in record.getMessage() for record in caplog.records)
