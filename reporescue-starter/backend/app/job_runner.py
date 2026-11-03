"""Background job runner for persistent investigations.

``POST /api/jobs`` snapshots the ingestion profile onto a SQLite row and
returns immediately; a worker thread then runs the investigation and
persists every stage. Debug mode delegates to the existing synchronous
:func:`real_investigation.investigate_repository` pipeline (re-clone,
baseline, repair, report) through its additive ``on_progress`` hook, so the
pipeline itself is unchanged. Explain/health modes run the deterministic
static builders over the persisted profile snapshot. General mode routes
first and then delegates; the requested mode stays on the row while the
resolved mode drives execution.

Test seams: production never passes ``backend``/``diagnose`` (Docker
selection + live Meta client). Tests inject them through ``_RUN_OVERRIDES``,
which :func:`submit_job` consults when no explicit seam is given.

Progress contract (honest, monotonic): materialize 10, baseline 30, repair
40 + attempt share (capped at 90), report 95, terminal 100.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import static_analysis, store
from .ingestion import get_ingestion
from .real_investigation import MAX_LIVE_ATTEMPTS, investigate_repository
from .routing import route_general

MAX_WORKERS = 4

TERMINAL_STATUSES = ("completed", "failed", "cancelled")

STAGE_PROGRESS = {"materialize": 10, "baseline": 30, "repair": 60, "report": 95}

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="reporescue-job")
_futures: dict[str, Any] = {}
_futures_lock = threading.Lock()

# Test seams (see module docstring). Production leaves both None.
_RUN_OVERRIDES: dict[str, Any] = {"backend": None, "diagnose": None}


class JobNotFoundError(Exception):
    """No owned job matches the id (missing or foreign read as missing)."""


class IngestionMissingError(Exception):
    """The ingestion record is unknown or expired (in-memory registry)."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def submit_job(
    *,
    user_id: str,
    ingestion_id: str,
    mode: str,
    message: str | None,
    max_attempts: int,
    backend: Any = None,
    diagnose: Any = None,
) -> dict[str, str]:
    """Persist a queued job row and start its worker thread.

    Raises :class:`IngestionMissingError` when the ingestion record is
    unknown (404 at the route). The profile snapshot on the row keeps the
    card/detail readable even if the in-memory ingestion later expires.
    """
    record = get_ingestion((ingestion_id or "").strip())
    if record.get("status") != "complete":
        raise IngestionMissingError(
            record.get("message") or "Unknown ingestion record."
        )
    profile = record["profile"]
    job_id = _new_id()
    investigation_id = _new_id()
    now = store.utcnow_iso()
    store.create_job(
        {
            "job_id": job_id,
            "investigation_id": investigation_id,
            "user_id": user_id,
            "ingestion_id": (ingestion_id or "").strip(),
            "mode": mode,
            "max_attempts": max(1, min(int(max_attempts), MAX_LIVE_ATTEMPTS)),
            "status": "queued",
            "progress_pct": 0,
            "repo_name": profile.get("name") or "",
            "repo_url": profile.get("url") or "",
            "commit_sha": profile.get("commit_sha") or "",
            "repo_ref": profile.get("ref"),
            "user_message": message,
            "verification_status": "pending",
            "created_at": now,
            "updated_at": now,
        }
    )
    if backend is None:
        backend = _RUN_OVERRIDES["backend"]
    if diagnose is None:
        diagnose = _RUN_OVERRIDES["diagnose"]
    future = _executor.submit(
        _run_job, job_id, user_id, mode, message, backend, diagnose
    )
    with _futures_lock:
        _futures[job_id] = future
    return {"job_id": job_id, "investigation_id": investigation_id, "status": "queued"}


def _forget_future(job_id: str) -> None:
    with _futures_lock:
        _futures.pop(job_id, None)


def _run_job(
    job_id: str,
    user_id: str,
    mode: str,
    message: str | None,
    backend: Any,
    diagnose: Any,
) -> None:
    started = time.monotonic()
    try:
        row = store.get_job(job_id, user_id)
        if row is None or row.get("cancelled"):
            return
        store.update_job(job_id, {"status": "running"})
        routing: dict[str, Any] | None = None
        resolved = mode
        if mode == "general":
            routing = route_general(message)
            resolved = str(routing["mode"])
            store.update_job(
                job_id,
                {
                    "resolved_mode": resolved,
                    "routing": store._dump_json(routing),
                },
            )
        if resolved in ("explain", "health"):
            _run_static(job_id, user_id, resolved, routing, started)
        else:
            _run_debug(
                job_id, user_id, row, routing, backend, diagnose, started
            )
    except Exception:  # defensive: a runner bug must fail the job, never lose it
        try:
            row = store.get_job(job_id, user_id)
            if row is None or row.get("status") in TERMINAL_STATUSES:
                return
            _fail_job(
                job_id,
                "internal_error",
                f"Job runner crashed: {traceback.format_exc(limit=3)}",
                verification_status="not_verified",
            )
        except Exception:
            pass
    finally:
        _forget_future(job_id)


def _handle_progress(
    job_id: str, user_id: str, max_attempts: int, stage: str, attempt: dict | None
) -> None:
    """Persist one pipeline progress callback (best effort, abandon on cancel)."""
    row = store.get_job(job_id, user_id)
    if row is None or row.get("cancelled"):
        return
    stages = store._load_json(row.get("stages_completed"), [])
    if stage not in stages:
        stages = [*stages, stage]
    progress = STAGE_PROGRESS.get(stage, row.get("progress_pct") or 0)
    fields: dict[str, Any] = {
        "stage": stage,
        "stages_completed": store._dump_json(stages),
        "progress_pct": progress,
    }
    if stage == "repair" and isinstance(attempt, dict):
        attempt_no = attempt.get("attempt_no") or 0
        share = round(50 * attempt_no / max(1, max_attempts))
        fields["progress_pct"] = min(90, 40 + share)
        attempts = store._load_json(row.get("attempts"), [])
        fields["attempts"] = store._dump_json(
            [
                *attempts,
                {
                    "attempt_no": attempt.get("attempt_no"),
                    "diagnosis": attempt.get("diagnosis"),
                    "confidence": attempt.get("confidence"),
                    "patch": attempt.get("patch"),
                    "guard": attempt.get("guard"),
                    "verification": attempt.get("verification"),
                },
            ]
        )
    partial = store._load_json(row.get("partial"), {}) or {}
    partial[f"{stage}_at"] = store.utcnow_iso()
    fields["partial"] = store._dump_json(partial)
    store.update_job(job_id, fields)


def _run_static(
    job_id: str,
    user_id: str,
    resolved: str,
    routing: dict[str, Any] | None,
    started: float,
) -> None:
    """Complete an explain/health investigation from the persisted profile."""
    row = store.get_job(job_id, user_id)
    if row is None or row.get("cancelled"):
        return
    record = get_ingestion(row["ingestion_id"])
    if record.get("status") != "complete":
        _fail_job(
            job_id,
            "not_found",
            (record.get("message") or "Unknown ingestion record.")
            + " Re-ingest the repository and submit again.",
            verification_status="not_verified",
        )
        return
    builder = (
        static_analysis.build_explain
        if resolved == "explain"
        else static_analysis.build_health
    )
    static = builder({"repo_name": row["repo_name"], "profile": record["profile"]})
    final_report: dict[str, Any] = {
        "status": "complete",
        "mode": resolved,
        "summary": static["summary"],
        "overall": static["overall"],
    }
    if routing is not None:
        final_report["routing"] = routing
    store.update_job(
        job_id,
        {
            "status": "completed",
            "progress_pct": 100,
            "stage": "report",
            "completed_at": store.utcnow_iso(),
            "verification_status": "not_applicable",
            "findings": store._dump_json(static["findings"]),
            "limitations": store._dump_json([static_analysis.STATIC_LIMITATION]),
            "final_report": store._dump_json(final_report),
            "timings": store._dump_json(
                {"total_ms": int((time.monotonic() - started) * 1000)}
            ),
        },
    )


def _run_debug(
    job_id: str,
    user_id: str,
    row: dict[str, Any],
    routing: dict[str, Any] | None,
    backend: Any,
    diagnose: Any,
    started: float,
) -> None:
    """Complete a debug investigation via the synchronous pipeline."""
    max_attempts = row.get("max_attempts") or MAX_LIVE_ATTEMPTS

    def _on_progress(stage: str, attempt: dict | None = None) -> None:
        _handle_progress(job_id, user_id, max_attempts, stage, attempt)

    report = investigate_repository(
        row["ingestion_id"],
        backend=backend,
        diagnose=diagnose,
        max_attempts=max_attempts,
        on_progress=_on_progress,
    )
    fresh = store.get_job(job_id, user_id)
    if fresh is None or fresh.get("cancelled"):
        return
    if report.get("status") == "complete":
        _complete_debug(job_id, report, routing, started)
        return
    _fail_job(
        job_id,
        report.get("reason") or "investigation_error",
        report.get("message") or "Investigation failed.",
        verification_status="not_verified",
        baseline=report.get("baseline"),
        final_report={
            "status": "error",
            "reason": report.get("reason"),
            "message": report.get("message"),
        },
    )


def _map_run(kind: str, result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "kind": kind,
        "backend": result.get("backend"),
        "status": result.get("status"),
        "passed": result.get("passed"),
        "exit_code": result.get("exit_code"),
        "output": result.get("output"),
        "failures": result.get("failures") or [],
        "duration_ms": result.get("duration_ms"),
    }


def _fail_job(
    job_id: str,
    reason: str,
    message: str,
    *,
    verification_status: str,
    baseline: dict[str, Any] | None = None,
    final_report: dict[str, Any] | None = None,
) -> None:
    """Mark a job failed, keeping whatever partial evidence was persisted."""
    fields: dict[str, Any] = {
        "status": "failed",
        "error": store._dump_json({"reason": reason, "message": message}),
        "completed_at": store.utcnow_iso(),
        "verification_status": verification_status,
    }
    run = _map_run("baseline", baseline)
    if run is not None:
        fields["runs"] = store._dump_json([run])
    if final_report is not None:
        fields["final_report"] = store._dump_json(final_report)
    store.update_job(job_id, fields)


def _complete_debug(
    job_id: str,
    report: dict[str, Any],
    routing: dict[str, Any] | None,
    started: float,
) -> None:
    """Persist a complete debug report onto its job row."""
    baseline = report.get("baseline") or {}
    verification = report.get("verification") or {}
    verified = baseline.get("passed") is False and verification.get("passed") is True
    green = report.get("attempts") == 0 and baseline.get("passed") is True
    verification_status = (
        "verified" if verified else ("not_applicable" if green else "not_verified")
    )

    runs: list[dict[str, Any]] = []
    for kind, result in (("baseline", baseline), ("verification", verification)):
        mapped = _map_run(kind, result)
        if mapped is not None:
            runs.append(mapped)

    findings: list[dict[str, Any]] = []
    diagnosis = report.get("diagnosis") or ""
    if diagnosis:
        findings.append(
            {
                "kind": "diagnosis",
                "category": None,
                "title": "AI diagnosis",
                "detail": diagnosis,
                "confidence": "confirmed" if verified else "hypothesis",
            }
        )
    elif green:
        findings.append(
            {
                "kind": "fact",
                "category": "tests",
                "title": "Baseline already passes",
                "detail": report.get("summary"),
                "confidence": "confirmed",
            }
        )

    patches: list[dict[str, Any]] = []
    patch = report.get("patch") or {}
    edits = patch.get("edits") or []
    if edits:
        guard = report.get("guard") or {}
        applied = bool(patch.get("applied")) or bool(guard.get("validated"))
        patches.append(
            {
                "status": "applied" if applied else "proposed",
                "reason": patch.get("reason"),
                "files": [
                    {
                        "file": edit.get("file"),
                        "before": edit.get("before"),
                        "after": edit.get("after"),
                    }
                    for edit in edits
                    if isinstance(edit, dict)
                ],
            }
        )

    final_report = dict(report)
    if routing is not None:
        final_report["routing"] = routing
    store.update_job(
        job_id,
        {
            "status": "completed",
            "progress_pct": 100,
            "stage": "report",
            "completed_at": store.utcnow_iso(),
            "verification_status": verification_status,
            "runs": store._dump_json(runs),
            "findings": store._dump_json(findings),
            "patches": store._dump_json(patches),
            "limitations": store._dump_json(report.get("limitations") or []),
            "final_report": store._dump_json(final_report),
            "timings": store._dump_json(
                {"total_ms": int((time.monotonic() - started) * 1000)}
            ),
        },
    )


def _attempt_count(row: dict[str, Any]) -> int:
    return len(store._load_json(row.get("attempts"), []) or [])


def to_card(row: dict[str, Any]) -> dict[str, Any]:
    """Project a job row onto the frontend ``JobCard`` shape."""
    return {
        "job_id": row["job_id"],
        "investigation_id": row["investigation_id"],
        "status": row["status"],
        "attempts": _attempt_count(row),
        "max_attempts": row.get("max_attempts") or MAX_LIVE_ATTEMPTS,
        "error": store._load_json(row.get("error"), None),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "repo_name": row.get("repo_name") or "",
        "mode": row.get("mode"),
        "progress_pct": row.get("progress_pct") or 0,
    }


def to_detail(
    row: dict[str, Any], messages: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Project a job row onto the frontend ``InvestigationDetail`` shape."""
    return {
        "investigation_id": row["investigation_id"],
        "repo_url": row.get("repo_url") or "",
        "repo_name": row.get("repo_name") or "",
        "commit_sha": row.get("commit_sha") or "",
        "ref": row.get("repo_ref"),
        "mode": row.get("mode"),
        "user_message": row.get("user_message"),
        "status": row["status"],
        "stage": row.get("stage"),
        "stages_completed": store._load_json(row.get("stages_completed"), []) or [],
        "progress_pct": row.get("progress_pct") or 0,
        "partial": store._load_json(row.get("partial"), None),
        "verification_status": row.get("verification_status") or "pending",
        "error": store._load_json(row.get("error"), None),
        "limitations": store._load_json(row.get("limitations"), []) or [],
        "final_report": store._load_json(row.get("final_report"), None),
        "timings": store._load_json(row.get("timings"), None),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "job": {
            "job_id": row["job_id"],
            "investigation_id": row["investigation_id"],
            "status": row["status"],
            "attempts": _attempt_count(row),
            "max_attempts": row.get("max_attempts") or MAX_LIVE_ATTEMPTS,
            "error": store._load_json(row.get("error"), None),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        },
        "attempts": store._load_json(row.get("attempts"), []) or [],
        "runs": store._load_json(row.get("runs"), []) or [],
        "findings": store._load_json(row.get("findings"), []) or [],
        "patches": store._load_json(row.get("patches"), []) or [],
        "messages": [
            {"role": message.get("role"), "content": message.get("content")}
            for message in messages or []
        ],
    }


def cancel_job(job_id: str, user_id: str) -> dict[str, Any]:
    """Cancel a non-terminal owned job; terminal jobs return unchanged.

    Raises :class:`JobNotFoundError` when no owned job matches the id. The
    worker checks the flag before persisting anything further, so a
    late-finishing pipeline cannot resurrect a cancelled job.
    """
    row = store.get_job(job_id, user_id)
    if row is None:
        raise JobNotFoundError(job_id)
    if row["status"] in TERMINAL_STATUSES:
        messages = store.list_messages(row["investigation_id"], user_id, 50)
        return to_detail(row, messages)
    with _futures_lock:
        future = _futures.pop(job_id, None)
    if future is not None:
        future.cancel()
    store.update_job(
        job_id,
        {
            "cancelled": 1,
            "status": "cancelled",
            "completed_at": store.utcnow_iso(),
            "verification_status": "not_verified",
        },
    )
    fresh = store.get_job(job_id, user_id)
    assert fresh is not None
    messages = store.list_messages(fresh["investigation_id"], user_id, 50)
    return to_detail(fresh, messages)


def startup_sweep() -> int:
    """Fail every job left non-terminal by a previous process.

    A restart abandons live worker threads, so queued/running rows can never
    complete: they fail honestly instead of polling forever.
    """
    return store.sweep_stale_jobs(
        "server_restart",
        "Server restarted while this investigation was running; "
        "submit again to retry.",
    )
