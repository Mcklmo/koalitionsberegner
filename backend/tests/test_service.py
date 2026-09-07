"""Acceptance: cross-user reuse, single-flight parsing, and retry after failure."""

from __future__ import annotations

import asyncio

import pytest

from app.service import ImportService, ImportState
from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import CountingParser, make_election, make_request, wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def build(parser=None, **store_kwargs):
    store = InMemoryElectionStore(**store_kwargs)
    parser = parser or CountingParser()
    return store, parser, ImportService(store, parser)


async def settle(service: ImportService, key: str):
    return await service.wait_for(key, timeout=2.0)


async def test_first_import_parses_once_and_stores_the_result():
    store, parser, service = build()
    result = await service.submit(make_request())
    assert result.state is ImportState.PENDING

    settled = await settle(service, result.election_hash)
    assert settled.state is ImportState.READY
    assert settled.election == parser.election
    assert parser.call_count == 1
    assert store.get_election(result.election_hash) is not None


async def test_second_import_of_a_stored_election_is_short_circuited():
    store, parser, service = build()
    first = await service.submit(make_request())
    await settle(service, first.election_hash)

    second = await service.submit(make_request())
    assert second.state is ImportState.READY
    assert second.reused is True
    assert second.election is not None
    assert parser.call_count == 1, "a stored election must never be parsed again"


async def test_one_users_import_serves_every_other_user():
    """A different user, submitting the same metadata with a different URL,
    is served the already-parsed election rather than re-parsing."""
    store, parser, service = build()
    first = await service.submit(make_request(source_url="https://example.org/results"))
    await settle(service, first.election_hash)

    other_user = await service.submit(
        make_request(source_url="https://mirror.example.net/other-page")
    )
    assert other_user.election_hash == first.election_hash
    assert other_user.state is ImportState.READY
    assert other_user.reused is True
    assert parser.call_count == 1


async def test_concurrent_requests_trigger_exactly_one_parse_and_share_its_result():
    parser = CountingParser()
    parser.hold()
    store, parser, service = build(parser)

    submissions = await asyncio.gather(*(service.submit(make_request()) for _ in range(8)))
    key = submissions[0].election_hash
    assert {s.election_hash for s in submissions} == {key}
    assert all(s.state is ImportState.PENDING for s in submissions)
    assert sum(1 for s in submissions if not s.reused) == 1, "exactly one caller owns the parse"

    await parser.started.wait()
    assert parser.call_count == 1

    parser.release()
    results = await asyncio.gather(*(settle(service, key) for _ in submissions))
    assert parser.call_count == 1, "only one parse ran for the whole race"
    assert all(r.state is ImportState.READY for r in results)
    assert {id(r.election) for r in results}, "every caller received a result"
    assert all(r.election == parser.election for r in results)


async def test_a_late_arrival_attaches_to_an_in_flight_parse():
    parser = CountingParser()
    parser.hold()
    store, parser, service = build(parser)

    owner = await service.submit(make_request())
    await parser.started.wait()

    latecomer = await service.submit(make_request())
    assert latecomer.state is ImportState.PENDING
    assert latecomer.reused is True
    assert parser.call_count == 1

    parser.release()
    assert (await settle(service, owner.election_hash)).state is ImportState.READY
    assert parser.call_count == 1


async def test_a_failed_parse_does_not_poison_the_hash():
    parser = CountingParser(fail_times=1)
    store, parser, service = build(parser)

    first = await service.submit(make_request())
    failed = await settle(service, first.election_hash)
    assert failed.state is ImportState.FAILED
    assert failed.error == "extraction failed"

    retry = await service.submit(make_request())
    assert retry.state is ImportState.PENDING, "the hash is claimable again after a failure"
    recovered = await settle(service, retry.election_hash)
    assert recovered.state is ImportState.READY
    assert parser.call_count == 2
    assert store.get_job(first.election_hash).attempt == 2


async def test_a_crashed_parse_is_reclaimed_once_its_lease_expires():
    now = [1000.0]
    parser = CountingParser()
    parser.hold()
    store, parser, service = build(parser, stale_after=60.0, clock=lambda: now[0])

    first = await service.submit(make_request())
    await parser.started.wait()
    assert store.get_job(first.election_hash).status is JobStatus.PENDING

    # Still inside the lease: a second caller waits rather than starting over.
    assert (await service.submit(make_request())).reused is True
    assert parser.call_count == 1

    now[0] += 61.0
    reclaim = await service.submit(make_request())
    assert reclaim.reused is False, "a dead parse must not pin the election forever"
    await wait_until(lambda: parser.call_count == 2)


async def test_status_reports_unknown_for_an_unseen_hash():
    _, _, service = build()
    result = await service.status("0" * 64)
    assert result.state is ImportState.UNKNOWN
    assert result.election is None


async def test_regional_and_national_imports_are_separate_elections():
    parser = CountingParser()
    store, parser, service = build(parser)

    national = await service.submit(make_request())
    regional = await service.submit(make_request(state="Nordjylland"))
    assert national.election_hash != regional.election_hash

    await settle(service, national.election_hash)
    await settle(service, regional.election_hash)
    assert parser.call_count == 2
    assert len(store.list_elections()) == 2


async def test_claim_outcomes_are_reported_directly_by_the_store():
    store = InMemoryElectionStore()
    request = make_request()
    assert store.claim("h", request).outcome is ClaimOutcome.STARTED
    assert store.claim("h", request).outcome is ClaimOutcome.ATTACHED
    store.complete("h", make_election())
    assert store.claim("h", request).outcome is ClaimOutcome.STORED


async def test_an_election_that_contradicts_its_identity_is_rejected():
    """A parser must not file an election under a hash that its own metadata
    disagrees with, or the same election could be stored twice."""
    wrong = make_election(nation="Sverige", election_date="2026-03-25")
    store, parser, service = build(CountingParser(wrong))

    submitted = await service.submit(make_request(nation="Danmark"))
    result = await settle(service, submitted.election_hash)

    assert result.state is ImportState.FAILED
    assert "does not match" in result.error
    assert store.get_election(submitted.election_hash) is None
