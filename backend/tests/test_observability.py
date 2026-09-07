"""Every outward call logs a start and a matching end, and leaks no content."""

from __future__ import annotations

import logging

import pytest

from app.extractor import MockExtractor
from app.fetcher import FetchError
from app.observability import io_span, scrub
from app.parser import LlmElectionParser
from app.service import ImportService
from app.store import InMemoryElectionStore
from tests.factories import CountingParser, make_request
from tests.test_extraction import StubFetcher

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def spans(caplog, system=None):
    """(system, operation, phase) triples, in order."""
    found = []
    for record in caplog.records:
        parts = record.getMessage().split()
        if len(parts) >= 3 and parts[2] in ("start", "ok", "failed"):
            if system is None or parts[0] == system:
                found.append((parts[0], parts[1], parts[2]))
    return found


def test_a_span_logs_before_and_after():
    log = logging.getLogger("app.test")
    with _capture(log) as records:
        with io_span(log, "system", "operation", key="value") as span:
            span["result"] = "fine"

    assert [r.getMessage().split()[2] for r in records] == ["start", "ok"]
    assert "key=value" in records[0].getMessage()
    assert "result=fine" in records[1].getMessage()
    assert "ms=" in records[1].getMessage(), "the end line carries a duration"


def test_a_failing_span_logs_the_failure_and_re_raises():
    log = logging.getLogger("app.test")
    with _capture(log) as records:
        with pytest.raises(FetchError):
            with io_span(log, "page", "get", url="https://e.org"):
                raise FetchError("gone")

    assert [r.getMessage().split()[2] for r in records] == ["start", "failed"]
    assert records[1].levelno == logging.WARNING
    assert "error=FetchError" in records[1].getMessage()
    assert "detail=gone" in records[1].getMessage()


def test_untrusted_values_cannot_forge_a_log_line():
    forged = "ok" + chr(10) + "INFO app.fake something happened"
    assert chr(10) not in scrub(forged)
    assert chr(13) not in scrub("a" + chr(13) + "b")
    assert scrub("x" * 500).endswith("…"), "long values are truncated"


async def test_the_whole_outward_path_is_bracketed(caplog):
    """Fetch, agent and extraction each log a start and an end."""
    caplog.set_level(logging.INFO, logger="app")
    store = InMemoryElectionStore()
    service = ImportService(store, LlmElectionParser(StubFetcher(), MockExtractor()))

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.page_key, timeout=2.0)

    logged = spans(caplog)
    assert ("anthropic", "extract", "start") in logged
    assert ("anthropic", "extract", "ok") in logged
    assert ("extraction", "run", "start") in logged
    assert ("extraction", "run", "ok") in logged


async def test_every_start_has_an_end(caplog):
    caplog.set_level(logging.INFO, logger="app")
    store = InMemoryElectionStore()
    service = ImportService(store, LlmElectionParser(StubFetcher(), MockExtractor()))

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.page_key, timeout=2.0)
    await service.confirm(submitted.page_key)

    starts = [(s, o) for s, o, phase in spans(caplog) if phase == "start"]
    ends = [(s, o) for s, o, phase in spans(caplog) if phase in ("ok", "failed")]
    assert starts, "something was logged"
    assert sorted(starts) == sorted(ends), "no span is left open"


def test_inbound_requests_are_logged(caplog):
    """The other side of every outward span: what asked for the work."""
    from fastapi.testclient import TestClient

    from app import main

    caplog.set_level(logging.INFO, logger="app")
    with TestClient(main.app) as client:
        client.get("/healthz")

    logged = spans(caplog, system="http")
    assert ("http", "request", "start") in logged
    assert ("http", "request", "ok") in logged
    assert any("status=200" in r.getMessage() for r in caplog.records)
    assert any("path=/healthz" in r.getMessage() for r in caplog.records)


async def test_a_failed_extraction_is_logged_as_failed(caplog):
    caplog.set_level(logging.INFO, logger="app")
    store = InMemoryElectionStore()
    service = ImportService(store, CountingParser(fail_times=1))

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.page_key, timeout=2.0)

    assert ("extraction", "run", "failed") in spans(caplog)


async def test_page_content_never_reaches_the_log(caplog):
    """Only shapes and sizes: a results page can be large and is untrusted."""
    caplog.set_level(logging.INFO, logger="app")
    secret = "SENSITIVE-PAGE-BODY-MARKER"
    store = InMemoryElectionStore()
    service = ImportService(
        store, LlmElectionParser(StubFetcher(text=secret), MockExtractor())
    )

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.page_key, timeout=2.0)

    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in everything
    assert "page_chars=" in everything, "its size is logged instead"


class _capture:
    """Minimal handler-based capture; caplog does not see records this early."""

    def __init__(self, logger):
        self._logger = logger
        self._records: list[logging.LogRecord] = []

    def __enter__(self):
        self._logger.setLevel(logging.INFO)
        handler = logging.Handler()
        handler.emit = self._records.append
        self._handler = handler
        self._logger.addHandler(handler)
        return self._records

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False
