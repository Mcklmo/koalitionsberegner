"""The store's atomicity guarantee, exercised with real threads."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.store import ClaimOutcome, InMemoryElectionStore
from tests.factories import make_election, make_request


def test_racing_threads_produce_exactly_one_started_claim():
    store = InMemoryElectionStore()
    request = make_request()

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(lambda _: store.claim("hash", request).outcome, range(64)))

    assert outcomes.count(ClaimOutcome.STARTED) == 1
    assert outcomes.count(ClaimOutcome.ATTACHED) == 63


def test_a_stored_election_ends_all_claiming():
    store = InMemoryElectionStore()
    store.complete("hash", make_election())

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: store.claim("hash", make_request()).outcome, range(32)))

    assert set(outcomes) == {ClaimOutcome.STORED}


def test_completing_a_job_marks_it_succeeded_and_stores_the_election():
    store = InMemoryElectionStore()
    store.claim("hash", make_request())
    election = make_election()
    store.complete("hash", election)

    assert store.get_election("hash") == election
    assert store.get_job("hash").status.value == "succeeded"
    assert [s.election_hash for s in store.list_elections()] == ["hash"]


def test_failing_a_job_records_the_error_and_frees_the_hash():
    store = InMemoryElectionStore()
    store.claim("hash", make_request())
    store.fail("hash", "boom")

    job = store.get_job("hash")
    assert job.status.value == "failed"
    assert job.error == "boom"
    assert store.claim("hash", make_request()).outcome is ClaimOutcome.STARTED
    assert store.get_job("hash").attempt == 2
