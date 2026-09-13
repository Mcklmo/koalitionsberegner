"""The store's atomicity guarantee and its two-key bookkeeping.

Work is keyed by request — a year and a place, as typed; storage is keyed by
election identity. The index between them is what lets a request made before
skip the whole lookup.

Every test here runs against *both* persistent-capable backends, because the
single-flight logic above them assumes one contract and cannot tell which store
it was handed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.sqlite_store import SqliteElectionStore
from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import make_election, make_request

HASH = "e" * 64
KEY = "k" * 64


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryElectionStore()
        return
    backend = SqliteElectionStore(tmp_path / "elections.db")
    yield backend
    backend.close()


def saved(store, key=KEY, election_hash=HASH, election=None):
    """Take one request through the full import -> preview -> confirm path."""
    store.claim(key, make_request())
    store.stage(key, election or make_election())
    return store.confirm(key, election_hash)


def test_racing_threads_produce_exactly_one_started_claim(store):
    request = make_request()

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(lambda _: store.claim(KEY, request).outcome, range(64)))

    assert outcomes.count(ClaimOutcome.STARTED) == 1
    assert outcomes.count(ClaimOutcome.ATTACHED) == 63


def test_a_request_that_already_produced_an_election_ends_all_claiming(store):
    saved(store)

    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(pool.map(lambda _: store.claim(KEY, make_request()), range(32)))

    assert {c.outcome for c in claims} == {ClaimOutcome.STORED}
    assert all(c.election_hash == HASH for c in claims)


def test_racing_confirmations_store_the_election_once(store):
    store.claim(KEY, make_request())
    store.stage(KEY, make_election())

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.confirm(KEY, HASH), range(32)))

    assert all(r is not None for r in results), "every caller sees the confirmed election"
    assert len(store.list_elections()) == 1


def test_staging_does_not_store_the_election(store):
    store.claim(KEY, make_request())
    store.stage(KEY, make_election())

    assert store.list_elections() == []
    assert store.resolve_request(KEY) is None
    job = store.get_job(KEY)
    assert job.status is JobStatus.AWAITING_CONFIRMATION
    assert job.result is not None


def test_confirming_stores_the_election_and_indexes_the_request(store):
    election = make_election()
    confirmation = saved(store, election=election)

    assert confirmation.election == election
    assert confirmation.duplicate is False
    assert store.get_election(HASH) == election
    assert store.resolve_request(KEY) == HASH
    assert store.get_job(KEY).status is JobStatus.SUCCEEDED


def test_a_second_request_naming_the_same_election_does_not_duplicate_it(store):
    """Two ways of asking for one election: the second is indexed onto the
    first's entry rather than stored beside it."""
    saved(store, key="asked-a")

    confirmation = saved(store, key="asked-b")
    assert confirmation.duplicate is True
    assert confirmation.election_hash == HASH
    assert len(store.list_elections()) == 1, "one election, two requests"
    assert store.resolve_request("asked-a") == store.resolve_request("asked-b") == HASH


def test_linking_points_a_request_at_an_existing_election(store):
    saved(store, key="asked-a")

    store.claim("asked-b", make_request())
    store.link("asked-b", HASH)

    assert store.resolve_request("asked-b") == HASH
    assert store.get_job("asked-b").status is JobStatus.SUCCEEDED
    assert store.claim("asked-b", make_request()).outcome is ClaimOutcome.STORED
    assert len(store.list_elections()) == 1


def test_linking_to_an_unknown_election_does_nothing(store):
    store.link(KEY, HASH)
    assert store.resolve_request(KEY) is None


def test_confirming_without_a_staged_draft_does_nothing(store):
    assert store.confirm(KEY, HASH) is None

    store.claim(KEY, make_request())
    assert store.confirm(KEY, HASH) is None, "a job still extracting has nothing to confirm"


def test_discarding_removes_the_draft_and_frees_the_request(store):
    store.claim(KEY, make_request())
    store.stage(KEY, make_election())

    assert store.discard(KEY) is True
    assert store.get_job(KEY) is None
    assert store.list_elections() == []
    assert store.claim(KEY, make_request()).outcome is ClaimOutcome.STARTED


def test_discarding_only_applies_to_staged_drafts(store):
    assert store.discard(KEY) is False

    store.claim(KEY, make_request())
    assert store.discard(KEY) is False, "a running import is not a preview"

    saved(store, key="other")
    assert store.discard("other") is False, "a saved election cannot be discarded this way"


def test_failing_a_job_records_the_error_and_frees_the_request(store):
    store.claim(KEY, make_request())
    store.fail(KEY, "boom")

    job = store.get_job(KEY)
    assert job.status is JobStatus.FAILED
    assert job.error == "boom"
    assert store.claim(KEY, make_request()).outcome is ClaimOutcome.STARTED
    assert store.get_job(KEY).attempt == 2


# --- finding a stored election from what somebody typed ---------------------

def test_a_stored_election_is_found_by_the_year_and_place_asked_for(store):
    """The bridge between the two keys. A request names a year and a place; an
    identity names a day. This is how one finds the other without a model."""
    saved(store, election=make_election(nation="Danmark", election_date="2026-03-25"))

    found = store.find_by_place(2026, "Danmark")
    assert found is not None and found.election_hash == HASH


@pytest.mark.parametrize("nation", ["danmark", "  DANMARK  "])
def test_the_spelling_a_previous_importer_used_does_not_matter(store, nation):
    saved(store, election=make_election(nation="Danmark", election_date="2026-03-25"))
    assert store.find_by_place(2026, nation) is not None


def test_a_regional_election_is_not_found_by_asking_for_the_national_one(store):
    saved(store, election=make_election(
        nation="Deutschland", state="Sachsen-Anhalt", election_date="2021-06-06"
    ))

    assert store.find_by_place(2021, "Deutschland") is None, "that would be the Bundestag"
    assert store.find_by_place(2021, "Deutschland", "Sachsen Anhalt") is not None


def test_another_year_in_the_same_place_is_not_a_match(store):
    saved(store, election=make_election(nation="Danmark", election_date="2026-03-25"))
    assert store.find_by_place(2025, "Danmark") is None


def test_nothing_stored_is_no_match(store):
    assert store.find_by_place(2026, "Danmark") is None


def test_the_account_that_started_an_attempt_stays_on_it(store):
    """Who may throw a preview away is decided by this, so staging must keep it."""
    claim = store.claim(KEY, make_request(), "uid-1")
    assert claim.job.owner == "uid-1"

    store.stage(KEY, make_election())

    assert store.get_job(KEY).owner == "uid-1"


def test_a_new_attempt_belongs_to_whoever_started_it(store):
    store.claim(KEY, make_request(), "uid-1")
    store.fail(KEY, "no seats")

    store.claim(KEY, make_request(), "uid-2")

    assert store.get_job(KEY).owner == "uid-2"


@pytest.mark.parametrize("finish", ["saved", "linked", "failed"])
def test_an_attempt_that_is_over_no_longer_names_its_account(store, finish):
    """Nothing is left to throw away, so who started it is not kept either."""
    store.claim(KEY, make_request(), "uid-1")
    if finish == "saved":
        store.stage(KEY, make_election())
        store.confirm(KEY, HASH)
    elif finish == "linked":
        saved(store, key="o" * 64)
        store.link(KEY, HASH)
    else:
        store.fail(KEY, "no seats")

    assert store.get_job(KEY).owner is None


# --- peeking: the claim decision without the claim -------------------------

def test_peeking_decides_like_a_claim_but_claims_nothing(store):
    assert store.peek(KEY).outcome is ClaimOutcome.STARTED
    assert store.get_job(KEY) is None, "a peek starts no job"

    store.claim(KEY, make_request())
    peeked = store.peek(KEY)

    assert peeked.outcome is ClaimOutcome.ATTACHED
    assert peeked.job.status is JobStatus.PENDING
    assert store.get_job(KEY).attempt == 1, "and joins nothing either"


def test_peeking_sees_a_stored_election(store):
    saved(store)

    peeked = store.peek(KEY)

    assert peeked.outcome is ClaimOutcome.STORED
    assert peeked.election_hash == HASH
    assert peeked.election is not None


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_a_job_past_its_lease_peeks_as_claimable(backend, tmp_path):
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    store = (
        InMemoryElectionStore(stale_after=60.0, clock=clock) if backend == "memory"
        else SqliteElectionStore(tmp_path / "elections.db", stale_after=60.0, clock=clock)
    )
    store.claim(KEY, make_request())
    now[0] += 61.0

    peeked = store.peek(KEY)

    assert peeked.outcome is ClaimOutcome.STARTED
    assert peeked.job.status is JobStatus.PENDING, "the dead job is reported, not replaced"
    assert store.get_job(KEY).attempt == 1
    if backend == "sqlite":
        store.close()


def test_replacing_an_election_keeps_its_curation_and_age(store):
    saved(store)
    store.set_selected(HASH, True)
    before = store.get_stored(HASH)
    corrected = make_election(title="Corrected")

    assert store.replace_election(HASH, corrected) is True
    after = store.get_stored(HASH)
    assert after.election == corrected
    assert (after.selected, after.stored_at) == (True, before.stored_at)
    assert store.replace_election("f" * 64, corrected) is False
