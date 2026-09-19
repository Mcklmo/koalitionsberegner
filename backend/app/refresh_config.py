"""When a tracked election is read again: the schedule, as configuration.

The intervals live in ``refresh.yaml`` next to this module rather than in code,
because they are a judgement about how fast polls move and results firm up,
and changing one should not be a code review. ``REFRESH_CONFIG`` names another
file; ``REFRESH_TICK`` is how often the scheduler fires (the Worker's cron),
which is the finest interval that can actually be honoured.

Loading is strict, for the reason :mod:`app.config` gives: a schedule with a
typo does not fail loudly on its own, it just refreshes the wrong elections at
the wrong times until somebody notices. So an unknown key, a missing key, a
duplicate key, rows out of order or a malformed duration is an error that
stops the boot, and every message quotes the value it could not accept.

Everything here is pure: no clock, no store. The refresh service passes ``now``
in, which is what lets the tests walk across election day a second at a time.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

log = logging.getLogger(__name__)

#: The variable naming a schedule file other than the bundled one.
CONFIG_VAR = "REFRESH_CONFIG"
#: The variable giving the scheduler's period, as a duration.
TICK_VAR = "REFRESH_TICK"

DEFAULT_PATH = Path(__file__).with_name("refresh.yaml")
#: The Worker's refresh cron fires every 30 minutes.
DEFAULT_TICK = "30m"

_UNITS = {"d": "days", "h": "hours", "m": "minutes"}
_PART = re.compile(r"([0-9]+)([dhm])")  # ASCII only: \d also matches other scripts
_WHOLE = re.compile(r"(?:[0-9]+[dhm])+")

_SECTIONS = ("before_election", "after_election", "results", "polls", "failures")


class RefreshConfigError(ValueError):
    """The refresh schedule cannot be understood."""


def parse_duration(value: Any, *, allow_zero: bool = False) -> timedelta:
    """``"1d12h30m"`` as a :class:`~datetime.timedelta`.

    One or more of an integer and a unit (``d``, ``h``, ``m``), each unit at
    most once, in any order, with nothing between them. The total must be
    above zero unless ``allow_zero``, which only the ``more_than: 0d`` row
    needs. A bare number is refused rather than read as some default unit:
    ``every: 30`` could mean minutes or days, and guessing is how a schedule
    ends up a thousand times too eager.
    """
    if not isinstance(value, str) or not _WHOLE.fullmatch(value):
        raise RefreshConfigError(
            f"{value!r} is not a duration: use an integer and a unit (d, h or m), "
            "chained without spaces, such as 30m, 12h or 1d12h"
        )
    parts: dict[str, int] = {}
    for amount, unit in _PART.findall(value):
        if unit in parts:
            raise RefreshConfigError(f"{value!r} gives the unit {unit!r} more than once")
        parts[unit] = int(amount)
    duration = timedelta(**{_UNITS[unit]: amount for unit, amount in parts.items()})
    if not allow_zero and duration <= timedelta(0):
        raise RefreshConfigError(f"{value!r} must be longer than zero")
    return duration


@dataclass(frozen=True)
class BeforeRow:
    """While more than ``more_than`` is left until election day, read every ``every``."""

    more_than: timedelta
    every: timedelta


@dataclass(frozen=True)
class AfterRow:
    """Until ``within`` has passed since election day began, read every ``every``."""

    within: timedelta
    every: timedelta


@dataclass(frozen=True)
class RefreshConfig:
    """The validated schedule. Build it with :meth:`load`, not by hand."""

    before_election: tuple[BeforeRow, ...]   # descending more_than, ending at 0
    after_election: tuple[AfterRow, ...]     # ascending within
    stable_after: int
    keep_newest: int
    backoff_factor: int
    max_backoff: timedelta
    park_after: int

    @classmethod
    def load(cls, path: Path | str | None = None, *, tick: timedelta | None = None) -> RefreshConfig:
        """Read and validate a schedule file; the bundled one when ``path`` is ``None``.

        ``tick`` is the scheduler's period. A row asking for more often than
        that is legal but cannot be honoured, so it earns a warning, not an
        error: tightening the cron should not require editing this file first.
        """
        path = Path(path) if path is not None else DEFAULT_PATH
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RefreshConfigError(f"cannot read {path}: {exc}") from None
        try:
            data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — a SafeLoader subclass
        except yaml.YAMLError as exc:
            raise RefreshConfigError(f"{path}: not valid YAML: {exc}") from None
        try:
            return cls.from_mapping(data, tick=tick)
        except RefreshConfigError as exc:
            raise RefreshConfigError(f"{path}: {exc}") from None

    @classmethod
    def from_mapping(cls, data: Any, *, tick: timedelta | None = None) -> RefreshConfig:
        """Validate an already-parsed document. :meth:`load` is the usual entry."""
        top = _mapping(data, "the file", _SECTIONS)

        before = tuple(
            BeforeRow(
                more_than=_duration(row, "more_than", where, allow_zero=True),
                every=_duration(row, "every", where),
            )
            for where, row in _rows(top["before_election"], "before_election", ("more_than", "every"))
        )
        for index in range(1, len(before)):
            if before[index].more_than >= before[index - 1].more_than:
                raise RefreshConfigError(
                    f"before_election[{index}]: more_than must be smaller than the row above it; "
                    "keep before_election in descending order"
                )
        if before[-1].more_than != timedelta(0):
            raise RefreshConfigError(
                "before_election needs a last row with more_than: 0d, "
                "or the final days before an election match no row"
            )

        after = tuple(
            AfterRow(
                within=_duration(row, "within", where),
                every=_duration(row, "every", where),
            )
            for where, row in _rows(top["after_election"], "after_election", ("within", "every"))
        )
        for index in range(1, len(after)):
            if after[index].within <= after[index - 1].within:
                raise RefreshConfigError(
                    f"after_election[{index}]: within must be larger than the row above it; "
                    "keep after_election in ascending order"
                )

        results = _mapping(top["results"], "results", ("stable_after",))
        polls = _mapping(top["polls"], "polls", ("keep_newest",))
        failures = _mapping(
            top["failures"], "failures", ("backoff_factor", "max_backoff", "park_after")
        )

        config = cls(
            before_election=before,
            after_election=after,
            stable_after=_positive_int(results, "stable_after", "results"),
            keep_newest=_positive_int(polls, "keep_newest", "polls"),
            backoff_factor=_positive_int(failures, "backoff_factor", "failures"),
            max_backoff=_duration(failures, "max_backoff", "failures"),
            park_after=_positive_int(failures, "park_after", "failures"),
        )
        config._warn_below_tick(tick if tick is not None else tick_from_env())
        return config

    def _warn_below_tick(self, tick: timedelta) -> None:
        rows = [
            *((f"before_election[{i}]", row) for i, row in enumerate(self.before_election)),
            *((f"after_election[{i}]", row) for i, row in enumerate(self.after_election)),
        ]
        for where, row in rows:
            if row.every < tick:
                log.warning(
                    "refresh schedule %s asks for every %s, but the scheduler only "
                    "fires every %s, so it cannot be read more often than that",
                    where, row.every, tick,
                )


def tick_from_env() -> timedelta:
    """The scheduler's period from ``REFRESH_TICK``, 30 minutes by default."""
    raw = os.environ.get(TICK_VAR)
    value = raw.strip() if raw and raw.strip() else DEFAULT_TICK
    try:
        return parse_duration(value)
    except RefreshConfigError as exc:
        raise RefreshConfigError(f"{TICK_VAR}: {exc}") from None


def config_path() -> Path:
    """The schedule file: ``REFRESH_CONFIG`` when set, else the bundled one."""
    raw = os.environ.get(CONFIG_VAR)
    return Path(raw.strip()).expanduser() if raw and raw.strip() else DEFAULT_PATH


def load_configured() -> RefreshConfig:
    """The schedule the environment asks for. What startup validation calls."""
    return RefreshConfig.load(config_path(), tick=tick_from_env())


def election_day_start(election_date: date) -> datetime:
    """00:00 UTC on election day, the instant both halves of the schedule count from."""
    return datetime.combine(election_date, time.min, tzinfo=UTC)


def interval_for(config: RefreshConfig, now: datetime, election_date: date) -> timedelta | None:
    """How long until the next read, or ``None`` once past the last window.

    Before election day the time left picks a ``before_election`` row: the
    first whose ``more_than`` it exceeds, so exactly 180 days left falls to the
    next row down. From 00:00 UTC on the day itself the time since picks an
    ``after_election`` row: the first whose ``within`` has not yet passed, so
    exactly two days after still counts as election night. ``None`` means the
    schedule is done with this election; the caller parks it unless it is
    already final.
    """
    day0 = election_day_start(election_date)
    if now < day0:
        left = day0 - now
        for row in config.before_election:
            if left > row.more_than:
                return row.every
    since = now - day0
    for row in config.after_election:
        if since <= row.within:
            return row.every
    return None


class _Scheduled(Protocol):
    next_refresh_at: datetime | None


def is_due(tracked: _Scheduled, now: datetime) -> bool:
    """Whether a tracked election should be read on this tick.

    Never scheduled (``None``) counts as due: a row the calendar scan or the
    owner has just added is read on the next tick.
    """
    return tracked.next_refresh_at is None or now >= tracked.next_refresh_at


def next_refresh_at(
    config: RefreshConfig, now: datetime, interval: timedelta, consecutive_failures: int
) -> datetime:
    """When to read again after a run that ended at ``now``.

    Each consecutive failure multiplies ``interval`` by ``backoff_factor``, up
    to ``max_backoff``. The cap limits the backoff, not the schedule: an
    election six months away is still read monthly after a failure, not every
    seven days because 30d exceeds ``max_backoff``.
    """
    if consecutive_failures < 0:
        raise ValueError(f"consecutive_failures must not be negative, got {consecutive_failures}")
    cap = max(interval, config.max_backoff)
    delay = interval
    # Multiplied step by step rather than by factor ** failures, which would
    # overflow timedelta long before an election is parked for failing.
    for _ in range(consecutive_failures if config.backoff_factor > 1 else 0):
        delay *= config.backoff_factor
        if delay >= cap:
            return now + cap
    return now + delay


# --- validation helpers -------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses a key given twice in one mapping.

    PyYAML otherwise keeps the later value silently, so a pasted row with a
    second ``every:`` would quietly change the schedule.
    """


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _mapping(value: Any, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """``value`` as a mapping with exactly ``keys``, all present, none extra."""
    if not isinstance(value, dict):
        raise RefreshConfigError(f"{where} must be a mapping of {', '.join(keys)}, got {value!r}")
    unknown = [key for key in value if key not in keys]
    if unknown:
        raise RefreshConfigError(
            f"{where}: unknown key {unknown[0]!r}; expected {', '.join(keys)}"
        )
    missing = [key for key in keys if key not in value]
    if missing:
        raise RefreshConfigError(f"{where}: missing {missing[0]!r}")
    return value


def _rows(value: Any, section: str, keys: tuple[str, ...]) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(value, list) or not value:
        raise RefreshConfigError(f"{section} must be a non-empty list of rows, got {value!r}")
    return [
        (f"{section}[{index}]", _mapping(row, f"{section}[{index}]", keys))
        for index, row in enumerate(value)
    ]


def _duration(row: dict[str, Any], key: str, where: str, *, allow_zero: bool = False) -> timedelta:
    try:
        return parse_duration(row[key], allow_zero=allow_zero)
    except RefreshConfigError as exc:
        raise RefreshConfigError(f"{where}.{key}: {exc}") from None


def _positive_int(section: dict[str, Any], key: str, where: str) -> int:
    value = section[key]
    # bool is an int subclass, and ``stable_after: yes`` is not a count.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RefreshConfigError(f"{where}.{key} must be a positive whole number, got {value!r}")
    return value
