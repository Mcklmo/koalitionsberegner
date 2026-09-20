"""Cross-user reuse, single-flight imports, and the confirmation gate.

Work is claimed per *request* — a year and a place, as typed. The election's
identity is whatever was read off the page that answered it, and is only stored
once the user confirms it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.identity import request_key
from app.service import ImportService, ImportState, identity_of
from app.store import ClaimOutcome, InMemoryElectionStore, JobStatus
from tests.factories import CountingParser, make_election, make_forecast, make_request, wait_until

pytestmark = pytest.mark.anyio

#: Two ways of asking for the same election. Different requests, so different
#: keys and a separate import each — and one stored election, because the
#: identity that comes back is the same.
ASKED = dict(year=2026, nation="Danmark")
SPELLED_OTHERWISE = dict(year=2026, nation="Denmark")


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
    """The full happy path: ask for an election, wait for the preview, confirm it."""
    submitted = await service.submit(request or make_request())
    await settle(service, submitted.request_key)
    return await service.confirm(submitted.request_key)


async def test_work_is_keyed_by_the_request_not_the_election():
    _, _, service = build()
    submitted = await service.submit(make_request(**ASKED))
    assert submitted.request_key == request_key(**ASKED)
    assert submitted.election_hash is None, "identity is unknown until a page is read"


async def test_an_extracted_election_is_previewed_not_saved():
    store, parser, service = build()
    submitted = await service.submit(make_request())

    previewed = await settle(service, submitted.request_key)
    assert previewed.state is ImportState.PREVIEW
    assert previewed.election is not None, "the extraction is shown to the user"
    assert previewed.election_hash == identity_of(previewed.election)
    assert store.list_elections() == [], "nothing saved without confirmation"
    assert parser.call_count == 1


async def test_the_preview_carries_the_identity_that_was_read():
    """The user supplied no address and may have misspelled the place, so the
    election this turned out to be is the thing they are confirming."""
    inferred = make_election(nation="Deutschland", state="Sachsen-Anhalt",
                             election_date="2021-06-06")
    _, _, service = build(CountingParser(infers=inferred))
    submitted = await service.submit(make_request())

    previewed = await settle(service, submitted.request_key)
    assert previewed.election.nation == "Deutschland"
    assert previewed.election.state == "Sachsen-Anhalt"
    assert previewed.election.election_date.isoformat() == "2021-06-06"


async def test_confirming_a_preview_saves_it_under_the_identity_that_was_read():
    store, _, service = build()
    submitted = await service.submit(make_request())
    previewed = await settle(service, submitted.request_key)

    confirmed = await service.confirm(submitted.request_key)
    assert confirmed.state is ImportState.READY
    assert confirmed.election_hash == previewed.election_hash
    assert store.get_election(confirmed.election_hash) is not None
    assert store.resolve_request(submitted.request_key) == confirmed.election_hash
    assert store.get_job(submitted.request_key).status is JobStatus.SUCCEEDED


async def test_discarding_a_preview_saves_nothing_and_frees_the_request():
    store, parser, service = build()
    submitted = await service.submit(make_request())
    await settle(service, submitted.request_key)

    assert await service.discard(submitted.request_key) is True
    assert store.list_elections() == []
    assert (await service.status(submitted.request_key)).state is ImportState.UNKNOWN

    retry = await service.submit(make_request())
    assert retry.reused is False, "a discarded preview leaves the request importable"
    await wait_until(lambda: parser.call_count == 2)


async def test_confirming_twice_is_harmless():
    store, _, service = build()
    first = await import_and_save(service)
    again = await service.confirm(first.request_key)

    assert again.state is ImportState.READY
    assert again.election == first.election
    assert len(store.list_elections()) == 1


async def test_asking_for_the_same_election_again_never_extracts_again():
    _, parser, service = build()
    first = await import_and_save(service)

    second = await service.submit(make_request())
    assert second.state is ImportState.READY
    assert second.reused is True
    assert second.election_hash == first.election_hash
    assert parser.call_count == 1, "a request made before must never be looked up again"


async def test_one_users_import_serves_every_other_user():
    _, parser, service = build()
    first = await import_and_save(service)

    other_user = await service.submit(make_request(**ASKED))
    assert other_user.state is ImportState.READY
    assert other_user.election_hash == first.election_hash
    assert other_user.reused is True
    assert parser.call_count == 1


async def test_another_spelling_of_a_stored_election_is_recognised_after_the_fact():
    """Dedup by request key cannot catch "Denmark" against "Danmark": the day
    the election was held is not known until it has been looked up. So it
    happens as soon as the identity is known, and stores nothing twice."""
    store, parser, service = build()
    first = await import_and_save(service, make_request(**ASKED))

    second = await service.submit(make_request(**SPELLED_OTHERWISE))
    settled = await settle(service, second.request_key)

    assert parser.call_count == 2, "the new request had to be answered to be identified"
    assert settled.state is ImportState.READY, "and needs no second confirmation"
    assert settled.election_hash == first.election_hash
    assert len(store.list_elections()) == 1, "one election, two ways of asking"
    assert store.resolve_request(second.request_key) == first.election_hash


async def test_concurrent_callers_trigger_exactly_one_import():
    parser = CountingParser()
    parser.hold()
    _, parser, service = build(parser)

    submissions = await asyncio.gather(*(service.submit(make_request()) for _ in range(8)))
    key = submissions[0].request_key
    assert {s.request_key for s in submissions} == {key}
    assert sum(1 for s in submissions if not s.reused) == 1, "exactly one caller owns the run"

    await parser.started.wait()
    assert parser.call_count == 1

    parser.release()
    results = await asyncio.gather(*(settle(service, key) for _ in submissions))
    assert parser.call_count == 1, "only one import ran for the whole race"
    assert all(r.state is ImportState.PREVIEW for r in results)
    assert all(r.election == results[0].election for r in results), "one result, shared by all"


async def test_a_late_arrival_attaches_to_an_in_flight_import():
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
    assert (await settle(service, owner.request_key)).state is ImportState.PREVIEW
    assert parser.call_count == 1


async def test_a_request_arriving_during_a_preview_sees_the_preview():
    _, parser, service = build()
    first = await service.submit(make_request())
    await settle(service, first.request_key)

    second = await service.submit(make_request())
    assert second.state is ImportState.PREVIEW
    assert second.reused is True
    assert parser.call_count == 1, "an unconfirmed preview must not trigger a second run"


async def test_a_failed_import_does_not_poison_the_request():
    parser = CountingParser(fail_times=1)
    store, parser, service = build(parser)

    first = await service.submit(make_request())
    failed = await settle(service, first.request_key)
    assert failed.state is ImportState.FAILED
    assert failed.error == "extraction failed"

    retry = await service.submit(make_request())
    assert retry.state is ImportState.PENDING, "the request is claimable again after a failure"
    assert (await settle(service, retry.request_key)).state is ImportState.PREVIEW
    assert parser.call_count == 2
    assert store.get_job(first.request_key).attempt == 2


async def test_a_crashed_import_is_reclaimed_once_its_lease_expires():
    now = [1000.0]
    parser = CountingParser()
    parser.hold()
    store, parser, service = build(parser, stale_after=60.0, clock=lambda: now[0])

    first = await service.submit(make_request())
    await parser.started.wait()
    assert store.get_job(first.request_key).status is JobStatus.PENDING

    assert (await service.submit(make_request())).reused is True
    assert parser.call_count == 1

    now[0] += 61.0
    reclaim = await service.submit(make_request())
    assert reclaim.reused is False, "a dead run must not pin the request forever"
    await wait_until(lambda: parser.call_count == 2)


async def test_an_abandoned_preview_is_reclaimed_once_its_lease_expires():
    now = [1000.0]
    store, parser, service = build(stale_after=60.0, clock=lambda: now[0])

    first = await service.submit(make_request())
    await settle(service, first.request_key)
    assert store.get_job(first.request_key).status is JobStatus.AWAITING_CONFIRMATION

    now[0] += 61.0
    reclaim = await service.submit(make_request())
    assert reclaim.reused is False, "an abandoned preview must not pin the request forever"
    await wait_until(lambda: parser.call_count == 2)


async def test_a_crashed_import_stops_being_reported_as_running_once_its_lease_expires():
    """Asking again reclaims a dead run; a poll must not be left waiting on it forever."""
    now = [1000.0]
    parser = CountingParser()
    parser.hold()
    _, parser, service = build(parser, stale_after=60.0, clock=lambda: now[0])

    first = await service.submit(make_request())
    await parser.started.wait()
    assert (await service.status(first.request_key)).state is ImportState.PENDING

    now[0] += 61.0
    stalled = await service.status(first.request_key)

    assert stalled.state is ImportState.FAILED
    assert stalled.error
    assert await service.peek(make_request()) is None, "and asking again would start afresh"
    parser.release()
    await settle(service, first.request_key)


async def test_peeking_hands_back_what_is_already_there_without_starting_anything():
    _, parser, service = build()
    assert await service.peek(make_request()) is None
    assert parser.call_count == 0

    first = await service.submit(make_request())
    await settle(service, first.request_key)
    peeked = await service.peek(make_request())

    assert peeked.state is ImportState.PREVIEW
    assert peeked.reused is True
    assert parser.call_count == 1


async def test_status_reports_unknown_for_an_unseen_request():
    _, _, service = build()
    result = await service.status("0" * 64)
    assert result.state is ImportState.UNKNOWN
    assert result.election is None


async def test_two_different_elections_are_stored_separately():
    danish = make_election(nation="Danmark", election_date="2026-03-25")
    german = make_election(nation="Deutschland", state="Sachsen-Anhalt",
                           election_date="2021-06-06")
    store, _, service = build(CountingParser(by_year={2026: danish, 2021: german}))

    first = await import_and_save(service, make_request(year=2026, nation="Danmark"))
    second = await import_and_save(
        service, make_request(year=2021, nation="Deutschland", subnation="Sachsen-Anhalt")
    )

    assert first.election_hash != second.election_hash
    assert len(store.list_elections()) == 2


async def test_claim_outcomes_are_reported_directly_by_the_store():
    store = InMemoryElectionStore()
    request = make_request()
    assert store.claim("key", request).outcome is ClaimOutcome.STARTED
    assert store.claim("key", request).outcome is ClaimOutcome.ATTACHED

    store.stage("key", make_election())
    assert store.claim("key", request).outcome is ClaimOutcome.ATTACHED, "preview is not re-run"

    store.confirm("key", "hash")
    assert store.claim("key", request).outcome is ClaimOutcome.STORED


class BrokenParser(CountingParser):
    """Fails the way an outage does: with an exception nobody wrote for a user."""

    async def parse(self, request):
        self.calls.append(request)
        raise RuntimeError(
            "Error code: 401 - {'error': {'message': 'invalid x-api-key'}, 'request_id': 'req_1'}"
        )


async def test_an_internal_failure_is_reported_without_its_internals():
    from app.service import IMPORT_FAILED

    store, parser, service = build(BrokenParser())

    first = await service.submit(make_request())
    failed = await settle(service, first.request_key)

    assert failed.state is ImportState.FAILED
    assert failed.error == IMPORT_FAILED


# --- resolve_id: a prefix miss past a stale cache ----------------------------


def _store_directly(inner, election):
    """Confirms `election` straight on `inner`, as another instance would —
    bypassing whatever cache sits between it and an `ImportService`."""
    request_key_ = "key-" + election.election_date.isoformat()
    inner.claim(request_key_, make_request())
    inner.stage(request_key_, election)
    election_hash = identity_of(election)
    inner.confirm(request_key_, election_hash)
    return election_hash


async def test_a_prefix_miss_falls_back_past_a_stale_cached_list():
    """A 16-char id always goes through `list_elections`'s cache; without a
    fallback, an election confirmed on another instance right after this one
    cached an empty (or merely older) listing would 404 here for as long as
    that listing stays stale."""
    from app.cached_store import CachedElectionStore

    now = [0.0]
    inner = InMemoryElectionStore()
    cache = CachedElectionStore(inner, clock=lambda: now[0])
    service = ImportService(cache, CountingParser())

    # Cache an empty listing, as a lookup just before the election below was
    # confirmed elsewhere would.
    assert await service.resolve_id("a" * 16) is None

    election_hash = _store_directly(inner, make_election())
    now[0] = 10.0  # past the refresh floor, still within the listing's TTL

    resolved = await service.resolve_id(election_hash[:16])

    assert resolved is not None
    assert resolved.election_hash == election_hash


async def test_a_prefix_miss_with_no_cache_to_fall_back_past_stays_a_miss():
    """A store with no `list_elections_uncached` (an uncached one, or any
    store that is not `CachedElectionStore`) has nothing stale to fall back
    past, so a genuine miss is still a miss."""
    store, _, service = build()
    assert await service.resolve_id("a" * 16) is None


async def test_polls_offered_before_election_day_are_still_offered_before_it():
    from datetime import date

    store, parser, _ = build()
    service = ImportService(store, parser, today=lambda: date(2026, 9, 1))
    request = make_request()
    key = service.request_key_for(request)
    store.claim(key, request)
    store.offer(key, [make_forecast(election_date="2026-09-13")])

    result = await service.submit(request)

    assert result.state is ImportState.CHOOSE
    assert result.reused is True
    assert parser.call_count == 0, "an offer still standing costs nothing to serve"


async def test_polls_offered_for_an_election_since_held_are_thrown_away():
    """Otherwise asking for Sweden 2026 a week after the vote answers
    "this election hasn't been held yet" with the polls read before it."""
    from datetime import date

    store, parser, _ = build()
    service = ImportService(store, parser, today=lambda: date(2026, 9, 20))
    request = make_request()
    key = service.request_key_for(request)
    store.claim(key, request)
    store.offer(key, [make_forecast(election_date="2026-09-13")])

    result = await service.submit(request)

    assert result.state is not ImportState.CHOOSE, "the stale offer is gone"
    settled = await settle(service, key)
    assert settled.state is ImportState.PREVIEW, "the election is read again"
    assert parser.call_count == 1


async def test_the_lookup_forgets_polls_offered_for_an_election_since_held():
    """`GET /api/elections/lookup` asks `status`, not `peek` — the page never
    got as far as submitting, so the offer has to expire here too."""
    from datetime import date

    store, _, _ = build()
    service = ImportService(store, CountingParser(), today=lambda: date(2026, 9, 20))
    request = make_request()
    key = service.request_key_for(request)
    store.claim(key, request)
    store.offer(key, [make_forecast(election_date="2026-09-13")])

    assert (await service.status(key)).state is ImportState.UNKNOWN
    assert await service.peek(request) is None
