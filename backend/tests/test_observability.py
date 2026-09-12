"""What reaches the log: the notable calls, every failure, and no page content.

The contract is deliberately quiet. One line per outward call that costs time or
money, nothing before the fact, and the store's chatter kept for
``LOG_LEVEL=DEBUG`` — an import that reads two pages should be half a dozen
lines, not forty.
"""

from __future__ import annotations

import logging

import pytest

from app.fetcher import FetchError
from app.observability import NOTABLE_SYSTEMS, io_span, scrub
from app.parser import LlmElectionParser
from app.resolver import MockResolver
from app.service import ImportService
from app.store import InMemoryElectionStore
from app.extractor import MockExtractor
from tests.factories import CountingParser, make_request
from tests.test_extraction import SEATS, SiteFetcher

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


def test_a_notable_span_logs_one_line_saying_how_it_went():
    log = logging.getLogger("app.test")
    with _capture(log) as records:
        with io_span(log, "page", "get", key="value") as span:
            span["result"] = "fine"

    assert [r.getMessage().split()[2] for r in records] == ["ok"], "one line, after the fact"
    assert records[0].levelno == logging.INFO
    assert "key=value" in records[0].getMessage()
    assert "result=fine" in records[0].getMessage()
    assert "ms=" in records[0].getMessage(), "which is where the duration is"


def test_the_stores_chatter_is_kept_for_debugging():
    """A Firestore read per request says nothing the import's own lines do not."""
    log = logging.getLogger("app.test")
    with _capture(log) as records:
        with io_span(log, "sqlite", "get_job", request="abc"):
            pass
    assert records == [], "nothing at INFO"

    with _capture(log, level=logging.DEBUG) as records:
        with io_span(log, "sqlite", "get_job", request="abc"):
            pass
    assert [r.getMessage().split()[2] for r in records] == ["start", "ok"]
    assert all(r.levelno == logging.DEBUG for r in records)


def test_the_start_of_a_notable_span_is_there_when_it_is_wanted():
    """A call that never returns has no "ok" line; DEBUG is how it is named."""
    log = logging.getLogger("app.test")
    with _capture(log, level=logging.DEBUG) as records:
        with io_span(log, "anthropic", "extract", model="m"):
            pass
    assert [r.getMessage().split()[2] for r in records] == ["start", "ok"]


def test_the_notable_systems_are_the_outward_ones():
    assert {"anthropic", "page"} <= NOTABLE_SYSTEMS
    assert not {"sqlite", "firestore", "http"} & NOTABLE_SYSTEMS


def test_a_failing_span_logs_the_failure_and_re_raises():
    log = logging.getLogger("app.test")
    with _capture(log) as records:
        with pytest.raises(FetchError):
            with io_span(log, "page", "get", url="https://e.org"):
                raise FetchError("gone")

    assert [r.getMessage().split()[2] for r in records] == ["failed"]
    assert records[0].levelno == logging.WARNING
    assert "error=FetchError" in records[0].getMessage()
    assert "detail=gone" in records[0].getMessage()


def test_a_quiet_system_still_logs_its_failures():
    """Volume is the reason the store is quiet. A failure is not volume."""
    log = logging.getLogger("app.test")
    with _capture(log) as records:
        with pytest.raises(RuntimeError):
            with io_span(log, "sqlite", "claim", request="abc"):
                raise RuntimeError("database is locked")

    assert [r.levelno for r in records] == [logging.WARNING]
    assert "database is locked" in records[0].getMessage()


def test_untrusted_values_cannot_forge_a_log_line():
    forged = "ok" + chr(10) + "INFO app.fake something happened"
    assert chr(10) not in scrub(forged)
    assert chr(13) not in scrub("a" + chr(13) + "b")
    assert scrub("x" * 500).endswith("…"), "long values are truncated"


def import_service(store, text="Sitze", extractor=None):
    """A whole pipeline with the models mocked. The fetcher is a stub, so the
    one span missing from these logs is its own — ``page`` is covered above as
    a notable system, and by ``test_fetcher``."""
    parser = LlmElectionParser(
        SiteFetcher({SEATS: text}),
        extractor or MockExtractor(),
        MockResolver(sources=(SEATS,)),
    )
    return ImportService(store, parser)


async def test_each_step_of_an_import_says_how_it_went(caplog):
    """Resolution, extraction, and the import around them — one line each."""
    caplog.set_level(logging.INFO, logger="app")
    service = import_service(InMemoryElectionStore())

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.request_key, timeout=2.0)

    logged = spans(caplog)
    assert ("anthropic", "resolve", "ok") in logged
    assert ("anthropic", "extract", "ok") in logged
    assert ("import", "run", "ok") in logged


async def test_a_successful_import_is_only_a_handful_of_lines(caplog):
    """The point of the whole arrangement: the log of one import is readable."""
    caplog.set_level(logging.INFO, logger="app")
    service = import_service(InMemoryElectionStore())

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.request_key, timeout=2.0)
    await service.confirm(submitted.request_key)

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) <= 10, "\n".join(lines)
    assert not any(" start " in line for line in lines), "nothing before the fact"


async def test_the_import_is_identified_by_what_was_asked_for(caplog):
    """A log line that cannot be tied back to a request is not much use."""
    caplog.set_level(logging.INFO, logger="app")
    service = import_service(InMemoryElectionStore())

    submitted = await service.submit(make_request(year=2026, nation="Danmark"))
    await service.wait_for(submitted.request_key, timeout=2.0)

    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "Danmark 2026" in everything
    assert submitted.request_key[:12] in everything


def test_inbound_requests_are_logged_for_debugging(caplog):
    """One per request, and uvicorn already logs its own; INFO is not the place."""
    from fastapi.testclient import TestClient

    from app import main

    caplog.set_level(logging.DEBUG, logger="app")
    with TestClient(main.app) as client:
        client.get("/healthz")

    logged = spans(caplog, system="http")
    assert ("http", "request", "ok") in logged
    assert all(
        r.levelno == logging.DEBUG
        for r in caplog.records
        if r.getMessage().startswith("http request")
    )
    assert any("status=200" in r.getMessage() for r in caplog.records)
    assert any("path=/healthz" in r.getMessage() for r in caplog.records)


async def test_a_failed_import_is_logged_as_failed(caplog):
    caplog.set_level(logging.INFO, logger="app")
    store = InMemoryElectionStore()
    service = ImportService(store, CountingParser(fail_times=1))

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.request_key, timeout=2.0)

    assert ("import", "run", "failed") in spans(caplog)


async def test_page_content_never_reaches_the_log(caplog):
    """Only shapes and sizes: a results page can be large and is untrusted."""
    caplog.set_level(logging.DEBUG, logger="app")
    secret = "SENSITIVE-PAGE-BODY-MARKER"
    service = import_service(InMemoryElectionStore(), text=secret)

    submitted = await service.submit(make_request())
    await service.wait_for(submitted.request_key, timeout=2.0)

    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in everything
    assert "page_chars=" in everything, "its size is logged instead"


class _capture:
    """Minimal handler-based capture; caplog does not see records this early."""

    def __init__(self, logger, level=logging.INFO):
        self._logger = logger
        self._level = level
        self._records: list[logging.LogRecord] = []

    def __enter__(self):
        self._logger.setLevel(self._level)
        handler = logging.Handler()
        handler.emit = self._records.append
        self._handler = handler
        self._logger.addHandler(handler)
        return self._records

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False
