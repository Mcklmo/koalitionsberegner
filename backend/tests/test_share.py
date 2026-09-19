"""A shared link's selection and the card it unfurls into (app/share.py).

``js/share.js`` mirrors the parsing rules; its test file lists the same cases.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.schema import MAX_BLOCKS, MAX_PARTIES_PER_BLOCK
from app.share import (
    MAX_DESCRIPTION,
    card,
    flatten,
    format_date,
    image_etag,
    parse_seats,
    parse_selection,
    share_id,
    valid_id,
    verdict,
)
from tests.factories import make_election, make_forecast

HASH = "3f" * 32


def _party(abbr: str, seats: int, color: str = "#123456") -> dict:
    return {"name": f"Party {abbr[:20]}", "abbr": abbr, "seats": seats, "color": color}


#: 179 seats, 90 for a majority, ordered so the plan's examples come out as written.
FOLKETING = {
    "total_seats": 179,
    "majority_seats": 90,
    "blocks": [
        {"name": "Red", "parties": [_party("A", 50, "#A82721"), _party("F", 15), _party("B", 7), _party("Ø", 7)]},
        {"name": "Blue", "parties": [_party("M", 15), _party("V", 17), _party("C", 13),
                                     _party("I", 20), _party("Æ", 20), _party("O", 15)]},
    ],
}
A, F, B, OE, M, V, C, I, AE, O = range(10)


def folketing(**overrides):
    return make_election(**{**FOLKETING, **overrides})


def folketing_forecast(**overrides):
    return make_forecast(**{**FOLKETING, "published_on": "2026-09-12", "computed": True, **overrides})


# --- ids ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0123456789ab", "3f" * 32, "abcdef0123456789"])
def test_ids_of_twelve_to_sixty_four_hex_characters_are_valid(value):
    assert valid_id(value)


@pytest.mark.parametrize("value", ["", "0123456789a", "3f" * 32 + "0", "0123456789AB", "0123456789ag", "../0123456789ab"])
def test_short_long_uppercase_or_non_hex_ids_are_not(value):
    assert not valid_id(value)


def test_a_link_carries_the_first_sixteen_characters_of_the_hash():
    assert share_id(HASH) == HASH[:16]
    assert valid_id(share_id(HASH))


# --- flatten and c -----------------------------------------------------------


def test_flatten_lists_parties_block_by_block():
    assert [p.abbr for p in flatten(folketing())] == ["A", "F", "B", "Ø", "M", "V", "C", "I", "Æ", "O"]


@pytest.mark.parametrize("c", [None, ""])
def test_missing_or_empty_c_selects_nothing(c):
    assert parse_selection(c, folketing()) == []


def test_c_is_read_as_positions():
    assert parse_selection("0,2,3,7", folketing()) == [0, 2, 3, 7]


def test_c_is_deduplicated_and_sorted():
    assert parse_selection("7,3,3,0,7", folketing()) == [0, 3, 7]


def test_an_index_past_the_last_party_is_dropped_on_its_own():
    assert parse_selection("1,10,9,500", folketing()) == [1, 9]


def test_leading_zeros_are_the_same_number():
    assert parse_selection("007", folketing()) == [7]


@pytest.mark.parametrize("c", [
    "a", "1,a", "1,,2", "1,", ",1", "-1", "+1", " 1", "1 ", "1.0", "0x1", "1e2",
    "١",  # an Arabic-Indic one: int() reads it, js/share.js would not
    "123456",  # wider than any position can be
])
def test_a_malformed_c_selects_nothing(c):
    assert parse_selection(c, folketing()) == []


def test_c_naming_more_parties_than_any_election_holds_selects_nothing():
    bound = MAX_BLOCKS * MAX_PARTIES_PER_BLOCK
    assert parse_selection(",".join(["1"] * (bound + 1)), folketing()) == []
    assert parse_selection("1" + ",1" * (bound - 1), folketing()) == [1]


def test_an_absurdly_long_c_is_refused_before_it_is_split():
    assert parse_selection("1," * 1_000_000, folketing()) == []


# --- s -----------------------------------------------------------------------


@pytest.mark.parametrize("s, expected", [("79", 79), ("0", 0), ("100000", 100_000), ("0079", 79)])
def test_s_is_a_seat_total(s, expected):
    assert parse_seats(s) == expected


@pytest.mark.parametrize("s", [None, "", "-1", "100001", "1234567", "7.5", "x", " 7", "٧"])
def test_a_missing_or_malformed_s_is_none(s):
    assert parse_seats(s) is None


# --- the card ----------------------------------------------------------------


def test_the_card_round_trips_through_its_own_link():
    election = folketing()
    made = card(election, HASH, parse_selection("7,0,3", election), None)
    assert made.page_path == f"/e/{HASH[:16]}?c=0,3,7&s=77"
    assert made.image_path == f"/api/og/{HASH[:16]}.png?c=0,3,7&s=77"

    query = dict(pair.split("=") for pair in made.page_path.split("?")[1].split("&"))
    again = card(election, HASH, parse_selection(query["c"], election), parse_seats(query["s"]))
    assert again == made
    assert not again.stale


def test_a_card_with_nothing_selected_links_to_the_bare_election():
    made = card(folketing(), HASH, [], None)
    assert made.page_path == f"/e/{HASH[:16]}"
    assert made.image_path == f"/api/og/{HASH[:16]}.png"
    assert made.parties == ()
    assert (made.total, made.majority) == (0, False)


def test_the_card_normalises_a_selection_it_was_handed():
    election = folketing()
    assert card(election, HASH, [9, 1, 1, 99, -1], None) == card(election, HASH, [1, 9], None)


def test_the_card_carries_the_selected_parties_in_list_order():
    made = card(folketing(), HASH, [AE, A], None)
    assert made.parties == (("A", 50, "#A82721"), ("Æ", 20, "#123456"))
    assert (made.total, made.total_seats, made.majority_seats) == (70, 179, 90)
    assert made.title == "Koalitionsberegner"


def test_a_seat_total_other_than_the_selection_makes_the_card_stale():
    election = folketing()
    assert card(election, HASH, [A, F], 65).stale is False
    assert card(election, HASH, [A, F], 64).stale is True
    assert card(election, HASH, [], 5).stale is True
    assert card(election, HASH, [A, F], None).stale is False


def test_description_with_nothing_selected():
    assert card(folketing(), HASH, [], None).description == (
        "Pick parties and see whether they reach the 90 seats a majority needs. "
        "Final result, 25 Mar 2026."
    )


def test_description_of_a_coalition_short_of_a_majority():
    made = card(folketing(), HASH, [A, F, B, OE], None)
    assert made.description == "A + F + B + Ø: 79 of 179 seats, 11 short of a majority. Final result, 25 Mar 2026."
    assert (made.majority, made.verdict) == (False, "short")


def test_description_of_a_majority_in_a_computed_forecast():
    made = card(folketing_forecast(), HASH, [A, M, V, C], None)
    assert made.description == (
        "A + M + V + C: 95 of 179 seats, a majority (+5). Forecast: Voxmeter, 12 Sep 2026, seats computed."
    )
    assert (made.majority, made.verdict) == (True, "majority")


def test_a_forecast_whose_seats_were_published_does_not_say_computed():
    made = card(folketing_forecast(computed=False), HASH, [A], None)
    assert made.description.endswith("Forecast: Voxmeter, 12 Sep 2026.")
    assert made.subtitle == "Forecast · Voxmeter · 12 Sep 2026"


def test_subtitles():
    assert card(folketing(), HASH, [], None).subtitle == "Final result · 25 Mar 2026"
    assert card(folketing_forecast(), HASH, [], None).subtitle == (
        "Forecast · Voxmeter · 12 Sep 2026 · seats computed"
    )


def test_description_of_a_large_majority():
    made = card(folketing(), HASH, [A, F, B, OE, M, V, C], None)
    assert made.description.startswith("A + F + B + Ø + M + V + C: 124 of 179 seats, a large majority (+34).")
    assert made.verdict == "large"


def test_the_description_names_eight_parties_then_counts_the_rest():
    made = card(folketing(), HASH, list(range(10)), None)
    assert made.description.startswith("A + F + B + Ø + M + V + C + I +2 more: 179 of 179 seats")
    assert len(made.parties) == 10


def test_the_description_stays_under_the_limit_with_the_longest_text_the_schema_allows():
    long_blocks = [{"name": "B", "parties": [_party(ch * 200, 1) for ch in "ABCDEFGHIJ"]}]
    election = make_forecast(
        total_seats=10, majority_seats=6, blocks=long_blocks, publisher="P" * 200, title="T" * 200,
    )
    for selection in ([], [0], list(range(10))):
        description = card(election, HASH, selection, None).description
        assert len(description) <= MAX_DESCRIPTION
        assert description


def test_long_abbreviations_name_fewer_parties_before_the_line_is_cut():
    blocks = [{"name": "B", "parties": [_party(ch * 40, 1) for ch in "ABCDEFGHIJ"]}]
    description = card(make_election(total_seats=10, majority_seats=6, blocks=blocks), HASH,
                       list(range(10)), None).description
    assert len(description) <= MAX_DESCRIPTION
    assert description.startswith("A" * 40 + " + " + "B" * 40 + " +8 more: 10 of 10 seats")


# --- verdict and dates -------------------------------------------------------


@pytest.mark.parametrize("total, expected", [
    (0, "short"), (89, "short"), (90, "majority"), (119, "majority"), (120, "large"), (179, "large"),
])
def test_verdict_draws_the_lines_where_the_page_does(total, expected):
    # js/app.js: a majority from 90; large above floor(179 * 2 / 3) = 119.
    assert verdict(total, folketing()) == expected


def test_dates_are_english_whatever_the_locale():
    assert format_date(date(2026, 3, 25)) == "25 Mar 2026"
    assert format_date(date(2026, 9, 1)) == "1 Sep 2026"


# --- the image's ETag --------------------------------------------------------


def test_the_etag_is_quoted_and_stable():
    tag = image_etag(HASH, [0, 3], 57, 1_700_000_000.5)
    assert tag == image_etag(HASH, [0, 3], 57, 1_700_000_000.5)
    assert tag.startswith('"') and tag.endswith('"')


@pytest.mark.parametrize("change", [
    {"election_hash": "4f" * 32},
    {"selection": [0, 4]},
    {"seats_claimed": 58},
    {"seats_claimed": None},
    {"stored_at": 1_700_000_001.5},
])
def test_the_etag_changes_with_anything_the_image_depends_on(change):
    base = {"election_hash": HASH, "selection": [0, 3], "seats_claimed": 57, "stored_at": 1_700_000_000.5}
    assert image_etag(**base) != image_etag(**{**base, **change})
