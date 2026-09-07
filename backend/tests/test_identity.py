"""Acceptance: same nation + state + date yields one hash; national != regional."""

import pytest

from app.identity import election_hash, normalize_place


def test_same_metadata_yields_the_same_hash():
    assert election_hash("Danmark", None, "2026-03-25") == election_hash("Danmark", None, "2026-03-25")


@pytest.mark.parametrize("nation", ["danmark", "  Danmark  ", "DANMARK", "\nDanmark\t"])
def test_case_and_surrounding_whitespace_do_not_create_duplicates(nation):
    assert election_hash(nation, None, "2026-03-25") == election_hash("danmark", None, "2026-03-25")


@pytest.mark.parametrize("state", ["North Province", "north   province", " North  Province "])
def test_internal_whitespace_is_collapsed_not_removed(state):
    assert election_hash("Danmark", state, "2026-03-25") == election_hash(
        "Danmark", "north province", "2026-03-25"
    )


def test_time_of_day_does_not_affect_identity():
    assert election_hash("Danmark", None, "2026-03-25T08:26") == election_hash(
        "Danmark", None, "2026-03-25"
    )


def test_national_and_regional_elections_differ():
    national = election_hash("Danmark", None, "2026-03-25")
    regional = election_hash("Danmark", "Nordjylland", "2026-03-25")
    assert national != regional


def test_empty_state_is_treated_as_national():
    assert election_hash("Danmark", "   ", "2026-03-25") == election_hash("Danmark", None, "2026-03-25")


def test_different_states_differ():
    assert election_hash("Danmark", "Nordjylland", "2026-03-25") != election_hash(
        "Danmark", "Sjaelland", "2026-03-25"
    )


def test_different_dates_differ():
    assert election_hash("Danmark", None, "2026-03-25") != election_hash("Danmark", None, "2026-03-26")


def test_field_boundaries_cannot_be_forged():
    # A nation containing the separator must not collide with a nation+state pair.
    assert election_hash("a|b", None, "2026-03-25") != election_hash("a", "b", "2026-03-25")


def test_normalize_place_rejects_nothing_useful():
    assert normalize_place(None) is None
    assert normalize_place("  ") is None
    assert normalize_place(" North  Province ") == "north province"


def test_nation_is_required():
    with pytest.raises(ValueError):
        election_hash("   ", None, "2026-03-25")


def test_malformed_date_is_rejected():
    with pytest.raises(ValueError):
        election_hash("Danmark", None, "25/03/2026")
