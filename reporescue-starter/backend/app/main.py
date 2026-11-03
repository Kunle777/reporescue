from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .ingestion import get_ingestion, ingest_repository
from .jobs import router as jobs_router
from .messages import router as messages_router
from .orchestrator import investigate as run_investigation
from .profile import router as profile_router
from .projects import router as projects_router
from .real_investigation import investigate_repository
from . import job_runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_runner.startup_sweep()
    yield


app = FastAPI(
    title="RepoRescue API",
    description="Local-first, test-backed patch verification demo.",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(jobs_router)
app.include_router(messages_router)
app.include_router(projects_router)
app.include_router(profile_router)


class InvestigationRequest(BaseModel):
    scenario: str = "negative-discount"


class IngestRequest(BaseModel):
    url: str
    ref: str | None = None


class RealInvestigationRequest(BaseModel):
    ingestion_id: str
    max_attempts: int = 3


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


@app.post("/api/investigations")
def start_real_investigation(request: RealInvestigationRequest) -> dict[str, Any]:
    return investigate_repository(
        request.ingestion_id, max_attempts=max(1, min(request.max_attempts, 3))
    )
