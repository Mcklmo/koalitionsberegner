"""The refresh schedule: its duration grammar, its validation and its windows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from app.refresh_config import (
    DEFAULT_PATH,
    AfterRow,
    BeforeRow,
    RefreshConfig,
    RefreshConfigError,
    interval_for,
    is_due,
    load_configured,
    next_refresh_at,
    parse_duration,
)

#: Mirrors the table in doc/plans/03-remaining-work.md, A2.
FIXTURE = """\
before_election:
  - more_than: 180d
    every: 30d
  - more_than: 60d
    every: 14d
  - more_than: 14d
    every: 7d
  - more_than: 0d
    every: 1d
after_election:
  - within: 2d
    every: 30m
  - within: 45d
    every: 1d
results:
  stable_after: 3
polls:
  keep_newest: 3
failures:
  backoff_factor: 2
  max_backoff: 7d
  park_after: 8
"""

D, H, M, S = timedelta(days=1), timedelta(hours=1), timedelta(minutes=1), timedelta(seconds=1)
TICK = 30 * M

ELECTION = date(2026, 11, 3)
DAY0 = datetime(2026, 11, 3, tzinfo=UTC)


def write(tmp_path, text, name="refresh.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path):
    return RefreshConfig.load(write(tmp_path, FIXTURE), tick=TICK)


def load_error(tmp_path, text) -> str:
    with pytest.raises(RefreshConfigError) as raised:
        RefreshConfig.load(write(tmp_path, text), tick=TICK)
    return str(raised.value)


# --- durations ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("30d", 30 * D),
        ("12h", 12 * H),
        ("30m", 30 * M),
        ("1d12h", D + 12 * H),
        ("1h30m", H + 30 * M),
        ("1d12h30m", D + 12 * H + 30 * M),
        ("30m1d", D + 30 * M),              # any order
        ("90m", 90 * M),                     # no carrying rules; it is just minutes
        ("0d1m", M),                         # a zero part is fine if the total is not
    ],
)
def test_valid_durations(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "value",
    [
        "", "30", "d", "30 m", "1d 12h", " 30m", "30m ", "30s", "30M", "1w",
        "-1d", "+1d", "1.5h", "1d,12h", "٣d",   # Arabic-Indic three: digits are ASCII only
        30, 1.5, None, True, ["30m"],
    ],
)
def test_invalid_durations_are_refused_quoting_the_value(value):
    with pytest.raises(RefreshConfigError) as raised:
        parse_duration(value)
    assert repr(value) in str(raised.value)


@pytest.mark.parametrize("text", ["1d2d", "1h30m1h", "5m5m"])
def test_a_unit_given_twice_is_refused(text):
    with pytest.raises(RefreshConfigError, match="more than once") as raised:
        parse_duration(text)
    assert repr(text) in str(raised.value)


@pytest.mark.parametrize("text", ["0d", "0m", "0d0h0m"])
def test_zero_is_refused_unless_allowed(text):
    with pytest.raises(RefreshConfigError, match="longer than zero"):
        parse_duration(text)
    assert parse_duration(text, allow_zero=True) == timedelta(0)


# --- loading ----------------------------------------------------------------------


def test_the_fixture_loads_into_the_table(config):
    assert config == RefreshConfig(
        before_election=(
            BeforeRow(180 * D, 30 * D),
            BeforeRow(60 * D, 14 * D),
            BeforeRow(14 * D, 7 * D),
            BeforeRow(timedelta(0), D),
        ),
        after_election=(AfterRow(2 * D, 30 * M), AfterRow(45 * D, D)),
        stable_after=3,
        keep_newest=3,
        backoff_factor=2,
        max_backoff=7 * D,
        park_after=8,
    )


def test_the_bundled_file_is_the_documented_table(config, caplog):
    with caplog.at_level(logging.WARNING, logger="app.refresh_config"):
        assert RefreshConfig.load(tick=TICK) == config
        assert RefreshConfig.load(DEFAULT_PATH, tick=TICK) == config
    assert not caplog.records   # nothing in it asks for more than the cron gives


def test_the_environment_picks_the_file_and_the_tick(tmp_path, monkeypatch, caplog):
    custom = FIXTURE.replace("every: 30m", "every: 10m")
    monkeypatch.setenv("REFRESH_CONFIG", str(write(tmp_path, custom)))
    monkeypatch.setenv("REFRESH_TICK", "10m")
    with caplog.at_level(logging.WARNING, logger="app.refresh_config"):
        assert load_configured().after_election[0].every == 10 * M
    assert not caplog.records


def test_without_the_environment_the_bundled_file_and_30m_are_used(monkeypatch, caplog):
    monkeypatch.delenv("REFRESH_CONFIG", raising=False)
    monkeypatch.delenv("REFRESH_TICK", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.refresh_config"):
        assert load_configured() == RefreshConfig.load(tick=TICK)
    assert not caplog.records


def test_a_malformed_tick_stops_the_boot(monkeypatch):
    monkeypatch.setenv("REFRESH_TICK", "30")
    with pytest.raises(RefreshConfigError, match="REFRESH_TICK: '30'"):
        load_configured()


def test_a_missing_file_stops_the_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("REFRESH_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(RefreshConfigError, match="cannot read"):
        load_configured()


def test_an_every_below_the_tick_is_a_warning_naming_the_row(tmp_path, caplog):
    text = FIXTURE.replace("every: 30m", "every: 15m")
    with caplog.at_level(logging.WARNING, logger="app.refresh_config"):
        loaded = RefreshConfig.load(write(tmp_path, text), tick=TICK)
    assert loaded.after_election[0].every == 15 * M
    assert len(caplog.records) == 1
    assert "after_election[0]" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    "old, new, expected",
    [
        # Unknown and missing keys, at every level.
        ("results:", "extra: 1\nresults:", "unknown key 'extra'"),
        ("    every: 14d", "    every: 14d\n    note: x", "before_election[1]: unknown key 'note'"),
        ("  stable_after: 3", "  stable_afer: 3", "results: unknown key 'stable_afer'"),
        ("  park_after: 8\n", "", "failures: missing 'park_after'"),
        ("polls:\n  keep_newest: 3\n", "", "missing 'polls'"),
        ("  - within: 45d\n    every: 1d", "  - within: 45d", "after_election[1]: missing 'every'"),
        # A key given twice would otherwise keep the later value silently.
        ("    every: 30d", "    every: 30d\n    every: 1d", "duplicate key 'every'"),
        # Order.
        ("more_than: 60d", "more_than: 200d", "before_election[1]: more_than must be smaller"),
        ("more_than: 60d", "more_than: 180d", "before_election[1]: more_than must be smaller"),
        ("within: 45d", "within: 1d", "after_election[1]: within must be larger"),
        ("within: 45d", "within: 2d", "after_election[1]: within must be larger"),
        # The final days before an election must match a row.
        ("more_than: 0d", "more_than: 1d", "more_than: 0d"),
        # Durations, quoting the value.
        ("every: 14d", "every: 14", "before_election[1].every: 14 is not a duration"),
        ("every: 7d", "every: 1 week", "before_election[2].every: '1 week' is not a duration"),
        ("within: 2d", "within: 0d", "after_election[0].within: '0d' must be longer than zero"),
        ("every: 1d\nafter", "every: 0m\nafter", "before_election[3].every: '0m' must be longer"),
        ("max_backoff: 7d", "max_backoff: 7d7d", "failures.max_backoff: '7d7d' gives the unit"),
        # Integers.
        ("stable_after: 3", "stable_after: 0", "results.stable_after must be a positive whole number, got 0"),
        ("keep_newest: 3", "keep_newest: -1", "polls.keep_newest must be a positive whole number, got -1"),
        ("keep_newest: 3", "keep_newest: 2.5", "got 2.5"),
        ("keep_newest: 3", "keep_newest: '3'", "got '3'"),
        ("park_after: 8", "park_after: yes", "failures.park_after must be a positive whole number, got True"),
        ("backoff_factor: 2", "backoff_factor: 0", "failures.backoff_factor must be a positive"),
    ],
)
def test_a_mistake_stops_the_boot_and_says_where(tmp_path, old, new, expected):
    assert old in FIXTURE
    message = load_error(tmp_path, FIXTURE.replace(old, new, 1))
    assert expected in message
    assert "refresh.yaml" in message


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", "the file must be a mapping"),
        ("- 1\n- 2\n", "the file must be a mapping"),
        ("before_election: [\n", "not valid YAML"),
        (FIXTURE.replace(
            "after_election:\n  - within: 2d\n    every: 30m\n  - within: 45d\n    every: 1d\n",
            "after_election: []\n",
        ), "after_election must be a non-empty list"),
        (FIXTURE.replace("  - more_than: 60d\n    every: 14d\n", "  - 5\n"),
         "before_election[1] must be a mapping"),
    ],
)
def test_a_malformed_document_stops_the_boot(tmp_path, text, expected):
    assert expected in load_error(tmp_path, text)


# --- windows ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "now, expected",
    [
        (DAY0 - 400 * D, 30 * D),
        (DAY0 - 180 * D - S, 30 * D),        # more than 180 days left
        (DAY0 - 180 * D, 14 * D),            # exactly 180 is not more than 180
        (DAY0 - 180 * D + S, 14 * D),
        (DAY0 - 60 * D - S, 14 * D),
        (DAY0 - 60 * D, 7 * D),
        (DAY0 - 14 * D - S, 7 * D),
        (DAY0 - 14 * D, D),
        (DAY0 - S, D),                        # the last second before the day
        (DAY0, 30 * M),                       # 00:00 UTC on election day: election night
        (DAY0 + S, 30 * M),
        (DAY0 + 2 * D, 30 * M),               # exactly two days after is still within
        (DAY0 + 2 * D + S, D),
        (DAY0 + 45 * D, D),
        (DAY0 + 45 * D + S, None),            # past the last window
        (DAY0 + 400 * D, None),
    ],
)
def test_interval_for_each_window(config, now, expected):
    assert interval_for(config, now, ELECTION) == expected


def test_interval_for_counts_from_midnight_utc_whatever_the_callers_zone(config):
    from datetime import timezone

    copenhagen = timezone(timedelta(hours=1))
    # 23:30 on the eve in Copenhagen is 22:30 UTC: before day0.
    assert interval_for(config, datetime(2026, 11, 2, 23, 30, tzinfo=copenhagen), ELECTION) == D
    # 00:30 on election day in Copenhagen is 23:30 UTC on the eve.
    assert interval_for(config, datetime(2026, 11, 3, 0, 30, tzinfo=copenhagen), ELECTION) == D
    assert interval_for(config, datetime(2026, 11, 3, 1, 0, tzinfo=copenhagen), ELECTION) == 30 * M


# --- due and next -----------------------------------------------------------------


@dataclass
class Tracked:
    next_refresh_at: datetime | None


@pytest.mark.parametrize(
    "scheduled, due",
    [(None, True), (DAY0 - S, True), (DAY0, True), (DAY0 + S, False)],
)
def test_is_due(scheduled, due):
    assert is_due(Tracked(scheduled), DAY0) is due


@pytest.mark.parametrize(
    "interval, failures, expected",
    [
        (D, 0, D),
        (D, 1, 2 * D),
        (D, 2, 4 * D),
        (D, 3, 7 * D),            # 8 days, capped at max_backoff
        (D, 30, 7 * D),
        (D, 10_000, 7 * D),       # no overflow however long it has been failing
        (30 * M, 4, 8 * H),
        (30 * D, 0, 30 * D),      # the cap limits backoff, never the schedule
        (30 * D, 5, 30 * D),
        (14 * D, 1, 14 * D),
    ],
)
def test_next_refresh_at_backs_off_and_caps(config, interval, failures, expected):
    assert next_refresh_at(config, DAY0, interval, failures) == DAY0 + expected


def test_next_refresh_at_refuses_negative_failures(config):
    with pytest.raises(ValueError):
        next_refresh_at(config, DAY0, D, -1)


def test_a_backoff_factor_of_one_never_backs_off(config):
    from dataclasses import replace

    flat = replace(config, backoff_factor=1)
    assert next_refresh_at(flat, DAY0, D, 10_000) == DAY0 + D
