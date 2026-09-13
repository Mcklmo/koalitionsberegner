"""The read cache in front of Firestore: fewer reads, never a wrong answer for long."""

from __future__ import annotations

from app.cached_store import CachedElectionStore
from app.service import identity_of
from app.store import InMemoryElectionStore
from tests.factories import make_election, make_request


class CountingStore(InMemoryElectionStore):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def list_elections(self, *, selected_only=False):
        self.reads += 1
        return super().list_elections(selected_only=selected_only)

    def get_stored(self, election_hash):
        self.reads += 1
        return super().get_stored(election_hash)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def stored(inner, election=None):
    election = election or make_election()
    request = make_request()
    key = "key-" + election.election_date.isoformat()
    inner.claim(key, request)
    inner.stage(key, election)
    election_hash = identity_of(election)
    inner.confirm(key, election_hash)
    return election_hash


def test_repeated_views_cost_one_read_until_the_copy_expires():
    inner, clock = CountingStore(), Clock()
    cache = CachedElectionStore(inner, ttl=30, clock=clock)
    election_hash = stored(inner)

    for _ in range(50):
        assert len(cache.list_elections()) == 1
        assert cache.get_stored(election_hash) is not None
    assert inner.reads == 2

    clock.now = 31
    cache.list_elections()
    assert inner.reads == 3


def test_an_election_not_found_is_asked_for_again():
    """So one confirmed on another instance is visible here at once."""
    inner = CountingStore()
    cache = CachedElectionStore(inner, clock=Clock())
    assert cache.get_stored("nope") is None
    assert cache.get_stored("nope") is None
    assert inner.reads == 2


def test_a_confirmation_here_shows_in_the_list_at_once():
    inner = CountingStore()
    cache = CachedElectionStore(inner, clock=Clock())
    assert cache.list_elections() == []
    stored(cache)
    assert len(cache.list_elections()) == 1


def test_curating_here_shows_at_once():
    inner = CountingStore()
    cache = CachedElectionStore(inner, clock=Clock())
    election_hash = stored(inner)
    assert cache.list_elections(selected_only=True) == []
    cache.set_selected(election_hash, True)
    assert [s.election_hash for s in cache.list_elections(selected_only=True)] == [election_hash]
    assert cache.get_stored(election_hash).selected is True


def test_everything_else_passes_straight_through():
    inner = CountingStore()
    cache = CachedElectionStore(inner, clock=Clock())
    claim = cache.claim("k", make_request())
    assert cache.get_job("k") == claim.job
