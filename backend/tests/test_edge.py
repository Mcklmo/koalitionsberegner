"""The edge of the app: what is refused, and what every answer carries.

All of it happens before an endpoint runs, so none of it may cost a store read,
a token check or a model call — which is the point when the traffic is hostile.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.config import ConfigError


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


# --- what every response carries -------------------------------------------

def test_responses_carry_a_policy_that_allows_only_our_own_scripts(client):
    headers = client.get("/api/config").headers
    policy = headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "connect-src 'self'" in policy
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


# --- the page, and nothing beside it ----------------------------------------

def test_the_page_and_its_scripts_are_served_and_nothing_next_to_them(client):
    if not (main.FRONTEND_DIR / "index.html").is_file():
        pytest.skip("no frontend next to this backend")
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert client.get("/js/main.js").status_code == 200
    # On a local checkout the frontend directory is the repo root.
    for path in ("/.env", "/.env.example", "/README.md", "/Dockerfile", "/backend/app/main.py"):
        assert client.get(path).status_code == 404, path


# --- only through the proxy, once there is one ------------------------------

SECRET = "s" * config.MIN_ORIGIN_SECRET_CHARS


def test_without_an_origin_secret_every_request_is_answered(client):
    assert client.get("/api/config").status_code == 200


def test_with_an_origin_secret_a_direct_request_is_refused(client, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", SECRET)
    assert client.get("/api/config").status_code == 403
    assert client.get("/api/config", headers={"x-origin-secret": "wrong"}).status_code == 403
    assert client.get("/", headers={"x-origin-secret": SECRET[:-1]}).status_code == 403


def test_with_an_origin_secret_the_proxy_gets_through(client, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", SECRET)
    assert client.get("/api/config", headers={"x-origin-secret": SECRET}).status_code == 200


def test_the_health_check_needs_no_origin_secret(client, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", SECRET)
    assert client.get("/healthz").status_code == 200


def test_a_short_origin_secret_stops_the_boot(monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", "changeme")
    with pytest.raises(ConfigError, match="ORIGIN_SECRET"):
        config.validate_configuration()


# --- bodies -----------------------------------------------------------------

def test_an_oversized_body_is_refused_before_it_is_read(client):
    response = client.post(
        "/api/elections/import",
        content=b" " * (main.MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_a_body_that_does_not_declare_its_length_is_refused(client):
    response = client.post("/api/elections/import", content=iter([b"{}"]))
    assert response.status_code == 411
