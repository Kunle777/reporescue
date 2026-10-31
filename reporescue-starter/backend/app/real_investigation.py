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
from pathlib import Path
from typing import Any

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
from .github_ingest import IngestionError, clone_and_pin, temporary_workspace
from .ingestion import get_ingestion
from .patch_guard import PatchRejected, PatchSet, validate_and_apply_patchset
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


def investigate_repository(
    ingestion_id: str,
    *,
    backend: ExecutionBackend | None = None,
    diagnose=None,
    max_attempts: int = MAX_LIVE_ATTEMPTS,
) -> dict[str, Any]:
    """Run a real investigation for a stored ingestion record.

    ``backend`` and ``diagnose`` are seams for tests (a fake backend and a
    stub diagnosis function); production passes neither, using Docker
    selection and the live Meta client.
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
    framework = profile.get("test_framework", "none")
    if framework not in SUPPORTED_FRAMEWORKS:
        return _error(
            f"Test framework {framework!r} is not supported for investigation "
            "(pytest first, unittest where practical).",
            "unsupported_framework",
            profile,
        )
    if backend is None:
        backend, backend_error = select_backend()
        if backend is None or backend_error is not None:
            assert backend_error is not None
            backend_error["profile"] = {
                "url": profile.get("url"),
                "name": profile.get("name"),
                "commit_sha": profile.get("commit_sha"),
            }
            return backend_error
    planned = plan_install(profile)
    if planned["error"]:
        return _error(planned["error"], "unsupported_dependencies", profile)
    install_commands = [list(cmd) for cmd in planned["commands"]]
    if install_commands == [["pip", "install", "pytest"]] and not backend.needs_pytest_install():
        # No dependency install is needed at all: run with the backend's
        # pre-existing pytest instead of creating a venv for nothing.
        install_commands = []
    if diagnose is None:
        try:
            api_key = meta_client.get_api_key()
        except meta_client.MetaConfigError as exc:
            return _error(str(exc), "config_error", profile)

        def diagnose(**kwargs):
            return meta_client.diagnose_and_propose_from_evidence(api_key=api_key, **kwargs)

    execution_info = {"backend": backend.name, "isolation": backend.isolation}
    try:
        with temporary_workspace(prefix="reporescue-invest-") as workspace:
            pristine = workspace / "pristine"
            try:
                _materialize(profile["url"], profile["commit_sha"], pristine)
            except IngestionError as exc:
                return _error(str(exc), exc.reason, profile, execution=execution_info)
            if requirements_pin_pytest(pristine, profile.get("dependency_files", [])):
                # The repository declares its own pytest version: installing
                # the plan's bare `pip install pytest` afterwards would
                # override that declaration with the latest release.
                install_commands = [
                    cmd for cmd in install_commands if cmd != ["pip", "install", "pytest"]
                ]
            request = ExecutionRequest(
                snapshot=pristine,
                test_command=list(SUPPORTED_FRAMEWORKS[framework]),
                install_commands=install_commands,
                framework=framework,
                python_requires=profile.get("python_requires"),
            )
            baseline_result = backend.run(request)
            baseline = baseline_result.to_dict()
            if baseline_result.status == "install_error":
                return _error(
                    "Dependency installation failed inside the execution "
                    f"environment: {baseline['install_log'][-500:]}",
                    "dependency_error",
                    profile,
                    execution=execution_info,
                    baseline=baseline,
                )
            if baseline_result.status == "timeout":
                return _error(
                    "Baseline test run timed out inside the execution environment.",
                    "timeout",
                    profile,
                    execution=execution_info,
                    baseline=baseline,
                )
            if baseline_result.status not in ("ok", "test_failure"):
                return _error(
                    f"Baseline execution failed ({baseline_result.status}): "
                    f"{baseline['output'][-500:]}",
                    "execution_error",
                    profile,
                    execution=execution_info,
                    baseline=baseline,
                )
            if not baseline_result.passed and not baseline_result.failures:
                harness_note = detect_harness_incompatibility(baseline_result.output)
                if harness_note:
                    return _error(
                        f"Baseline test execution failed before any test ran: {harness_note}",
                        "harness_incompatible",
                        profile,
                        execution=execution_info,
                        baseline=baseline,
                    )
            if baseline_result.passed:
                return {
                    "status": "complete",
                    "repository": profile.get("name"),
                    "commit_sha": profile.get("commit_sha"),
                    "mode": MODE,
                    "summary": "Tests already pass at the pinned revision; nothing to repair.",
                    "verdict": "INCONCLUSIVE",
                    "diagnosis": "",
                    "baseline": baseline,
                    "verification": baseline,
                    "patch": {"edits": [], "reason": ""},
                    "guard": {"validated": False, "message": "No patch proposed; baseline is green."},
                    "execution": execution_info,
                    "attempts": 0,
                    "limitations": ["No AI diagnosis was requested because the baseline passed."],
                }
            stored_excerpts = dict(context.get("excerpts", {}))
            evidence = select_evidence(
                pristine, stored_excerpts, baseline_result.failures, baseline_result.output
            )
            feedback: str | None = None
            attempts = 0
            last_diagnosis = ""
            last_patch: PatchSet | None = None
            last_verification: dict[str, Any] | None = None
            last_harness_note: str | None = None
            for attempt in range(1, max_attempts + 1):
                attempts = attempt
                try:
                    diagnosis, patchset = diagnose(
                        evidence_files=evidence,
                        failures=baseline_result.failures,
                        baseline_tail=baseline_result.output,
                        feedback=feedback,
                    )
                except meta_client.MetaRateLimitError as exc:
                    return _error(
                        str(exc), "rate_limit", profile,
                        execution=execution_info, baseline=baseline, attempts=attempts,
                    )
                except meta_client.MetaAPIError as exc:
                    return _error(
                        str(exc), "api_error", profile,
                        execution=execution_info, baseline=baseline, attempts=attempts,
                    )
                except meta_client.MetaResponseError as exc:
                    feedback = f"Previous model output was invalid and discarded: {exc}"
                    if attempt == max_attempts:
                        return _error(
                            f"Model returned invalid output {max_attempts} times; "
                            f"last error: {exc}",
                            "invalid_output", profile,
                            execution=execution_info, baseline=baseline, attempts=attempts,
                        )
                    continue
                work = _fresh_work_copy(pristine, workspace / f"attempt-{attempt}")
                try:
                    applied = validate_and_apply_patchset(work, patchset)
                except PatchRejected as exc:
                    feedback = (
                        "Previous patch was rejected by the safety guard and not "
                        f"applied: {exc}"
                    )
                    if attempt == max_attempts:
                        return _error(
                            str(exc), "patch_rejected", profile,
                            execution=execution_info, baseline=baseline, attempts=attempts,
                        )
                    continue
                verify_request = ExecutionRequest(
                    snapshot=work,
                    test_command=list(SUPPORTED_FRAMEWORKS[framework]),
                    install_commands=[],
                    framework=framework,
                    python_requires=profile.get("python_requires"),
                )
                verification_result = backend.run(verify_request)
                verification = verification_result.to_dict()
                last_diagnosis, last_patch, last_verification = diagnosis, patchset, verification
                if verification_result.passed is True:
                    return build_real_report(
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
                return _error(
                    "Investigation ended without a usable proposal.",
                    "invalid_output", profile,
                    execution=execution_info, baseline=baseline, attempts=attempts,
                )
            return build_real_report(
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
    except OSError as exc:
        return _error(
            f"Could not prepare an investigation workspace: {exc}.",
            "workspace_error",
            profile,
            execution=execution_info,
        )
