"""The Python schema must enforce exactly what js/election.js enforces."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.factories import make_election


def reject(**overrides):
    with pytest.raises(ValidationError) as caught:
        make_election(**overrides)
    return str(caught.value)


def test_a_valid_election_is_accepted_and_frozen():
    election = make_election()
    assert election.total_seats == 10
    with pytest.raises(ValidationError):
        election.total_seats = 11


def test_optional_state_defaults_to_none():
    assert make_election().state is None
    assert make_election(state="Nordjylland").state == "Nordjylland"


def test_date_times_are_reduced_to_the_calendar_day():
    assert make_election(election_date="2026-03-25T08:26").election_date.isoformat() == "2026-03-25"


def test_unknown_fields_are_rejected():
    assert "Extra inputs are not permitted" in reject(sneaky="value")


def test_unknown_party_fields_are_rejected():
    blocks = make_election().model_dump()["blocks"]
    blocks[0]["parties"][0]["onclick"] = "alert(1)"
    assert "Extra inputs are not permitted" in reject(blocks=blocks)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"nation": "   "}, "must not be empty"),
        ({"title": ""}, "must not be empty"),
        ({"total_seats": "10"}, "valid integer"),
        ({"total_seats": 0}, "greater than or equal to 1"),
        ({"majority_seats": 5}, "not a majority of 10 seats"),
        ({"majority_seats": 11}, "not a majority of 10 seats"),
        ({"election_date": "25/03/2026"}, "ISO 8601"),
        ({"election_date": "2026-02-30"}, "ISO 8601"),
        ({"source_url": "not a url"}, "http or https"),
        ({"source_url": "javascript:alert(1)"}, "http or https"),
        ({"blocks": []}, "at least 1 item"),
    ],
)
def test_malformed_input_is_rejected(overrides, expected):
    assert expected in reject(**overrides)


def test_seat_sums_must_match_the_declared_total():
    blocks = make_election().model_dump()["blocks"]
    blocks[0]["parties"][0]["seats"] = 5
    assert "party seats sum to 9" in reject(blocks=blocks)


def test_negative_and_fractional_seats_are_rejected():
    blocks = make_election().model_dump()["blocks"]
    blocks[0]["parties"][0]["seats"] = -1
    assert "greater than or equal to 0" in reject(blocks=blocks)

    blocks[0]["parties"][0]["seats"] = 6.5
    assert reject(blocks=blocks)


def test_colors_must_be_hex():
    blocks = make_election().model_dump()["blocks"]
    blocks[0]["parties"][0]["color"] = "red; background:url(x)"
    assert "hex colour" in reject(blocks=blocks)


def test_control_characters_are_rejected():
    assert "control characters" in reject(title="Koalitions" + chr(7) + "beregner")


def test_survives_the_storage_round_trip():
    """FirestoreElectionStore writes model_dump(mode="json") and reads it back
    through model_validate, so that round trip must be lossless under strict mode."""
    from app.schema import Election

    original = make_election(state="Nordjylland", election_date="2026-03-25T08:26")
    assert Election.model_validate(original.model_dump(mode="json")) == original
