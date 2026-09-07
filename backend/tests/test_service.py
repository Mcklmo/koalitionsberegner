"""Cross-user reuse, single-flight extraction, and the confirmation gate.

Work is claimed per page; the election's identity is whatever the agent infers
from that page, and is only stored once the user confirms it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.identity import source_url_key
from app.service import ImportService, ImportState, identity_of
from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import CountingParser, make_election, make_request, wait_until

pytestmark = pytest.mark.anyio

URL = "https://www.dst.dk/valg"
OTHER_URL = "https://mirror.example.net/other-page"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def build(parser=None, **store_kwargs):
    store = InMemoryElectionStore(**store_kwargs)
    parser = parser or CountingParser()
    return store, parser, ImportService(store, parser)


async def settle(service: ImportService, key: str):
    return await service.wait_for(key, timeout=2.0)


async def import_and_save(service: ImportService, request=None):
    """The full happy path: submit a URL, wait for the preview, confirm it."""
    submitted = await service.submit(request or make_request())
    await settle(service, submitted.page_key)
    return await service.confirm(submitted.page_key)


async def test_work_is_keyed_by_the_page_not_the_election():
    _, _, service = build()
    submitted = await service.submit(make_request(source_url=URL))
    assert submitted.page_key == source_url_key(URL)
    assert submitted.election_hash is None, "identity is unknown until the page is read"


async def test_an_extracted_election_is_previewed_not_saved():
    store, parser, service = build()
    submitted = await service.submit(make_request())

    previewed = await settle(service, submitted.page_key)
    assert previewed.state is ImportState.PREVIEW
    assert previewed.election is not None, "the extraction is shown to the user"
    assert previewed.election_hash == identity_of(previewed.election)
    assert store.list_elections() == [], "nothing saved without confirmation"
    assert parser.call_count == 1


async def test_the_preview_carries_the_identity_the_agent_inferred():
    """The user supplied only a URL, so this is what they are confirming."""
    inferred = make_election(nation="Deutschland", state="Sachsen-Anhalt",
                             election_date="2021-06-06")
    _, _, service = build(CountingParser(infers=inferred))
    submitted = await service.submit(make_request())

    previewed = await settle(service, submitted.page_key)
    assert previewed.election.nation == "Deutschland"
    assert previewed.election.state == "Sachsen-Anhalt"
    assert previewed.election.election_date.isoformat() == "2021-06-06"


async def test_confirming_a_preview_saves_it_under_the_inferred_identity():
    store, _, service = build()
    submitted = await service.submit(make_request())
    previewed = await settle(service, submitted.page_key)

    confirmed = await service.confirm(submitted.page_key)
    assert confirmed.state is ImportState.READY
    assert confirmed.election_hash == previewed.election_hash
    assert store.get_election(confirmed.election_hash) is not None
    assert store.resolve_page(submitted.page_key) == confirmed.election_hash
    assert store.get_job(submitted.page_key).status is JobStatus.SUCCEEDED


async def test_discarding_a_preview_saves_nothing_and_frees_the_page():
    store, parser, service = build()
    submitted = await service.submit(make_request())
    await settle(service, submitted.page_key)

    assert await service.discard(submitted.page_key) is True
    assert store.list_elections() == []
    assert (await service.status(submitted.page_key)).state is ImportState.UNKNOWN

    retry = await service.submit(make_request())
    assert retry.reused is False, "a discarded preview leaves the page importable"
    await wait_until(lambda: parser.call_count == 2)


async def test_confirming_twice_is_harmless():
    store, _, service = build()
    first = await import_and_save(service)
    again = await service.confirm(first.page_key)

    assert again.state is ImportState.READY
    assert again.election == first.election
    assert len(store.list_elections()) == 1


async def test_reimporting_the_same_page_never_extracts_again():
    _, parser, service = build()
    first = await import_and_save(service)

    second = await service.submit(make_request())
    assert second.state is ImportState.READY
    assert second.reused is True
    assert second.election_hash == first.election_hash
    assert parser.call_count == 1, "a known page must never be fetched or extracted again"


async def test_one_users_import_serves_every_other_user():
    _, parser, service = build()
    first = await import_and_save(service)

    other_user = await service.submit(make_request(source_url=URL))
    assert other_user.state is ImportState.READY
    assert other_user.election_hash == first.election_hash
    assert other_user.reused is True
    assert parser.call_count == 1


async def test_a_second_url_for_a_stored_election_is_recognised_after_extraction():
    """Dedup can no longer happen up front — the page must be read before its
    election is known — so it happens as soon as the agent has identified it."""
    store, parser, service = build()
    first = await import_and_save(service, make_request(source_url=URL))

    second = await service.submit(make_request(source_url=OTHER_URL))
    settled = await settle(service, second.page_key)

    assert parser.call_count == 2, "the new page had to be read to be identified"
    assert settled.state is ImportState.READY, "and needs no second confirmation"
    assert settled.election_hash == first.election_hash
    assert len(store.list_elections()) == 1, "one election, two pages"
    assert store.resolve_page(second.page_key) == first.election_hash


async def test_concurrent_requests_trigger_exactly_one_extraction():
    parser = CountingParser()
    parser.hold()
    _, parser, service = build(parser)

    submissions = await asyncio.gather(*(service.submit(make_request()) for _ in range(8)))
    key = submissions[0].page_key
    assert {s.page_key for s in submissions} == {key}
    assert sum(1 for s in submissions if not s.reused) == 1, "exactly one caller owns the run"

    await parser.started.wait()
    assert parser.call_count == 1

    parser.release()
    results = await asyncio.gather(*(settle(service, key) for _ in submissions))
    assert parser.call_count == 1, "only one extraction ran for the whole race"
    assert all(r.state is ImportState.PREVIEW for r in results)
    assert all(r.election == results[0].election for r in results), "one result, shared by all"


async def test_a_late_arrival_attaches_to_an_in_flight_extraction():
    parser = CountingParser()
    parser.hold()
    _, parser, service = build(parser)

    owner = await service.submit(make_request())
    await parser.started.wait()

    latecomer = await service.submit(make_request())
    assert latecomer.state is ImportState.PENDING
    assert latecomer.reused is True
    assert parser.call_count == 1

    parser.release()
    assert (await settle(service, owner.page_key)).state is ImportState.PREVIEW
    assert parser.call_count == 1


async def test_a_request_arriving_during_a_preview_sees_the_preview():
    _, parser, service = build()
    first = await service.submit(make_request())
    await settle(service, first.page_key)

    second = await service.submit(make_request())
    assert second.state is ImportState.PREVIEW
    assert second.reused is True
    assert parser.call_count == 1, "an unconfirmed preview must not trigger a second run"


async def test_a_failed_extraction_does_not_poison_the_page():
    parser = CountingParser(fail_times=1)
    store, parser, service = build(parser)

    first = await service.submit(make_request())
    failed = await settle(service, first.page_key)
    assert failed.state is ImportState.FAILED
    assert failed.error == "extraction failed"

    retry = await service.submit(make_request())
    assert retry.state is ImportState.PENDING, "the page is claimable again after a failure"
    assert (await settle(service, retry.page_key)).state is ImportState.PREVIEW
    assert parser.call_count == 2
    assert store.get_job(first.page_key).attempt == 2


async def test_a_crashed_extraction_is_reclaimed_once_its_lease_expires():
    now = [1000.0]
    parser = CountingParser()
    parser.hold()
    store, parser, service = build(parser, stale_after=60.0, clock=lambda: now[0])

    first = await service.submit(make_request())
    await parser.started.wait()
    assert store.get_job(first.page_key).status is JobStatus.PENDING

    assert (await service.submit(make_request())).reused is True
    assert parser.call_count == 1

    now[0] += 61.0
    reclaim = await service.submit(make_request())
    assert reclaim.reused is False, "a dead run must not pin the page forever"
    await wait_until(lambda: parser.call_count == 2)


async def test_an_abandoned_preview_is_reclaimed_once_its_lease_expires():
    now = [1000.0]
    store, parser, service = build(stale_after=60.0, clock=lambda: now[0])

    first = await service.submit(make_request())
    await settle(service, first.page_key)
    assert store.get_job(first.page_key).status is JobStatus.AWAITING_CONFIRMATION

    now[0] += 61.0
    reclaim = await service.submit(make_request())
    assert reclaim.reused is False, "an abandoned preview must not pin the page forever"
    await wait_until(lambda: parser.call_count == 2)


async def test_status_reports_unknown_for_an_unseen_page():
    _, _, service = build()
    result = await service.status("0" * 64)
    assert result.state is ImportState.UNKNOWN
    assert result.election is None


async def test_two_different_elections_are_stored_separately():
    danish = make_election(nation="Danmark", election_date="2026-03-25")
    german = make_election(nation="Deutschland", state="Sachsen-Anhalt",
                           election_date="2021-06-06")
    store, _, service = build(CountingParser(by_url={URL: danish, OTHER_URL: german}))

    first = await import_and_save(service, make_request(source_url=URL))
    second = await import_and_save(service, make_request(source_url=OTHER_URL))

    assert first.election_hash != second.election_hash
    assert len(store.list_elections()) == 2


async def test_claim_outcomes_are_reported_directly_by_the_store():
    store = InMemoryElectionStore()
    request = make_request()
    assert store.claim("page", request).outcome is ClaimOutcome.STARTED
    assert store.claim("page", request).outcome is ClaimOutcome.ATTACHED

    store.stage("page", make_election())
    assert store.claim("page", request).outcome is ClaimOutcome.ATTACHED, "preview is not re-run"

    store.confirm("page", "hash")
    assert store.claim("page", request).outcome is ClaimOutcome.STORED
