"""Persistent background job queue over SQLAlchemy (Phase 5A Steps 5-6).

One job per investigation. Claiming is an atomic conditional UPDATE
guarded by status plus a rowcount check, retried over candidates: only
one worker can win a job, on PostgreSQL and on SQLite alike. (``SELECT
... FOR UPDATE SKIP LOCKED`` would also work on PostgreSQL, but the
conditional UPDATE is equally correct for single-row claims and keeps
the tested path identical across dialects.)

Status vocabulary is enforced by DB CHECK constraints; every transition
also passes through :func:`transition_allowed`. Jobs and their
investigations mirror status so either row answers "what is happening".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .models import (
    TERMINAL_STATUSES,
    ExecutionRun,
    Finding,
    Investigation,
    InvestigationAttempt,
    InvestigationMessage,
    Job,
    Patch,
    PatchFile,
    RepoSnapshot,
    transition_allowed,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 60
DEFAULT_BACKOFF_BASE_SECONDS = 60
CLAIM_CANDIDATES = 5

MAX_OUTPUT_CHARS = 20_000
MAX_MESSAGE_CHARS = 8_000
MAX_USER_MESSAGE_CHARS = 2_000


class InvalidTransition(Exception):
    """A requested status change is not a legal lifecycle transition."""


class InvalidMode(Exception):
    """The requested investigation mode is unknown."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[-limit:]


def _set_status(entity: Investigation | Job, to_status: str) -> None:
    if not transition_allowed(entity.status, to_status):
        raise InvalidTransition(
            f"Cannot move {type(entity).__name__} from "
            f"{entity.status!r} to {to_status!r}."
        )
    entity.status = to_status


def create_investigation(
    db: Session,
    *,
    owner_id: UUID,
    repo_url: str,
    repo_name: str,
    commit_sha: str,
    ref: str | None = None,
    mode: str = "debug",
    user_message: str | None = None,
    max_attempts: int = 3,
    project_id: UUID | None = None,
    preselected_mode: str | None = None,
    preselect_rationale: str | None = None,
    preselect_confidence: str | None = None,
    preselect_source: str | None = None,
    commit: bool = True,
) -> tuple[Investigation, Job]:
    """Create a queued investigation plus its job, atomically.

    The initial user message is stored both as the immutable request
    echo (``user_message``) and as the first conversation row. The
    pre-classified General route (from ``POST /api/general/classify``)
    is stored when it names a genuine strategy mode; anything else is
    dropped so the worker routes deterministically.
    """
    if mode not in ("debug", "explain", "health", "general"):
        raise InvalidMode(f"Unknown investigation mode {mode!r}.")
    message = (user_message or "").strip() or None
    if message is not None:
        message = message[:MAX_USER_MESSAGE_CHARS]
    honored = (
        preselected_mode
        if preselected_mode in ("debug", "explain", "health")
        else None
    )
    investigation = Investigation(
        owner_id=owner_id,
        project_id=project_id,
        repo_url=repo_url,
        repo_name=repo_name,
        commit_sha=commit_sha,
        ref=ref,
        mode=mode,
        user_message=message,
        max_attempts=max(1, min(max_attempts, 3)),
        preselected_mode=honored,
        preselect_rationale=(
            str(preselect_rationale)[:2000]
            if honored and preselect_rationale
            else None
        ),
        preselect_confidence=(
            preselect_confidence
            if honored and preselect_confidence in ("high", "medium", "low")
            else None
        ),
        preselect_source=(
            str(preselect_source)[:40] if honored and preselect_source else None
        ),
    )
    db.add(investigation)
    db.flush()
    if message is not None:
        db.add(
            InvestigationMessage(
                investigation_id=investigation.id, role="user", content=message
            )
        )
    job = Job(
        investigation_id=investigation.id,
        owner_id=owner_id,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    )
    db.add(job)
    db.flush()
    if commit:
        db.commit()
        db.refresh(investigation)
        db.refresh(job)
    return investigation, job


def claim_next_job(
    db: Session,
    worker_id: str,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Job | None:
    """Atomically claim the oldest claimable job for ``worker_id``.

    Returns the claimed job, or None when the queue is empty (or the
    claim lost every race). Claim attempts increment the job's attempt
    counter; a job that lost a race is simply skipped, never double-run.
    """
    moment = now or utcnow()
    lease_until = moment + timedelta(seconds=lease_seconds)
    for _ in range(CLAIM_CANDIDATES):
        candidate = db.scalars(
            select(Job)
            .where(
                (Job.status == "queued")
                | (
                    (Job.status == "waiting")
                    & (
                        (Job.not_before_at.is_(None))
                        | (Job.not_before_at <= moment)
                    )
                )
            )
            .order_by(Job.created_at.asc(), Job.id.asc())
            .limit(1)
        ).first()
        if candidate is None:
            return None
        try:
            # synchronize_session=False: only the rowcount matters here;
            # the winner refreshes explicitly below. (Python-side
            # predicate evaluation would also choke on tz-aware vs naive
            # datetimes across dialects.)
            result = db.execute(
                update(Job)
                .where(
                    Job.id == candidate.id,
                    Job.status == candidate.status,
                    (Job.not_before_at.is_(None)) | (Job.not_before_at <= moment),
                )
                .values(
                    status="running",
                    attempts=Job.attempts + 1,
                    worker_id=worker_id,
                    claimed_at=moment,
                    lease_expires_at=lease_until,
                    last_heartbeat_at=moment,
                    not_before_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
        except OperationalError:
            db.rollback()
            return None
        if result.rowcount == 1:
            _mirror_status(db, candidate.id, "running")
            db.commit()
            db.refresh(candidate)
            return candidate
        db.rollback()
    return None


def heartbeat(
    db: Session,
    job_id: UUID,
    worker_id: str,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend the lease on a running job owned by ``worker_id``.

    Returns False when the job is no longer running under this worker
    (cancelled, reaped, or finished): the caller must stop work.
    """
    moment = now or utcnow()
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.worker_id == worker_id, Job.status == "running")
        .values(
            lease_expires_at=moment + timedelta(seconds=lease_seconds),
            last_heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def release_to_waiting(
    db: Session,
    job: Job,
    *,
    backoff_seconds: int,
    error: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    """Return a running job to the queue with a not-before backoff."""
    moment = now or utcnow()
    _set_status(job, "waiting")
    job.not_before_at = moment + timedelta(seconds=max(0, backoff_seconds))
    job.worker_id = None
    job.lease_expires_at = None
    if error is not None:
        job.error = error
    _mirror_status(db, job.id, "waiting")
    db.commit()
    db.refresh(job)
    return job


def complete_job(db: Session, job: Job) -> Job:
    _set_status(job, "completed")
    _mirror_status(db, job.id, "completed")
    db.commit()
    db.refresh(job)
    return job


def fail_job(
    db: Session, job: Job, error: dict[str, Any] | None = None
) -> Job:
    _set_status(job, "failed")
    if error is not None:
        job.error = error
    _mirror_status(db, job.id, "failed", error=error)
    db.commit()
    db.refresh(job)
    return job


def cancel_job(db: Session, job: Job) -> Job:
    """Cancel a non-terminal job. Terminal jobs are returned unchanged."""
    if job.status in TERMINAL_STATUSES:
        return job
    _set_status(job, "cancelled")
    _mirror_status(db, job.id, "cancelled")
    db.commit()
    db.refresh(job)
    return job


def recover_stale_jobs(
    db: Session,
    *,
    now: datetime | None = None,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
) -> dict[str, int]:
    """Requeue or fail jobs whose worker lease expired (crash recovery).

    A job with attempts remaining returns to ``waiting`` with backoff;
    an exhausted job fails with its lease evidence preserved. Jobs whose
    worker is alive keep their lease extended and are never touched.
    """
    moment = now or utcnow()
    stale = list(
        db.scalars(
            select(Job).where(
                Job.status == "running",
                (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < moment),
            )
        ).all()
    )
    if not stale:
        return {"requeued": 0, "failed": 0}
    requeued = 0
    failed = 0
    for job in stale:
        evidence = {
            "reason": "worker_lease_expired",
            "message": (
                f"Worker {job.worker_id or 'unknown'} stopped heartbeating; "
                f"the job was {'requeued' if job.attempts < job.max_attempts else 'failed'} "
                f"(attempt {job.attempts} of {job.max_attempts})."
            ),
            "worker_id": job.worker_id,
        }
        if job.attempts < job.max_attempts:
            _set_status(job, "waiting")
            job.not_before_at = moment + timedelta(
                seconds=backoff_base_seconds * max(1, job.attempts)
            )
            job.worker_id = None
            job.lease_expires_at = None
            job.error = evidence
            _mirror_status(db, job.id, "waiting")
            requeued += 1
        else:
            _set_status(job, "failed")
            job.error = evidence
            _mirror_status(db, job.id, "failed", error=evidence)
            failed += 1
    db.commit()
    return {"requeued": requeued, "failed": failed}


def _mirror_status(
    db: Session, job_id: UUID, to_status: str, error: dict[str, Any] | None = None
) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    investigation = db.get(Investigation, job.investigation_id)
    if investigation is None:
        return
    _set_status(investigation, to_status)
    if error is not None:
        investigation.error = error
    if to_status in TERMINAL_STATUSES:
        investigation.completed_at = utcnow()


def get_job_for_owner(db: Session, *, job_id: UUID, owner_id: UUID) -> Job | None:
    """Return one owned job, or None (including other owners' rows)."""
    return db.scalars(
        select(Job).where(Job.id == job_id, Job.owner_id == owner_id)
    ).first()


def save_snapshot_record(
    db: Session, *, owner_id: UUID, record: dict[str, Any]
) -> RepoSnapshot | None:
    """Persist an ingestion record as a durable snapshot row.

    Uses the record's own ingestion id so job creation resolves the
    same identifier from memory or the database. Returns None when
    the record is incomplete. An existing row id is left untouched
    (ids are random per ingestion; a collision keeps the first row).
    """
    ingestion_id = record.get("ingestion_id")
    profile = record.get("profile")
    context = record.get("context")
    if (
        not isinstance(ingestion_id, str)
        or not ingestion_id.strip()
        or not isinstance(profile, dict)
        or not isinstance(context, dict)
    ):
        return None
    existing = db.get(RepoSnapshot, ingestion_id.strip())
    if existing is not None:
        return existing
    row = RepoSnapshot(
        id=ingestion_id.strip(),
        owner_id=owner_id,
        repo_url=str(profile.get("url", "")),
        repo_name=str(profile.get("name", "")),
        commit_sha=str(profile.get("commit_sha", "")),
        ref=profile.get("ref"),
        profile=profile,
        context=context,
        warnings=list(record.get("warnings", []) or []),
        timings=record.get("timings"),
    )
    db.add(row)
    db.commit()
    return row


def get_snapshot_for_owner(
    db: Session, *, snapshot_id: str, owner_id: UUID
) -> RepoSnapshot | None:
    """Return the owner's snapshot row, or None."""
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        return None
    return db.scalars(
        select(RepoSnapshot).where(
            RepoSnapshot.id == snapshot_id.strip(),
            RepoSnapshot.owner_id == owner_id,
        )
    ).first()


def snapshot_to_record(row: RepoSnapshot) -> dict[str, Any]:
    """Rebuild an ingestion-shaped record from a durable snapshot row."""
    return {
        "status": "complete",
        "ingestion_id": row.id,
        "profile": dict(row.profile or {}),
        "context": dict(row.context or {}),
        "warnings": list(row.warnings or []),
        "timings": dict(row.timings or {}),
        "owner_id": str(row.owner_id),
    }


def get_investigation_for_owner(
    db: Session, *, investigation_id: UUID, owner_id: UUID
) -> Investigation | None:
    return db.scalars(
        select(Investigation).where(
            Investigation.id == investigation_id, Investigation.owner_id == owner_id
        )
    ).first()


def get_active_job_for_owner(db: Session, *, owner_id: UUID) -> Job | None:
    """Most recently updated non-terminal job: the restore candidate."""
    return db.scalars(
        select(Job)
        .where(
            Job.owner_id == owner_id,
            Job.status.notin_(("completed", "failed", "cancelled")),
        )
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(1)
    ).first()


def list_job_cards(
    db: Session, *, owner_id: UUID, limit: int = 20
) -> list[dict[str, Any]]:
    """Newest-first job cards with investigation display fields (one query)."""
    rows = list(
        db.execute(
            select(Job, Investigation)
            .join(Investigation, Investigation.id == Job.investigation_id)
            .where(Job.owner_id == owner_id)
            .order_by(Job.updated_at.desc(), Job.id.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )
    cards = []
    for job, investigation in rows:
        card = job_summary(job)
        card.update(
            {
                "repo_name": investigation.repo_name,
                "mode": investigation.mode,
                "progress_pct": investigation.progress_pct,
            }
        )
        cards.append(card)
    return cards


def list_jobs_for_owner(
    db: Session, *, owner_id: UUID, limit: int = 20
) -> list[Job]:
    return list(
        db.scalars(
            select(Job)
            .where(Job.owner_id == owner_id)
            .order_by(Job.updated_at.desc(), Job.id.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )


def get_job_by_investigation(db: Session, investigation_id: UUID) -> Job | None:
    return db.scalars(
        select(Job).where(Job.investigation_id == investigation_id)
    ).first()


def attempt_status_summary(attempt: InvestigationAttempt) -> str:
    """Derive an attempt's lifecycle state from persisted columns.

    No migration needed: runtime attempts always store ``verification``
    (recorded only after an end-to-end diagnose-verify cycle), static
    attempts store explicit unapplied/unvalidated markers, and retried
    cycles leave no row at all. Returns one of ``proposed`` (diagnosed,
    never verified), ``applied`` (defensive; unreachable via the current
    loop, which always verifies after applying), ``verified``, ``failed``.
    """
    verification = attempt.verification or {}
    if not verification:
        patch = attempt.patch or {}
        if patch.get("applied"):
            return "applied"
        return "proposed"
    if verification.get("passed") is True:
        return "verified"
    return "failed"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def job_summary(job: Job) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "investigation_id": str(job.investigation_id),
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "error": job.error,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def investigation_detail(db: Session, investigation: Investigation) -> dict[str, Any]:
    """Full restore payload: investigation, job, and all children."""
    attempts = list(
        db.scalars(
            select(InvestigationAttempt)
            .where(InvestigationAttempt.investigation_id == investigation.id)
            .order_by(InvestigationAttempt.attempt_no.asc())
        ).all()
    )
    runs = list(
        db.scalars(
            select(ExecutionRun)
            .where(ExecutionRun.investigation_id == investigation.id)
            .order_by(ExecutionRun.created_at.asc())
        ).all()
    )
    findings = list(
        db.scalars(
            select(Finding)
            .where(Finding.investigation_id == investigation.id)
            .order_by(Finding.created_at.asc())
        ).all()
    )
    patches = list(
        db.scalars(
            select(Patch)
            .where(Patch.investigation_id == investigation.id)
            .order_by(Patch.created_at.asc())
        ).all()
    )
    patch_ids = [patch.id for patch in patches]
    files_by_patch: dict[UUID, list[dict[str, Any]]] = {pid: [] for pid in patch_ids}
    if patch_ids:
        for patch_file in db.scalars(
            select(PatchFile).where(PatchFile.patch_id.in_(patch_ids))
        ).all():
            files_by_patch[patch_file.patch_id].append(
                {
                    "file": patch_file.file,
                    "before": patch_file.before,
                    "after": patch_file.after,
                }
            )
    messages = list(
        db.scalars(
            select(InvestigationMessage)
            .where(InvestigationMessage.investigation_id == investigation.id)
            .order_by(InvestigationMessage.created_at.asc())
        ).all()
    )
    job = get_job_by_investigation(db, investigation.id)
    return {
        "investigation_id": str(investigation.id),
        "owner_id": str(investigation.owner_id),
        "project_id": str(investigation.project_id) if investigation.project_id else None,
        "repo_url": investigation.repo_url,
        "repo_name": investigation.repo_name,
        "commit_sha": investigation.commit_sha,
        "ref": investigation.ref,
        "mode": investigation.mode,
        "selected_mode": investigation.selected_mode,
        "routing": investigation.routing,
        "preselected": {
            "mode": investigation.preselected_mode,
            "rationale": investigation.preselect_rationale,
            "confidence": investigation.preselect_confidence,
            "source": investigation.preselect_source,
        },
        "user_message": investigation.user_message,
        "status": investigation.status,
        "stage": investigation.stage,
        "stages_completed": list(investigation.stages_completed or []),
        "progress_pct": investigation.progress_pct,
        "partial": investigation.partial,
        "verification_status": investigation.verification_status,
        "error": investigation.error,
        "limitations": list(investigation.limitations or []),
        "final_report": investigation.final_report,
        "timings": investigation.timings,
        "created_at": _iso(investigation.created_at),
        "updated_at": _iso(investigation.updated_at),
        "completed_at": _iso(investigation.completed_at),
        "job": job_summary(job) if job else None,
        "attempts": [
            {
                "attempt_no": attempt.attempt_no,
                "status": attempt_status_summary(attempt),
                "diagnosis": attempt.diagnosis,
                "confidence": attempt.confidence,
                "patch": attempt.patch,
                "guard": attempt.guard,
                "verification": attempt.verification,
                "timings": attempt.timings,
            }
            for attempt in attempts
        ],
        "runs": [
            {
                "kind": run.kind,
                "command": run.command,
                "backend": run.backend,
                "isolation": run.isolation,
                "status": run.status,
                "passed": run.passed,
                "exit_code": run.exit_code,
                "output": run.output,
                "truncated": run.truncated,
                "failures": list(run.failures or []),
                "install_ok": run.install_ok,
                "install_log": run.install_log,
                "duration_ms": run.duration_ms,
            }
            for run in runs
        ],
        "findings": [
            {
                "kind": finding.kind,
                "category": finding.category,
                "title": finding.title,
                "detail": finding.detail,
                "evidence": finding.evidence,
                "confidence": finding.confidence,
            }
            for finding in findings
        ],
        "patches": [
            {
                "status": patch.status,
                "reason": patch.reason,
                "files": files_by_patch[patch.id],
            }
            for patch in patches
        ],
        "messages": [
            {"role": message.role, "content": message.content}
            for message in messages
        ],
    }
