"""The sign-in endpoints ``AUTH_MODE=sqlite`` adds, driven over HTTP.

The flow this pins down is the one a browser runs: register, get a token back,
carry it on every later request, and stop being anybody when you sign out. The
account behind it is an ordinary free account — registering is not buying.

Everything here is wired through the same seams the app uses in production;
what makes it the local mode rather than the Firebase one is which store is
injected, and nothing else.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.accounts import InMemoryAccountStore
from app.auth import PrincipalRules, StoreBackedVerifier
from app.billing import DisabledBilling
from app.sqlite_auth import SqliteCredentialStore

EMAIL = "voter@example.org"
PASSWORD = "a-long-enough-password"
CREDENTIALS = {"email": EMAIL, "password": PASSWORD}


@pytest.fixture
def credentials(tmp_path):
    return SqliteCredentialStore(tmp_path / "elections.db")


@pytest.fixture
def accounts():
    return InMemoryAccountStore()


@pytest.fixture
def client(credentials, accounts):
    overrides = main.app.dependency_overrides
    overrides[main.get_password_credentials] = lambda: credentials
    overrides[main.get_token_verifier] = lambda: StoreBackedVerifier(
        credentials, PrincipalRules.of(frozenset({"boss@example.org"}))
    )
    overrides[main.get_account_store] = lambda: accounts
    overrides[main.get_billing_provider] = DisabledBilling
    with TestClient(main.app) as test_client:
        yield test_client
    overrides.clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def register(client, **overrides):
    response = client.post("/api/auth/register", json={**CREDENTIALS, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


# --- the flow ---------------------------------------------------------------

def test_registering_returns_a_token_that_the_rest_of_the_api_accepts(client):
    session = register(client)

    me = client.get("/api/me", headers=auth(session["token"]))

    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert me.json()["uid"] == session["uid"]


def test_a_new_account_is_a_free_one(client):
    """Registering opens an account; it does not buy anything."""
    me = client.get("/api/me", headers=auth(register(client)["token"])).json()

    assert me["tier"] == "free"
    assert me["may_import"] is False


def test_signing_in_again_returns_a_working_token(client):
    register(client)

    response = client.post("/api/auth/login", json=CREDENTIALS)

    assert response.status_code == 200
    assert client.get("/api/me", headers=auth(response.json()["token"])).status_code == 200


def test_the_address_can_only_be_registered_once(client):
    register(client)

    response = client.post("/api/auth/register", json=CREDENTIALS)

    assert response.status_code == 409
    assert "already an account" in response.json()["detail"]


def test_a_password_that_is_too_short_is_refused_before_an_account_exists(client):
    response = client.post("/api/auth/register", json={"email": EMAIL, "password": "short"})

    assert response.status_code == 422
    assert client.post("/api/auth/login", json=CREDENTIALS).status_code == 401


def test_the_wrong_password_is_401_and_says_no_more_than_that(client):
    register(client)

    response = client.post("/api/auth/login", json={**CREDENTIALS, "password": "wrong-one"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "wrong email address or password"


def test_an_unknown_address_is_refused_exactly_like_a_wrong_password(client):
    register(client)

    unknown = client.post("/api/auth/login", json={"email": "who@example.org",
                                                   "password": PASSWORD})
    wrong = client.post("/api/auth/login", json={**CREDENTIALS, "password": "wrong-one"})

    assert unknown.status_code == wrong.status_code
    assert unknown.json() == wrong.json()


def test_signing_out_ends_the_session(client):
    session = register(client)

    assert client.post("/api/auth/logout", headers=auth(session["token"])).status_code == 204

    refused = client.get("/api/me", headers=auth(session["token"]))
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Bearer"


def test_a_forged_token_is_refused_rather_than_treated_as_a_visitor(client):
    response = client.get("/api/me", headers=auth("not-a-session"))

    assert response.status_code == 401


def test_viewing_is_still_open_to_somebody_who_never_signed_in(client):
    assert client.get("/api/elections").status_code == 200


# --- the rules, over HTTP ---------------------------------------------------

def test_an_allowlisted_address_may_curate_and_an_ordinary_one_may_not(client):
    ordinary = register(client)
    boss = register(client, email="boss@example.org")

    for token, expected in ((ordinary["token"], 403), (boss["token"], 404)):
        response = client.put(
            "/api/elections/nosuchhash/selected",
            json={"selected": True},
            headers=auth(token),
        )
        # 404 is the admin getting through to a hash that does not exist; 403
        # is being stopped at the door.
        assert response.status_code == expected


def test_the_page_is_told_to_run_the_password_flow(client):
    config = client.get("/api/config").json()

    assert config["auth_provider"] == "password"
    assert config["auth_required"] is True


# --- the modes that have no password to manage ------------------------------

@pytest.fixture
def firebase_client():
    """No password store injected — what every other mode looks like."""
    main.app.dependency_overrides[main.get_password_credentials] = lambda: None
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/api/auth/register", "/api/auth/login", "/api/auth/logout"])
def test_without_a_password_store_there_is_nothing_to_sign_in_to(firebase_client, path):
    response = firebase_client.post(path, json=CREDENTIALS)

    assert response.status_code == 404
    assert "does not manage sign-in" in response.json()["detail"]
