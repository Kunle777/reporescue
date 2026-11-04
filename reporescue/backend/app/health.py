"""Static repository health assessment (Phase 5A Step 3).

Health consumes only the profile and context data captured during
ingestion. It never clones a repository, never installs dependencies, and
never invokes the Docker execution backend. Checks are deterministic
observations of the stored data (confidence: confirmed); the prose summary
is AI interpretation of those checks.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from . import meta_client
from .ingestion import get_ingestion
from .meta_client import MAX_EXPLAIN_MESSAGE_CHARS
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
from .recorder import JobCancelled, ProgressRecorder

AssessFn = Callable[..., str]


def _failed_result(stage: str, legacy: dict[str, Any]) -> StageResult:
    """Wrap an exact legacy error dict as a failed stage outcome."""
    return StageResult(
        stage=stage,
        status="failed",
        reason=str(legacy.get("reason", "stage_failed")),
        detail=str(legacy.get("message", "Stage failed."))[:2000],
        output={"legacy_error": legacy},
    )


def stage_health_checks(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Gate on static availability and run the deterministic checks."""
    _ = deps
    profile = ctx.profile
    if not ctx.capabilities.get("static", {}).get("available", False):
        return _failed_result(
            "checks",
            _health_error(
                "This repository contains nothing RepoRescue can assess.",
                "nothing_to_assess",
                profile,
            ),
        )
    started = time.perf_counter()
    # The static file lists stand in for the ingestion context: the only
    # list the checks read is entry_points.
    checks = _run_checks(profile, ctx.file_lists)
    overall = "healthy"
    if any(check["status"] == "fail" for check in checks):
        overall = "needs_attention"
    elif any(check["status"] == "warn" for check in checks):
        overall = "needs_attention"
    progress.record_partial({"overall": overall})
    scope.outputs["checks"] = {
        "checks": checks,
        "overall": overall,
        "profile_summary": _profile_summary(profile),
        "structure": _structure_text(profile, ctx.file_lists),
        "checks_text": _checks_text(checks),
        "excerpts": dict(ctx.excerpts),
        "started": started,
    }
    return StageResult(stage="checks", status="ok")


def stage_health_summarize(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Interpret the deterministic checks (one AI call)."""
    _ = progress
    profile = ctx.profile
    prepared = scope.outputs["checks"]
    user_message = (ctx.message or "").strip() or None
    if user_message is not None:
        user_message = user_message[:MAX_EXPLAIN_MESSAGE_CHARS]

    summarize = deps.summarize
    if summarize is None:
        try:
            api_key = meta_client.get_api_key()
        except meta_client.MetaConfigError as exc:
            return _failed_result(
                "summarize", _health_error(str(exc), "config_error", profile)
            )

        def summarize(
            *,
            profile_summary: str,
            structure: str,
            checks_text: str,
            excerpts: dict[str, str],
            user_message: str | None,
        ) -> str:
            return meta_client.assess_from_context(
                profile_summary=profile_summary,
                structure=structure,
                checks_text=checks_text,
                excerpts=excerpts,
                user_message=user_message,
                api_key=api_key,
            )

    try:
        text = summarize(
            profile_summary=prepared["profile_summary"],
            structure=prepared["structure"],
            checks_text=prepared["checks_text"],
            excerpts=prepared["excerpts"],
            user_message=user_message,
        )
    except meta_client.MetaRateLimitError as exc:
        return _failed_result("summarize", _health_error(str(exc), "rate_limit", profile))
    except meta_client.MetaAPIError as exc:
        return _failed_result("summarize", _health_error(str(exc), "api_error", profile))
    except meta_client.MetaResponseError as exc:
        return _failed_result(
            "summarize", _health_error(str(exc), "invalid_output", profile)
        )
    total_ms = int((time.perf_counter() - prepared["started"]) * 1000)
    return StageResult(
        stage="summarize",
        status="ok",
        output=_health_report(
            profile, prepared["checks"], prepared["overall"], text,
            user_message, total_ms,
        ),
    )


def _health_report(
    profile: dict[str, Any],
    checks: list[dict[str, str]],
    overall: str,
    text: str,
    user_message: str | None,
    total_ms: int,
) -> dict[str, Any]:
    return {
        "status": "complete",
        "mode": "health",
        "repository": profile.get("name"),
        "commit_sha": profile.get("commit_sha"),
        "message": user_message,
        "summary": text,
        "overall": overall,
        "checks": checks,
        "recommendations": _recommendations(checks),
        "profile": profile,
        "limitations": [
            "Health assessment is static-only: no dependencies were installed and no code was executed.",
            "Checks are direct observations of the ingested data; the summary is AI interpretation of those checks.",
        ],
        "timings": {"total_ms": total_ms},
    }


def assess_repository(
    ingestion_id: str,
    *,
    message: str | None = None,
    summarize: AssessFn | None = None,
    progress: ProgressRecorder | None = None,
) -> dict[str, Any]:
    """Assess an ingested repository's health from stored profile/context.

    Legacy entry point: validates the stored record (as before), then
    delegates to the shared orchestration engine and unwraps the result
    into the exact historical response shape. ``message`` is an optional
    user question preserved alongside the assessment. ``summarize`` is
    a test seam with the same keyword contract as
    :func:`meta_client.assess_from_context` minus ``api_key``; when
    omitted, one live Meta API call is made. ``progress`` is an optional
    write-through recorder (None preserves exact legacy behavior).
    Cancellation propagates as ``JobCancelled``.
    """
    record = get_ingestion(ingestion_id)
    if record is None or record.get("status") != "complete":
        return _health_error(
            "Repository is not ready for health assessment.",
            "not_found",
        )
    profile = record["profile"]
    context = record.get("context", {}) or {}
    try:
        owner_id = uuid.UUID(str(record.get("owner_id") or ""))
    except (TypeError, ValueError):
        # Untagged legacy/test records carry no owner; the caller
        # authorized this invocation before reaching the wrapper.
        owner_id = uuid.uuid4()
    try:
        ctx = assemble_context(
            profile=profile,
            excerpts=context.get("excerpts", {}),
            file_lists=context,
            investigation_id=uuid.uuid4(),
            owner_id=owner_id,
            mode="health",
            message=message,
            max_attempts=3,
            plan_id=None,
        )
    except ContextError as exc:
        return _health_error(str(exc), exc.reason, profile)
    result = execute(
        ctx,
        progress=progress if progress is not None else NullRecorder(),
        deps=HandlerDeps(summarize=summarize),
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
    return _health_error(
        str(error.get("message", "Stage failed.")),
        str(error.get("reason", "stage_failed")),
        profile,
    )


def _health_error(
    message: str,
    reason: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "status": "error",
        "mode": "health",
        "reason": reason,
        "message": message,
    }
    if profile is not None:
        error["profile"] = {
            "url": profile.get("url"),
            "name": profile.get("name"),
            "commit_sha": profile.get("commit_sha"),
        }
    return error


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "confidence": "confirmed",
    }


def _run_checks(
    profile: dict[str, Any], context: dict[str, Any]
) -> list[dict[str, str]]:
    """Run deterministic health checks over stored profile/context data."""
    capabilities = profile.get("capabilities", {})
    python_deps = capabilities.get("dependencies", {}).get("python", {})
    dep_status = python_deps.get("status", "none")
    dep_error = python_deps.get("error")
    if dep_error:
        dependencies = _check(
            "dependencies", "fail", f"Python dependency metadata is invalid: {dep_error}"
        )
    elif dep_status == "none":
        dependencies = _check(
            "dependencies",
            "warn",
            "No Python dependency metadata found; runtime needs are unknown.",
        )
    else:
        files = ", ".join(python_deps.get("files", []) or [dep_status])
        dependencies = _check(
            "dependencies", "pass", f"Dependency metadata present ({files})."
        )

    tests = profile.get("tests", {}) or {}
    test_framework = profile.get("test_framework") or "none"
    if not tests.get("count"):
        tests_check = _check(
            "tests", "warn", "No test files discovered; behavior cannot be verified."
        )
    elif test_framework not in ("pytest", "unittest"):
        tests_check = _check(
            "tests",
            "warn",
            f"{tests['count']} test file(s) found but framework "
            f"{test_framework!r} is not supported for verification.",
        )
    else:
        tests_check = _check(
            "tests",
            "pass",
            f"{tests['count']} {test_framework} test file(s) discovered.",
        )

    runnable = capabilities.get("runnable", {})
    if runnable.get("python"):
        runtime = _check(
            "runtime", "pass", "Test-backed verification is available."
        )
    else:
        runtime = _check(
            "runtime",
            "warn",
            f"Test-backed verification is unavailable ({runnable.get('reason') or 'unknown'}).",
        )

    entry_points = context.get("entry_points", []) or []
    if entry_points:
        structure = _check(
            "structure",
            "pass",
            f"{len(entry_points)} entry point(s): {', '.join(entry_points[:5])}"
            + ("…" if len(entry_points) > 5 else "")
            + ".",
        )
    else:
        structure = _check(
            "structure", "warn", "No entry points detected; start from the top-level sources."
        )
    return [dependencies, tests_check, runtime, structure]


def _recommendations(checks: list[dict[str, str]]) -> list[str]:
    """Derive deterministic follow-ups from non-passing checks."""
    advice: list[str] = []
    for check in checks:
        if check["status"] == "pass":
            continue
        if check["name"] == "dependencies":
            if check["status"] == "fail":
                advice.append("Fix the invalid Python dependency metadata before running anything.")
            else:
                advice.append("Add dependency metadata (pyproject.toml or requirements) so installs are reproducible.")
        elif check["name"] == "tests":
            advice.append("Add automated tests so behavior can be verified, not just inspected.")
        elif check["name"] == "runtime":
            advice.append("Use Debug-static review until a supported test framework is available.")
        elif check["name"] == "structure":
            advice.append("Identify the main entry points (README or module docstrings) to orient newcomers.")
    return advice


def _checks_text(checks: list[dict[str, str]]) -> str:
    return "\n".join(
        f"[{check['status'].upper()}] {check['name']}: {check['detail']}"
        for check in checks
    )


def _profile_summary(profile: dict[str, Any]) -> str:
    languages = profile.get("capabilities", {}).get("languages", {})
    lang_bits = []
    for language, info in sorted(languages.items()):
        files = info.get("files", 0) if isinstance(info, dict) else 0
        lang_bits.append(f"{language}: {files} file(s)")
    frameworks = profile.get("frameworks", []) or []
    tests = profile.get("tests", {}) or {}
    lines = [
        f"Repository: {profile.get('name')} @ {profile.get('commit_sha')}",
        f"Primary language: {profile.get('primary_language')}",
        f"Languages: {', '.join(lang_bits) or 'none detected'}",
        f"Dependency files: {', '.join(profile.get('dependency_files', []) or []) or 'none'}",
        f"Frameworks: {', '.join(frameworks) or 'none detected'}",
        f"Tests: {tests.get('count', 0)} file(s) ({profile.get('test_framework') or 'none'})",
        f"Profile: {profile.get('structure_summary', '')}",
    ]
    return "\n".join(lines)


def _structure_text(profile: dict[str, Any], context: dict[str, Any]) -> str:
    lines = [
        f"Source directories: {', '.join(profile.get('source_dirs', []) or []) or 'none'}",
        f"Package directories: {', '.join(profile.get('package_dirs', []) or []) or 'none'}",
        f"Entry points: {', '.join(context.get('entry_points', []) or []) or 'none'}",
        f"Configs: {', '.join(context.get('configs', []) or []) or 'none'}",
        f"Tests: {', '.join(context.get('tests', []) or []) or 'none'}",
        f"Docs: {', '.join(context.get('docs', []) or []) or 'none'}",
    ]
    return "\n".join(lines)
