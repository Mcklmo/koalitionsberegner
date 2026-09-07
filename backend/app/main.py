"""FastAPI surface over the shared election store."""

from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .config import get_parser, get_store, max_wait_seconds
from .identity import election_hash
from .schema import Election
from .service import ImportRequest, ImportResult, ImportService, ImportState

app = FastAPI(title="Koalitionsberegner election store", version="1.0.0")


def get_service() -> ImportService:
    return ImportService(get_store(), get_parser())


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nation: str = Field(min_length=1, max_length=200)
    state: str | None = Field(default=None, max_length=200)
    election_date: date
    source_url: str = Field(min_length=1, max_length=2000)

    def to_request(self) -> ImportRequest:
        return ImportRequest(
            nation=self.nation,
            state=self.state,
            election_date=self.election_date.isoformat(),
            source_url=self.source_url,
        )


class ImportResponse(BaseModel):
    election_hash: str
    state: ImportState
    election: Election | None = None
    error: str | None = None
    attempt: int | None = None
    reused: bool = False

    @classmethod
    def of(cls, result: ImportResult) -> "ImportResponse":
        return cls(
            election_hash=result.election_hash,
            state=result.state,
            election=result.election,
            error=result.error,
            attempt=result.attempt,
            reused=result.reused,
        )


class ElectionSummary(BaseModel):
    election_hash: str
    nation: str
    state: str | None
    election_date: date
    title: str
    total_seats: int


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/elections/import", response_model=ImportResponse)
async def import_election(
    body: ImportBody,
    response: Response,
    wait_seconds: float = Query(0.0, ge=0.0, description="Block for up to this long for a result."),
    service: ImportService = Depends(get_service),
) -> ImportResponse:
    """Import an election, or return the stored one if it already exists.

    Concurrent callers for the same nation/state/date never start a second
    parse: the first claims the hash, the rest attach to that run.
    """
    result = await service.submit(body.to_request())
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(
            result.election_hash, min(wait_seconds, max_wait_seconds())
        )
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)


@app.get("/api/elections", response_model=list[ElectionSummary])
async def list_elections(service: ImportService = Depends(get_service)) -> list[ElectionSummary]:
    return [
        ElectionSummary(
            election_hash=stored.election_hash,
            nation=stored.election.nation,
            state=stored.election.state,
            election_date=stored.election.election_date,
            title=stored.election.title,
            total_seats=stored.election.total_seats,
        )
        for stored in await service.list_elections()
    ]


@app.get("/api/elections/lookup", response_model=ImportResponse)
async def lookup_election(
    nation: str,
    election_date: date,
    state: str | None = None,
    service: ImportService = Depends(get_service),
) -> ImportResponse:
    """Resolve metadata to a hash and report what the store holds for it."""
    key = election_hash(nation, state, election_date)
    return ImportResponse.of(await service.status(key))


@app.get("/api/elections/{election_hash}", response_model=ImportResponse)
async def get_election(
    election_hash: str,
    response: Response,
    wait_seconds: float = Query(0.0, ge=0.0),
    service: ImportService = Depends(get_service),
) -> ImportResponse:
    result = await service.status(election_hash)
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(election_hash, min(wait_seconds, max_wait_seconds()))
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no election or parse job for that hash")
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)
