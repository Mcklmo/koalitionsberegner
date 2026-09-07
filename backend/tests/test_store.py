"""The store's atomicity guarantee and its two-key bookkeeping.

Work is keyed by page; storage is keyed by election identity. The index between
them is what lets a re-imported page skip extraction entirely.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import make_election, make_request

HASH = "e" * 64
PAGE = "p" * 64


def saved(store: InMemoryElectionStore, page=PAGE, election_hash=HASH, election=None):
    """Take a page through the full extract -> preview -> confirm path."""
    store.claim(page, make_request())
    store.stage(page, election or make_election())
    return store.confirm(page, election_hash)


def test_racing_threads_produce_exactly_one_started_claim():
    store = InMemoryElectionStore()
    request = make_request()

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(lambda _: store.claim(PAGE, request).outcome, range(64)))

    assert outcomes.count(ClaimOutcome.STARTED) == 1
    assert outcomes.count(ClaimOutcome.ATTACHED) == 63


def test_a_page_that_already_produced_an_election_ends_all_claiming():
    store = InMemoryElectionStore()
    saved(store)

    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(pool.map(lambda _: store.claim(PAGE, make_request()), range(32)))

    assert {c.outcome for c in claims} == {ClaimOutcome.STORED}
    assert all(c.election_hash == HASH for c in claims)


def test_racing_confirmations_store_the_election_once():
    store = InMemoryElectionStore()
    store.claim(PAGE, make_request())
    store.stage(PAGE, make_election())

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.confirm(PAGE, HASH), range(32)))

    assert all(r is not None for r in results), "every caller sees the confirmed election"
    assert len(store.list_elections()) == 1


def test_staging_does_not_store_the_election():
    store = InMemoryElectionStore()
    store.claim(PAGE, make_request())
    store.stage(PAGE, make_election())

    assert store.list_elections() == []
    assert store.resolve_page(PAGE) is None
    job = store.get_job(PAGE)
    assert job.status is JobStatus.AWAITING_CONFIRMATION
    assert job.result is not None


def test_confirming_stores_the_election_and_indexes_the_page():
    store = InMemoryElectionStore()
    election = make_election()
    confirmation = saved(store, election=election)

    assert confirmation.election == election
    assert confirmation.duplicate is False
    assert store.get_election(HASH) == election
    assert store.resolve_page(PAGE) == HASH
    assert store.get_job(PAGE).status is JobStatus.SUCCEEDED


def test_a_second_page_describing_the_same_election_does_not_duplicate_it():
    """Two URLs for one election: the second is indexed onto the first's entry."""
    store = InMemoryElectionStore()
    saved(store, page="page-a")

    confirmation = saved(store, page="page-b")
    assert confirmation.duplicate is True
    assert confirmation.election_hash == HASH
    assert len(store.list_elections()) == 1, "one election, two pages"
    assert store.resolve_page("page-a") == store.resolve_page("page-b") == HASH


def test_linking_points_a_page_at_an_existing_election():
    store = InMemoryElectionStore()
    saved(store, page="page-a")

    store.claim("page-b", make_request())
    store.link("page-b", HASH)

    assert store.resolve_page("page-b") == HASH
    assert store.get_job("page-b").status is JobStatus.SUCCEEDED
    assert store.claim("page-b", make_request()).outcome is ClaimOutcome.STORED
    assert len(store.list_elections()) == 1


def test_linking_to_an_unknown_election_does_nothing():
    store = InMemoryElectionStore()
    store.link(PAGE, HASH)
    assert store.resolve_page(PAGE) is None


def test_confirming_without_a_staged_draft_does_nothing():
    store = InMemoryElectionStore()
    assert store.confirm(PAGE, HASH) is None

    store.claim(PAGE, make_request())
    assert store.confirm(PAGE, HASH) is None, "a job still extracting has nothing to confirm"


def test_discarding_removes_the_draft_and_frees_the_page():
    store = InMemoryElectionStore()
    store.claim(PAGE, make_request())
    store.stage(PAGE, make_election())

    assert store.discard(PAGE) is True
    assert store.get_job(PAGE) is None
    assert store.list_elections() == []
    assert store.claim(PAGE, make_request()).outcome is ClaimOutcome.STARTED


def test_discarding_only_applies_to_staged_drafts():
    store = InMemoryElectionStore()
    assert store.discard(PAGE) is False

    store.claim(PAGE, make_request())
    assert store.discard(PAGE) is False, "a running extraction is not a preview"

    saved(store, page="other")
    assert store.discard("other") is False, "a saved election cannot be discarded this way"


def test_failing_a_job_records_the_error_and_frees_the_page():
    store = InMemoryElectionStore()
    store.claim(PAGE, make_request())
    store.fail(PAGE, "boom")

    job = store.get_job(PAGE)
    assert job.status is JobStatus.FAILED
    assert job.error == "boom"
    assert store.claim(PAGE, make_request()).outcome is ClaimOutcome.STARTED
    assert store.get_job(PAGE).attempt == 2
