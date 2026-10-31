from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .ingestion import get_ingestion, ingest_repository
from .orchestrator import investigate as run_investigation

app = FastAPI(
    title="RepoRescue API",
    description="Local-first, test-backed patch verification demo.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class InvestigationRequest(BaseModel):
    scenario: str = "negative-discount"


class IngestRequest(BaseModel):
    url: str
    ref: str | None = None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "RepoRescue API", "mode": "local-demo-no-model-calls"}


@app.post("/api/demo/investigate")
def investigate(request: InvestigationRequest) -> dict[str, Any]:
    return run_investigation(request.scenario)


@app.post("/api/repos/ingest")
def ingest_repo(request: IngestRequest) -> dict[str, Any]:
    return ingest_repository(request.url, request.ref)


@app.get("/api/repos/{ingestion_id}")
def get_repo(ingestion_id: str) -> dict[str, Any]:
    return get_ingestion(ingestion_id)
