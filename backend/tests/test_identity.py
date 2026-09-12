"""Acceptance: same nation + state + date yields one hash; national != regional.

Plus the other key — what a user *asked* for — and the forgiving reading of the
year and of place names that the asking depends on.
"""

import pytest

from app.identity import (
    election_hash,
    normalize_place,
    normalize_year,
    request_key,
    same_place,
)


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


# --- the request key: what was asked for, before it is known what that is ----

def test_the_same_request_yields_the_same_key():
    assert request_key(2026, "Danmark") == request_key(2026, "Danmark")


@pytest.mark.parametrize("nation", ["danmark", "  Danmark  ", "DANMARK"])
def test_case_and_whitespace_do_not_split_a_request(nation):
    assert request_key(2026, nation) == request_key(2026, "danmark")


def test_a_year_given_as_text_keys_the_same_request():
    assert request_key("2026", "Danmark") == request_key(2026, "Danmark")


def test_a_region_makes_it_a_different_request():
    assert request_key(2026, "Germany") != request_key(2026, "Germany", "Saxony-Anhalt")


def test_field_boundaries_cannot_be_forged_in_a_request_key():
    assert request_key(2026, "a|b") != request_key(2026, "a", "b")


def test_a_request_key_is_not_an_election_hash():
    """Two keys for two different things: one is asked, the other is found."""
    assert request_key(2026, "Danmark") != election_hash("Danmark", None, "2026-03-25")


def test_a_request_needs_a_nation():
    with pytest.raises(ValueError):
        request_key(2026, "   ")


# --- the year, as typed ------------------------------------------------------

@pytest.mark.parametrize("given", [2026, "2026", " 2026 ", "2o26", "2O26"])
def test_a_year_is_read_the_way_it_was_meant(given):
    assert normalize_year(given) == 2026


@pytest.mark.parametrize("given", ["", "   ", "twenty", "20226", "1799", "2101", "20.26", None, True])
def test_a_year_that_cannot_be_a_year_is_refused(given):
    with pytest.raises((ValueError, TypeError)):
        normalize_year(given)


# --- comparing two spellings of one place -----------------------------------

@pytest.mark.parametrize(
    "left,right",
    [
        ("Saxony-Anhalt", "saxony anhalt"),
        ("Saxony-Anhalt", "SaxonyAnhalt"),
        ("Baden-Württemberg", "baden wurttemberg"),
        ("North Rhine-Westphalia", "North Rhine Westphalia"),
    ],
)
def test_one_place_spelled_two_ways_is_one_place(left, right):
    assert same_place(left, right)


@pytest.mark.parametrize("left,right", [("Saxony-Anhalt", "Saxony"), ("Germany", "Austria")])
def test_two_places_are_not_one(left, right):
    assert not same_place(left, right)


def test_no_region_is_a_place_of_its_own():
    """``None`` means "this was the national election", which is a claim."""
    assert same_place(None, None)
    assert not same_place(None, "Saxony-Anhalt")
    assert not same_place("Saxony-Anhalt", None)
