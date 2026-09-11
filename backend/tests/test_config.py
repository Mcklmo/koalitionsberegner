"""Configuration is rejected loudly, at startup — never silently defaulted."""

from __future__ import annotations

import logging

import pytest

@pytest.fixture
def clean_config():
    """Config getters are cached; clear them around each check.

    All of them, not just the ones a given case touches: a verifier cached here
    under a different AUTH_MODE would otherwise leak into whatever runs next.
    """
    from app import config

    cached = (
        config.get_store,
        config.get_parser,
        config.get_accounts,
        config.get_password_store,
        config.get_verifier,
        config.get_quota_policy,
        config.get_billing,
    )
    for getter in cached:
        getter.cache_clear()
    yield
    for getter in cached:
        getter.cache_clear()


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"ELECTION_STORE": "postgres"}, "ELECTION_STORE must be one of"),
        ({"ELECTION_STORE": "Memory "}, None),               # case and space are fine
        ({"LLM_MODE": "production"}, "LLM_MODE must be one of"),
        ({"LLM_MODE": "LIVE", "ANTHROPIC_API_KEY": "sk-x"}, None),
        ({"LLM_MODE": "live", "ANTHROPIC_API_KEY": ""}, "requires ANTHROPIC_API_KEY"),
        ({"ELECTION_STORE": "firestore", "GOOGLE_CLOUD_PROJECT": ""},
         "requires GOOGLE_CLOUD_PROJECT"),
        ({"PARSE_LEASE_SECONDS": "soon"}, "must be a number"),
        ({"AUTH_MODE": "firebase", "FIREBASE_PROJECT_ID": "", "GOOGLE_CLOUD_PROJECT": ""},
         "requires FIREBASE_PROJECT_ID"),
        ({"AUTH_MODE": "on"}, "AUTH_MODE must be one of"),
        ({"AUTH_MODE": "firebase", "FIREBASE_PROJECT_ID": "demo"}, None),
        ({"BASIC_MONTHLY_IMPORTS": "lots"}, "must be a whole number"),
        ({"PREMIUM_MONTHLY_IMPORTS": "-1"}, "must not be negative"),
        # Half-configured billing is a mistake, not a reason to sell nothing.
        ({"STRIPE_PRICE_BASIC": "price_1"}, "STRIPE_API_KEY is not"),
        ({"STRIPE_API_KEY": "sk_test"}, "no STRIPE_PRICE_BASIC/PREMIUM"),
        ({"STRIPE_API_KEY": "sk_test", "STRIPE_PRICE_BASIC": "price_1"},
         "requires PUBLIC_BASE_URL"),
        ({"STRIPE_API_KEY": "sk_test", "STRIPE_PRICE_BASIC": "price_1",
          "PUBLIC_BASE_URL": "https://app.test"}, "requires STRIPE_WEBHOOK_SECRET"),
    ],
)
def test_unrecognised_configuration_is_rejected(env, expected, monkeypatch, clean_config):
    from app.config import ConfigError, validate_configuration

    monkeypatch.setenv("ELECTION_STORE", "memory")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("AUTH_MODE", "off")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    if expected is None:
        validate_configuration()
        return
    with pytest.raises(ConfigError, match=expected):
        validate_configuration()


def test_an_unknown_llm_mode_never_silently_becomes_the_mock(monkeypatch, clean_config):
    """The dangerous failure: a typo would otherwise serve fabricated results."""
    from app.config import ConfigError, get_parser

    monkeypatch.setenv("LLM_MODE", "prod")
    with pytest.raises(ConfigError):
        get_parser()


def test_a_bad_configuration_stops_the_app_from_starting(monkeypatch, clean_config):
    """Not a 500 on the first import — the process must refuse to come up."""
    from fastapi.testclient import TestClient

    from app import main
    from app.config import ConfigError

    monkeypatch.setenv("ELECTION_STORE", "postgres")
    with pytest.raises(ConfigError, match="ELECTION_STORE"):
        with TestClient(main.app):
            pass


def test_a_good_configuration_starts_and_logs_what_it_chose(monkeypatch, clean_config, caplog):
    from fastapi.testclient import TestClient

    from app import main

    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setenv("ELECTION_STORE", "memory")
    monkeypatch.setenv("LLM_MODE", "mock")
    with TestClient(main.app) as client:
        assert client.get("/healthz").status_code == 200

    assert any(
        "configuration ok store=memory llm_mode=mock auth_mode=off billing=off"
        in r.getMessage()
        for r in caplog.records
    )


def test_auth_is_on_by_default_once_there_is_a_project(monkeypatch, clean_config):
    """The dangerous default: a deployment that silently serves everything ungated."""
    from app.config import auth_mode

    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
    assert auth_mode() == "firebase"

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    assert auth_mode() == "off", "a bare local checkout still runs without Firebase"


def test_a_stub_verifier_is_never_reached_by_accident(monkeypatch, clean_config, caplog):
    """It trusts whatever the caller types, so choosing it must be deliberate and loud."""
    from app.config import get_verifier
    from app.auth import StubCredentials

    caplog.set_level(logging.WARNING, logger="app")
    monkeypatch.setenv("AUTH_MODE", "stub")

    assert isinstance(get_verifier().store, StubCredentials)
    assert any("local use only" in r.getMessage() for r in caplog.records)


def test_sqlite_auth_wires_a_credential_store_and_the_shared_rules(
    monkeypatch, clean_config, tmp_path
):
    """The point of the mode: gating with no Firebase project behind it."""
    from app.config import get_password_store, get_verifier
    from app.sqlite_auth import SqliteCredentialStore

    monkeypatch.setenv("ELECTION_STORE", "sqlite")
    monkeypatch.setenv("AUTH_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "elections.db"))
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.org")

    verifier = get_verifier()

    assert isinstance(verifier.store, SqliteCredentialStore)
    assert verifier.store is get_password_store(), "one store, not one per caller"
    assert verifier.anonymous() is None, "sqlite gates like Firebase does"
    assert verifier.provider == "password"

    # The admin allowlist is the same rule the Firebase mode applies, applied
    # to an identity that never went near Google.
    session = verifier.store.register("boss@example.org", "a-good-password")
    assert verifier.verify(session.token).admin is True


def test_sqlite_auth_warns_when_the_accounts_it_signs_in_are_not_persisted(
    monkeypatch, clean_config, tmp_path, caplog
):
    from app.config import get_verifier

    caplog.set_level(logging.WARNING, logger="app")
    monkeypatch.setenv("ELECTION_STORE", "memory")
    monkeypatch.setenv("AUTH_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "elections.db"))

    get_verifier()

    assert any("accounts do not" in r.getMessage() for r in caplog.records)


def test_quota_limits_come_from_the_environment(monkeypatch, clean_config):
    from app.accounts import Tier
    from app.config import get_quota_policy

    monkeypatch.setenv("BASIC_MONTHLY_IMPORTS", "3")
    monkeypatch.setenv("PREMIUM_MONTHLY_IMPORTS", "300")

    policy = get_quota_policy()

    assert policy.limit(Tier.FREE) == 0, "free is not configurable; it is the free tier"
    assert policy.limit(Tier.BASIC) == 3
    assert policy.limit(Tier.PREMIUM) == 300


def test_without_stripe_the_app_still_starts_and_sells_nothing(monkeypatch, clean_config):
    from app.config import get_billing

    for name in ("STRIPE_API_KEY", "STRIPE_PRICE_BASIC", "STRIPE_PRICE_PREMIUM"):
        monkeypatch.delenv(name, raising=False)

    billing = get_billing()

    assert billing.enabled is False
    assert billing.tiers() == ()
