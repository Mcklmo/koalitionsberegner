"""Firestore-backed :class:`~app.store.ElectionStore`.

``claim`` and ``confirm`` run inside Firestore transactions, which is what makes
single-flight hold across Cloud Run instances: two containers racing on the same
page both read the same job document, and only the one whose transaction commits
creates it. The loser retries, re-reads the now-pending job, and attaches.

Three collections mirror the two-key model — elections by identity hash, jobs by
page key, and a page-to-election index.
"""

from __future__ import annotations

import logging
import time

from google.cloud import firestore

from .observability import io_span
from .schema import Election
from .store import (
    DEFAULT_STALE_AFTER_SECONDS,
    Claim,
    ClaimOutcome,
    Confirmation,
    ImportRequest,
    Job,
    JobStatus,
    StoredElection,
    _decide,
)

log = logging.getLogger(__name__)

ELECTIONS_COLLECTION = "elections"
JOBS_COLLECTION = "extraction_jobs"
PAGES_COLLECTION = "pages"


def _job_from_doc(page_key: str, data: dict) -> Job:
    result = data.get("result")
    return Job(
        page_key=page_key,
        status=JobStatus(data["status"]),
        source_url=data.get("source_url", ""),
        started_at=float(data.get("started_at", 0.0)),
        attempt=int(data.get("attempt", 1)),
        error=data.get("error"),
        # A staged draft is re-validated on read like anything else from storage.
        result=Election.model_validate(result) if result else None,
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

    def _job_ref(self, page_key: str):
        return self._db.collection(JOBS_COLLECTION).document(page_key)

    def _page_ref(self, page_key: str):
        return self._db.collection(PAGES_COLLECTION).document(page_key)

    def get_election(self, election_hash: str) -> Election | None:
        with io_span(log, "firestore", "get_election", hash=election_hash[:12]) as span:
            snapshot = self._election_ref(election_hash).get()
            span["found"] = snapshot.exists
            if not snapshot.exists:
                return None
            # Re-validate on read: storage is not trusted to have preserved the schema.
            return Election.model_validate(snapshot.to_dict()["election"])

    def list_elections(self) -> list[StoredElection]:
        with io_span(log, "firestore", "list_elections") as span:
            stored = []
            for snapshot in self._db.collection(ELECTIONS_COLLECTION).stream():
                data = snapshot.to_dict()
                stored.append(
                    StoredElection(
                        election_hash=snapshot.id,
                        election=Election.model_validate(data["election"]),
                        stored_at=float(data.get("stored_at", 0.0)),
                    )
                )
            span["count"] = len(stored)
            return sorted(stored, key=lambda s: s.stored_at)

    def get_job(self, page_key: str) -> Job | None:
        with io_span(log, "firestore", "get_job", page=page_key[:12]) as span:
            snapshot = self._job_ref(page_key).get()
            job = _job_from_doc(page_key, snapshot.to_dict()) if snapshot.exists else None
            span["status"] = job.status.value if job else "absent"
            return job

    def resolve_page(self, page_key: str) -> str | None:
        with io_span(log, "firestore", "resolve_page", page=page_key[:12]) as span:
            snapshot = self._page_ref(page_key).get()
            resolved = snapshot.to_dict().get("election_hash") if snapshot.exists else None
            span["hash"] = resolved[:12] if resolved else "unresolved"
            return resolved

    def claim(self, page_key: str, request: ImportRequest) -> Claim:
        job_ref = self._job_ref(page_key)
        page_ref = self._page_ref(page_key)
        db, stale_after, clock = self._db, self._stale_after, self._clock
        election_ref_for = self._election_ref

        @firestore.transactional
        def _claim(transaction):
            page_doc = page_ref.get(transaction=transaction)
            election_hash = page_doc.to_dict().get("election_hash") if page_doc.exists else None
            election = None
            if election_hash:
                election_doc = election_ref_for(election_hash).get(transaction=transaction)
                if election_doc.exists:
                    election = Election.model_validate(election_doc.to_dict()["election"])

            job_doc = job_ref.get(transaction=transaction)
            job = _job_from_doc(page_key, job_doc.to_dict()) if job_doc.exists else None

            outcome = _decide(election, job, clock(), stale_after)
            if outcome is ClaimOutcome.STORED:
                return Claim(outcome, page_key, election=election,
                             election_hash=election_hash, job=job)
            if outcome is ClaimOutcome.ATTACHED:
                return Claim(outcome, page_key, job=job)

            new_job = Job(
                page_key=page_key,
                status=JobStatus.PENDING,
                source_url=request.source_url,
                started_at=clock(),
                attempt=(job.attempt + 1) if job else 1,
            )
            # The write is what serialises racing claimers: whichever transaction
            # commits first turns the others' reads stale, forcing them to retry.
            transaction.set(
                job_ref,
                {
                    "status": new_job.status.value,
                    "source_url": new_job.source_url,
                    "started_at": new_job.started_at,
                    "attempt": new_job.attempt,
                    "error": None,
                    "result": None,
                },
            )
            return Claim(outcome, page_key, job=new_job)

        with io_span(log, "firestore", "claim", page=page_key[:12]) as span:
            claim = _claim(db.transaction())
            span["outcome"] = claim.outcome.value
            span["attempt"] = claim.job.attempt if claim.job else None
            return claim

    def stage(self, page_key: str, election: Election) -> None:
        with io_span(log, "firestore", "stage", page=page_key[:12],
                     total_seats=election.total_seats):
            self._job_ref(page_key).set(
                {
                    "status": JobStatus.AWAITING_CONFIRMATION.value,
                    "result": election.model_dump(mode="json"),
                    "error": None,
                    # Restart the lease so the user gets a full window to confirm.
                    "started_at": self._clock(),
                },
                merge=True,
            )

    def confirm(self, page_key: str, election_hash: str) -> Confirmation | None:
        election_ref = self._election_ref(election_hash)
        job_ref = self._job_ref(page_key)
        page_ref = self._page_ref(page_key)
        clock = self._clock

        @firestore.transactional
        def _confirm(transaction):
            job_doc = job_ref.get(transaction=transaction)
            election_doc = election_ref.get(transaction=transaction)
            page_doc = page_ref.get(transaction=transaction)
            already = (
                Election.model_validate(election_doc.to_dict()["election"])
                if election_doc.exists else None
            )

            job = _job_from_doc(page_key, job_doc.to_dict()) if job_doc.exists else None
            if job is None or job.status is not JobStatus.AWAITING_CONFIRMATION or job.result is None:
                # Confirming twice is harmless as long as the page resolved here.
                resolved = page_doc.to_dict().get("election_hash") if page_doc.exists else None
                if already is not None and resolved == election_hash:
                    return Confirmation(election_hash, already, duplicate=False)
                return None

            stored_at = clock()
            duplicate = already is not None
            if not duplicate:
                transaction.set(
                    election_ref,
                    {"election": job.result.model_dump(mode="json"), "stored_at": stored_at},
                )
            transaction.set(page_ref, {"election_hash": election_hash, "linked_at": stored_at})
            transaction.set(
                job_ref,
                {"status": JobStatus.SUCCEEDED.value, "result": None, "finished_at": stored_at},
                merge=True,
            )
            return Confirmation(election_hash, already or job.result, duplicate=duplicate)

        with io_span(log, "firestore", "confirm", page=page_key[:12],
                     hash=election_hash[:12]) as span:
            confirmation = _confirm(self._db.transaction())
            span["result"] = "none" if confirmation is None else (
                "duplicate" if confirmation.duplicate else "stored"
            )
            return confirmation

    def link(self, page_key: str, election_hash: str) -> None:
        with io_span(log, "firestore", "link", page=page_key[:12],
                     hash=election_hash[:12]) as span:
            if not self._election_ref(election_hash).get().exists:
                span["linked"] = False
                return
            linked_at = self._clock()
            batch = self._db.batch()
            batch.set(self._page_ref(page_key),
                      {"election_hash": election_hash, "linked_at": linked_at})
            batch.set(
                self._job_ref(page_key),
                {"status": JobStatus.SUCCEEDED.value, "result": None, "finished_at": linked_at},
                merge=True,
            )
            batch.commit()
            span["linked"] = True

    def discard(self, page_key: str) -> bool:
        job_ref = self._job_ref(page_key)

        @firestore.transactional
        def _discard(transaction):
            job_doc = job_ref.get(transaction=transaction)
            if not job_doc.exists:
                return False
            if JobStatus(job_doc.to_dict()["status"]) is not JobStatus.AWAITING_CONFIRMATION:
                return False
            transaction.delete(job_ref)
            return True

        with io_span(log, "firestore", "discard", page=page_key[:12]) as span:
            discarded = _discard(self._db.transaction())
            span["discarded"] = discarded
            return discarded

    def fail(self, page_key: str, error: str) -> None:
        with io_span(log, "firestore", "fail", page=page_key[:12], reason=error):
            self._job_ref(page_key).set(
                {
                    "status": JobStatus.FAILED.value,
                    "error": error[:1000],
                    "result": None,
                    "finished_at": self._clock(),
                },
                merge=True,
            )
