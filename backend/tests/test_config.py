"""Configuration is rejected loudly, at startup — never silently defaulted."""

from __future__ import annotations

import logging

import pytest

@pytest.fixture
def clean_config():
    """Config getters are cached; clear them around each check."""
    from app.config import get_parser, get_store

    get_store.cache_clear()
    get_parser.cache_clear()
    yield
    get_store.cache_clear()
    get_parser.cache_clear()


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
    ],
)
def test_unrecognised_configuration_is_rejected(env, expected, monkeypatch, clean_config):
    from app.config import ConfigError, validate_configuration

    monkeypatch.setenv("ELECTION_STORE", "memory")
    monkeypatch.setenv("LLM_MODE", "mock")
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
        "configuration ok store=memory llm_mode=mock" in r.getMessage()
        for r in caplog.records
    )
