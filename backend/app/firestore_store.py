"""Firestore-backed :class:`~app.store.ElectionStore`.

``claim`` and ``confirm`` run inside Firestore transactions, which is what makes
single-flight hold across Cloud Run instances: two containers racing on the same
request both read the same job document, and only the one whose transaction
commits creates it. The loser retries, re-reads the now-pending job, and attaches.

Three collections mirror the two-key model — elections by identity hash, jobs by
request key, and a request-to-election index.
"""

from __future__ import annotations

import logging
import time

from google.cloud import firestore

from .observability import io_span
from .outreach import RETRYABLE_STATUSES, DraftStatus, NewDraft, OutreachDraft
from .outreach import thread_id as _draft_thread_id
from .schema import Election
from .store import (
    DEFAULT_STALE_AFTER_SECONDS,
    DISCARDABLE_STATUSES,
    Claim,
    ClaimOutcome,
    Confirmation,
    ImportRequest,
    Job,
    JobStatus,
    StoredElection,
    _decide,
    select_by_place,
)

log = logging.getLogger(__name__)

ELECTIONS_COLLECTION = "elections"
JOBS_COLLECTION = "import_jobs"
# Request key -> election hash. Keyed by request, so a new collection rather
# than the URL-keyed "pages" one it replaces.
RESULTS_COLLECTION = "import_results"
OUTREACH_COLLECTION = "outreach_drafts"
# thing_id -> draft id. A document of its own so a duplicate ``thing_id`` can
# be caught inside the same transaction that creates the draft, the way the
# election store's single-flight claim uses a document to serialise racers.
OUTREACH_THING_IDS_COLLECTION = "outreach_thing_ids"


def _stored_from_doc(election_hash: str, data: dict) -> StoredElection:
    return StoredElection(
        election_hash=election_hash,
        # Re-validate on read: storage is not trusted to have preserved the schema.
        election=Election.model_validate(data["election"]),
        stored_at=float(data.get("stored_at", 0.0)),
        selected=bool(data.get("selected", False)),
    )


def _job_from_doc(request_key: str, data: dict) -> Job:
    result = data.get("result")
    return Job(
        request_key=request_key,
        status=JobStatus(data["status"]),
        query=data.get("query", ""),
        started_at=float(data.get("started_at", 0.0)),
        attempt=int(data.get("attempt", 1)),
        error=data.get("error"),
        # A staged draft is re-validated on read like anything else from storage.
        result=Election.model_validate(result) if result else None,
        forecasts=tuple(Election.model_validate(item) for item in data.get("forecasts") or ()),
        owner=data.get("owner"),
    )


class FirestoreElectionStore:
    def __init__(
        self,
        client: firestore.Client,
        *,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        clock=time.time,
    ):
        self._db = client
        self._stale_after = stale_after
        self._clock = clock

    def _election_ref(self, election_hash: str):
        return self._db.collection(ELECTIONS_COLLECTION).document(election_hash)

    def _job_ref(self, request_key: str):
        return self._db.collection(JOBS_COLLECTION).document(request_key)

    def _result_ref(self, request_key: str):
        return self._db.collection(RESULTS_COLLECTION).document(request_key)

    def get_election(self, election_hash: str) -> Election | None:
        with io_span(log, "firestore", "get_election", hash=election_hash[:12]) as span:
            snapshot = self._election_ref(election_hash).get()
            span["found"] = snapshot.exists
            if not snapshot.exists:
                return None
            # Re-validate on read: storage is not trusted to have preserved the schema.
            return Election.model_validate(snapshot.to_dict()["election"])

    def get_stored(self, election_hash: str) -> StoredElection | None:
        with io_span(log, "firestore", "get_stored", hash=election_hash[:12]) as span:
            snapshot = self._election_ref(election_hash).get()
            span["found"] = snapshot.exists
            if not snapshot.exists:
                return None
            return _stored_from_doc(election_hash, snapshot.to_dict())

    def list_elections(self, *, selected_only: bool = False) -> list[StoredElection]:
        with io_span(log, "firestore", "list_elections", selected_only=selected_only) as span:
            collection = self._db.collection(ELECTIONS_COLLECTION)
            # Filtering server-side keeps a signed-out visitor from costing a
            # read of every election in the store.
            query = (
                collection.where(filter=firestore.FieldFilter("selected", "==", True))
                if selected_only
                else collection
            )
            stored = [
                _stored_from_doc(snapshot.id, snapshot.to_dict()) for snapshot in query.stream()
            ]
            span["count"] = len(stored)
            return sorted(stored, key=lambda s: s.stored_at)

    def find_by_place(
        self, year: int, nation: str, subnation: str | None = None
    ) -> StoredElection | None:
        with io_span(log, "firestore", "find_by_place", year=year) as span:
            found = select_by_place(self.list_elections(), year, nation, subnation)
            span["found"] = found is not None
            return found

    def set_selected(self, election_hash: str, selected: bool) -> bool:
        with io_span(log, "firestore", "set_selected", hash=election_hash[:12],
                     selected=selected) as span:
            ref = self._election_ref(election_hash)
            if not ref.get().exists:
                span["found"] = False
                return False
            ref.set({"selected": selected}, merge=True)
            span["found"] = True
            return True

    def replace_election(self, election_hash: str, election: Election) -> bool:
        with io_span(log, "firestore", "replace_election", hash=election_hash[:12]) as span:
            ref = self._election_ref(election_hash)
            if not ref.get().exists:
                span["found"] = False
                return False
            # update, not a merge: the whole election is replaced, lists and all.
            ref.update({"election": election.model_dump(mode="json")})
            span["found"] = True
            return True

    def get_job(self, request_key: str) -> Job | None:
        with io_span(log, "firestore", "get_job", request=request_key[:12]) as span:
            snapshot = self._job_ref(request_key).get()
            job = _job_from_doc(request_key, snapshot.to_dict()) if snapshot.exists else None
            span["status"] = job.status.value if job else "absent"
            return job

    def resolve_request(self, request_key: str) -> str | None:
        with io_span(log, "firestore", "resolve_request", request=request_key[:12]) as span:
            snapshot = self._result_ref(request_key).get()
            resolved = snapshot.to_dict().get("election_hash") if snapshot.exists else None
            span["hash"] = resolved[:12] if resolved else "unresolved"
            return resolved

    def _peek(self, request_key: str, transaction=None) -> Claim:
        """The claim decision as read now — inside ``transaction`` when claiming."""
        result_doc = self._result_ref(request_key).get(transaction=transaction)
        election_hash = result_doc.to_dict().get("election_hash") if result_doc.exists else None
        election = None
        if election_hash:
            election_doc = self._election_ref(election_hash).get(transaction=transaction)
            if election_doc.exists:
                election = Election.model_validate(election_doc.to_dict()["election"])

        job_doc = self._job_ref(request_key).get(transaction=transaction)
        job = _job_from_doc(request_key, job_doc.to_dict()) if job_doc.exists else None

        outcome = _decide(election, job, self._clock(), self._stale_after)
        if outcome is ClaimOutcome.STORED:
            return Claim(outcome, request_key, election=election,
                         election_hash=election_hash, job=job)
        return Claim(outcome, request_key, job=job)

    def peek(self, request_key: str) -> Claim:
        with io_span(log, "firestore", "peek", request=request_key[:12]) as span:
            peeked = self._peek(request_key)
            span["outcome"] = peeked.outcome.value
            return peeked

    def claim(
        self, request_key: str, request: ImportRequest, owner: str | None = None
    ) -> Claim:
        job_ref = self._job_ref(request_key)
        db, clock = self._db, self._clock

        @firestore.transactional
        def _claim(transaction):
            peeked = self._peek(request_key, transaction)
            if peeked.outcome is not ClaimOutcome.STARTED:
                return peeked
            job = peeked.job

            new_job = Job(
                request_key=request_key,
                status=JobStatus.PENDING,
                query=request.describe(),
                started_at=clock(),
                attempt=(job.attempt + 1) if job else 1,
                owner=owner,
            )
            # The write is what serialises racing claimers: whichever transaction
            # commits first turns the others' reads stale, forcing them to retry.
            transaction.set(
                job_ref,
                {
                    "status": new_job.status.value,
                    "query": new_job.query,
                    "started_at": new_job.started_at,
                    "attempt": new_job.attempt,
                    "owner": new_job.owner,
                    "error": None,
                    "result": None,
                    "forecasts": None,
                },
            )
            return Claim(ClaimOutcome.STARTED, request_key, job=new_job)

        with io_span(log, "firestore", "claim", request=request_key[:12]) as span:
            claim = _claim(db.transaction())
            span["outcome"] = claim.outcome.value
            span["attempt"] = claim.job.attempt if claim.job else None
            return claim

    def stage(self, request_key: str, election: Election) -> None:
        with io_span(log, "firestore", "stage", request=request_key[:12],
                     total_seats=election.total_seats):
            self._job_ref(request_key).set(
                {
                    "status": JobStatus.AWAITING_CONFIRMATION.value,
                    "result": election.model_dump(mode="json"),
                    "forecasts": None,
                    "error": None,
                    # Restart the lease so the user gets a full window to confirm.
                    "started_at": self._clock(),
                },
                merge=True,
            )

    def offer(self, request_key: str, forecasts: list[Election]) -> None:
        with io_span(log, "firestore", "offer", request=request_key[:12],
                     forecasts=len(forecasts)):
            self._job_ref(request_key).set(
                {
                    "status": JobStatus.AWAITING_CHOICE.value,
                    "forecasts": [f.model_dump(mode="json") for f in forecasts],
                    "result": None,
                    "error": None,
                    # Restart the lease so the user gets a full window to choose.
                    "started_at": self._clock(),
                },
                merge=True,
            )

    def confirm_forecast(
        self, request_key: str, forecast: Election, election_hash: str
    ) -> Confirmation | None:
        election_ref = self._election_ref(election_hash)
        job_ref = self._job_ref(request_key)
        clock = self._clock

        @firestore.transactional
        def _confirm(transaction):
            job_doc = job_ref.get(transaction=transaction)
            election_doc = election_ref.get(transaction=transaction)
            job = _job_from_doc(request_key, job_doc.to_dict()) if job_doc.exists else None
            if job is None or job.status is not JobStatus.AWAITING_CHOICE \
                    or forecast not in job.forecasts:
                return None
            if election_doc.exists:
                already = Election.model_validate(election_doc.to_dict()["election"])
                return Confirmation(election_hash, already, duplicate=True)
            transaction.set(
                election_ref,
                {
                    "election": forecast.model_dump(mode="json"),
                    "stored_at": clock(),
                    "selected": False,
                },
            )
            return Confirmation(election_hash, forecast, duplicate=False)

        with io_span(log, "firestore", "confirm_forecast", request=request_key[:12],
                     hash=election_hash[:12]) as span:
            confirmation = _confirm(self._db.transaction())
            span["result"] = "none" if confirmation is None else (
                "duplicate" if confirmation.duplicate else "stored"
            )
            return confirmation

    def confirm(self, request_key: str, election_hash: str) -> Confirmation | None:
        election_ref = self._election_ref(election_hash)
        job_ref = self._job_ref(request_key)
        result_ref = self._result_ref(request_key)
        clock = self._clock

        @firestore.transactional
        def _confirm(transaction):
            job_doc = job_ref.get(transaction=transaction)
            election_doc = election_ref.get(transaction=transaction)
            result_doc = result_ref.get(transaction=transaction)
            already = (
                Election.model_validate(election_doc.to_dict()["election"])
                if election_doc.exists else None
            )

            job = _job_from_doc(request_key, job_doc.to_dict()) if job_doc.exists else None
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION or job.result is None:
                # Confirming twice is harmless as long as the request resolved here.
                resolved = result_doc.to_dict().get("election_hash") if result_doc.exists else None
                if already is not None and resolved == election_hash:
                    return Confirmation(election_hash, already, duplicate=False)
                return None

            stored_at = clock()
            duplicate = already is not None
            if not duplicate:
                transaction.set(
                    election_ref,
                    {
                        "election": job.result.model_dump(mode="json"),
                        "stored_at": stored_at,
                        "selected": False,
                    },
                )
            transaction.set(result_ref, {"election_hash": election_hash, "linked_at": stored_at})
            transaction.set(
                job_ref,
                {"status": JobStatus.SUCCEEDED.value, "result": None, "owner": None,
                 "finished_at": stored_at},
                merge=True,
            )
            return Confirmation(election_hash, already or job.result, duplicate=duplicate)

        with io_span(log, "firestore", "confirm", request=request_key[:12],
                     hash=election_hash[:12]) as span:
            confirmation = _confirm(self._db.transaction())
            span["result"] = "none" if confirmation is None else (
                "duplicate" if confirmation.duplicate else "stored"
            )
            return confirmation

    def link(self, request_key: str, election_hash: str) -> None:
        with io_span(log, "firestore", "link", request=request_key[:12],
                     hash=election_hash[:12]) as span:
            if not self._election_ref(election_hash).get().exists:
                span["linked"] = False
                return
            linked_at = self._clock()
            batch = self._db.batch()
            batch.set(self._result_ref(request_key),
                      {"election_hash": election_hash, "linked_at": linked_at})
            batch.set(
                self._job_ref(request_key),
                {"status": JobStatus.SUCCEEDED.value, "result": None, "forecasts": None,
                 "owner": None, "finished_at": linked_at},
                merge=True,
            )
            batch.commit()
            span["linked"] = True

    def discard(self, request_key: str) -> bool:
        job_ref = self._job_ref(request_key)

        @firestore.transactional
        def _discard(transaction):
            job_doc = job_ref.get(transaction=transaction)
            if not job_doc.exists:
                return False
            if JobStatus(job_doc.to_dict()["status"]) not in DISCARDABLE_STATUSES:
                return False
            transaction.delete(job_ref)
            return True

        with io_span(log, "firestore", "discard", request=request_key[:12]) as span:
            discarded = _discard(self._db.transaction())
            span["discarded"] = discarded
            return discarded

    def fail(self, request_key: str, error: str) -> None:
        with io_span(log, "firestore", "fail", request=request_key[:12], reason=error):
            self._job_ref(request_key).set(
                {
                    "status": JobStatus.FAILED.value,
                    "error": error[:1000],
                    "result": None,
                    "forecasts": None,
                    "owner": None,
                    "finished_at": self._clock(),
                },
                merge=True,
            )


def _draft_from_doc(draft_id: str, data: dict) -> OutreachDraft:
    return OutreachDraft(
        id=draft_id,
        source=data["source"],
        subreddit=data["subreddit"],
        thing_id=data["thing_id"],
        thread_id=data["thread_id"],
        kind=data["kind"],
        permalink=data["permalink"],
        title=data["title"],
        excerpt=data["excerpt"],
        election_hash=data["election_hash"],
        election_title=data["election_title"],
        link=data["link"],
        reply_text=data["reply_text"],
        verification=data["verification"],
        classifier_reason=data["classifier_reason"],
        created_at=data["created_at"],
        status=DraftStatus(data["status"]),
        token_hash=data.get("token_hash"),
        token_expires_at=data.get("token_expires_at"),
        emailed_at=data.get("emailed_at"),
        decided_at=data.get("decided_at"),
        posted_at=data.get("posted_at"),
        posted_url=data.get("posted_url"),
        last_error=data.get("last_error"),
        edited=bool(data.get("edited", False)),
    )


class FirestoreOutreachStore:
    """The outreach approval queue, alongside the elections in the same project."""

    def __init__(self, client: firestore.Client, *, clock=time.time):
        self._db = client
        self._clock = clock

    def _draft_ref(self, draft_id: str):
        return self._db.collection(OUTREACH_COLLECTION).document(draft_id)

    def _thing_id_ref(self, thing_id: str):
        return self._db.collection(OUTREACH_THING_IDS_COLLECTION).document(thing_id)

    def create(
        self, draft_id: str, draft: NewDraft, *, token_hash: str, token_expires_at: float,
        emailed_at: float,
    ) -> OutreachDraft | None:
        draft_ref = self._draft_ref(draft_id)
        thing_ref = self._thing_id_ref(draft.thing_id)
        data = {
            "source": draft.source,
            "subreddit": draft.subreddit,
            "thing_id": draft.thing_id,
            "thread_id": _draft_thread_id(draft.permalink),
            "kind": draft.kind,
            "permalink": draft.permalink,
            "title": draft.title,
            "excerpt": draft.excerpt,
            "election_hash": draft.election_hash,
            "election_title": draft.election_title,
            "link": draft.link,
            "reply_text": draft.reply_text,
            "verification": draft.verification,
            "classifier_reason": draft.classifier_reason,
            "created_at": draft.created_at,
            "status": DraftStatus.PENDING.value,
            "token_hash": token_hash,
            "token_expires_at": token_expires_at,
            "emailed_at": emailed_at,
        }

        @firestore.transactional
        def _create(transaction):
            if thing_ref.get(transaction=transaction).exists:
                return None
            transaction.set(thing_ref, {"draft_id": draft_id})
            transaction.set(draft_ref, data)
            return _draft_from_doc(draft_id, data)

        with io_span(log, "firestore", "outreach-create", thing_id=draft.thing_id) as span:
            created = _create(self._db.transaction())
            span["duplicate"] = created is None
            return created

    def get(self, draft_id: str) -> OutreachDraft | None:
        with io_span(log, "firestore", "outreach-get", id=draft_id) as span:
            snapshot = self._draft_ref(draft_id).get()
            span["found"] = snapshot.exists
            return _draft_from_doc(draft_id, snapshot.to_dict()) if snapshot.exists else None

    def get_by_token_hash(self, token_hash: str) -> OutreachDraft | None:
        with io_span(log, "firestore", "outreach-get-by-token") as span:
            query = self._db.collection(OUTREACH_COLLECTION).where(
                filter=firestore.FieldFilter("token_hash", "==", token_hash)
            ).limit(1)
            for snapshot in query.stream():
                span["found"] = True
                return _draft_from_doc(snapshot.id, snapshot.to_dict())
            span["found"] = False
            return None

    def list_drafts(self) -> list[OutreachDraft]:
        with io_span(log, "firestore", "outreach-list") as span:
            drafts = [
                _draft_from_doc(snapshot.id, snapshot.to_dict())
                for snapshot in self._db.collection(OUTREACH_COLLECTION).stream()
            ]
            span["count"] = len(drafts)
            return sorted(drafts, key=lambda d: d.created_at)

    def set_status(
        self, draft_id: str, status: DraftStatus, *, consume_token: bool = False, **fields
    ) -> None:
        with io_span(log, "firestore", "outreach-set-status", id=draft_id, status=status.value):
            updates = dict(fields)
            updates["status"] = status.value
            if consume_token:
                updates["token_hash"] = None
                updates["token_expires_at"] = None
            self._draft_ref(draft_id).set(updates, merge=True)

    def claim(
        self, draft_id: str, *, decided_at: float, edited: bool = False
    ) -> OutreachDraft | None:
        draft_ref = self._draft_ref(draft_id)
        retryable = {s.value for s in RETRYABLE_STATUSES}

        @firestore.transactional
        def _claim(transaction):
            snapshot = draft_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            if data.get("status") not in retryable:
                return None
            updates = {
                "status": DraftStatus.APPROVED.value, "decided_at": decided_at, "edited": edited,
            }
            transaction.update(draft_ref, updates)
            data.update(updates)
            return _draft_from_doc(draft_id, data)

        with io_span(log, "firestore", "outreach-claim", id=draft_id) as span:
            claimed = _claim(self._db.transaction())
            span["claimed"] = claimed is not None
            return claimed

    def delete(self, draft_id: str) -> None:
        with io_span(log, "firestore", "outreach-delete", id=draft_id):
            draft = self.get(draft_id)
            batch = self._db.batch()
            batch.delete(self._draft_ref(draft_id))
            if draft is not None:
                batch.delete(self._thing_id_ref(draft.thing_id))
            batch.commit()

    def thread_posted(self, thread_id: str) -> bool:
        with io_span(log, "firestore", "outreach-thread-posted") as span:
            query = self._db.collection(OUTREACH_COLLECTION).where(
                filter=firestore.FieldFilter("thread_id", "==", thread_id)
            ).where(filter=firestore.FieldFilter("status", "==", DraftStatus.POSTED.value)).limit(1)
            posted = any(True for _ in query.stream())
            span["posted"] = posted
            return posted

    def count_posted(self, subreddit: str, since: float) -> int:
        with io_span(log, "firestore", "outreach-count-posted", subreddit=subreddit) as span:
            query = self._db.collection(OUTREACH_COLLECTION).where(
                filter=firestore.FieldFilter("subreddit", "==", subreddit)
            ).where(
                filter=firestore.FieldFilter("status", "==", DraftStatus.POSTED.value)
            ).where(filter=firestore.FieldFilter("posted_at", ">=", since))
            count = sum(1 for _ in query.stream())
            span["count"] = count
            return count

    def count_posted_total(self, since: float) -> int:
        with io_span(log, "firestore", "outreach-count-posted-total") as span:
            query = self._db.collection(OUTREACH_COLLECTION).where(
                filter=firestore.FieldFilter("status", "==", DraftStatus.POSTED.value)
            ).where(filter=firestore.FieldFilter("posted_at", ">=", since))
            count = sum(1 for _ in query.stream())
            span["count"] = count
            return count
