from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session

from . import projects as projects_module
from .auth import AuthenticatedUser, get_current_user
from .db import get_db
from .github_ingest import IngestionError
from .ingestion import (
    get_ingestion,
    get_ingestion_owner,
    ingest_repository,
    set_ingestion_owner,
)
from .orchestrator import investigate as run_investigation
from . import profiles as profiles_module
from .profiles import (
    DifferentGitHubAccountError,
    InvalidUsernameError,
    NoGitHubIdentityError,
    UsernameTakenError,
    username_attempt_from_claims,
)
from .projects import DuplicateProjectError
from . import jobqueue as jobqueue_module
from .jobqueue import InvalidMode
from .orchestrate import (
    ContextError,
    HandlerDeps,
    NullRecorder,
    build_context_from_record,
    execute,
    legacy_error_of,
)
from .request_logging import RequestTimingMiddleware

app = FastAPI(
    title="RepoRescue API",
    description="Local-first, test-backed patch verification demo.",
    version="0.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.add_middleware(RequestTimingMiddleware)


class InvestigationRequest(BaseModel):
    scenario: str = "negative-discount"


class IngestRequest(BaseModel):
    url: str
    ref: str | None = None


class RealInvestigationRequest(BaseModel):
    ingestion_id: str
    max_attempts: int = 3
    mode: Literal["debug", "explain", "health"] = "debug"
    message: str | None = None


DisplayName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
BranchName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class ProjectCreate(BaseModel):
    github_url: str
    display_name: DisplayName | None = None
    default_branch: BranchName | None = None


class ProjectUpdate(BaseModel):
    display_name: DisplayName


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    github_owner: str
    repository_name: str
    github_url: str
    default_branch: str | None
    visibility: str
    created_at: datetime
    updated_at: datetime


class ProfileUpdate(BaseModel):
    username: str


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str | None
    display_name: str | None
    avatar_url: str | None
    github_username: str | None
    github_user_id: str | None
    github_avatar_url: str | None
    github_display_name: str | None
    github_connected_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GitHubSyncRequest(BaseModel):
    confirm: bool = False


class JobCreate(BaseModel):
    ingestion_id: str
    mode: Literal["debug", "explain", "health"] = "debug"
    message: str | None = None
    max_attempts: int = 3
    project_id: UUID | None = None


def _require_ingestion_owner(ingestion_id: str, user: AuthenticatedUser) -> None:
    """Reject records the caller does not own, including untagged records.

    Ownership tags are set by the authenticated ingest route. Records without
    a tag (legacy, or created outside the route) are inaccessible through
    protected routes: an unknown owner must never read as "no owner".
    """
    owner = get_ingestion_owner(ingestion_id)
    if owner is None or owner != str(user.id):
        raise HTTPException(status_code=404, detail="Not found.")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "RepoRescue API", "mode": "local-demo-no-model-calls"}


@app.post("/api/demo/investigate")
def investigate(request: InvestigationRequest) -> dict[str, Any]:
    return run_investigation(request.scenario)


@app.post("/api/repos/ingest")
def ingest_repo(
    request: IngestRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, Any]:
    record = ingest_repository(request.url, request.ref)
    if record.get("status") == "complete" and record.get("ingestion_id"):
        set_ingestion_owner(record["ingestion_id"], str(user.id))
    return record


@app.get("/api/repos/{ingestion_id}")
def get_repo(
    ingestion_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, Any]:
    _require_ingestion_owner(ingestion_id, user)
    return get_ingestion(ingestion_id)


@app.post("/api/investigations")
def start_real_investigation(
    request: RealInvestigationRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Run an investigation synchronously through shared orchestration.

    Builds a validated context from the live ingestion record and
    executes it through the same ``execute()`` entry point the worker
    uses; responses keep the exact historical per-mode shapes.
    """
    _require_ingestion_owner(request.ingestion_id, user)
    record = get_ingestion(request.ingestion_id)
    try:
        ctx = build_context_from_record(
            record,
            ingestion_id=request.ingestion_id,
            owner_id=user.id,
            mode=request.mode,
            message=request.message,
            max_attempts=max(1, min(request.max_attempts, 3)),
        )
    except ContextError as exc:
        if exc.reason == "not_owner":
            raise HTTPException(status_code=404, detail="Not found.")
        return _sync_error(request.mode, record, exc.reason, str(exc))
    result = execute(ctx, progress=NullRecorder(), deps=HandlerDeps())
    if result.status == "complete":
        assert result.report is not None
        return result.report
    legacy = legacy_error_of(result)
    if legacy is not None:
        return legacy
    error = result.error or {"reason": "stage_failed", "message": "Stage failed."}
    return _sync_error(
        request.mode, record,
        str(error.get("reason", "stage_failed")),
        str(error.get("message", "Stage failed.")),
    )


def _sync_error(
    mode: str, record: dict[str, Any], reason: str, message: str
) -> dict[str, Any]:
    """Build a legacy-shaped sync error for pre-stage failures.

    These mirror the historical pipeline responses exactly: the debug
    record passthrough, the fixed static-mode texts, and the
    unsupported-project message. Anything else is a corrupt record that
    previously crashed with ``KeyError``; it now fails controlled.
    """
    profile = record.get("profile") if isinstance(record, dict) else None
    if reason in ("unknown_record", "not_found"):
        if mode == "explain":
            return {
                "status": "error",
                "mode": "explain",
                "reason": "not_found",
                "message": "Repository is not ready for explanation.",
            }
        if mode == "health":
            return {
                "status": "error",
                "mode": "health",
                "reason": "not_found",
                "message": "Repository is not ready for health assessment.",
            }
        return {
            "status": "error",
            "reason": record.get("reason", "not_found"),
            "message": record.get("message", "Unknown ingestion record."),
        }
    if reason == "unsupported_project" and mode == "debug" and isinstance(profile, dict):
        return {
            "status": "error",
            "reason": "unsupported_project",
            "message": (
                f"Repository {profile.get('name')} is not a supported Python project."
            ),
            "profile": {
                "url": profile.get("url"),
                "name": profile.get("name"),
                "commit_sha": profile.get("commit_sha"),
            },
        }
    error: dict[str, Any] = {"status": "error", "reason": reason, "message": message}
    if mode in ("explain", "health"):
        error["mode"] = mode
    if isinstance(profile, dict):
        error["profile"] = {
            "url": profile.get("url"),
            "name": profile.get("name"),
            "commit_sha": profile.get("commit_sha"),
        }
    return error


@app.post("/api/jobs", status_code=201)
def create_job_route(
    body: JobCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enqueue a persistent background investigation.

    Snapshots the repository coordinates from the owned ingestion record;
    the worker re-ingests from the pinned SHA, so execution never depends
    on the API process's in-memory store.
    """
    _require_ingestion_owner(body.ingestion_id, user)
    record = get_ingestion(body.ingestion_id)
    if record.get("status") != "complete":
        raise HTTPException(
            status_code=422,
            detail="Ingestion record is no longer available; inspect the repository again.",
        )
    if body.project_id is not None:
        project = projects_module.get_project(
            db, owner_id=user.id, project_id=body.project_id
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Not found.")
    profiles_module.get_or_create_profile(
        db, user_id=user.id, username=username_attempt_from_claims(user.claims)
    )
    profile = record["profile"]
    try:
        investigation, job = jobqueue_module.create_investigation(
            db,
            owner_id=user.id,
            repo_url=profile["url"],
            repo_name=profile["name"],
            commit_sha=profile["commit_sha"],
            ref=profile.get("ref"),
            mode=body.mode,
            user_message=body.message,
            max_attempts=body.max_attempts,
            project_id=body.project_id,
        )
    except InvalidMode as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "job_id": str(job.id),
        "investigation_id": str(investigation.id),
        "status": job.status,
    }


@app.get("/api/jobs/active")
def get_active_job_route(
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Most recently updated non-terminal job: the refresh-restore payload."""
    job = jobqueue_module.get_active_job_for_owner(db, owner_id=user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="No active investigation.")
    investigation = jobqueue_module.get_investigation_for_owner(
        db, investigation_id=job.investigation_id, owner_id=user.id
    )
    if investigation is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return jobqueue_module.investigation_detail(db, investigation)


@app.get("/api/jobs")
def list_jobs_route(
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 20,
) -> dict[str, Any]:
    return {
        "jobs": jobqueue_module.list_job_cards(db, owner_id=user.id, limit=limit)
    }


@app.get("/api/jobs/{job_id}")
def get_job_route(
    job_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job = jobqueue_module.get_job_for_owner(db, job_id=job_id, owner_id=user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found.")
    investigation = jobqueue_module.get_investigation_for_owner(
        db, investigation_id=job.investigation_id, owner_id=user.id
    )
    if investigation is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return jobqueue_module.investigation_detail(db, investigation)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job_route(
    job_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job = jobqueue_module.get_job_for_owner(db, job_id=job_id, owner_id=user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Not found.")
    jobqueue_module.cancel_job(db, job)
    investigation = jobqueue_module.get_investigation_for_owner(
        db, investigation_id=job.investigation_id, owner_id=user.id
    )
    if investigation is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return jobqueue_module.investigation_detail(db, investigation)


@app.get("/api/investigations/{investigation_id}")
def get_investigation_route(
    investigation_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    investigation = jobqueue_module.get_investigation_for_owner(
        db, investigation_id=investigation_id, owner_id=user.id
    )
    if investigation is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return jobqueue_module.investigation_detail(db, investigation)


@app.post("/api/projects", status_code=201, response_model=ProjectResponse)
def create_project_route(
    body: ProjectCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    profiles_module.get_or_create_profile(
        db, user_id=user.id, username=username_attempt_from_claims(user.claims)
    )
    try:
        return projects_module.create_project(
            db,
            owner_id=user.id,
            github_url=body.github_url,
            display_name=body.display_name,
            default_branch=body.default_branch,
        )
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except DuplicateProjectError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/projects", response_model=list[ProjectResponse])
def list_projects_route(
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    return projects_module.list_projects(db, owner_id=user.id)


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project_route(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    project = projects_module.get_project(
        db, owner_id=user.id, project_id=project_id
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return project


@app.patch("/api/projects/{project_id}", response_model=ProjectResponse)
def update_project_route(
    project_id: UUID,
    body: ProjectUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    project = projects_module.update_project_display_name(
        db, owner_id=user.id, project_id=project_id, display_name=body.display_name
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return project


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project_route(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if not projects_module.delete_project(
        db, owner_id=user.id, project_id=project_id
    ):
        raise HTTPException(status_code=404, detail="Not found.")
    return None


@app.get("/api/profile", response_model=ProfileResponse)
def get_profile_route(
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    profile = profiles_module.get_profile(db, user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return profile


@app.patch("/api/profile", response_model=ProfileResponse)
def update_profile_route(
    body: ProfileUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return profiles_module.set_username(db, user_id=user.id, username=body.username)
    except InvalidUsernameError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except UsernameTakenError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/profile/github", response_model=ProfileResponse)
def sync_github_route(
    body: GitHubSyncRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """Store the session's verified GitHub identity. Claims only: the body
    carries just the replace-confirmation flag, never an identity."""
    try:
        return profiles_module.sync_github_identity(
            db, user_id=user.id, claims=user.claims, confirm=body.confirm
        )
    except NoGitHubIdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except DifferentGitHubAccountError as exc:
        stored = f"@{exc.stored_username}" if exc.stored_username else "another account"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{stored} is already connected to this profile. "
                "Confirm replacement to connect a different GitHub account."
            ),
        )


@app.delete("/api/profile/github", response_model=ProfileResponse)
def disconnect_github_route(
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """Clear the GitHub link. Account, username, and projects survive."""
    return profiles_module.disconnect_github_identity(db, user_id=user.id)
