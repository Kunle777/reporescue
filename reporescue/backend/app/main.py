from __future__ import annotations

from contextlib import asynccontextmanager

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from . import meta_client as meta_client_module
from . import projects as projects_module
from .auth import AuthenticatedUser, get_current_user
from .db import get_db
from .general import route_objective
from .github_ingest import IngestionError
from .rate_limit import check_rate_limit
from .redact import redact_text, sanitize_model_text
from .usage_quota import (
    UsageLimitExceeded,
    release_global_call,
    release_operation,
    reserve_global_call,
    reserve_operation,
    usage_status,
)
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
from . import patch_download as patch_download_module
from .jobqueue import InvalidMode
from .patch_download import (
    PatchDownloadError,
    PatchNotFoundError,
    PatchTooLargeError,
    PatchUnrenderableError,
)
from .orchestrate import (
    ContextError,
    HandlerDeps,
    NullRecorder,
    build_context_from_record,
    execute,
    legacy_error_of,
)
from .request_logging import RequestTimingMiddleware
from .models import (
    ExecutionRun,
    Finding,
    IdempotencyRecord,
    Investigation,
    InvestigationMessage,
    InvestigationAttempt,
    Patch,
    PatchFile,
)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Log the safe provider/deployment snapshot once at startup."""
    print(meta_client_module.startup_diagnostic_line("[api]"), flush=True)
    yield


app = FastAPI(
    title="RepoRescue API",
    description="Local-first, test-backed patch verification demo.",
    version="0.4.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get_cors_origins(),
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
    mode: Literal["debug", "explain", "health", "general"] = "debug"
    message: str | None = Field(default=None, max_length=8000)
    selected_mode: Literal["debug", "explain", "health"] | None = None
    routing_rationale: str | None = Field(default=None, max_length=2000)
    routing_confidence: Literal["high", "medium", "low"] | None = None
    routing_source: str | None = Field(default=None, max_length=40)


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


class ClassifyRequest(BaseModel):
    message: str = Field(max_length=8000)


class JobCreate(BaseModel):
    ingestion_id: str
    mode: Literal["debug", "explain", "health", "general"] = "debug"
    message: str | None = Field(default=None, max_length=8000)
    max_attempts: int = 3
    project_id: UUID | None = None
    selected_mode: Literal["debug", "explain", "health"] | None = None
    routing_rationale: str | None = Field(default=None, max_length=2000)
    routing_confidence: Literal["high", "medium", "low"] | None = None
    routing_source: str | None = Field(default=None, max_length=40)


class ConversationMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _require_ingestion_owner(ingestion_id: str, user: AuthenticatedUser) -> None:
    """Reject records the caller does not own, including untagged records.

    Ownership tags are set by the authenticated ingest route. Records without
    a tag (legacy, or created outside the route) are inaccessible through
    protected routes: an unknown owner must never read as "no owner".
    """
    owner = get_ingestion_owner(ingestion_id)
    if owner is None or owner != str(user.id):
        raise HTTPException(status_code=404, detail="Not found.")


def _resolve_record(
    ingestion_id: str, user: AuthenticatedUser, db: Session
) -> dict[str, Any]:
    """Return the caller record from memory or the durable snapshot row.

    Memory first (fresh owner tag); durable snapshot rows keep the same
    identifier valid across API restarts and browser refreshes. Unknown
    ids and other owners rows are all 404: an unknown owner must never
    read as "no owner".
    """
    owner = get_ingestion_owner(ingestion_id)
    if owner is not None and owner == str(user.id):
        return get_ingestion(ingestion_id)
    row = jobqueue_module.get_snapshot_for_owner(
        db, snapshot_id=ingestion_id, owner_id=user.id
    )
    if row is not None:
        return jobqueue_module.snapshot_to_record(row)
    raise HTTPException(status_code=404, detail="Not found.")


@app.get("/api/health")
def health() -> dict[str, str]:
    """Process liveness endpoint; it does not claim database readiness."""
    return {"status": "ok", "service": "RepoRescue API"}


def _is_container_backed_debug(record: dict[str, Any], selected_mode: str | None) -> bool:
    """Apply the Debug allowance only when this request can reach a container."""
    if selected_mode != "debug":
        return False
    profile = record.get("profile") if isinstance(record, dict) else None
    capabilities = profile.get("capabilities", {}) if isinstance(profile, dict) else {}
    runnable = capabilities.get("runnable", {}) if isinstance(capabilities, dict) else {}
    if not isinstance(runnable, dict) or not runnable.get("python"):
        return False
    try:
        return config.get_execution_backend() in ("auto", "modal", "docker")
    except config.ConfigError:
        return False


def _reserve_ai_usage(
    db: Session,
    user: AuthenticatedUser,
    *,
    debug: bool = False,
    global_only: bool = False,
    commit: bool = True,
) -> None:
    """Reserve quota before accepting work that can spend Meta API credits."""
    try:
        if global_only:
            reserve_global_call(db, commit=commit)
        else:
            reserve_operation(
                db, user_id=str(user.id), include_debug=debug, commit=commit
            )
    except UsageLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": exc.code,
                "limit": exc.limit,
                "timezone": "Africa/Lagos",
                "resets_at": exc.reset_at.isoformat(),
            },
            headers={"Retry-After": str(max(1, int((exc.reset_at - datetime.now(exc.reset_at.tzinfo)).total_seconds())))},
        ) from exc


@app.get("/api/usage/quota")
def get_usage_quota(
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the authenticated user's daily AI and Debug allowances."""
    return usage_status(db, user_id=str(user.id))


def _request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# A 'processing' claim older than this cannot still be in flight (provider
# calls time out after 60s), so a crash orphan is safe to reclaim: refund
# its quota once and let the same key start a fresh attempt.
IDEMPOTENCY_PROCESSING_TTL_SECONDS = 900


def _processing_expired(record: IdempotencyRecord) -> bool:
    updated = record.updated_at
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age > IDEMPOTENCY_PROCESSING_TTL_SECONDS


def _begin_idempotent_request(
    db: Session,
    *,
    owner_id: UUID,
    scope: str,
    request_key: str,
    payload: dict[str, Any],
    quota_kind: str = "user",
) -> tuple[IdempotencyRecord | None, dict[str, Any] | None]:
    """Create a unique request claim or return the exact cached response.

    A 'failed' claim (or a stale 'processing' orphan) with the same
    payload hash is reclaimed for a fresh attempt: outstanding quota is
    refunded once and the caller reserves anew.
    """
    fingerprint = _request_fingerprint(payload)
    record = IdempotencyRecord(
        owner_id=owner_id,
        scope=scope,
        request_key=request_key,
        request_hash=fingerprint,
        status="processing",
        quota_kind=quota_kind,
        quota_refunded=False,
    )
    db.add(record)
    try:
        db.flush()
        return record, None
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == owner_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.request_key == request_key,
            )
        )
        if existing is None:
            raise HTTPException(status_code=409, detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This request key is already in use. Start a new request.",
            })
        if existing.request_hash != fingerprint:
            raise HTTPException(status_code=409, detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "This request key belongs to a different request. Start a new request.",
            })
        if existing.status == "completed" and isinstance(existing.response, dict):
            return None, existing.response
        if existing.status == "failed" or (
            existing.status == "processing" and _processing_expired(existing)
        ):
            _refund_claim_quota(db, owner_id, existing)
            existing.status = "processing"
            existing.quota_refunded = False
            existing.quota_kind = quota_kind
            db.flush()
            return existing, None
        raise HTTPException(status_code=409, detail={
            "code": "REQUEST_ALREADY_PROCESSING",
            "message": "This request is already being processed. Wait or refresh before retrying.",
        })


def _complete_idempotent_request(
    record: IdempotencyRecord, response: dict[str, Any]
) -> None:
    record.status = "completed"
    record.response = response


def _refund_claim_quota(
    db: Session, owner_id: UUID, record: IdempotencyRecord
) -> None:
    """Return a claim's reserved quota unless already refunded."""
    if record.quota_refunded or record.quota_kind == "none":
        return
    if record.quota_kind == "global":
        release_global_call(db, commit=False)
    else:
        release_operation(
            db,
            user_id=str(owner_id),
            include_debug=(record.quota_kind == "user_debug"),
            commit=False,
        )


def _fail_idempotent_request(
    db: Session, *, owner_id: UUID, record: IdempotencyRecord
) -> None:
    """Mark a claim failed and refund its quota exactly once."""
    record.status = "failed"
    _refund_claim_quota(db, owner_id, record)
    record.quota_refunded = True
    db.commit()


@app.post("/api/demo/investigate")
def investigate(
    request: InvestigationRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Fixture-only demo investigation. Authenticated: in meta AI mode
    this endpoint spends API quota, so anonymous callers are rejected."""
    check_rate_limit("demo", user)
    try:
        if meta_client_module.get_ai_mode() == "meta" and isinstance(db, Session):
            _reserve_ai_usage(db, user)
    except meta_client_module.MetaConfigError:
        pass
    return run_investigation(request.scenario)


def _try_model_classification(text: str) -> dict[str, Any] | None:
    """Route one objective with the model; None when unavailable/failing.

    Model classification runs only in meta AI mode with a configured
    key, and only for objectives the deterministic router did not
    already resolve with high confidence. Any model failure (config,
    transport, refusal, invalid output) yields None so the caller
    falls back to the deterministic result.
    """
    if not text:
        return None
    try:
        if meta_client_module.get_ai_mode() != "meta":
            return None
        api_key = meta_client_module.get_api_key()
    except meta_client_module.MetaConfigError:
        return None
    try:
        return meta_client_module.classify_objective(
            objective=text, api_key=api_key
        )
    except meta_client_module.MetaClientError:
        return None


@app.post("/api/general/classify")
def classify_general_route(
    body: ClassifyRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=16, max_length=128),
) -> dict[str, Any]:
    """Classify a General objective, caching repeat requests by idempotency key."""
    request_record: IdempotencyRecord | None = None
    if isinstance(idempotency_key, str) and idempotency_key:
        profiles_module.get_or_create_profile(
            db, user_id=user.id, username=username_attempt_from_claims(user.claims)
        )
        request_record, cached = _begin_idempotent_request(
            db,
            owner_id=user.id,
            scope="general_classify",
            request_key=idempotency_key,
            payload={"message": body.message.strip()},
            quota_kind="global",
        )
        if cached is not None:
            return cached

    try:
        check_rate_limit("classify", user)
        text = (body.message or "").strip()
        deterministic = route_objective(text)
        signals = deterministic.get("signals", {})
        if (
            deterministic.get("selected_mode")
            and deterministic.get("confidence") == "high"
        ):
            response = {**deterministic, "source": "deterministic"}
        else:
            model_ready = False
            try:
                model_ready = (
                    meta_client_module.get_ai_mode() == "meta"
                    and bool(meta_client_module.get_api_key())
                )
            except meta_client_module.MetaConfigError:
                model_ready = False

            if model_ready:
                # Classification is a distinct provider request but not a
                # second user operation. The final investigation is charged
                # once by POST /api/jobs.
                _reserve_ai_usage(
                    db, user, global_only=True, commit=request_record is None
                )
            model = _try_model_classification(text)
            if model is not None and model.get("selected_mode"):
                response = {
                    "selected_mode": model["selected_mode"],
                    "rationale": model["rationale"],
                    "confidence": model["confidence"],
                    "signals": signals,
                    "secondary_intents": deterministic.get("secondary_intents", []),
                    "source": "model",
                }
            elif model is not None:
                response = {
                    "selected_mode": None,
                    "reason": "needs_clarification",
                    "question": model["question"],
                    "rationale": model["rationale"],
                    "confidence": "none",
                    "signals": signals,
                    "suggested_mode": deterministic.get("suggested_mode"),
                    "source": "model",
                }
            else:
                response = {**deterministic, "source": "deterministic"}

        if request_record is not None:
            _complete_idempotent_request(request_record, response)
            db.commit()
        return response
    except Exception:
        if request_record is not None:
            db.rollback()
        raise


@app.post("/api/repos/ingest")
def ingest_repo(
    request: IngestRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    check_rate_limit("ingest", user)
    record = ingest_repository(request.url, request.ref)
    if record.get("status") == "complete" and record.get("ingestion_id"):
        set_ingestion_owner(record["ingestion_id"], str(user.id))
        try:
            jobqueue_module.save_snapshot_record(
                db, owner_id=user.id, record=record
            )
        except Exception:
            # The in-memory record still serves immediate flows; the
            # durable row is a refresh-safety net, not a gate.
            pass
    return record


@app.get("/api/repos/{ingestion_id}")
def get_repo(
    ingestion_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _resolve_record(ingestion_id, user, db)


@app.post("/api/investigations")
def start_real_investigation(
    request: RealInvestigationRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run an investigation synchronously through shared orchestration.

    Builds a validated context from the live ingestion record and
    executes it through the same ``execute()`` entry point the worker
    uses; responses keep the exact historical per-mode shapes.
    """
    record = _resolve_record(request.ingestion_id, user, db)
    try:
        ctx = build_context_from_record(
            record,
            ingestion_id=request.ingestion_id,
            owner_id=user.id,
            mode=request.mode,
            message=request.message,
            max_attempts=max(1, min(request.max_attempts, 3)),
            preselected_mode=request.selected_mode,
            preselect_rationale=request.routing_rationale,
            preselect_confidence=request.routing_confidence,
            preselect_source=request.routing_source,
        )
    except ContextError as exc:
        if exc.reason == "not_owner":
            raise HTTPException(status_code=404, detail="Not found.")
        return _sync_error(request.mode, record, exc.reason, str(exc))
    effective_mode = request.selected_mode
    if request.mode == "general" and effective_mode is None:
        effective_mode = route_objective(request.message).get("selected_mode")
    if request.mode != "general" or effective_mode is not None:
        _reserve_ai_usage(
            db, user,
            debug=_is_container_backed_debug(record, effective_mode or request.mode),
        )
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
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=16, max_length=128),
) -> dict[str, Any]:
    """Enqueue one persistent investigation; repeat keys return the same job."""
    # Direct Python callers in the isolated unit tests do not pass FastAPI
    # headers. HTTP requests must provide the key; the fallback only keeps
    # direct function invocation backwards-compatible.
    if not isinstance(idempotency_key, str):
        idempotency_key = uuid4().hex

    profiles_module.get_or_create_profile(
        db, user_id=user.id, username=username_attempt_from_claims(user.claims)
    )
    request_record, cached = _begin_idempotent_request(
        db,
        owner_id=user.id,
        scope="create_investigation",
        request_key=idempotency_key,
        payload=body.model_dump(mode="json"),
        quota_kind="user",
    )
    if cached is not None:
        return cached

    try:
        check_rate_limit("jobs", user)
        record = _resolve_record(body.ingestion_id, user, db)
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

        profile = record["profile"]
        preselect = body.mode == "general"
        inferred_mode = body.selected_mode
        if preselect and inferred_mode is None:
            inferred_mode = route_objective(body.message).get("selected_mode")
        if body.mode != "general" or inferred_mode is not None:
            debug_charged = _is_container_backed_debug(record, inferred_mode or body.mode)
            request_record.quota_kind = "user_debug" if debug_charged else "user"
            _reserve_ai_usage(
                db,
                user,
                debug=debug_charged,
                commit=False,
            )

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
            preselected_mode=body.selected_mode if preselect else None,
            preselect_rationale=body.routing_rationale if preselect else None,
            preselect_confidence=body.routing_confidence if preselect else None,
            preselect_source=body.routing_source if preselect else None,
            commit=False,
        )
        response = {
            "job_id": str(job.id),
            "investigation_id": str(investigation.id),
            "status": job.status,
        }
        _complete_idempotent_request(request_record, response)
        db.commit()
        return response
    except InvalidMode as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


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


def _redact_conversation_text(value: object, limit: int = 1800) -> str:
    """Redact credentials from conversation context (see app.redact)."""
    return redact_text(value, limit=limit)


def _build_conversation_context(
    db: Session, investigation: Investigation
) -> tuple[dict[str, Any], list[dict[str, str]], set[str]]:
    """Build bounded, owner-scoped context exclusively from persisted evidence."""
    findings = db.scalars(
        select(Finding)
        .where(Finding.investigation_id == investigation.id)
        .order_by(Finding.created_at.desc())
        .limit(12)
    ).all()
    runs = db.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.investigation_id == investigation.id)
        .order_by(ExecutionRun.created_at.desc())
        .limit(8)
    ).all()
    attempts = db.scalars(
        select(InvestigationAttempt)
        .where(InvestigationAttempt.investigation_id == investigation.id)
        .order_by(InvestigationAttempt.attempt_no.desc())
        .limit(5)
    ).all()
    patches = db.scalars(
        select(Patch)
        .where(Patch.investigation_id == investigation.id)
        .order_by(Patch.created_at.desc())
        .limit(5)
    ).all()
    patch_ids = [patch.id for patch in patches]
    patch_files: dict[UUID, list[str]] = {patch_id: [] for patch_id in patch_ids}
    if patch_ids:
        for patch_file in db.scalars(
            select(PatchFile).where(PatchFile.patch_id.in_(patch_ids))
        ).all():
            patch_files[patch_file.patch_id].append(
                _redact_conversation_text(patch_file.file, 300)
            )

    prior_rows = db.scalars(
        select(InvestigationMessage)
        .where(InvestigationMessage.investigation_id == investigation.id)
        .order_by(InvestigationMessage.created_at.desc())
        .limit(8)
    ).all()
    history = [
        {"role": row.role, "content": _redact_conversation_text(row.content, 1200)}
        for row in reversed(prior_rows)
        if row.role in ("user", "assistant")
    ]

    report = investigation.final_report if isinstance(investigation.final_report, dict) else {}
    evidence_ids: set[str] = set()
    finding_context = []
    for finding in findings:
        evidence_id = f"finding:{finding.id}"
        evidence_ids.add(evidence_id)
        finding_context.append({
            "evidence_id": evidence_id,
            "kind": finding.kind,
            "category": finding.category,
            "title": _redact_conversation_text(finding.title, 300),
            "detail": _redact_conversation_text(finding.detail, 1400),
            "confidence": finding.confidence,
            "evidence": _redact_conversation_text(
                json.dumps(finding.evidence or {}, ensure_ascii=False, default=str), 1600
            ),
        })

    run_context = []
    for run in runs:
        evidence_id = f"run:{run.id}"
        evidence_ids.add(evidence_id)
        failures = []
        for failure in (run.failures or [])[:5]:
            if isinstance(failure, dict):
                failures.append({
                    "test": _redact_conversation_text(failure.get("test"), 200),
                    "kind": _redact_conversation_text(failure.get("kind"), 100),
                    "message": _redact_conversation_text(failure.get("message"), 500),
                    "traceback": _redact_conversation_text(failure.get("traceback"), 700),
                })
        run_context.append({
            "evidence_id": evidence_id,
            "kind": run.kind,
            "status": run.status,
            "passed": run.passed,
            "exit_code": run.exit_code,
            "command": _redact_conversation_text(run.command, 300),
            "backend": run.backend,
            "duration_ms": run.duration_ms,
            "output": _redact_conversation_text(run.output, 1200),
            "failures": failures,
        })

    context = {
        "repository": {
            "name": investigation.repo_name,
            "url": investigation.repo_url,
            "ref": investigation.ref,
            "commit_sha": investigation.commit_sha,
        },
        "investigation": {
            "id": str(investigation.id),
            "requested_mode": investigation.mode,
            "selected_mode": investigation.selected_mode,
            "status": investigation.status,
            "stage": investigation.stage,
            "objective": _redact_conversation_text(investigation.user_message, 1200),
            "verification_status": investigation.verification_status,
            "stages_completed": list(investigation.stages_completed or [])[:20],
            "error": (
                {
                    "reason": investigation.error.get("reason"),
                    "message": _redact_conversation_text(investigation.error.get("message"), 800),
                }
                if isinstance(investigation.error, dict) else None
            ),
        },
        "report": {
            "summary": _redact_conversation_text(report.get("summary"), 1600),
            "diagnosis": _redact_conversation_text(report.get("diagnosis"), 1600),
            "verdict": report.get("verdict"),
            "limitations": [
                _redact_conversation_text(item, 300)
                for item in (investigation.limitations or [])[:8]
            ],
            "routing": report.get("routing") if isinstance(report.get("routing"), dict) else investigation.routing,
        },
        "attempts": [
            {
                "attempt": attempt.attempt_no,
                "diagnosis": _redact_conversation_text(attempt.diagnosis, 1000),
                "confidence": attempt.confidence,
                "guard": attempt.guard,
                "verification": attempt.verification,
            }
            for attempt in attempts
        ],
        "findings": finding_context,
        "execution_runs": run_context,
        "patches": [
            {
                "status": patch.status,
                "reason": _redact_conversation_text(patch.reason, 600),
                "files": patch_files.get(patch.id, []),
            }
            for patch in patches
        ],
    }
    return context, history, evidence_ids


@app.post("/api/investigations/{investigation_id}/conversation", status_code=201)
def send_investigation_message(
    investigation_id: UUID,
    body: ConversationMessageRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=16, max_length=128),
) -> dict[str, Any]:
    """Answer a read-only follow-up using only this owned investigation's evidence."""
    if not isinstance(idempotency_key, str):
        idempotency_key = uuid4().hex
    investigation = jobqueue_module.get_investigation_for_owner(
        db, investigation_id=investigation_id, owner_id=user.id
    )
    if investigation is None:
        raise HTTPException(status_code=404, detail="Not found.")

    request_record, cached = _begin_idempotent_request(
        db,
        owner_id=user.id,
        scope=f"conversation:{investigation_id}",
        request_key=idempotency_key,
        payload={"message": body.message.strip()},
        quota_kind="user",
    )
    if cached is not None:
        return cached

    try:
        check_rate_limit("conversation", user)
        try:
            if meta_client_module.get_ai_mode() != "meta":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "CONVERSATION_AI_UNAVAILABLE",
                        "message": "Investigation Conversation requires Meta AI mode.",
                    },
                )
            api_key = meta_client_module.get_api_key()
        except meta_client_module.MetaConfigError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "CONVERSATION_AI_UNAVAILABLE",
                    "message": "Investigation Conversation is not configured on the backend.",
                },
            ) from exc

        context, history, allowed_evidence = _build_conversation_context(db, investigation)
        _reserve_ai_usage(db, user, commit=False)
        # The claim and quota reservation commit before the provider call
        # so a concurrent retry sees 'processing' instead of double
        # calling. Messages persist only on success, so a failure leaves
        # no orphan rows behind.
        db.commit()
    except Exception:
        db.rollback()
        raise

    try:
        model_result = meta_client_module.conversation_reply(
            context=context,
            history=history,
            question=_redact_conversation_text(body.message.strip(), 2000),
            api_key=api_key,
        )
        returned_ids = model_result["evidence_ids"]
        valid_ids = [item for item in returned_ids if item in allowed_evidence]
        invalid_ids = [item for item in returned_ids if item not in allowed_evidence]
        grounding_status = (
            "citation_issue" if invalid_ids
            else "verified" if valid_ids
            else "unverified"
        )
        answer = sanitize_model_text(model_result["answer"])
    except meta_client_module.MetaRateLimitError as exc:
        _fail_idempotent_request(db, owner_id=user.id, record=request_record)
        raise HTTPException(
            status_code=429,
            detail={
                "code": "PROVIDER_RATE_LIMITED",
                "message": (
                    "The AI provider is rate limiting requests right now. "
                    "Your daily AI allowance hasn't been used. Please try again."
                ),
            },
        ) from exc
    except meta_client_module.MetaClientError as exc:
        _fail_idempotent_request(db, owner_id=user.id, record=request_record)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PROVIDER_UNAVAILABLE",
                "message": (
                    "RepoRescue couldn't complete the AI request right now. "
                    "Your daily AI allowance hasn't been used. Please try again."
                ),
            },
        ) from exc
    except Exception:
        _fail_idempotent_request(db, owner_id=user.id, record=request_record)
        raise

    user_message = InvestigationMessage(
        investigation_id=investigation.id,
        role="user",
        content=body.message.strip(),
    )
    assistant_message = InvestigationMessage(
        investigation_id=investigation.id,
        role="assistant",
        content=answer,
        grounding_status=grounding_status,
        evidence_ids=valid_ids,
    )
    response = {
        "user_message": {"role": "user", "content": user_message.content},
        "assistant_message": {
            "role": "assistant",
            "content": answer,
            "grounding_status": grounding_status,
            "evidence_ids": valid_ids,
        },
    }
    try:
        db.add(user_message)
        db.add(assistant_message)
        _complete_idempotent_request(request_record, response)
        db.commit()
    except Exception:
        db.rollback()
        _fail_idempotent_request(db, owner_id=user.id, record=request_record)
        raise
    return response


@app.get("/api/investigations/{investigation_id}/patch")
def download_investigation_patch(
    investigation_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download an investigation's guarded patch as a unified diff.

    Owner-scoped: unknown ids and other owners' rows are all 404.
    """
    investigation = jobqueue_module.get_investigation_for_owner(
        db, investigation_id=investigation_id, owner_id=user.id
    )
    if investigation is None:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        content, filename, verified = patch_download_module.build_patch_download(
            db, investigation
        )
    except PatchNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PatchUnrenderableError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except PatchTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except PatchDownloadError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return Response(
        content=content,
        media_type="text/x-patch",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Patch-Verified": "true" if verified else "false",
            "X-Patch-Commit": investigation.commit_sha,
        },
    )


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
