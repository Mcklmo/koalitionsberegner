"""Logging around the boundaries where this service talks to something else.

Every call out — fetching a page, asking the agent, reading or writing Firestore
— is wrapped in :func:`io_span`, which logs once before and once after with a
duration and an outcome. That pairing is what makes a hung or slow dependency
visible: a "start" with no matching "ok" or "failed" names the culprit.

What is deliberately *not* logged: page content, prompts, model output, and
anything else that could carry a page's text into the log. Only shapes and
sizes. Values are sanitised because most of them originate in a URL somebody
pasted, and a newline in a log line is a forged log entry.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

MAX_VALUE_CHARS = 200


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
    """Log the start and end of one interaction with an external system.

    Yields a dict; anything put in it is added to the completion line, so a
    caller can report what came back (status codes, sizes, token counts).

        with io_span(log, "anthropic", "extract", model=MODEL) as span:
            response = await client.messages.parse(...)
            span["output_tokens"] = response.usage.output_tokens
    """
    started = time.perf_counter()
    log.info("%s %s start %s", system, operation, _fields(context))
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
        log.info("%s %s ok %s", system, operation,
                 _fields({**context, **result, "ms": elapsed_ms}))


def configure_logging() -> None:
    """Send our loggers to stdout at ``LOG_LEVEL`` (default INFO).

    Cloud Run collects stdout, and uvicorn already owns the root handler, so
    this only sets levels rather than adding a second handler.
    """
    import os

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.getLogger("app").setLevel(level)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
