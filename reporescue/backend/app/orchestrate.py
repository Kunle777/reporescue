"""Shared investigation orchestration (Phase 5B Tasks 2-3).

One modular-monolith engine hosting the Debug, Debug-static, Explain,
and Health stage plans. Mode logic lives in the mode modules
(``real_investigation``, ``explain``, ``health``); this module owns the
contracts, context builders, plans, and the stage runner only.

Progress ownership: the runner programs recorder stage boundaries for
every migrated stage. Handlers persist data records (attempts, runs)
but never call ``set_stages``/``stage_started``/``stage_completed``;
``manages_progress=True`` remains supported for whole-pipeline handlers
outside the migrated plans. The final ``report`` boundary stays owned
by worker finalization, which marks it only after the final report is
persisted.

Handlers share one ``RunScope`` per run: the disposable workspace
lifetime plus prior stages' outputs. The scope is created and closed by
the runner, so workspaces are removed even when a stage fails or the
run is cancelled.
"""

from __future__ import annotations

import importlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from .recorder import JobCancelled, ProgressRecorder

MAX_CONTEXT_MESSAGE_CHARS = 2000
MAX_CONTEXT_EXCERPT_FILES = 12
MAX_CONTEXT_EXCERPT_CHARS = 4000

# Ingestion context list keys carried for static report builders. The
# lists are path inventories already bounded by ingestion (5000 files),
# so they pass through without further capping to keep prompts identical.
CONTEXT_LIST_KEYS = ("entry_points", "configs", "tests", "docs")

MODES = ("debug", "explain", "health", "general")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class OrchestrationError(Exception):
    """Base for orchestration contract violations."""


class ContextError(OrchestrationError):
    """Investigation context is missing, invalid, or unowned."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnknownPlanError(OrchestrationError):
    """No executable plan exists for the requested mode."""


class UnsupportedProjectError(OrchestrationError):
    """Capabilities admit no investigation plan (not a runnable project)."""


@dataclass(frozen=True)
class InvestigationContext:
    """Validated, self-contained input for one orchestration run.

    Built once (from a validated ingestion record, or via worker
    re-ingestion) and then immutable: handlers never touch the global
    ingestion store. ``investigation_id`` is ephemeral (fresh ``uuid4``)
    for sync runs that persist nothing.
    """

    investigation_id: UUID
    owner_id: UUID
    repo_url: str
    repo_name: str
    commit_sha: str
    ref: str | None
    mode: str
    plan_id: str
    message: str | None
    max_attempts: int
    profile: dict[str, Any]
    capabilities: dict[str, Any]
    excerpts: dict[str, str]
    file_lists: dict[str, list[str]] = field(default_factory=dict)
    routing: dict[str, Any] | None = None
    preselected_mode: str | None = None
    preselect_rationale: str | None = None
    preselect_confidence: str | None = None
    preselect_source: str | None = None


@dataclass(frozen=True)
class StageResult:
    """One stage's outcome. ``output`` carries a handler's primary blob
    (the final mode report, or ``{"legacy_error": ...}`` on failure);
    at most one stage per plan sets the report. ``reason`` preserves the
    handler's exact error code (``clone_failed``, ``rate_limit``, ...)."""

    stage: str
    status: str  # "ok" | "failed" | "cancelled" | "skipped"
    detail: str | None = None
    reason: str | None = None
    refs: dict[str, list[str]] = field(default_factory=dict)
    elapsed_ms: int = 0
    output: dict[str, Any] | None = None


@dataclass(frozen=True)
class StageDefinition:
    """One planned stage: handler key, capability gates, progress owner.

    ``manages_progress=True`` means the handler drives the recorder's
    stage boundaries itself (whole-pipeline handlers); the runner then
    passes the recorder through and performs no boundary calls for that
    stage. ``requires`` holds dotted capability paths; a missing or
    falsy path skips the stage with a recorded reason (fail closed).
    """

    name: str
    handler: str
    requires: tuple[str, ...] = ()
    manages_progress: bool = False


@dataclass(frozen=True)
class InvestigationPlan:
    """An explicit, validated stage plan for one investigation."""

    plan_id: str
    mode: str
    stages: tuple[StageDefinition, ...]
    routing: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stages:
            raise OrchestrationError(f"Plan {self.plan_id!r} has no stages.")
        names = [stage.name for stage in self.stages]
        if len(set(names)) != len(names):
            raise OrchestrationError(f"Plan {self.plan_id!r} repeats a stage name.")
        known = set(HANDLERS) | set(HANDLER_PATHS)
        unknown = [s.handler for s in self.stages if s.handler not in known]
        if unknown:
            raise OrchestrationError(
                f"Plan {self.plan_id!r} references unknown handlers: {unknown}."
            )


@dataclass(frozen=True)
class OrchestrationResult:
    """Honest overall outcome. ``complete`` requires every stage ``ok``;
    ``report`` keeps the mode's existing per-mode shape (no unification)."""

    status: str  # "complete" | "failed" | "cancelled"
    stages: list[StageResult] = field(default_factory=list)
    report: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class HandlerDeps:
    """Test seams threaded to handlers; production leaves them empty."""

    backend: Any = None
    diagnose: Any = None
    summarize: Any = None


@dataclass
class RunScope:
    """Mutable per-run state shared by a plan's handlers.

    ``workspace`` is the disposable investigation directory (as a plain
    string path), created by the materialize stage and removed by
    :meth:`close`. ``outputs`` carries prior stages' intermediate data
    by stage name; handlers read what they need explicitly instead of
    reaching into global state. ``budget_override`` pins the wall-clock
    budget for callers (legacy wrappers, tests) that pass one;
    ``budget`` holds the live budget object once created.
    """

    workspace: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    budget_override: float | None = None
    budget: Any = None

    def close(self) -> None:
        workspace, self.workspace = self.workspace, None
        if workspace:
            from .github_ingest import remove_workspace

            try:
                remove_workspace(workspace)
            except OSError:
                pass


HandlerFn = Callable[
    [InvestigationContext, ProgressRecorder, HandlerDeps, RunScope], StageResult
]


class NullRecorder:
    """No-op recorder for sync runs and unit tests."""

    def set_stages(self, stages: tuple[str, ...] | list[str]) -> None:
        _ = stages

    def stage_started(self, stage: str) -> None:
        _ = stage

    def stage_completed(
        self, stage: str, data: dict[str, Any] | None = None
    ) -> None:
        _ = (stage, data)

    def record_run(
        self,
        kind: str,
        result: dict[str, Any],
        attempt_id: UUID | None = None,
    ) -> UUID | None:
        _ = (kind, result, attempt_id)
        return None

    def record_attempt(self, attempt_no: int, **kwargs: Any) -> UUID | None:
        _ = (attempt_no, kwargs)
        return None

    def record_finding(self, **kwargs: Any) -> UUID | None:
        _ = kwargs
        return None

    def record_patch(self, **kwargs: Any) -> UUID | None:
        _ = kwargs
        return None

    def record_message(self, *, role: str, content: str) -> UUID | None:
        _ = (role, content)
        return None

    def record_partial(self, data: dict[str, Any]) -> None:
        _ = data

    def record_routing(self, selected_mode: str, routing: dict[str, Any]) -> None:
        _ = (selected_mode, routing)

    def note_progress(self, detail: str) -> None:
        _ = detail

    def check_cancelled(self) -> None:
        return None


# --- handler registry ---

# Directly registered handlers (test doubles; production handlers resolve
# lazily via HANDLER_PATHS so mode modules can import these contracts
# without an import cycle).
HANDLERS: dict[str, HandlerFn] = {}

# Production handler locations as (module, attribute); resolved on first
# use and cached in HANDLERS.
HANDLER_PATHS: dict[str, tuple[str, str]] = {
    "debug_materialize": (".real_investigation", "stage_debug_materialize"),
    "debug_baseline": (".real_investigation", "stage_debug_baseline"),
    "debug_repair": (".real_investigation", "stage_debug_repair"),
    "static_evidence": (".real_investigation", "stage_static_evidence"),
    "static_diagnose": (".real_investigation", "stage_static_diagnose"),
    "explain_context": (".explain", "stage_explain_context"),
    "explain_summarize": (".explain", "stage_explain_summarize"),
    "health_checks": (".health", "stage_health_checks"),
    "general_route": (".general", "stage_general_route"),
    "health_summarize": (".health", "stage_health_summarize"),
}


def _resolve_handler(name: str) -> HandlerFn | None:
    handler = HANDLERS.get(name)
    if handler is not None:
        return handler
    location = HANDLER_PATHS.get(name)
    if location is None:
        return None
    module_name, attr = location
    try:
        module = importlib.import_module(module_name, package=__package__)
        handler = getattr(module, attr, None)
    except ImportError:
        return None
    if not callable(handler):
        return None
    HANDLERS[name] = handler
    return handler


def _fine_plan(
    plan_id: str, mode: str, stages: tuple[tuple[str, str], ...]
) -> InvestigationPlan:
    return InvestigationPlan(
        plan_id=plan_id,
        mode=mode,
        stages=tuple(
            StageDefinition(name=name, handler=handler) for name, handler in stages
        ),
        routing={"plan_id": plan_id},
    )


PLANS: dict[str, InvestigationPlan] = {
    "debug": _fine_plan(
        "debug", "debug",
        (
            ("materialize", "debug_materialize"),
            ("baseline", "debug_baseline"),
            ("repair", "debug_repair"),
        ),
    ),
    "debug-static": _fine_plan(
        "debug-static", "debug",
        (
            ("evidence", "static_evidence"),
            ("diagnose", "static_diagnose"),
        ),
    ),
    "explain": _fine_plan(
        "explain", "explain",
        (
            ("context", "explain_context"),
            ("summarize", "explain_summarize"),
        ),
    ),
    "health": _fine_plan(
        "health", "health",
        (
            ("checks", "health_checks"),
            ("summarize", "health_summarize"),
        ),
    ),
    "general": _fine_plan(
        "general", "general",
        (
            ("route", "general_route"),
        ),
    ),
}


def plan_for_mode(mode: str, capabilities: dict[str, Any]) -> InvestigationPlan:
    """Resolve the executable plan for ``mode`` given ``capabilities``.

    Debug resolves to ``debug`` vs ``debug-static`` by the same rule the
    pipeline gate uses (supported test framework present). General
    resolves to the routing plan, which delegates to a strategy plan.
    """
    if mode == "general":
        return PLANS["general"]
    if mode not in ("debug", "explain", "health"):
        raise UnknownPlanError(f"No plan for mode {mode!r}.")
    runnable = capabilities.get("runnable", {}) or {}
    if mode == "debug":
        if runnable.get("reason") == "unsupported_project":
            raise UnsupportedProjectError(
                "Capabilities admit no investigation plan (unsupported project)."
            )
        frameworks = capabilities.get("test_frameworks", []) or []
        if not frameworks:
            return PLANS["debug-static"]
        return PLANS["debug"]
    return PLANS[mode]


# --- context builders ---


def _validate_message(message: str | None) -> str | None:
    text = (message or "").strip() or None
    if text is not None:
        text = text[:MAX_CONTEXT_MESSAGE_CHARS]
    return text


def _validate_excerpts(excerpts: Any) -> dict[str, str]:
    if not isinstance(excerpts, dict):
        return {}
    capped: dict[str, str] = {}
    for path in sorted(excerpts):
        if len(capped) >= MAX_CONTEXT_EXCERPT_FILES:
            break
        if not isinstance(path, str) or not path or path.startswith("/"):
            continue
        if ".." in path.split("/"):
            continue
        text = excerpts[path]
        if not isinstance(text, str):
            continue
        capped[path] = text[:MAX_CONTEXT_EXCERPT_CHARS]
    return capped


def _validate_file_lists(context: Any) -> dict[str, list[str]]:
    if not isinstance(context, dict):
        return {}
    lists: dict[str, list[str]] = {}
    for key in CONTEXT_LIST_KEYS:
        items = context.get(key, [])
        if not isinstance(items, list):
            continue
        kept = [item for item in items if isinstance(item, str)]
        if kept:
            lists[key] = kept
    return lists


def build_context_from_record(
    record: dict[str, Any],
    *,
    ingestion_id: str,
    investigation_id: UUID | None = None,
    owner_id: UUID,
    mode: str = "debug",
    message: str | None = None,
    max_attempts: int = 3,
    plan_id: str | None = None,
    preselected_mode: str | None = None,
    preselect_rationale: str | None = None,
    preselect_confidence: str | None = None,
    preselect_source: str | None = None,
) -> InvestigationContext:
    """Build validated context from a live ingestion record (sync path).

    Ownership is verified against the record's owner tag; the commit SHA
    comes from the record's own validated profile.
    """
    from .ingestion import get_ingestion_owner

    if not isinstance(record, dict) or record.get("status") != "complete":
        raise ContextError("unknown_record", "Ingestion record is not available.")
    if record.get("ingestion_id") != ingestion_id:
        raise ContextError(
            "record_mismatch",
            "Ingestion record does not match the requested ingestion ID.",
        )
    if get_ingestion_owner(ingestion_id) != str(owner_id):
        raise ContextError("not_owner", "Ingestion record is not owned by this user.")
    profile = record.get("profile")
    if not isinstance(profile, dict):
        raise ContextError("bad_profile", "Ingestion record has no profile.")
    return assemble_context(
        profile=profile,
        excerpts=(record.get("context", {}) or {}).get("excerpts", {}),
        file_lists=record.get("context", {}) or {},
        investigation_id=investigation_id or uuid.uuid4(),
        owner_id=owner_id,
        mode=mode,
        message=message,
        max_attempts=max_attempts,
        plan_id=plan_id,
        preselected_mode=preselected_mode,
        preselect_rationale=preselect_rationale,
        preselect_confidence=preselect_confidence,
        preselect_source=preselect_source,
    )


def build_context_for_worker(
    *,
    repo_url: str,
    commit_sha: str,
    investigation_id: UUID,
    owner_id: UUID,
    mode: str = "debug",
    message: str | None = None,
    max_attempts: int = 3,
    plan_id: str | None = None,
    ingest_fn: Callable[..., dict[str, Any]] | None = None,
    on_phase: Callable[[str], None] | None = None,
    preselected_mode: str | None = None,
    preselect_rationale: str | None = None,
    preselect_confidence: str | None = None,
    preselect_source: str | None = None,
) -> InvestigationContext:
    """Rebuild context for a background job without the global store.

    Re-ingests from the persisted pinned SHA (single validation path via
    :func:`build_context_from_record` semantics), then verifies the
    resulting SHA byte-identically: a different commit is never silently
    substituted. Ownership comes from the persisted investigation row
    (server-side, trusted); the fresh record carries no owner tag.
    """
    from .ingestion import ingest_repository

    if not _SHA_RE.match(commit_sha or ""):
        raise ContextError("bad_sha", "Persisted commit SHA is invalid.")
    ingest = ingest_fn or ingest_repository
    if on_phase is not None and ingest is ingest_repository:
        record = ingest(repo_url, ref=commit_sha, on_phase=on_phase)
    else:
        record = ingest(repo_url, ref=commit_sha)
    if not isinstance(record, dict) or record.get("status") != "complete":
        raise ContextError(
            str(record.get("reason", "ingest_failed")) if isinstance(record, dict) else "ingest_failed",
            str(record.get("message", "Repository re-ingestion failed."))
            if isinstance(record, dict)
            else "Repository re-ingestion failed.",
        )
    profile = record.get("profile")
    if not isinstance(profile, dict) or profile.get("commit_sha") != commit_sha:
        raise ContextError(
            "sha_mismatch",
            "Re-ingested commit does not match the pinned SHA; refusing to continue.",
        )
    return assemble_context(
        profile=profile,
        excerpts=(record.get("context", {}) or {}).get("excerpts", {}),
        file_lists=record.get("context", {}) or {},
        investigation_id=investigation_id,
        owner_id=owner_id,
        mode=mode,
        message=message,
        max_attempts=max_attempts,
        plan_id=plan_id,
        preselected_mode=preselected_mode,
        preselect_rationale=preselect_rationale,
        preselect_confidence=preselect_confidence,
        preselect_source=preselect_source,
    )


def assemble_context(
    *,
    profile: dict[str, Any],
    excerpts: Any,
    investigation_id: UUID,
    owner_id: UUID,
    mode: str,
    message: str | None,
    max_attempts: int,
    plan_id: str | None,
    file_lists: Any = None,
    preselected_mode: str | None = None,
    preselect_rationale: str | None = None,
    preselect_confidence: str | None = None,
    preselect_source: str | None = None,
) -> InvestigationContext:
    """Validate parts into an :class:`InvestigationContext`.

    Shared by the ownership-verifying builders above and the legacy
    pipeline wrappers, whose callers authorized the record before
    invoking them; it performs no ownership check itself. Pre-selected
    routes (from ``POST /api/general/classify``) are validated here:
    only a genuine strategy mode is honored downstream.
    """
    if mode not in MODES:
        raise ContextError("bad_mode", f"Unknown investigation mode {mode!r}.")
    capabilities = profile.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ContextError("bad_profile", "Profile has no capabilities.")
    commit_sha = profile.get("commit_sha", "")
    if not _SHA_RE.match(commit_sha or ""):
        raise ContextError("bad_sha", "Profile commit SHA is invalid.")
    resolved = plan_id or _resolve_plan_id(mode, capabilities)
    if resolved not in PLANS:
        raise ContextError("bad_plan", f"Unknown plan {resolved!r}.")
    try:
        attempts = int(max_attempts or 3)
    except (TypeError, ValueError):
        raise ContextError("bad_attempts", "max_attempts must be an integer.")
    # Trust boundary for `ref`: user-supplied refs are validated by
    # github_ingest.validate_ref at ingestion AND clone time (charset,
    # length, traversal/flag rejection). The orchestrated path checks
    # out by validated 40-hex SHA only and never consumes `ref` for
    # repository resolution, so the builder carries it as display-only
    # metadata verbatim.
    honored = preselected_mode if preselected_mode in ("debug", "explain", "health") else None
    return InvestigationContext(
        investigation_id=investigation_id,
        owner_id=owner_id,
        repo_url=str(profile.get("url", "")),
        repo_name=str(profile.get("name", "")),
        commit_sha=commit_sha,
        ref=profile.get("ref"),
        mode=mode,
        plan_id=resolved,
        message=_validate_message(message),
        max_attempts=max(1, min(attempts, 3)),
        profile=profile,
        capabilities=capabilities,
        excerpts=_validate_excerpts(excerpts),
        file_lists=_validate_file_lists(file_lists),
        preselected_mode=honored,
        preselect_rationale=(
            str(preselect_rationale)[:2000] if honored and preselect_rationale else None
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


def _resolve_plan_id(mode: str, capabilities: dict[str, Any]) -> str:
    try:
        return plan_for_mode(mode, capabilities).plan_id
    except UnknownPlanError as exc:
        raise ContextError("bad_plan", str(exc))
    except UnsupportedProjectError as exc:
        raise ContextError("unsupported_project", str(exc))


# --- stage runner ---


def _capability_holds(capabilities: dict[str, Any], path: str) -> bool:
    current: Any = capabilities
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return bool(current)


def run(
    plan: InvestigationPlan,
    ctx: InvestigationContext,
    *,
    progress: ProgressRecorder,
    deps: HandlerDeps | None = None,
    time_budget_seconds: float | None = None,
) -> OrchestrationResult:
    """Execute ``plan`` stage by stage; return an honest overall outcome.

    The runner programs recorder boundaries for stages that do not
    manage their own progress, shares one :class:`RunScope` across the
    plan's handlers, and closes it (removing the workspace) no matter
    how the run ends. Cancellation is checked between stages; a stage
    interrupted mid-flight is ``cancelled`` while stages that never
    began are ``skipped`` (neither is completed). The first failure
    wins; handler exceptions convert to ``failed`` results and only
    ``JobCancelled`` propagates as cancellation.
    """
    resolved_deps = deps or HandlerDeps()
    scope = RunScope(budget_override=time_budget_seconds)
    try:
        return _run_inner(plan, ctx, progress=progress, deps=resolved_deps, scope=scope)
    finally:
        scope.close()


def _run_inner(
    plan: InvestigationPlan,
    ctx: InvestigationContext,
    *,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> OrchestrationResult:
    results: list[StageResult] = []
    if not any(stage.manages_progress for stage in plan.stages):
        try:
            progress.set_stages([stage.name for stage in plan.stages])
        except Exception as exc:
            return OrchestrationResult(
                status="failed",
                stages=results,
                error={"reason": "recorder_error", "message": str(exc)[:500]},
            )
    for index, stage in enumerate(plan.stages):
        try:
            progress.check_cancelled()
        except JobCancelled:
            results.extend(_skipped(plan.stages[index:]))
            return OrchestrationResult(status="cancelled", stages=results)
        unmet = [p for p in stage.requires if not _capability_holds(ctx.capabilities, p)]
        if unmet:
            results.append(
                StageResult(
                    stage=stage.name,
                    status="skipped",
                    detail=f"Missing capabilities: {', '.join(sorted(unmet))}.",
                )
            )
            continue
        handler = _resolve_handler(stage.handler)
        if handler is None:  # defensive; plan validation already rejects this
            results.append(
                StageResult(
                    stage=stage.name, status="failed",
                    detail=f"Unknown handler {stage.handler!r}.",
                )
            )
            results.extend(_skipped(plan.stages[index + 1 :]))
            return _failed(results)
        if not stage.manages_progress:
            try:
                progress.stage_started(stage.name)
            except JobCancelled:
                results.extend(_skipped(plan.stages[index:]))
                return OrchestrationResult(status="cancelled", stages=results)
            except Exception as exc:
                results.append(_recorder_failed(stage.name, exc))
                results.extend(_skipped(plan.stages[index + 1 :]))
                return _failed(results)
        started = time.perf_counter()
        try:
            outcome = handler(ctx, progress, deps, scope)
        except JobCancelled:
            results.append(
                StageResult(
                    stage=stage.name,
                    status="cancelled",
                    detail="Cancelled during execution.",
                    elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
                )
            )
            results.extend(_skipped(plan.stages[index + 1 :]))
            return OrchestrationResult(status="cancelled", stages=results)
        except Exception as exc:
            outcome = StageResult(
                stage=stage.name,
                status="failed",
                detail=f"{type(exc).__name__}: {exc}"[:2000],
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        if not isinstance(outcome, StageResult):
            outcome = StageResult(
                stage=stage.name,
                status="failed",
                detail=f"Handler {stage.handler!r} returned no StageResult.",
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        elif outcome.elapsed_ms == 0:
            outcome = replace(
                outcome,
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        results.append(outcome)
        if outcome.status == "failed":
            # A failed stage is never marked completed in persisted
            # progress; its data records stay, the boundary does not.
            results.extend(_skipped(plan.stages[index + 1 :]))
            return _failed(results)
        if not stage.manages_progress:
            try:
                progress.stage_completed(stage.name)
            except JobCancelled:
                results.extend(_skipped(plan.stages[index + 1 :]))
                return OrchestrationResult(status="cancelled", stages=results)
            except Exception as exc:
                results.append(_recorder_failed(stage.name, exc))
                results.extend(_skipped(plan.stages[index + 1 :]))
                return _failed(results)
    report = None
    for outcome in reversed(results):
        if isinstance(outcome, StageResult) and outcome.output is not None:
            report = outcome.output
            break
    return OrchestrationResult(status="complete", stages=results, report=report)


def _skipped(stages: Any) -> list[StageResult]:
    return [
        StageResult(stage=stage.name, status="skipped", detail="Not run.")
        for stage in stages
    ]


def _recorder_failed(stage: str, exc: Exception) -> StageResult:
    return StageResult(
        stage=stage, status="failed",
        detail=f"Recorder error: {type(exc).__name__}: {exc}"[:2000],
    )


def _failed(results: list[StageResult]) -> OrchestrationResult:
    first = next((r for r in results if r.status == "failed"), None)
    reason = (first.reason if first and first.reason else "stage_failed")
    message = (first.detail if first and first.detail else "Stage failed.")
    return OrchestrationResult(
        status="failed",
        stages=results,
        error={"reason": reason, "message": message},
    )


def execute(
    ctx: InvestigationContext,
    *,
    progress: ProgressRecorder,
    deps: HandlerDeps | None = None,
    time_budget_seconds: float | None = None,
) -> OrchestrationResult:
    """Run the investigation for ``ctx`` through its resolved plan.

    The single shared entry point for synchronous and worker execution:
    looks up ``ctx.plan_id`` and runs it. Failing handlers attach their
    exact legacy error dict as ``output["legacy_error"]`` so callers can
    preserve existing response shapes; ``report`` keeps the mode's
    existing per-mode shape on success.
    """
    plan = PLANS.get(ctx.plan_id)
    if plan is None:
        return OrchestrationResult(
            status="failed",
            stages=[],
            error={
                "reason": "bad_plan",
                "message": f"Unknown plan {ctx.plan_id!r}.",
            },
        )
    return run(
        plan, ctx, progress=progress, deps=deps,
        time_budget_seconds=time_budget_seconds,
    )


def legacy_error_of(result: OrchestrationResult) -> dict[str, Any] | None:
    """Return the first failed stage's legacy error dict, if attached."""
    for stage in result.stages:
        if stage.status != "failed" or not isinstance(stage.output, dict):
            continue
        legacy = stage.output.get("legacy_error")
        if isinstance(legacy, dict):
            return legacy
    return None
