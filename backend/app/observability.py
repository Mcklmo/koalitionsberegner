"""Logging around the boundaries where this service talks to something else.

Every call out — asking an agent, searching, fetching a page, reading or writing
the store — is wrapped in :func:`io_span`, which times it and reports how it
went. What reaches the default log, though, is only the part worth reading:

- **One line per notable call.** An import that resolves an election, reads two
  pages and extracts one is half a dozen lines, not forty. The systems that
  count as notable are the ones that leave this process and cost real time or
  money (:data:`NOTABLE_SYSTEMS`); the store and the inbound request are not
  among them, however often they are called.
- **Nothing before the fact.** The "start" half of every span is DEBUG. A
  finished call says how long it took, which is the same information and one
  line instead of two — and ``LOG_LEVEL=DEBUG`` brings the pairs back when a
  hung dependency has to be named.
- **Every failure, always.** A span that raises logs at WARNING whatever system
  it belongs to, because that is the line somebody is looking for.

What is deliberately *not* logged: page content, prompts, model output, and
anything else that could carry a page's text into the log. Only shapes and
sizes. Values are sanitised because some of them originate in text a user typed
or an address a model produced, and a newline in a log line is a forged entry.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

MAX_VALUE_CHARS = 200

#: Systems whose every call earns a line of its own: a model call, a search, a
#: page fetch, a report email, and the import all of them serve. What is missing from this set is the store and the inbound request —
#: called several times per import, and saying nothing the import's own lines
#: do not already say. They are still there at ``LOG_LEVEL=DEBUG``.
NOTABLE_SYSTEMS = frozenset(
    {"anthropic", "google", "wikipedia", "page", "import", "smtp"}
)


def scrub(value: object, limit: int = MAX_VALUE_CHARS) -> str:
    """Render a value safe to concatenate into a log line."""
    text = str(value)
    # Control characters would let untrusted input forge log records.
    text = "".join(ch if 32 <= ord(ch) < 127 or ord(ch) > 159 else " " for ch in text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fields(fields: dict) -> str:
    return " ".join(f"{k}={scrub(v)}" for k, v in fields.items() if v is not None)


@contextmanager
def io_span(log: logging.Logger, system: str, operation: str, **context):
    """Time one interaction with an external system, and report how it went.

    Yields a dict; anything put in it is added to the completion line, so a
    caller can report what came back (status codes, sizes, token counts).

        with io_span(log, "anthropic", "extract", model=MODEL) as span:
            response = await client.messages.parse(...)
            span["output_tokens"] = response.usage.output_tokens

    The completion line is INFO for a system in :data:`NOTABLE_SYSTEMS` and
    DEBUG for the rest; a failure is always WARNING.
    """
    level = logging.INFO if system in NOTABLE_SYSTEMS else logging.DEBUG
    started = time.perf_counter()
    # Before the fact there is nothing to say but "this was attempted", so it
    # stays at DEBUG — where it is exactly what names a call that never returned.
    log.debug("%s %s start %s", system, operation, _fields(context))
    result: dict = {}
    try:
        yield result
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        log.warning(
            "%s %s failed %s",
            system, operation,
            _fields({**context, **result, "ms": elapsed_ms,
                     "error": type(exc).__name__, "detail": exc}),
        )
        raise
    else:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        log.log(level, "%s %s ok %s", system, operation,
                _fields({**context, **result, "ms": elapsed_ms}))


def configure_logging() -> None:
    """Send our loggers to stdout at ``LOG_LEVEL`` (default INFO).

    Cloud Run collects stdout, and uvicorn already owns the root handler, so
    this only sets levels rather than adding a second handler.

    ``httpx`` is turned down to warnings unless we are debugging: it logs a line
    for every request, which says what :mod:`app.fetcher`'s own span already
    says, only without the part that matters.
    """
    import os

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.getLogger("app").setLevel(level)
    logging.getLogger("httpx").setLevel(logging.DEBUG if level == "DEBUG" else logging.WARNING)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
