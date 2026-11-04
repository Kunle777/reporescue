from __future__ import annotations

"""Real-repository investigation pipeline (Phase 3).

Pipeline::

    ingestion record -> backend select -> materialize pinned SHA
        -> install deps -> baseline tests -> select evidence
        -> Meta diagnosis + patch set -> guard -> apply in work copy
        -> independent verification -> evidence-backed report

The existing fixture workflow in ``orchestrator.py`` is preserved unchanged;
this module handles real ingested repositories only. No diagnosis or patch
is ever hardcoded: every proposal comes from the live Meta client (or a
caller-supplied stand-in in tests), and every proposal is guard-validated
and verified by independent test execution before any success is reported.
"""

import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config
from . import meta_client
from .execution import (
    ExecutionBackend,
    ExecutionRequest,
    detect_harness_incompatibility,
    plan_install,
    requirements_pin_pytest,
    select_backend,
    traceback_locations,
)
from .github_ingest import IngestionError, clone_and_pin
from .ingestion import get_ingestion
from .orchestrate import (
    ContextError,
    HandlerDeps,
    InvestigationContext,
    NullRecorder,
    RunScope,
    StageResult,
    assemble_context,
    execute,
    legacy_error_of,
)
from .patch_guard import PatchRejected, PatchSet, validate_and_apply_patchset
from .recorder import JobCancelled, ProgressRecorder
from .repo_context import _is_probably_binary
from .verdict import build_real_report

MODE = "real-repo-meta"
MAX_LIVE_ATTEMPTS = 3
MAX_EVIDENCE_FILES = 12
MAX_EVIDENCE_FILE_CHARS = 4000
EVIDENCE_WINDOW_LINES = 80
VERIFICATION_FEEDBACK_CHARS = 1500

SUPPORTED_FRAMEWORKS = {
    "pytest": ["python", "-m", "pytest", "-q", "--tb=short"],
    "unittest": ["python", "-m", "unittest", "discover"],
}

_monotonic = time.monotonic


class _Budget:
    """Monotonic wall-clock budget for one investigation run.

    Checked before each expensive stage; an in-flight step always runs
    to its own per-operation timeout, so overrun is bounded by one step.
    """

    def __init__(self, seconds: float) -> None:
        self.start = _monotonic()
        self.deadline = self.start + max(0.0, seconds)
        self.seconds = seconds

    def exhausted(self) -> bool:
        return _monotonic() >= self.deadline

    def elapsed_ms(self) -> int:
        return max(0, round((_monotonic() - self.start) * 1000))


def _error(
    message: str,
    reason: str,
    profile: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "error", "reason": reason, "message": message}
    if profile is not None:
        result["profile"] = {
            "url": profile.get("url"),
            "name": profile.get("name"),
            "commit_sha": profile.get("commit_sha"),
        }
    result.update(extra)
    return result


def _node_id_to_file(node_id: str) -> str | None:
    head = node_id.split("::")[0].replace("\\", "/")
    if head.endswith(".py") and ".." not in head.split("/"):
        return head
    return None


def _read_evidence_window(target: Path, lineno: int) -> str | None:
    """Read a bounded window around ``lineno`` (1-based), or file head."""
    try:
        if not target.is_file() or target.is_symlink():
            return None
        if target.stat().st_size > 1_000_000:
            return None
    except OSError:
        return None
    if _is_probably_binary(target):
        return None
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return None
    if lineno <= 0 or lineno > len(lines):
        window = lines[: EVIDENCE_WINDOW_LINES * 2]
    else:
        start = max(0, lineno - EVIDENCE_WINDOW_LINES - 1)
        window = lines[start : lineno + EVIDENCE_WINDOW_LINES]
    return "\n".join(window)[:MAX_EVIDENCE_FILE_CHARS]


def select_evidence(
    workspace: Path,
    stored_excerpts: dict[str, str],
    failures: list[dict[str, str]],
    output: str,
) -> dict[str, str]:
    """Select bounded source evidence for AI diagnosis.

    Candidates are failing test files (to show expected behavior) and
    repository sources named in tracebacks. Traceback frames re-read a
    bounded window around the failing line so large files still show
    the relevant region; anything else prefers stored Phase 2 excerpts.
    Secrets, binaries, and oversized files are never included.
    """
    locations = traceback_locations(output)
    line_by_file = {path: lineno for path, lineno in locations}
    candidates: list[str] = []
    for failure in failures:
        path = _node_id_to_file(failure.get("test", ""))
        if path and path not in candidates:
            candidates.append(path)
    for path, _ in locations:
        if path not in candidates:
            candidates.append(path)
    evidence: dict[str, str] = {}
    for rel in candidates[:MAX_EVIDENCE_FILES]:
        if rel not in line_by_file and rel in stored_excerpts:
            evidence[rel] = stored_excerpts[rel][:MAX_EVIDENCE_FILE_CHARS]
            continue
        text = _read_evidence_window(workspace / rel, line_by_file.get(rel, 0))
        if text is not None:
            evidence[rel] = text
    return evidence


def _materialize(url: str, sha: str, destination: Path) -> None:
    """Clone the repository and check out the exact pinned SHA."""
    if not isinstance(sha, str) or len(sha) != 40:
        raise IngestionError("Stored commit SHA is invalid; refusing to materialize.", "clone_failed")
    try:
        pinned = clone_and_pin(url, destination, sha)
    except IngestionError:
        raise
    if pinned != sha:
        raise IngestionError(
            "Materialized revision does not match the pinned SHA; refusing to continue.",
            "clone_failed",
        )


def _skip_symlinks(directory: str, names: list[str]) -> list[str]:
    return [name for name in names if (Path(directory) / name).is_symlink()]


def _fresh_work_copy(pristine: Path, work: Path) -> Path:
    """Copy pristine to a work directory, dropping symlinks.

    Symlinks are never carried into patch/verify workspaces: following one
    could read outside the snapshot, and the patch guard refuses symlink
    targets anyway.
    """
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(pristine, work, symlinks=False, ignore=_skip_symlinks)
    return work


def _failed_result(stage: str, legacy: dict[str, Any]) -> StageResult:
    """Wrap an exact legacy error dict as a failed stage outcome."""
    return StageResult(
        stage=stage,
        status="failed",
        reason=str(legacy.get("reason", "stage_failed")),
        detail=str(legacy.get("message", "Stage failed."))[:2000],
        output={"legacy_error": legacy},
    )


def _expired_before(
    scope: RunScope,
    stage_desc: str,
    profile: dict[str, Any],
    execution_info: dict[str, str],
    **extra: Any,
) -> dict[str, Any] | None:
    """Return a budget-exhausted legacy error when the deadline passed."""
    budget = scope.budget
    if budget.exhausted():
        return _error(
            f"Investigation wall-clock budget ({budget.seconds:g}s) "
            f"exhausted before {stage_desc}; no further stages were started.",
            "budget_exhausted",
            profile,
            execution=execution_info,
            timings={"total_ms": budget.elapsed_ms()},
            **extra,
        )
    return None


def _green_report(
    profile: dict[str, Any],
    baseline: dict[str, Any],
    execution_info: dict[str, str],
    materialize_ms: int,
    total_ms: int,
) -> dict[str, Any]:
    """Report a baseline that already passes: nothing to repair."""
    return {
        "status": "complete",
        "repository": profile.get("name"),
        "commit_sha": profile.get("commit_sha"),
        "mode": MODE,
        "summary": "Tests already pass at the pinned revision; nothing to repair.",
        "verdict": "INCONCLUSIVE",
        "diagnosis": "",
        "diagnosis_confidence": "hypothesis",
        "baseline": baseline,
        "verification": baseline,
        "patch": {"edits": [], "reason": ""},
        "guard": {"validated": False, "message": "No patch proposed; baseline is green."},
        "execution": execution_info,
        "attempts": 0,
        "limitations": ["No AI diagnosis was requested because the baseline passed."],
        "timings": {
            "materialize_ms": materialize_ms,
            "baseline_ms": baseline.get("duration_ms", 0),
            "work_copy_ms": 0,
            "attempts": [],
            "total_ms": total_ms,
        },
    }


def _static_report(
    profile: dict[str, Any],
    diagnosis: str,
    patchset: PatchSet,
    framework: str,
    total_ms: int,
) -> dict[str, Any]:
    """Report a static-only investigation: an unverified hypothesis."""
    unavailable = {
        "status": "unavailable",
        "reason": "no_supported_test_framework",
        "passed": None,
        "message": (
            f"No supported test framework is available ({framework!r}); "
            "nothing was executed, so this diagnosis is unverified."
        ),
    }
    report = build_real_report(
        repository=profile.get("name"),
        commit_sha=profile.get("commit_sha"),
        mode=MODE,
        diagnosis=diagnosis,
        baseline=dict(unavailable),
        verification=dict(unavailable),
        patch={**patchset.to_dict(), "applied": False},
        guard={
            "validated": False,
            "message": "Static investigation: the suggested patch was not applied or verified.",
        },
        execution={"backend": "static-only", "isolation": "no-execution"},
        attempts=1,
        extra_limitations=[
            "Static investigation only: no tests exist to confirm or refute this diagnosis.",
            "The suggested patch is an unverified hypothesis; it was not applied.",
        ],
    )
    report["summary"] = (
        "Static investigation only: no supported test framework exists, so no "
        "tests were run and the diagnosis is an unverified hypothesis."
    )
    report["timings"] = {
        "materialize_ms": 0,
        "baseline_ms": 0,
        "work_copy_ms": 0,
        "attempts": [],
        "total_ms": total_ms,
    }
    return report


# --- debug plan stages ---
#
# Handlers take (ctx, progress, deps, scope) and never touch the global
# ingestion store or recorder stage boundaries; the runner owns both.
# Failures wrap the exact legacy error dict so existing response shapes
# survive the migration unchanged.


def stage_debug_materialize(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Resolve the budget and backend, then clone the pinned SHA.

    Pre-clone checks run in legacy order (budget config, backend,
    dependency plan, diagnosis key) so invalid repositories fail fast
    without a pointless clone, exactly as before.
    """
    _ = progress
    profile = ctx.profile
    if scope.budget_override is None:
        try:
            budget_seconds = config.get_investigation_timeout_seconds()
        except config.ConfigError as exc:
            return _failed_result(
                "materialize", _error(str(exc), "config_error", profile)
            )
    else:
        budget_seconds = scope.budget_override
    scope.budget = _Budget(budget_seconds)
    backend = deps.backend
    if backend is None:
        backend, backend_error = select_backend()
        if backend is None or backend_error is not None:
            assert backend_error is not None
            backend_error["profile"] = {
                "url": profile.get("url"),
                "name": profile.get("name"),
                "commit_sha": profile.get("commit_sha"),
            }
            return _failed_result("materialize", backend_error)
    execution_info = {"backend": backend.name, "isolation": backend.isolation}
    planned = plan_install(profile)
    if planned["error"]:
        return _failed_result(
            "materialize",
            _error(planned["error"], "unsupported_dependencies", profile),
        )
    diagnose = deps.diagnose
    if diagnose is None:
        try:
            api_key = meta_client.get_api_key()
        except meta_client.MetaConfigError as exc:
            return _failed_result(
                "materialize", _error(str(exc), "config_error", profile)
            )

        def diagnose(**kwargs):
            return meta_client.diagnose_and_propose_from_evidence(api_key=api_key, **kwargs)

    expired = _expired_before(
        scope, "materializing the repository", profile, execution_info, attempts=0
    )
    if expired is not None:
        return _failed_result("materialize", expired)
    try:
        workspace = Path(tempfile.mkdtemp(prefix="reporescue-invest-"))
    except OSError as exc:
        return _failed_result(
            "materialize",
            _error(
                f"Could not prepare an investigation workspace: {exc}.",
                "workspace_error",
                profile,
                execution=execution_info,
            ),
        )
    scope.workspace = str(workspace)
    pristine = workspace / "pristine"
    mat_start = _monotonic()
    try:
        _materialize(profile["url"], profile["commit_sha"], pristine)
    except IngestionError as exc:
        return _failed_result(
            "materialize",
            _error(str(exc), exc.reason, profile, execution=execution_info),
        )
    materialize_ms = max(0, round((_monotonic() - mat_start) * 1000))
    scope.outputs["materialize"] = {
        "pristine": str(pristine),
        "materialize_ms": materialize_ms,
        "backend": backend,
        "install_commands": [list(cmd) for cmd in planned["commands"]],
        "diagnose": diagnose,
    }
    return StageResult(stage="materialize", status="ok")


def _cancel_callback(progress: ProgressRecorder) -> Callable[[], bool]:
    """Adapt a progress recorder to a backend cancellation probe.

    Returns True once the job is cancelled or the worker is stopping.
    Recorder failures never cancel a run: a broken probe must not
    trap (or kill) an investigation.
    """

    def _probe() -> bool:
        check = getattr(progress, "check_cancelled", None)
        if not callable(check):
            return False
        try:
            check()
        except JobCancelled:
            return True
        except Exception:
            return False
        return False

    return _probe


def stage_debug_baseline(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Install dependencies and run the baseline tests in isolation."""
    _ = deps
    profile = ctx.profile
    prepared = scope.outputs["materialize"]
    backend = prepared["backend"]
    pristine = Path(prepared["pristine"])
    execution_info = {"backend": backend.name, "isolation": backend.isolation}
    install_commands = [list(cmd) for cmd in prepared["install_commands"]]
    if install_commands == [["pip", "install", "pytest"]] and not backend.needs_pytest_install():
        # No dependency install is needed at all: run with the backend's
        # pre-existing pytest instead of creating a venv for nothing.
        install_commands = []
    if requirements_pin_pytest(pristine, profile.get("dependency_files", [])):
        # The repository declares its own pytest version: installing
        # the plan's bare `pip install pytest` afterwards would
        # override that declaration with the latest release.
        install_commands = [
            cmd for cmd in install_commands if cmd != ["pip", "install", "pytest"]
        ]
    framework = profile.get("test_framework", "none")
    request = ExecutionRequest(
        snapshot=pristine,
        test_command=list(SUPPORTED_FRAMEWORKS[framework]),
        install_commands=install_commands,
        language="python",
        commit_sha=profile.get("commit_sha"),
        cancel=_cancel_callback(progress),
        framework=framework,
        python_requires=profile.get("python_requires"),
    )
    expired = _expired_before(
        scope, "baseline test execution", profile, execution_info, attempts=0
    )
    if expired is not None:
        return _failed_result("baseline", expired)
    baseline_result = backend.run(request)
    if baseline_result.status == "cancelled":
        raise JobCancelled("Baseline execution was cancelled.")
    baseline = baseline_result.to_dict()
    progress.record_run("baseline", baseline)
    progress.record_partial(
        {
            "baseline_passed": baseline_result.passed,
            "baseline_failures": len(baseline_result.failures),
        }
    )
    if baseline_result.status == "install_error":
        return _failed_result(
            "baseline",
            _error(
                "Dependency installation failed inside the execution "
                f"environment: {baseline['install_log'][-500:]}",
                "dependency_error",
                profile,
                execution=execution_info,
                baseline=baseline,
            ),
        )
    if baseline_result.status == "timeout":
        return _failed_result(
            "baseline",
            _error(
                "Baseline test run timed out inside the execution environment.",
                "timeout",
                profile,
                execution=execution_info,
                baseline=baseline,
            ),
        )
    if baseline_result.status not in ("ok", "test_failure"):
        return _failed_result(
            "baseline",
            _error(
                f"Baseline execution failed ({baseline_result.status}): "
                f"{baseline['output'][-500:]}",
                "execution_error",
                profile,
                execution=execution_info,
                baseline=baseline,
            ),
        )
    if not baseline_result.passed and not baseline_result.failures:
        harness_note = detect_harness_incompatibility(baseline_result.output)
        if harness_note:
            return _failed_result(
                "baseline",
                _error(
                    f"Baseline test execution failed before any test ran: {harness_note}",
                    "harness_incompatible",
                    profile,
                    execution=execution_info,
                    baseline=baseline,
                ),
            )
    scope.outputs["baseline"] = {
        "baseline": baseline,
        "already_green": baseline_result.passed,
        "install_commands": [list(cmd) for cmd in install_commands],
        "framework": framework,
        # Live failure/output references for evidence selection and
        # diagnosis, exactly as the legacy pipeline passed them.
        "failures": baseline_result.failures,
        "output": baseline_result.output,
    }
    return StageResult(stage="baseline", status="ok")


def stage_debug_repair(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Run the diagnose -> guard -> apply -> verify attempt loop.

    The loop is one stage deliberately: attempts share feedback and
    evidence, and splitting them would slice the patch-safety and
    independent-verification boundary the plan exists to preserve.
    """
    _ = deps
    profile = ctx.profile
    prepared = scope.outputs["materialize"]
    base = scope.outputs["baseline"]
    backend = prepared["backend"]
    pristine = Path(prepared["pristine"])
    baseline = base["baseline"]
    framework = base["framework"]
    execution_info = {"backend": backend.name, "isolation": backend.isolation}
    diagnose = prepared["diagnose"]
    if base["already_green"]:
        return StageResult(
            stage="repair",
            status="ok",
            output=_green_report(
                profile,
                baseline,
                execution_info,
                prepared["materialize_ms"],
                scope.budget.elapsed_ms(),
            ),
        )
    stored_excerpts = dict(ctx.excerpts)
    evidence = select_evidence(
        pristine, stored_excerpts, base["failures"], base["output"]
    )
    feedback: str | None = None
    attempts = 0
    last_diagnosis = ""
    last_patch: PatchSet | None = None
    last_verification: dict[str, Any] | None = None
    last_harness_note: str | None = None
    work_copy_ms = 0
    attempt_timings: list[dict[str, int]] = []
    workspace = Path(scope.workspace or "")
    for attempt in range(1, ctx.max_attempts + 1):
        attempts = attempt
        expired = _expired_before(
            scope,
            f"diagnosis for attempt {attempt}",
            profile,
            execution_info,
            baseline=baseline,
            attempts=attempts - 1,
        )
        if expired is not None:
            return _failed_result("repair", expired)
        diag_start = _monotonic()
        try:
            diagnosis, patchset = diagnose(
                evidence_files=evidence,
                failures=base["failures"],
                baseline_tail=base["output"],
                feedback=feedback,
            )
        except meta_client.MetaRateLimitError as exc:
            return _failed_result(
                "repair",
                _error(
                    str(exc), "rate_limit", profile,
                    execution=execution_info, baseline=baseline, attempts=attempts,
                ),
            )
        except meta_client.MetaAPIError as exc:
            return _failed_result(
                "repair",
                _error(
                    str(exc), "api_error", profile,
                    execution=execution_info, baseline=baseline, attempts=attempts,
                ),
            )
        except meta_client.MetaResponseError as exc:
            feedback = f"Previous model output was invalid and discarded: {exc}"
            if attempt == ctx.max_attempts:
                return _failed_result(
                    "repair",
                    _error(
                        f"Model returned invalid output {ctx.max_attempts} times; "
                        f"last error: {exc}",
                        "invalid_output", profile,
                        execution=execution_info, baseline=baseline, attempts=attempts,
                    ),
                )
            continue
        diagnose_ms = max(0, round((_monotonic() - diag_start) * 1000))
        copy_start = _monotonic()
        try:
            work = _fresh_work_copy(pristine, workspace / f"attempt-{attempt}")
        except OSError as exc:
            return _failed_result(
                "repair",
                _error(
                    f"Could not prepare an investigation workspace: {exc}.",
                    "workspace_error",
                    profile,
                    execution=execution_info,
                ),
            )
        work_copy_ms += max(0, round((_monotonic() - copy_start) * 1000))
        try:
            applied = validate_and_apply_patchset(work, patchset)
        except PatchRejected as exc:
            feedback = (
                "Previous patch was rejected by the safety guard and not "
                f"applied: {exc}"
            )
            if attempt == ctx.max_attempts:
                return _failed_result(
                    "repair",
                    _error(
                        str(exc), "patch_rejected", profile,
                        execution=execution_info, baseline=baseline, attempts=attempts,
                    ),
                )
            continue
        verify_request = ExecutionRequest(
            snapshot=work,
            test_command=list(SUPPORTED_FRAMEWORKS[framework]),
            install_commands=(
            [list(cmd) for cmd in base.get("install_commands", [])]
            if getattr(backend, "requires_fresh_install", False)
            else []
        ),
        language="python",
        commit_sha=profile.get("commit_sha"),
        cancel=_cancel_callback(progress),
            framework=framework,
            python_requires=profile.get("python_requires"),
        )
        expired = _expired_before(
            scope,
            f"patch verification for attempt {attempt}",
            profile,
            execution_info,
            baseline=baseline,
            attempts=attempts,
        )
        if expired is not None:
            return _failed_result("repair", expired)
        verification_result = backend.run(verify_request)
        if verification_result.status == "cancelled":
            raise JobCancelled("Patch verification was cancelled.")
        verification = verification_result.to_dict()
        last_diagnosis, last_patch, last_verification = diagnosis, patchset, verification
        attempt_id = progress.record_attempt(
            attempt,
            diagnosis=diagnosis,
            confidence=(
                "confirmed" if verification_result.passed is True
                else "hypothesis"
            ),
            patch={**patchset.to_dict(), "applied": applied},
            guard={
                "validated": True,
                "message": f"Guard approved {len(applied)} edit(s).",
            },
            verification=verification,
            timings={
                "diagnose_ms": diagnose_ms,
                "verify_ms": verification.get("duration_ms", 0),
            },
        )
        progress.record_run("verification", verification, attempt_id)
        progress.record_partial({"verification_passed": verification_result.passed})
        # Only diagnose->verify cycles that ran end to end are timed;
        # retried attempts (invalid model output, rejected patches)
        # contribute to the attempts count but not to this list.
        attempt_timings.append(
            {
                "diagnose_ms": diagnose_ms,
                "verify_ms": verification.get("duration_ms", 0),
            }
        )
        if verification_result.passed is True:
            report = build_real_report(
                repository=profile.get("name"),
                commit_sha=profile.get("commit_sha"),
                mode=MODE,
                diagnosis=diagnosis,
                baseline=baseline,
                verification=verification,
                patch={**patchset.to_dict(), "applied": applied},
                guard={"validated": True, "message": f"Guard approved {len(applied)} edit(s)."},
                execution=execution_info,
                attempts=attempts,
            )
            report["timings"] = {
                "materialize_ms": prepared["materialize_ms"],
                "baseline_ms": baseline.get("duration_ms", 0),
                "work_copy_ms": work_copy_ms,
                "attempts": attempt_timings,
                "total_ms": scope.budget.elapsed_ms(),
            }
            return StageResult(stage="repair", status="ok", output=report)
        output_tail = str(verification.get("output", ""))[-VERIFICATION_FEEDBACK_CHARS:]
        feedback = (
            "Previous patch applied but tests still fail "
            f"(status {verification.get('status')}):\n{output_tail}"
        )
        harness_note = detect_harness_incompatibility(verification_result.output)
        if harness_note:
            last_harness_note = harness_note
            feedback += (
                "\nNote: the verification failure looks like a "
                f"{harness_note} Do not try to fix test-runner "
                "issues with source patches."
            )
        evidence = select_evidence(
            pristine,
            stored_excerpts,
            verification_result.failures,
            verification_result.output,
        ) or evidence
    if last_patch is None or last_verification is None:  # defensive
        return _failed_result(
            "repair",
            _error(
                "Investigation ended without a usable proposal.",
                "invalid_output", profile,
                execution=execution_info, baseline=baseline, attempts=attempts,
            ),
        )
    report = build_real_report(
        repository=profile.get("name"),
        commit_sha=profile.get("commit_sha"),
        mode=MODE,
        diagnosis=last_diagnosis,
        baseline=baseline,
        verification=last_verification,
        patch=last_patch.to_dict(),
        guard={"validated": True, "message": "Guard approved the applied patch."},
        execution=execution_info,
        attempts=attempts,
        extra_limitations=[last_harness_note] if last_harness_note else None,
    )
    report["timings"] = {
        "materialize_ms": prepared["materialize_ms"],
        "baseline_ms": baseline.get("duration_ms", 0),
        "work_copy_ms": work_copy_ms,
        "attempts": attempt_timings,
        "total_ms": scope.budget.elapsed_ms(),
    }
    return StageResult(stage="repair", status="ok", output=report)


# --- debug-static plan stages ---
#
# Genuinely static: no backend, no subprocess, no workspace. The context
# excerpts are the only repository data these handlers touch.


def stage_static_evidence(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Select the bounded stored excerpts for static diagnosis."""
    _ = deps
    started = _monotonic()
    stored_excerpts = dict(ctx.excerpts)
    evidence = {
        path: stored_excerpts[path][:MAX_EVIDENCE_FILE_CHARS]
        for path in sorted(stored_excerpts)[:MAX_EVIDENCE_FILES]
    }
    progress.record_partial({"evidence_files": len(evidence)})
    scope.outputs["evidence"] = {"evidence": evidence, "started": started}
    return StageResult(stage="evidence", status="ok")


def stage_static_diagnose(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Diagnose from stored evidence; the patch stays an unverified hypothesis."""
    profile = ctx.profile
    user_message = (ctx.message or "").strip() or None
    if user_message is not None:
        user_message = user_message[: meta_client.MAX_STATIC_MESSAGE_CHARS]
    diagnose = deps.diagnose
    if diagnose is None:
        try:
            api_key = meta_client.get_api_key()
        except meta_client.MetaConfigError as exc:
            return _failed_result(
                "diagnose", _error(str(exc), "config_error", profile)
            )

        def diagnose(**kwargs):
            return meta_client.diagnose_static_from_evidence(api_key=api_key, **kwargs)

    evidence = scope.outputs["evidence"]["evidence"]
    try:
        diagnosis, patchset = diagnose(
            evidence_files=evidence,
            user_message=user_message,
        )
    except meta_client.MetaRateLimitError as exc:
        return _failed_result("diagnose", _error(str(exc), "rate_limit", profile))
    except meta_client.MetaAPIError as exc:
        return _failed_result("diagnose", _error(str(exc), "api_error", profile))
    except meta_client.MetaResponseError as exc:
        return _failed_result("diagnose", _error(str(exc), "invalid_output", profile))

    progress.record_attempt(
        1,
        diagnosis=diagnosis,
        confidence="hypothesis",
        patch={**patchset.to_dict(), "applied": False},
        guard={
            "validated": False,
            "message": "Static investigation: the suggested patch was not applied or verified.",
        },
    )
    framework = profile.get("test_framework", "none")
    total_ms = max(
        0, round((_monotonic() - scope.outputs["evidence"]["started"]) * 1000)
    )
    return StageResult(
        stage="diagnose",
        status="ok",
        output=_static_report(profile, diagnosis, patchset, framework, total_ms),
    )


def investigate_repository(
    ingestion_id: str,
    *,
    backend: ExecutionBackend | None = None,
    diagnose=None,
    max_attempts: int = MAX_LIVE_ATTEMPTS,
    time_budget_seconds: float | None = None,
    message: str | None = None,
    progress: ProgressRecorder | None = None,
) -> dict[str, Any]:
    """Run a real investigation for a stored ingestion record.

    Legacy entry point: validates the stored record (as before), then
    delegates to the shared orchestration engine and unwraps the result
    into the exact historical response shape. ``backend`` and
    ``diagnose`` are seams for tests; production passes neither, using
    Docker selection and the live Meta client. ``time_budget_seconds``
    caps total wall-clock time (default: configured investigation
    timeout). ``message`` guides the static plan when no test framework
    can run. ``progress`` is an optional write-through recorder (None
    preserves exact legacy behavior). Cancellation still propagates as
    ``JobCancelled``.
    """
    record = get_ingestion(ingestion_id)
    if record.get("status") != "complete":
        return _error(
            record.get("message", "Unknown ingestion record."),
            record.get("reason", "not_found"),
        )
    profile = record["profile"]
    context = record["context"]
    if not profile.get("supported"):
        return _error(
            f"Repository {profile.get('name')} is not a supported Python project.",
            "unsupported_project",
            profile,
        )
    try:
        owner_id = uuid.UUID(str(record.get("owner_id") or ""))
    except (TypeError, ValueError):
        # Untagged legacy/test records carry no owner; the caller
        # authorized this invocation before reaching the wrapper.
        owner_id = uuid.uuid4()
    try:
        ctx = assemble_context(
            profile=profile,
            excerpts=(context or {}).get("excerpts", {}),
            file_lists=context or {},
            investigation_id=uuid.uuid4(),
            owner_id=owner_id,
            mode="debug",
            message=message,
            max_attempts=max_attempts,
            plan_id=None,
        )
    except ContextError as exc:
        return _error(str(exc), exc.reason, profile)
    result = execute(
        ctx,
        progress=progress if progress is not None else NullRecorder(),
        deps=HandlerDeps(backend=backend, diagnose=diagnose),
        time_budget_seconds=time_budget_seconds,
    )
    if result.status == "complete":
        assert result.report is not None
        return result.report
    if result.status == "cancelled":
        raise JobCancelled("Job was cancelled.")
    legacy = legacy_error_of(result)
    if legacy is not None:
        return legacy
    error = result.error or {"reason": "stage_failed", "message": "Stage failed."}
    return _error(
        str(error.get("message", "Stage failed.")),
        str(error.get("reason", "stage_failed")),
        profile,
    )
