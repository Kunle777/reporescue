"""Background investigation worker (Phase 5A Step 6).

Run as a dedicated process separate from FastAPI request handling::

    python -m app.worker

The worker claims queued jobs atomically, re-ingests the pinned
repository revision, runs the existing investigation pipelines with a
write-through recorder, and finalizes persisted reports. Progress,
partials, and results live in PostgreSQL, so investigations survive
browser refreshes, restarts, and disconnects.

Reliability model: the worker holds a heartbeat-extended lease while a
job runs. A crash (or SIGTERM mid-job) stops the heartbeats; the lease
expires and any worker — including a restarted one — requeues the job
to ``waiting`` with backoff, or fails it when attempts are exhausted.
Cancellation is honored at stage boundaries; in-flight containers run
to their own timeouts and existing cleanup applies.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from . import config
from .db import get_session_factory
from .ingestion import clear_ingestions
from .jobqueue import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_LEASE_SECONDS,
    cancel_job,
    claim_next_job,
    complete_job,
    fail_job,
    get_job_by_investigation,
    heartbeat,
    recover_stale_jobs,
    release_to_waiting,
    utcnow,
)
from .models import ConversationSession, Investigation, Job
from .orchestrate import (
    ContextError,
    HandlerDeps,
    build_context_for_worker,
    execute,
    legacy_error_of,
)
from .recorder import DBRecorder, JobCancelled

# Pipeline error reasons worth one more attempt (transient provider
# issues). Everything else fails fast with diagnostics preserved.
TRANSIENT_ERROR_REASONS = frozenset({"rate_limit", "api_error"})

MAX_FINAL_MESSAGE_CHARS = 8_000
MAX_FINDINGS_PER_RUN = 10


def _fresh_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def create_conversation_session(
    db: Session, investigation: Investigation
) -> ConversationSession:
    """Persist the canonical context snapshot for future conversation."""
    summary = ""
    if isinstance(investigation.final_report, dict):
        summary = str(investigation.final_report.get("summary", ""))[:2000]
    session = ConversationSession(
        investigation_id=investigation.id,
        owner_id=investigation.owner_id,
        context_snapshot={
            "repo_name": investigation.repo_name,
            "repo_url": investigation.repo_url,
            "commit_sha": investigation.commit_sha,
            "mode": investigation.mode,
            "status": investigation.status,
            "verification_status": investigation.verification_status,
            "stages_completed": list(investigation.stages_completed or []),
            "summary": summary,
        },
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _derive_verification(mode: str, report: dict[str, Any]) -> str:
    if mode in ("explain", "health"):
        return "unavailable"
    execution = report.get("execution", {}) or {}
    if execution.get("backend") == "static-only":
        return "unavailable"
    verification = report.get("verification", {}) or {}
    if report.get("verdict") == "TESTS_PASSED":
        return "passed"
    if verification.get("passed") is True:
        return "passed"
    return "failed"


def _finalize_success(
    db: Session,
    investigation: Investigation,
    report: dict[str, Any],
    recorder: DBRecorder,
) -> None:
    investigation.final_report = report
    investigation.limitations = list(report.get("limitations", []) or [])
    investigation.timings = report.get("timings")
    investigation.verification_status = _derive_verification(
        investigation.mode, report
    )
    partial = dict(investigation.partial or {})
    partial["summary"] = str(report.get("summary", ""))[:2000]
    investigation.partial = partial
    db.commit()

    mode = investigation.mode
    if mode == "debug":
        baseline = report.get("baseline", {}) or {}
        for failure in (baseline.get("failures", []) or [])[:MAX_FINDINGS_PER_RUN]:
            recorder.record_finding(
                kind="fact",
                category="test_failure",
                title=str(failure.get("test", "baseline failure"))[:500],
                detail=(
                    f"{failure.get('message', '')}\n"
                    f"{failure.get('traceback', '')}".strip() or None
                ),
                confidence="confirmed",
            )
        if report.get("diagnosis"):
            recorder.record_finding(
                kind="hypothesis",
                category="diagnosis",
                title="AI diagnosis",
                detail=report.get("diagnosis"),
                confidence=report.get("diagnosis_confidence", "hypothesis"),
            )
        patch = report.get("patch", {}) or {}
        edits = patch.get("edits", []) or []
        if edits:
            guard = report.get("guard", {}) or {}
            if patch.get("applied"):
                patch_status = "applied"
            elif guard.get("validated"):
                patch_status = "validated"
            else:
                patch_status = "proposed"
            recorder.record_patch(
                edits=edits, reason=patch.get("reason"), status=patch_status
            )
    elif mode == "health":
        for check in (report.get("checks", []) or []):
            recorder.record_finding(
                kind="fact",
                category="health_check",
                title=f"[{str(check.get('status', '?')).upper()}] {check.get('name', '?')}",
                detail=check.get("detail"),
                confidence="confirmed",
            )
    summary = str(report.get("summary", "")).strip()
    if summary:
        recorder.record_message(role="assistant", content=summary)
    recorder.stage_completed("report")
    create_conversation_session(db, investigation)


def _finalize_error(
    db: Session,
    investigation: Investigation,
    reason: str,
    message: str,
) -> dict[str, Any]:
    error = {"reason": reason, "message": str(message)[:2000]}
    investigation.error = error
    db.commit()
    return error


def run_job(
    session_factory: Callable[[], Session],
    job_id: UUID,
    worker_id: str,
    *,
    backend: Any = None,
    diagnose: Any = None,
    summarize: Any = None,
    is_stopping: Callable[[], bool] | None = None,
) -> str:
    """Execute one claimed job to a terminal (or released) state.

    Returns the job's resulting status. ``backend``/``diagnose``/
    ``summarize`` are test seams; production passes none. Raises nothing:
    unexpected failures are persisted as job/investigation errors.
    """
    stopping = is_stopping or (lambda: False)
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None:
            return "missing"
        if job.status == "cancelled":
            cancel_job(db, job)
            return "cancelled"
        investigation = db.get(Investigation, job.investigation_id)
        if investigation is None:
            return fail_job(
                db, job, {"reason": "missing_investigation", "message": "Investigation row is gone."}
            ).status
        investigation_id = investigation.id
        owner_id = investigation.owner_id
        mode = investigation.mode
        repo_url = investigation.repo_url
        commit_sha = investigation.commit_sha
        max_attempts = investigation.max_attempts
        user_message = investigation.user_message

    recorder = DBRecorder(
        session_factory,
        investigation_id=investigation_id,
        job_id=job_id,
        is_stopping=stopping,
    )
    try:
        recorder.check_cancelled()
        if mode == "debug":
            recorder.stage_started("materialize")
        try:
            ctx = build_context_for_worker(
                repo_url=repo_url,
                commit_sha=commit_sha,
                investigation_id=investigation_id,
                owner_id=owner_id,
                mode=mode,
                message=user_message,
                max_attempts=max_attempts,
            )
        except ContextError as exc:
            with session_factory() as db:
                job = db.get(Job, job_id)
                investigation = db.get(Investigation, investigation_id)
                error = _finalize_error(
                    db, investigation, exc.reason, str(exc),
                )
                return fail_job(db, job, error).status
        try:
            result = execute(
                ctx,
                progress=recorder,
                deps=HandlerDeps(
                    backend=backend, diagnose=diagnose, summarize=summarize
                ),
            )
        finally:
            clear_ingestions()
    except JobCancelled:
        with session_factory() as db:
            job = db.get(Job, job_id)
            return _settle_cancel(db, job, stopping)
    except Exception as exc:  # defensive: never lose a job to a crash
        with session_factory() as db:
            job = db.get(Job, job_id)
            investigation = db.get(Investigation, investigation_id)
            error = _finalize_error(
                db, investigation, "worker_error", f"{type(exc).__name__}: {exc}"
            )
            return fail_job(db, job, error).status

    with session_factory() as db:
        job = db.get(Job, job_id)
        investigation = db.get(Investigation, investigation_id)
        if job.status == "cancelled":
            return cancel_job(db, job).status
        if result.status == "cancelled":
            # The runner converts an in-flight JobCancelled into an
            # honest cancelled outcome instead of raising it.
            return _settle_cancel(db, job, stopping)
        if result.status != "complete":
            legacy = legacy_error_of(result)
            if legacy is not None:
                reason = str(legacy.get("reason", "unknown"))
                message = str(legacy.get("message", "Investigation failed."))
            else:
                error_info = result.error or {}
                reason = str(error_info.get("reason", "unknown"))
                message = str(error_info.get("message", "Investigation failed."))
            error = _finalize_error(db, investigation, reason, message)
            if reason in TRANSIENT_ERROR_REASONS and job.attempts < job.max_attempts:
                release_to_waiting(
                    db, job,
                    backoff_seconds=DEFAULT_BACKOFF_BASE_SECONDS * max(1, job.attempts),
                    error=error,
                )
                return "waiting"
            return fail_job(db, job, error).status
        assert result.report is not None
        _finalize_success(db, investigation, result.report, recorder)
        return complete_job(db, job).status


def _settle_cancel(
    db: Session, job: Job | None, stopping: Callable[[], bool]
) -> str:
    """Settle a cancelled run: requeue when the worker is stopping, else cancel."""
    if job is None or job.status in ("completed", "failed", "cancelled"):
        return job.status if job else "missing"
    if stopping() and job.status != "cancelled":
        release_to_waiting(db, job, backoff_seconds=0)
        return "waiting"
    return cancel_job(db, job).status


class _Heartbeat(threading.Thread):
    """Extend the job lease until told to stop (daemon thread)."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        job_id: UUID,
        worker_id: str,
        interval_seconds: int,
        lease_seconds: int,
    ) -> None:
        super().__init__(daemon=True)
        self._factory = session_factory
        self._job_id = job_id
        self._worker_id = worker_id
        self._interval = max(1, interval_seconds)
        self._lease = lease_seconds
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._factory() as db:
                    alive = heartbeat(
                        db, self._job_id, self._worker_id,
                        lease_seconds=self._lease,
                    )
            except Exception:
                return
            if not alive:
                return


def run_forever(
    session_factory: Callable[[], Session] | None = None,
    *,
    worker_id: str | None = None,
    poll_seconds: int | None = None,
    lease_seconds: int | None = None,
    heartbeat_seconds: int | None = None,
    max_jobs: int | None = None,
    is_stopping: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Claim and execute jobs until stopped. Returns outcome counts.

    ``max_jobs`` bounds one call for tests; production passes None and
    runs until SIGINT/SIGTERM.
    """
    factory = session_factory or get_session_factory()
    wid = worker_id or _fresh_worker_id()
    poll = config.get_worker_poll_seconds() if poll_seconds is None else poll_seconds
    lease = config.get_worker_lease_seconds() if lease_seconds is None else lease_seconds
    beat = (
        config.get_worker_heartbeat_seconds()
        if heartbeat_seconds is None
        else heartbeat_seconds
    )
    stopping = is_stopping or (lambda: False)
    counts = {"completed": 0, "failed": 0, "waiting": 0, "cancelled": 0}
    jobs_done = 0
    with factory() as db:
        recovered = recover_stale_jobs(db)
    if recovered["requeued"] or recovered["failed"]:
        print(
            f"[worker {wid}] startup recovery: "
            f"{recovered['requeued']} requeued, {recovered['failed']} failed",
            flush=True,
        )
    print(f"[worker {wid}] started; polling every {poll}s", flush=True)
    while not stopping():
        if max_jobs is not None and jobs_done >= max_jobs:
            break
        with factory() as db:
            recover_stale_jobs(db)
            job = claim_next_job(db, wid, lease_seconds=lease)
        if job is None:
            time.sleep(max(0, poll))
            continue
        pulse = _Heartbeat(factory, job.id, wid, beat, lease)
        pulse.start()
        try:
            outcome = run_job(factory, job.id, wid, is_stopping=stopping)
        finally:
            pulse.stop()
            pulse.join(timeout=5)
        jobs_done += 1
        if outcome in counts:
            counts[outcome] += 1
        print(f"[worker {wid}] job {job.id} -> {outcome}", flush=True)
    print(f"[worker {wid}] stopping", flush=True)
    return counts


def main() -> dict[str, int]:
    """Process entrypoint with graceful SIGINT/SIGTERM handling."""
    stop = threading.Event()
    try:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except (OSError, ValueError):
        pass
    return run_forever(is_stopping=stop.is_set)


if __name__ == "__main__":
    main()
