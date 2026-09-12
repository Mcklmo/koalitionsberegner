"""Reading a ``.env`` file, so a local run needs nothing exported.

Every switch this app has is an environment variable, which is exactly right
for a deployment — Cloud Run sets them, Secret Manager mounts the secrets — and
tedious locally, where the alternative was prefixing each command with the same
keys, or ``source``-ing a file and remembering which shell it happened in.

Two rules keep that convenience from becoming a hazard:

*The real environment always wins.* A variable already present is never
replaced, so ``LLM_MODE=live uv run uvicorn …`` still overrides a ``.env`` that
says otherwise, and a file that accidentally lands in an image cannot shadow
what the platform set.

*A file that cannot be read is an error, not a shrug.* A malformed line or a
missing ``ENV_FILE`` stops the boot, for the same reason :mod:`app.config`
refuses an unrecognised value: a typo that silently leaves ``ANTHROPIC_API_KEY``
unset is discovered by the first user, not by the developer who made it.

Nothing here logs a value. Only names — the file holds API keys.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

#: The variable naming a file explicitly. Empty means "load no file at all",
#: which is how the test suite keeps a developer's keys out of the run.
ENV_FILE_VAR = "ENV_FILE"

DEFAULT_FILENAME = ".env"

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EnvFileError(RuntimeError):
    """The ``.env`` file exists but cannot be understood."""


def parse_env_file(text: str, source: str = ".env") -> dict[str, str]:
    """Parse ``KEY=value`` lines.

    Supported, because a hand-written file uses it: blank lines, ``#`` comments,
    a leading ``export``, quoted values (single or double), and a trailing
    ``# comment`` after an unquoted value. Deliberately not supported: values
    spanning several lines, and ``$VAR`` expansion — a shell does those, this is
    a file of literals, and guessing would make the same file mean two things.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.fullmatch(name):
            raise EnvFileError(f"{source}:{number}: expected NAME=value, got {raw.strip()!r}")
        try:
            values[name] = _clean_value(value.strip())
        except ValueError as exc:
            raise EnvFileError(f"{source}:{number}: {exc}") from None
    return values


def _clean_value(value: str) -> str:
    """The literal a value denotes: quotes unwrapped, a trailing comment dropped."""
    if value[:1] in ("\"", "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end < 0:
            raise ValueError(f"unterminated {quote} in {value!r}")
        rest = value[end + 1:].strip()
        if rest and not rest.startswith("#"):
            # Keeping either half would be a wrong value that nothing reports.
            raise ValueError(f"unexpected text after the closing {quote}: {rest!r}")
        return value[1:end]
    # Unquoted: only a ``#`` that follows whitespace starts a comment, because a
    # secret may legitimately contain one and ``pw=a#b`` must keep it.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def find_env_file() -> Path | None:
    """Locate the file to load, or ``None`` when there is none to load.

    ``ENV_FILE`` decides when set — a path, or empty to load nothing. Otherwise
    a ``.env`` is looked for upwards from the working directory and from this
    package, so the repo-root file is found whether the server was started from
    ``backend/`` (as the README says) or from the root.
    """
    explicit = os.environ.get(ENV_FILE_VAR)
    if explicit is not None:
        if not explicit.strip():
            return None
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise EnvFileError(f"{ENV_FILE_VAR}={explicit!r} is not a file")
        return path

    seen: set[Path] = set()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for directory in (start, *start.parents):
            if directory in seen:
                continue
            seen.add(directory)
            candidate = directory / DEFAULT_FILENAME
            if candidate.is_file():
                return candidate
    return None


def load_env_file(path: Path | None = None) -> tuple[Path | None, tuple[str, ...]]:
    """Put the file's variables into :data:`os.environ`, without overwriting.

    Returns the file used and the names it actually set, which is what the
    startup log reports — a deployment should be able to see that a stray file
    was picked up, and a local run that a variable came from it.
    """
    path = path or find_env_file()
    if path is None:
        return None, ()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvFileError(f"cannot read {path}: {exc}") from None

    applied, overridden = [], []
    for name, value in parse_env_file(text, str(path)).items():
        if name in os.environ:
            overridden.append(name)
            continue
        os.environ[name] = value
        applied.append(name)

    if overridden:
        log.debug("%s: kept the exported %s", path, ", ".join(sorted(overridden)))
    return path, tuple(applied)
