"""Static repository explanation (Phase 5A Step 2).

Explain consumes only the profile and context data captured during
ingestion. It never clones a repository, never installs dependencies, and
never invokes the Docker execution backend.
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

ExplainFn = Callable[..., str]


def _failed_result(stage: str, legacy: dict[str, Any]) -> StageResult:
    """Wrap an exact legacy error dict as a failed stage outcome."""
    return StageResult(
        stage=stage,
        status="failed",
        reason=str(legacy.get("reason", "stage_failed")),
        detail=str(legacy.get("message", "Stage failed."))[:2000],
        output={"legacy_error": legacy},
    )


def stage_explain_context(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Gate on static availability and assemble the prompt payload."""
    _ = deps
    profile = ctx.profile
    if not ctx.capabilities.get("static", {}).get("available", False):
        return _failed_result(
            "context",
            _explain_error(
                "This repository contains nothing RepoRescue can explain statically.",
                "nothing_to_explain",
                profile,
            ),
        )
    started = time.perf_counter()
    excerpts = dict(ctx.excerpts)
    progress.record_partial({"excerpt_files": len(excerpts)})
    scope.outputs["context"] = {
        "profile_summary": _profile_summary(profile),
        "structure": _structure_text(profile, ctx.file_lists),
        "excerpts": excerpts,
        "started": started,
    }
    return StageResult(stage="context", status="ok")


def stage_explain_summarize(
    ctx: InvestigationContext,
    progress: ProgressRecorder,
    deps: HandlerDeps,
    scope: RunScope,
) -> StageResult:
    """Summarize the repository from the assembled context (one AI call)."""
    _ = progress
    profile = ctx.profile
    prepared = scope.outputs["context"]
    user_message = (ctx.message or "").strip() or None
    if user_message is not None:
        user_message = user_message[:MAX_EXPLAIN_MESSAGE_CHARS]

    summarize = deps.summarize
    if summarize is None:
        try:
            api_key = meta_client.get_api_key()
        except meta_client.MetaConfigError as exc:
            return _failed_result(
                "summarize", _explain_error(str(exc), "config_error", profile)
            )

        def summarize(
            *,
            profile_summary: str,
            structure: str,
            excerpts: dict[str, str],
            user_message: str | None,
        ) -> str:
            return meta_client.explain_from_context(
                profile_summary=profile_summary,
                structure=structure,
                excerpts=excerpts,
                user_message=user_message,
                api_key=api_key,
            )

    try:
        text = summarize(
            profile_summary=prepared["profile_summary"],
            structure=prepared["structure"],
            excerpts=prepared["excerpts"],
            user_message=user_message,
        )
    except meta_client.MetaRateLimitError as exc:
        return _failed_result("summarize", _explain_error(str(exc), "rate_limit", profile))
    except meta_client.MetaAPIError as exc:
        return _failed_result("summarize", _explain_error(str(exc), "api_error", profile))
    except meta_client.MetaResponseError as exc:
        return _failed_result(
            "summarize", _explain_error(str(exc), "invalid_output", profile)
        )
    total_ms = int((time.perf_counter() - prepared["started"]) * 1000)
    return StageResult(
        stage="summarize",
        status="ok",
        output=_explain_report(profile, ctx.capabilities, text, user_message, total_ms),
    )


def _explain_report(
    profile: dict[str, Any],
    capabilities: dict[str, Any],
    text: str,
    user_message: str | None,
    total_ms: int,
) -> dict[str, Any]:
    runnable = capabilities.get("runnable", {})
    limitations = [
        "Explain is static-only: no dependencies were installed and no code was executed."
    ]
    if runnable.get("python"):
        limitations.append("Nothing was run or verified; use Debug to test behavior.")
    else:
        reason = runnable.get("reason") or "unknown"
        limitations.append(f"Runtime verification is unavailable ({reason}).")
    return {
        "status": "complete",
        "mode": "explain",
        "repository": profile.get("name"),
        "commit_sha": profile.get("commit_sha"),
        "message": user_message,
        "summary": text,
        "profile": profile,
        "limitations": limitations,
        "timings": {"total_ms": total_ms},
    }


def explain_repository(
    ingestion_id: str,
    *,
    message: str | None = None,
    summarize: ExplainFn | None = None,
    progress: ProgressRecorder | None = None,
) -> dict[str, Any]:
    """Explain an ingested repository from stored profile/context data.

    Legacy entry point: validates the stored record (as before), then
    delegates to the shared orchestration engine and unwraps the result
    into the exact historical response shape. ``message`` is an optional
    user question preserved alongside the explanation. ``summarize`` is
    a test seam with the same keyword contract as
    :func:`meta_client.explain_from_context` minus ``api_key``; when
    omitted, one live Meta API call is made. ``progress`` is an optional
    write-through recorder (None preserves exact legacy behavior).
    Cancellation propagates as ``JobCancelled``.
    """
    record = get_ingestion(ingestion_id)
    if record is None or record.get("status") != "complete":
        return _explain_error(
            "Repository is not ready for explanation.",
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
            mode="explain",
            message=message,
            max_attempts=3,
            plan_id=None,
        )
    except ContextError as exc:
        return _explain_error(str(exc), exc.reason, profile)
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
    return _explain_error(
        str(error.get("message", "Stage failed.")),
        str(error.get("reason", "stage_failed")),
        profile,
    )


def _explain_error(
    message: str,
    reason: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "status": "error",
        "mode": "explain",
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
