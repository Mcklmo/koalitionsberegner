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

    def list_elections(self):
        self.reads += 1
        return super().list_elections()

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


def test_list_elections_uncached_bypasses_and_refreshes_the_cache():
    """For a caller (`app.service.ImportService.resolve_id`) that already
    tried `list_elections` and needs to know it was not merely stale."""
    inner, clock = CountingStore(), Clock()
    cache = CachedElectionStore(inner, ttl=30, clock=clock)
    assert cache.list_elections() == []
    assert inner.reads == 1

    stored(inner)  # confirmed straight on `inner`, cache none the wiser

    assert cache.list_elections() == [], "still within the TTL of the empty listing"
    assert inner.reads == 1, "served from the (stale) cache, not asked again"

    clock.now = 6  # past the refresh floor, well within the TTL
    assert len(cache.list_elections_uncached()) == 1, "an uncached read sees it"
    assert inner.reads == 2

    assert len(cache.list_elections()) == 1, "and the cache now holds the fresh listing too"
    assert inner.reads == 2, "served from what list_elections_uncached just refreshed"


def test_uncached_reads_cost_at_most_one_store_read_per_refresh_floor():
    """Anyone can send links to unknown ids; each asks for a fresh listing."""
    inner, clock = CountingStore(), Clock()
    cache = CachedElectionStore(inner, ttl=30, refresh_floor=5, clock=clock)
    cache.list_elections()
    for _ in range(100):
        cache.list_elections_uncached()
    assert inner.reads == 1, "a listing younger than the floor is reused"

    clock.now = 5
    cache.list_elections_uncached()
    cache.list_elections_uncached()
    assert inner.reads == 2


def test_a_confirmation_here_shows_in_the_list_at_once():
    inner = CountingStore()
    cache = CachedElectionStore(inner, clock=Clock())
    assert cache.list_elections() == []
    stored(cache)
    assert len(cache.list_elections()) == 1


def test_a_replaced_election_shows_at_once():
    inner, clock = CountingStore(), Clock()
    cache = CachedElectionStore(inner, clock=clock)
    election = make_election()
    stored(inner, election)
    election_hash = identity_of(election)
    cache.get_election(election_hash)

    corrected = make_election(title="Corrected")
    assert cache.replace_election(election_hash, corrected)
    assert cache.get_election(election_hash) == corrected


def test_everything_else_passes_straight_through():
    inner = CountingStore()
    cache = CachedElectionStore(inner, clock=Clock())
    claim = cache.claim("k", make_request())
    assert cache.get_job("k") == claim.job
