"""The store's atomicity guarantee and its staged-draft bookkeeping."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import make_election, make_request


def saved(store: InMemoryElectionStore, key: str, election=None):
    """Take a hash through the full parse -> preview -> confirm path."""
    store.claim(key, make_request())
    store.stage(key, election or make_election())
    return store.confirm(key)


def test_racing_threads_produce_exactly_one_started_claim():
    store = InMemoryElectionStore()
    request = make_request()

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(lambda _: store.claim("hash", request).outcome, range(64)))

    assert outcomes.count(ClaimOutcome.STARTED) == 1
    assert outcomes.count(ClaimOutcome.ATTACHED) == 63


def test_a_saved_election_ends_all_claiming():
    store = InMemoryElectionStore()
    saved(store, "hash")

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: store.claim("hash", make_request()).outcome, range(32)))

    assert set(outcomes) == {ClaimOutcome.STORED}


def test_racing_confirmations_store_the_election_once():
    store = InMemoryElectionStore()
    store.claim("hash", make_request())
    store.stage("hash", make_election())

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.confirm("hash"), range(32)))

    assert all(r is not None for r in results), "every caller sees the confirmed election"
    assert len(store.list_elections()) == 1


def test_staging_does_not_store_the_election():
    store = InMemoryElectionStore()
    store.claim("hash", make_request())
    store.stage("hash", make_election())

    assert store.get_election("hash") is None
    assert store.list_elections() == []
    job = store.get_job("hash")
    assert job.status is JobStatus.AWAITING_CONFIRMATION
    assert job.result is not None


def test_confirming_stores_the_election_and_closes_the_job():
    store = InMemoryElectionStore()
    election = make_election()
    assert saved(store, "hash", election) == election

    assert store.get_election("hash") == election
    assert store.get_job("hash").status is JobStatus.SUCCEEDED
    assert [s.election_hash for s in store.list_elections()] == ["hash"]


def test_confirming_without_a_staged_draft_does_nothing():
    store = InMemoryElectionStore()
    assert store.confirm("hash") is None

    store.claim("hash", make_request())
    assert store.confirm("hash") is None, "a job still parsing has nothing to confirm"


def test_discarding_removes_the_draft_and_frees_the_hash():
    store = InMemoryElectionStore()
    store.claim("hash", make_request())
    store.stage("hash", make_election())

    assert store.discard("hash") is True
    assert store.get_job("hash") is None
    assert store.get_election("hash") is None
    assert store.claim("hash", make_request()).outcome is ClaimOutcome.STARTED


def test_discarding_only_applies_to_staged_drafts():
    store = InMemoryElectionStore()
    assert store.discard("hash") is False

    store.claim("hash", make_request())
    assert store.discard("hash") is False, "a running parse is not a preview"

    saved(store, "other")
    assert store.discard("other") is False, "a saved election cannot be discarded this way"


def test_failing_a_job_records_the_error_and_frees_the_hash():
    store = InMemoryElectionStore()
    store.claim("hash", make_request())
    store.fail("hash", "boom")

    job = store.get_job("hash")
    assert job.status is JobStatus.FAILED
    assert job.error == "boom"
    assert store.claim("hash", make_request()).outcome is ClaimOutcome.STARTED
    assert store.get_job("hash").attempt == 2
