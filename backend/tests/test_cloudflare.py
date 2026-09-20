"""The Cloudflare front door must not be a weaker copy of the app's own.

Static files are served by Cloudflare from the repo root, so two things the
backend guarantees for itself have to be restated there: the security headers
(``_headers``) and the rule that nothing but the page is published
(``.assetsignore``). These tests hold the restatements to the originals.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from app import main

ROOT = Path(__file__).resolve().parents[2]


def _headers_file() -> dict[str, str]:
    headers = {}
    for line in (ROOT / "_headers").read_text().splitlines():
        if line.startswith("  ") and ":" in line:
            name, _, value = line.strip().partition(":")
            headers[name.lower()] = value.strip()
    return headers


def test_cloudflare_sends_the_same_security_headers_as_the_backend():
    assert _headers_file() == main.SECURITY_HEADERS


def test_nothing_but_the_page_is_published():
    ignored = {
        line.strip()
        for line in (ROOT / ".assetsignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    for secret_or_source in (".env*", "*.local.sh", "backend", "worker", ".git"):
        assert secret_or_source in ignored, secret_or_source

    published = {
        path.name for path in ROOT.iterdir()
        if not any(fnmatch(path.name, pattern) for pattern in ignored)
    }
    assert published <= {"index.html", "js", "_headers", "approve.html"}, (
        f"{sorted(published - {'index.html', 'js', '_headers', 'approve.html'})} would become "
        "public URLs — add them to .assetsignore"
    )
