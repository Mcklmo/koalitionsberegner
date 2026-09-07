"""FastAPI surface over the shared election store."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import get_parser, get_store, max_wait_seconds
from .identity import election_hash
from .schema import Election
from .service import ImportRequest, ImportResult, ImportService, ImportState

app = FastAPI(title="Koalitionsberegner election store", version="1.1.0")

# Only needed when the page is served from a different origin than this API
# (e.g. the frontend on Cloudflare, the API on Cloud Run).
_origins = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["content-type"],
    )


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


@app.post("/api/elections/{election_hash}/confirm", response_model=ImportResponse)
async def confirm_election(
    election_hash: str, service: ImportService = Depends(get_service)
) -> ImportResponse:
    """Save a previewed election. This is the only path into storage."""
    result = await service.confirm(election_hash)
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no preview to confirm for that hash")
    if result.state is not ImportState.READY:
        raise HTTPException(
            status_code=409, detail=f"nothing awaiting confirmation (state: {result.state.value})"
        )
    return ImportResponse.of(result)


@app.delete("/api/elections/{election_hash}/preview", status_code=status.HTTP_204_NO_CONTENT)
async def discard_preview(
    election_hash: str, service: ImportService = Depends(get_service)
) -> Response:
    """Reject a previewed election, leaving the hash free to import again."""
    if not await service.discard(election_hash):
        raise HTTPException(status_code=404, detail="no preview to discard for that hash")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


# Serving the page from this app keeps the API same-origin, which is what the
# frontend expects by default. Mounted last so it cannot shadow the API routes.
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2]))
if (FRONTEND_DIR / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
