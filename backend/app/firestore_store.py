"""Firestore-backed :class:`~app.store.ElectionStore`.

``claim`` runs inside a Firestore transaction, which is what makes single-flight
hold across Cloud Run instances: two containers racing on the same hash both read
the same job document, and only the one whose transaction commits creates it.
The loser retries, re-reads the now-pending job, and attaches instead.
"""

from __future__ import annotations

import time

from google.cloud import firestore

from .schema import Election
from .store import (
    DEFAULT_STALE_AFTER_SECONDS,
    Claim,
    ClaimOutcome,
    ImportRequest,
    Job,
    JobStatus,
    StoredElection,
    _decide,
)

ELECTIONS_COLLECTION = "elections"
JOBS_COLLECTION = "parse_jobs"


def _job_from_doc(election_hash: str, data: dict) -> Job:
    return Job(
        election_hash=election_hash,
        status=JobStatus(data["status"]),
        source_url=data.get("source_url", ""),
        started_at=float(data.get("started_at", 0.0)),
        attempt=int(data.get("attempt", 1)),
        error=data.get("error"),
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

    def _job_ref(self, election_hash: str):
        return self._db.collection(JOBS_COLLECTION).document(election_hash)

    def get_election(self, election_hash: str) -> Election | None:
        snapshot = self._election_ref(election_hash).get()
        if not snapshot.exists:
            return None
        # Re-validate on read: storage is not trusted to have preserved the schema.
        return Election.model_validate(snapshot.to_dict()["election"])

    def list_elections(self) -> list[StoredElection]:
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
        return sorted(stored, key=lambda s: s.stored_at)

    def get_job(self, election_hash: str) -> Job | None:
        snapshot = self._job_ref(election_hash).get()
        return _job_from_doc(election_hash, snapshot.to_dict()) if snapshot.exists else None

    def claim(self, election_hash: str, request: ImportRequest) -> Claim:
        election_ref = self._election_ref(election_hash)
        job_ref = self._job_ref(election_hash)
        stale_after, clock = self._stale_after, self._clock

        @firestore.transactional
        def _claim(transaction):
            election_doc = election_ref.get(transaction=transaction)
            job_doc = job_ref.get(transaction=transaction)
            election = (
                Election.model_validate(election_doc.to_dict()["election"])
                if election_doc.exists
                else None
            )
            job = _job_from_doc(election_hash, job_doc.to_dict()) if job_doc.exists else None

            outcome = _decide(election, job, clock(), stale_after)
            if outcome is ClaimOutcome.STORED:
                return Claim(outcome, election_hash, election=election, job=job)
            if outcome is ClaimOutcome.ATTACHED:
                return Claim(outcome, election_hash, job=job)

            new_job = Job(
                election_hash=election_hash,
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
                    "nation": request.nation,
                    "state": request.state,
                    "election_date": request.election_date,
                },
            )
            return Claim(outcome, election_hash, job=new_job)

        return _claim(self._db.transaction())

    def complete(self, election_hash: str, election: Election) -> None:
        stored_at = self._clock()
        batch = self._db.batch()
        batch.set(
            self._election_ref(election_hash),
            {
                "election": election.model_dump(mode="json"),
                "stored_at": stored_at,
            },
        )
        batch.set(
            self._job_ref(election_hash),
            {"status": JobStatus.SUCCEEDED.value, "error": None, "finished_at": stored_at},
            merge=True,
        )
        batch.commit()

    def fail(self, election_hash: str, error: str) -> None:
        self._job_ref(election_hash).set(
            {
                "status": JobStatus.FAILED.value,
                "error": error[:1000],
                "finished_at": self._clock(),
            },
            merge=True,
        )
