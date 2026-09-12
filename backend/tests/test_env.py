"""The .env file fills gaps in the environment and never overrides it."""

from __future__ import annotations

import os

import pytest

from app.env import EnvFileError, find_env_file, load_env_file, parse_env_file


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A=1\nB=2\n", {"A": "1", "B": "2"}),
        ("# comment\n\n  \nA=1\n", {"A": "1"}),
        ("export A=1\n", {"A": "1"}),
        ("A = 1 \n", {"A": "1"}),                       # spaces around both sides
        ('A="sk ant"\n', {"A": "sk ant"}),              # quotes preserve the space
        ("A='#notacomment'\n", {"A": "#notacomment"}),
        ("A=1  # trailing\n", {"A": "1"}),
        ('A="sk ant"  # trailing\n', {"A": "sk ant"}),   # quotes *and* a comment
        ('A="a#b"\n', {"A": "a#b"}),
        ("A=a#b\n", {"A": "a#b"}),                      # no whitespace, so not a comment
        ("A=\n", {"A": ""}),                            # explicitly empty
        ("A=1\nA=2\n", {"A": "2"}),                     # the later line wins
    ],
)
def test_parsing(text, expected):
    assert parse_env_file(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "A\n",
        "not a variable\n",
        "9A=1\n",
        "A B=1\n",
        'A="unterminated\n',
        'A="quoted" and more\n',   # the value is ambiguous; do not guess at one
    ],
)
def test_a_malformed_line_is_an_error_naming_it(text):
    with pytest.raises(EnvFileError) as raised:
        parse_env_file(text, "somewhere/.env")
    assert "somewhere/.env:1" in str(raised.value)


def write_env(tmp_path, text, name=".env"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_it_sets_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("KB_TEST_NEW", raising=False)
    path, applied = load_env_file(write_env(tmp_path, "KB_TEST_NEW=from-file\n"))
    assert applied == ("KB_TEST_NEW",)
    assert os.environ["KB_TEST_NEW"] == "from-file"
    assert path.name == ".env"
    monkeypatch.delenv("KB_TEST_NEW")


def test_the_real_environment_wins(tmp_path, monkeypatch):
    """`LLM_MODE=live uv run …` must beat a .env that says mock."""
    monkeypatch.setenv("KB_TEST_SET", "exported")
    _, applied = load_env_file(write_env(tmp_path, "KB_TEST_SET=from-file\n"))
    assert applied == ()
    assert os.environ["KB_TEST_SET"] == "exported"


def test_no_file_is_not_an_error(tmp_path, monkeypatch):
    """A deployment has no .env; the platform set the variables."""
    monkeypatch.setenv("ENV_FILE", "")
    assert find_env_file() is None
    assert load_env_file() == (None, ())


def test_an_explicit_file_is_used(tmp_path, monkeypatch):
    path = write_env(tmp_path, "KB_TEST_EXPLICIT=yes\n", name="other.env")
    monkeypatch.setenv("ENV_FILE", str(path))
    monkeypatch.delenv("KB_TEST_EXPLICIT", raising=False)
    assert find_env_file() == path
    load_env_file()
    assert os.environ["KB_TEST_EXPLICIT"] == "yes"
    monkeypatch.delenv("KB_TEST_EXPLICIT")


def test_a_missing_explicit_file_stops_the_boot(tmp_path, monkeypatch):
    """Silently ignoring the typo means discovering it as an unset API key."""
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "typo.env"))
    with pytest.raises(EnvFileError):
        find_env_file()


def test_it_is_found_from_a_subdirectory(tmp_path, monkeypatch):
    """The README starts the server in backend/; the file is at the repo root."""
    path = write_env(tmp_path, "KB_TEST_ROOT=yes\n")
    nested = tmp_path / "backend" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.chdir(nested)
    assert find_env_file() == path
