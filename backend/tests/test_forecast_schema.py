"""A forecast's shape, and its identity next to a result's."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.identity import election_hash
from app.service import identity_of
from tests.factories import make_election, make_forecast


def test_a_forecast_is_accepted_and_frozen():
    forecast = make_forecast(computed=True)
    assert forecast.forecast.publisher == "Voxmeter"
    assert forecast.forecast.published_on.isoformat() == "2026-09-07"
    assert forecast.forecast.computed is True
    with pytest.raises(ValidationError):
        forecast.forecast.publisher = "Someone else"


def test_a_result_has_no_forecast():
    assert make_election().forecast is None


def test_a_forecast_published_after_the_election_is_refused():
    with pytest.raises(ValidationError, match="after the election"):
        make_forecast(published_on="2027-11-01")


@pytest.mark.parametrize("forecast, expected", [
    ({"publisher": "Vox‮meter", "published_on": "2026-09-07"}, "direction-changing"),
    ({"publisher": "  ", "published_on": "2026-09-07"}, "must not be empty"),
    ({"publisher": "V", "published_on": "07/09/2026"}, "ISO 8601"),
    ({"publisher": "V", "published_on": "2026-09-07", "url": "x"}, "Extra inputs"),
    ({"publisher": "V", "published_on": "2026-09-07", "computed": "yes"}, "boolean"),
])
def test_a_malformed_forecast_is_refused(forecast, expected):
    with pytest.raises(ValidationError, match=expected):
        make_election(election_date="2027-10-31", forecast=forecast)


def test_a_results_identity_is_what_it_was_before_forecasts_existed():
    """Every election already stored is keyed by this digest; it must not move."""
    payload = json.dumps(
        {"version": "v1", "nation": "danmark", "state": None, "election_date": "2026-03-25"},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    assert election_hash("Danmark", None, "2026-03-25") == expected
    assert identity_of(make_election()) == expected


def test_each_poll_of_one_election_is_its_own_identity():
    result = election_hash("Danmark", None, "2027-10-31")
    voxmeter = election_hash("Danmark", None, "2027-10-31", ("Voxmeter", "2026-09-07"))
    later = election_hash("Danmark", None, "2027-10-31", ("Voxmeter", "2026-09-14"))
    epinion = election_hash("Danmark", None, "2027-10-31", ("Epinion", "2026-09-07"))

    assert len({result, voxmeter, later, epinion}) == 4


def test_a_publisher_spelled_in_another_case_is_the_same_poll():
    assert election_hash("Danmark", None, "2027-10-31", ("VOXMETER ", "2026-09-07")) == \
        election_hash("Danmark", None, "2027-10-31", ("voxmeter", "2026-09-07"))


def test_whether_the_seats_were_computed_does_not_change_which_poll_it_is():
    assert identity_of(make_forecast(computed=True)) == identity_of(make_forecast(computed=False))
