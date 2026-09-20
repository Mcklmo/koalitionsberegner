"""Configuration is rejected loudly, at startup — never silently defaulted."""

from __future__ import annotations

import logging

import pytest

@pytest.fixture
def clean_config():
    """Config getters are cached; clear them around each check."""
    from app import config

    cached = (
        config.get_store,
        config.get_parser,
        config.get_refresh_parser,
        config.get_search,
        config.get_wikipedia,
        config.get_wishlist,
        config.get_usage,
        config.get_usage_recorder,
        config.get_mailer,
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
        ({"IMPORT_PAGE_LIMIT": "a few"}, "must be a whole number"),
        ({"SEARCH_MODE": "bing"}, "SEARCH_MODE must be one of"),
        # Asked for by name, so its credentials are required even under a
        # mocked agent that will never reach it.
        ({"SEARCH_MODE": "google"}, "GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX"),
        ({"SEARCH_MODE": "google", "GOOGLE_SEARCH_API_KEY": "k"},
         "GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX"),
        ({"SEARCH_MODE": "google", "GOOGLE_SEARCH_API_KEY": "k", "GOOGLE_SEARCH_CX": "c"}, None),
        ({"SEARCH_MODE": "anthropic", "ANTHROPIC_API_KEY": ""}, "requires ANTHROPIC_API_KEY"),
        ({"WIKIPEDIA": "yes"}, "WIKIPEDIA must be one of"),
        # Checked even under a mocked agent that will never look anything up: it
        # becomes a hostname, and a typo should not wait for the first import.
        ({"WIKIPEDIA_LANGUAGE": "deutsche sprache"}, "WIKIPEDIA_LANGUAGE must be"),
        ({"WIKIPEDIA_LANGUAGE": "DE"}, None),
        # And auto asks for nothing: a local checkout with no keys still boots.
        ({"SEARCH_MODE": "auto"}, None),
        # A placeholder is a boot failure, like the other secrets.
        ({"ADMIN_SECRET": "changeme"}, "ADMIN_SECRET must be at least 32"),
        ({"ADMIN_SECRET": "a" * 32}, None),
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


@pytest.mark.parametrize(
    "env, expected",
    [
        # Nothing extra configured: no second search. The resolver searches as
        # part of identifying the election, and paying twice for the same
        # hosted tool is not a default worth having.
        ({}, "off"),
        ({"GOOGLE_SEARCH_API_KEY": "k", "GOOGLE_SEARCH_CX": "c"}, "google"),
        # An index of its own is worth consulting on top of the resolver, and
        # asking for the hosted tool anyway is allowed — just not by default.
        ({"SEARCH_MODE": "anthropic"}, "anthropic"),
        ({"SEARCH_MODE": "off", "GOOGLE_SEARCH_API_KEY": "k", "GOOGLE_SEARCH_CX": "c"}, "off"),
    ],
)
def test_auto_search_follows_whichever_engine_is_configured(
    env, expected, monkeypatch, clean_config
):
    from app.config import search_mode

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert search_mode() == expected


def test_a_mocked_agent_never_reaches_a_search_engine(monkeypatch, clean_config):
    """A mocked resolver names its own page, so a search would be dead code —
    and a local run must not spend anyone's search quota."""
    from app.config import get_search, search_mode
    from app.search import DisabledSearch

    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_SEARCH_CX", "c")
    assert search_mode() == "off"
    assert isinstance(get_search(), DisabledSearch)


def test_the_parser_is_given_the_search_and_the_budgets_it_may_use(monkeypatch, clean_config):
    from app.config import get_parser, get_search

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("IMPORT_PAGE_LIMIT", "1")
    monkeypatch.setenv("IMPORT_SEARCH_LIMIT", "4")

    parser = get_parser()
    assert parser._search is get_search()
    assert (parser._page_limit, parser._search_limit) == (1, 4)



def test_wikipedia_is_looked_up_first_and_serves_its_own_articles(monkeypatch, clean_config):
    """Both halves of the seam, from one object: the candidate source the parser
    asks, and the fetcher that reads what it found."""
    from app.config import get_parser, get_wikipedia
    from app.wikipedia import WikipediaFetcher

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("WIKIPEDIA_CONTACT", "ops@example.org")
    monkeypatch.setenv("WIKIPEDIA_LANGUAGE", "de")

    parser = get_parser()
    assert parser._wikipedia is get_wikipedia()
    assert parser._wikipedia.host == "de.wikipedia.org"
    assert isinstance(parser._fetcher, WikipediaFetcher)


def test_wikipedia_can_be_switched_off_without_touching_anything_else(
    monkeypatch, clean_config
):
    from app.config import get_parser, get_wikipedia
    from app.fetcher import HttpPageFetcher

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("WIKIPEDIA", "off")

    assert get_wikipedia() is None
    parser = get_parser()
    assert parser._wikipedia is None
    assert isinstance(parser._fetcher, HttpPageFetcher), "the ordinary path, unwrapped"


def test_a_mocked_agent_never_reaches_wikipedia_either(monkeypatch, clean_config):
    """The mock extractor ignores the page it is handed, so fetching a real
    article would be a call made for nothing."""
    from app.config import get_wikipedia, wikipedia_mode

    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("WIKIPEDIA", "on")
    assert wikipedia_mode() == "off"
    assert get_wikipedia() is None


def test_an_unconfigured_deployment_still_names_itself_to_wikimedia(
    monkeypatch, clean_config
):
    """A client that identifies nobody is answered 403, so the contact is not
    something a deployment has to discover: it has a default, and
    ``WIKIPEDIA_CONTACT`` replaces it."""
    from app.config import get_wikipedia

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.delenv("WIKIPEDIA_CONTACT", raising=False)

    wikipedia = get_wikipedia()
    assert wikipedia is not None
    assert "github.com" in wikipedia._user_agent


def test_live_mode_wires_both_agents(monkeypatch, clean_config):
    """Two agents, one key: the resolver that searches, the extractor that reads."""
    from app.config import get_parser
    from app.extractor import AnthropicExtractor
    from app.resolver import AnthropicResolver

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")

    parser = get_parser()
    assert isinstance(parser._resolver, AnthropicResolver)
    assert isinstance(parser._extractor, AnthropicExtractor)


def test_mock_mode_wires_neither(monkeypatch, clean_config):
    from app.config import get_parser
    from app.extractor import MockExtractor
    from app.resolver import MockResolver

    monkeypatch.setenv("LLM_MODE", "mock")

    parser = get_parser()
    assert isinstance(parser._resolver, MockResolver)
    assert isinstance(parser._extractor, MockExtractor)


def test_the_refresh_parser_takes_a_cheaper_model_when_one_is_named(monkeypatch, clean_config):
    from app.config import get_parser, get_refresh_parser
    from app.extractor import MODEL

    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.delenv("REFRESH_MODEL", raising=False)

    assert get_parser()._extractor._model == MODEL
    assert get_refresh_parser()._extractor._model == MODEL  # unset: same as the import's

    get_refresh_parser.cache_clear()
    monkeypatch.setenv("REFRESH_MODEL", "claude-haiku-5")
    assert get_refresh_parser()._extractor._model == "claude-haiku-5"
    assert get_parser()._extractor._model == MODEL  # the ordinary import is untouched


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
        "configuration ok store=memory llm_mode=mock admin=open "
        "requests=off reports=off search=off wikipedia=off" in r.getMessage()
        for r in caplog.records
    )


def test_cloud_run_refuses_to_boot_without_an_admin_secret(monkeypatch, clean_config):
    """Without one every caller is the owner: right on a laptop, never deployed."""
    from app.config import ConfigError, validate_configuration

    monkeypatch.setenv("K_SERVICE", "koalitionsberegner")
    monkeypatch.setenv("ELECTION_STORE", "memory")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)

    with pytest.raises(ConfigError, match="ADMIN_SECRET is required on Cloud Run"):
        validate_configuration()

    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    validate_configuration()


def test_the_issues_url_is_built_from_the_same_repository_the_wishlist_files_against(monkeypatch):
    from app.config import ConfigError, issues_url

    monkeypatch.delenv("GITHUB_ISSUES_REPO", raising=False)
    assert issues_url() == ""

    monkeypatch.setenv("GITHUB_ISSUES_REPO", "owner/repo")
    assert issues_url() == "https://github.com/owner/repo/issues"

    monkeypatch.setenv("GITHUB_ISSUES_REPO", "not a repo")
    with pytest.raises(ConfigError, match="must be owner/name"):
        issues_url()
