"""Seats from vote shares: the arithmetic a poll in percent is put through."""

from __future__ import annotations

import pytest

from app.seats import allocate

#: The textbook example: four parties, eight seats.
VOTES = [100_000, 80_000, 30_000, 20_000]


def test_dhondt_favours_the_large_parties():
    assert allocate(VOTES, 8, method="dhondt") == [4, 3, 1, 0]


def test_sainte_lague_is_kinder_to_the_small_ones():
    assert allocate(VOTES, 8, method="sainte_lague") == [3, 3, 1, 1]


@pytest.mark.parametrize("method", ["dhondt", "sainte_lague"])
def test_every_seat_is_allocated(method):
    assert sum(allocate([31.2, 24.9, 17.0, 11.3, 8.8, 6.8], 179, method=method)) == 179


def test_a_share_below_the_threshold_wins_nothing():
    won = allocate([40.0, 35.0, 20.0, 4.9], 100, method="sainte_lague", threshold=5.0)
    assert won[3] == 0
    assert sum(won) == 100


def test_a_share_exactly_at_the_threshold_counts():
    assert allocate([60.0, 5.0], 20, method="sainte_lague", threshold=5.0)[1] > 0


def test_nobody_reaching_the_threshold_is_an_error_not_an_empty_assembly():
    with pytest.raises(ValueError, match="threshold"):
        allocate([3.0, 2.0], 10, method="dhondt", threshold=5.0)


def test_a_tie_for_the_last_seat_is_decided_the_same_way_every_time():
    assert allocate([50.0, 50.0], 3, method="dhondt") == [2, 1]
    assert allocate([50.0, 50.0], 3, method="dhondt") == [2, 1]


@pytest.mark.parametrize("shares, seats", [([-1.0, 50.0], 10), ([50.0], 0)])
def test_nonsense_is_refused(shares, seats):
    with pytest.raises(ValueError):
        allocate(shares, seats, method="dhondt")
