"""Write-through progress persistence for investigations (Steps 5-6).

Pipelines accept an optional ``progress`` recorder (default None, which
preserves exact legacy behavior). The worker passes a :class:`DBRecorder`
that persists stage transitions, attempts, runs, findings, and patches
in short sessions at stage boundaries — never holding a transaction
open across a Docker run or a model call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from .jobqueue import _clip, utcnow
from .models import (
    ExecutionRun,
    Finding,
    Investigation,
    InvestigationAttempt,
    InvestigationMessage,
    Job,
    Patch,
    PatchFile,
)

MAX_DIAGNOSIS_CHARS = 4_000
MAX_DETAIL_CHARS = 8_000
MAX_TITLE_CHARS = 500

class JobCancelled(Exception):
    """The job was cancelled (or the worker is stopping): abort promptly."""


class ProgressRecorder(Protocol):
    """What pipelines may call. All methods are optional to implement."""

    def set_stages(self, stages: tuple[str, ...] | list[str]) -> None: ...
    def stage_started(self, stage: str) -> None: ...
    def stage_completed(self, stage: str, data: dict[str, Any] | None = None) -> None: ...
    def record_run(
        self,
        kind: str,
        result: dict[str, Any],
        attempt_id: UUID | None = None,
    ) -> UUID | None: ...
    def record_attempt(
        self,
        attempt_no: int,
        *,
        diagnosis: str | None = None,
        confidence: str = "hypothesis",
        patch: dict[str, Any] | None = None,
        guard: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        timings: dict[str, Any] | None = None,
    ) -> UUID | None: ...
    def record_finding(
        self,
        *,
        kind: str,
        title: str,
        detail: str | None = None,
        evidence: dict[str, Any] | None = None,
        confidence: str = "hypothesis",
        category: str | None = None,
    ) -> UUID | None: ...
    def record_patch(
        self,
        *,
        edits: list[dict[str, Any]],
        reason: str | None = None,
        status: str = "proposed",
        attempt_id: UUID | None = None,
    ) -> UUID | None: ...
    def record_message(self, *, role: str, content: str) -> UUID | None: ...
    def record_partial(self, data: dict[str, Any]) -> None: ...
    def check_cancelled(self) -> None: ...


class DBRecorder:
    """Persist progress to the investigation/job rows in short sessions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        investigation_id: UUID,
        job_id: UUID,
        is_stopping: Callable[[], bool] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._investigation_id = investigation_id
        self._job_id = job_id
        self._is_stopping = is_stopping or (lambda: False)
        self._stages: tuple[str, ...] = ()

    def set_stages(self, stages: tuple[str, ...] | list[str]) -> None:
        self._stages = tuple(stages)

    def stage_started(self, stage: str) -> None:
        self.check_cancelled()
        with self._sessions() as db:
            investigation = db.get(Investigation, self._investigation_id)
            if investigation is None:
                return
            investigation.stage = stage
            db.commit()

    def stage_completed(
        self, stage: str, data: dict[str, Any] | None = None
    ) -> None:
        with self._sessions() as db:
            investigation = db.get(Investigation, self._investigation_id)
            if investigation is None:
                return
            completed = list(investigation.stages_completed or [])
            if stage not in completed:
                completed.append(stage)
            investigation.stages_completed = completed
            total = len(self._stages) or len(completed)
            investigation.progress_pct = min(
                100, round(100 * len(completed) / max(1, total))
            )
            if data:
                partial = dict(investigation.partial or {})
                partial.update(data)
                investigation.partial = partial
            db.commit()
        self.check_cancelled()

    def record_partial(self, data: dict[str, Any]) -> None:
        """Merge handler-owned partial facts without a stage boundary.

        Migrated stage handlers report data records (attempts, runs,
        partial facts) while the runner owns ``stage_started`` /
        ``stage_completed``; this keeps those ownerships separate.
        """
        if not data:
            return
        with self._sessions() as db:
            investigation = db.get(Investigation, self._investigation_id)
            if investigation is None:
                return
            partial = dict(investigation.partial or {})
            partial.update(data)
            investigation.partial = partial
            db.commit()

    def record_run(
        self,
        kind: str,
        result: dict[str, Any],
        attempt_id: UUID | None = None,
    ) -> UUID | None:
        from .jobqueue import MAX_OUTPUT_CHARS

        with self._sessions() as db:
            run = ExecutionRun(
                investigation_id=self._investigation_id,
                attempt_id=attempt_id,
                kind=kind,
                backend=result.get("backend"),
                isolation=result.get("isolation"),
                status=result.get("status"),
                passed=result.get("passed"),
                exit_code=result.get("exit_code"),
                output=_clip(result.get("output"), MAX_OUTPUT_CHARS),
                truncated=bool(result.get("truncated", False)),
                failures=list(result.get("failures", []) or [])[:50],
                install_ok=result.get("install_ok"),
                install_log=_clip(result.get("install_log"), MAX_OUTPUT_CHARS),
                duration_ms=result.get("duration_ms"),
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            return run.id

    def record_attempt(
        self,
        attempt_no: int,
        *,
        diagnosis: str | None = None,
        confidence: str = "hypothesis",
        patch: dict[str, Any] | None = None,
        guard: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        timings: dict[str, Any] | None = None,
    ) -> UUID | None:
        with self._sessions() as db:
            attempt = InvestigationAttempt(
                investigation_id=self._investigation_id,
                attempt_no=attempt_no,
                diagnosis=_clip(diagnosis, MAX_DIAGNOSIS_CHARS),
                confidence=confidence if confidence in ("confirmed", "hypothesis") else "hypothesis",
                patch=patch,
                guard=guard,
                verification=verification,
                timings=timings,
            )
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            return attempt.id

    def record_finding(
        self,
        *,
        kind: str,
        title: str,
        detail: str | None = None,
        evidence: dict[str, Any] | None = None,
        confidence: str = "hypothesis",
        category: str | None = None,
    ) -> UUID | None:
        with self._sessions() as db:
            finding = Finding(
                investigation_id=self._investigation_id,
                kind=kind if kind in ("fact", "hypothesis") else "hypothesis",
                category=category,
                title=_clip(title, MAX_TITLE_CHARS) or "",
                detail=_clip(detail, MAX_DETAIL_CHARS),
                evidence=dict(evidence or {}),
                confidence=confidence if confidence in ("confirmed", "hypothesis") else "hypothesis",
            )
            db.add(finding)
            db.commit()
            db.refresh(finding)
            return finding.id

    def record_patch(
        self,
        *,
        edits: list[dict[str, Any]],
        reason: str | None = None,
        status: str = "proposed",
        attempt_id: UUID | None = None,
    ) -> UUID | None:
        if status not in ("proposed", "validated", "applied", "rejected"):
            status = "proposed"
        with self._sessions() as db:
            patch = Patch(
                investigation_id=self._investigation_id,
                attempt_id=attempt_id,
                status=status,
                reason=reason,
            )
            db.add(patch)
            db.flush()
            for edit in edits[:25]:
                db.add(
                    PatchFile(
                        patch_id=patch.id,
                        file=str(edit.get("file", ""))[:1000],
                        before=edit.get("before"),
                        after=edit.get("after"),
                    )
                )
            db.commit()
            db.refresh(patch)
            return patch.id

    def record_message(self, *, role: str, content: str) -> UUID | None:
        from .jobqueue import MAX_MESSAGE_CHARS

        if role not in ("user", "assistant", "system"):
            role = "system"
        with self._sessions() as db:
            message = InvestigationMessage(
                investigation_id=self._investigation_id,
                role=role,
                content=_clip(content, MAX_MESSAGE_CHARS) or "",
            )
            db.add(message)
            db.commit()
            db.refresh(message)
            return message.id

    def check_cancelled(self) -> None:
        if self._is_stopping():
            raise JobCancelled("Worker is stopping.")
        with self._sessions() as db:
            job = db.get(Job, self._job_id)
            if job is not None and job.status == "cancelled":
                raise JobCancelled("Job was cancelled.")
