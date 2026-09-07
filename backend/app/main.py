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
from .identity import source_url_key
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

    source_url: str = Field(min_length=1, max_length=2000)

    def to_request(self) -> ImportRequest:
        return ImportRequest(source_url=self.source_url)


class ImportResponse(BaseModel):
    page_key: str
    state: ImportState
    election: Election | None = None
    election_hash: str | None = None
    error: str | None = None
    attempt: int | None = None
    reused: bool = False
    duplicate: bool = False

    @classmethod
    def of(cls, result: ImportResult) -> "ImportResponse":
        return cls(
            page_key=result.page_key,
            state=result.state,
            election=result.election,
            election_hash=result.election_hash,
            error=result.error,
            attempt=result.attempt,
            reused=result.reused,
            duplicate=result.duplicate,
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
    """Import an election from a results URL.

    The agent identifies which election the page reports; the caller supplies
    nothing but the address. A page imported before is served from storage
    without fetching or extracting anything.
    """
    try:
        result = await service.submit(body.to_request())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(result.page_key, min(wait_seconds, max_wait_seconds()))
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
async def lookup_page(
    source_url: str, service: ImportService = Depends(get_service)
) -> ImportResponse:
    """Has this page been imported already? Answered without fetching it."""
    try:
        key = source_url_key(source_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return ImportResponse.of(await service.status(key))


@app.get("/api/elections/pages/{page_key}", response_model=ImportResponse)
async def get_page(
    page_key: str,
    response: Response,
    wait_seconds: float = Query(0.0, ge=0.0),
    service: ImportService = Depends(get_service),
) -> ImportResponse:
    result = await service.status(page_key)
    if result.state is ImportState.PENDING and wait_seconds > 0:
        result = await service.wait_for(page_key, min(wait_seconds, max_wait_seconds()))
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no import for that page")
    if result.state is ImportState.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return ImportResponse.of(result)


@app.post("/api/elections/pages/{page_key}/confirm", response_model=ImportResponse)
async def confirm_election(
    page_key: str, service: ImportService = Depends(get_service)
) -> ImportResponse:
    """Save a previewed election under the identity the agent inferred."""
    result = await service.confirm(page_key)
    if result.state is ImportState.UNKNOWN:
        raise HTTPException(status_code=404, detail="no preview to confirm for that page")
    if result.state is not ImportState.READY:
        raise HTTPException(
            status_code=409, detail=f"nothing awaiting confirmation (state: {result.state.value})"
        )
    return ImportResponse.of(result)


@app.delete("/api/elections/pages/{page_key}/preview", status_code=status.HTTP_204_NO_CONTENT)
async def discard_preview(
    page_key: str, service: ImportService = Depends(get_service)
) -> Response:
    """Reject a previewed election, leaving the page free to import again."""
    if not await service.discard(page_key):
        raise HTTPException(status_code=404, detail="no preview to discard for that page")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/elections/{election_hash}", response_model=ImportResponse)
async def get_election(
    election_hash: str, service: ImportService = Depends(get_service)
) -> ImportResponse:
    """Fetch a stored election by identity, for the picker."""
    election = await service.get_stored(election_hash)
    if election is None:
        raise HTTPException(status_code=404, detail="no stored election with that hash")
    return ImportResponse(
        page_key="", state=ImportState.READY, election=election, election_hash=election_hash
    )



# Serving the page from this app keeps the API same-origin, which is what the
# frontend expects by default. Mounted last so it cannot shadow the API routes.
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2]))
if (FRONTEND_DIR / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
