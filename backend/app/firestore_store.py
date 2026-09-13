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

    def claim(
        self, request_key: str, request: ImportRequest, owner: str | None = None
    ) -> Claim:
        job_ref = self._job_ref(request_key)
        result_ref = self._result_ref(request_key)
        db, stale_after, clock = self._db, self._stale_after, self._clock
        election_ref_for = self._election_ref

        @firestore.transactional
        def _claim(transaction):
            result_doc = result_ref.get(transaction=transaction)
            election_hash = result_doc.to_dict().get("election_hash") if result_doc.exists else None
            election = None
            if election_hash:
                election_doc = election_ref_for(election_hash).get(transaction=transaction)
                if election_doc.exists:
                    election = Election.model_validate(election_doc.to_dict()["election"])

            job_doc = job_ref.get(transaction=transaction)
            job = _job_from_doc(request_key, job_doc.to_dict()) if job_doc.exists else None

            outcome = _decide(election, job, clock(), stale_after)
            if outcome is ClaimOutcome.STORED:
                return Claim(outcome, request_key, election=election,
                             election_hash=election_hash, job=job)
            if outcome is ClaimOutcome.ATTACHED:
                return Claim(outcome, request_key, job=job)

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
            return Claim(outcome, request_key, job=new_job)

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
                {"status": JobStatus.SUCCEEDED.value, "result": None, "finished_at": stored_at},
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
                 "finished_at": linked_at},
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
                    "finished_at": self._clock(),
                },
                merge=True,
            )
