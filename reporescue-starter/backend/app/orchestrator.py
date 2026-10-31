from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import meta_client, repository, runner, verdict
from .patch_guard import PatchRejected, validate_and_apply

SCENARIO_ID = "negative-discount"
SCENARIO_LABEL = "Negative discount result"
REPOSITORY_LABEL = "checkout-service-demo"
MODE = "deterministic-local-demo"
META_MODE = "meta-muse-spark-1.3"

MAX_LIVE_CALLS = 3
VERIFICATION_FEEDBACK_CHARS = 1500


def investigate(scenario: str) -> dict[str, Any]:
    """Run one investigation: reproduce, propose, guard, verify, report."""
    if scenario != SCENARIO_ID:
        return {"status": "error", "message": "Unknown demo scenario."}
    if not repository.fixture_exists():
        return {"status": "error", "message": "Bundled demo repository is missing."}
    try:
        ai_mode = meta_client.get_ai_mode()
    except meta_client.MetaConfigError as exc:
        return {"status": "error", "message": str(exc), "reason": "config_error"}
    if ai_mode == "meta":
        return _investigate_with_meta()
    return _investigate_with_stub()


def _investigate_with_stub() -> dict[str, Any]:
    """Original deterministic flow; unchanged behavior and report shape."""
    with tempfile.TemporaryDirectory(prefix="reporescue-") as temp_dir:
        workspace = repository.copy_fixture_to(Path(temp_dir) / "checkout_service")

        baseline = runner.run_tests(workspace)

        try:
            proposal = meta_client.propose_patch(repository.list_source_files(workspace))
            validate_and_apply(workspace, proposal)
        except PatchRejected as exc:
            return {"status": "error", "message": str(exc), "baseline": baseline}

        verification = runner.run_tests(workspace)
        return verdict.build_report(
            scenario=SCENARIO_LABEL,
            repository=REPOSITORY_LABEL,
            mode=MODE,
            baseline=baseline,
            verification=verification,
            patch=proposal.to_dict(),
        )


def _investigate_with_meta() -> dict[str, Any]:
    """Live flow: at most one diagnosis plus two repair attempts.

    Every proposal is guard-validated and verified by real test execution.
    API failures return an error report; they never fabricate a success.
    """
    try:
        api_key = meta_client.get_api_key()
    except meta_client.MetaConfigError as exc:
        return {"status": "error", "message": str(exc), "reason": "config_error"}

    with tempfile.TemporaryDirectory(prefix="reporescue-") as temp_dir:
        base = Path(temp_dir)
        baseline = runner.run_tests(repository.copy_fixture_to(base / "baseline"))

        feedback: str | None = None
        attempts = 0
        last_diagnosis = ""
        last_proposal = None
        last_verification = None

        for attempt in range(1, MAX_LIVE_CALLS + 1):
            attempts = attempt
            workspace = repository.copy_fixture_to(base / f"attempt-{attempt}")
            try:
                diagnosis, proposal = meta_client.diagnose_and_propose(
                    workspace=workspace,
                    baseline=baseline,
                    feedback=feedback,
                    api_key=api_key,
                )
            except meta_client.MetaRateLimitError as exc:
                return _meta_error(str(exc), "rate_limit", baseline, attempts)
            except meta_client.MetaAPIError as exc:
                return _meta_error(str(exc), "api_error", baseline, attempts)
            except meta_client.MetaResponseError as exc:
                feedback = f"Previous model output was invalid and discarded: {exc}"
                if attempt == MAX_LIVE_CALLS:
                    return _meta_error(
                        f"Model returned invalid output {MAX_LIVE_CALLS} times; "
                        f"last error: {exc}",
                        "invalid_output",
                        baseline,
                        attempts,
                    )
                continue

            try:
                validate_and_apply(workspace, proposal)
            except PatchRejected as exc:
                feedback = (
                    "Previous patch was rejected by the safety guard and not "
                    f"applied: {exc}"
                )
                if attempt == MAX_LIVE_CALLS:
                    return _meta_error(str(exc), "patch_rejected", baseline, attempts)
                continue

            verification = runner.run_tests(workspace)
            last_diagnosis, last_proposal, last_verification = (
                diagnosis,
                proposal,
                verification,
            )
            if verification.get("passed") is True:
                report = verdict.build_report(
                    scenario=SCENARIO_LABEL,
                    repository=REPOSITORY_LABEL,
                    mode=META_MODE,
                    baseline=baseline,
                    verification=verification,
                    patch=proposal.to_dict(),
                )
                report["diagnosis"] = diagnosis
                return report
            output_tail = str(verification.get("output", ""))[
                -VERIFICATION_FEEDBACK_CHARS:
            ]
            feedback = (
                "Previous patch applied but tests still fail "
                f"(exit {verification.get('exit_code')}):\n{output_tail}"
            )

        if last_proposal is None or last_verification is None:  # defensive
            return _meta_error(
                "Meta investigation ended without a usable proposal.",
                "invalid_output",
                baseline,
                attempts,
            )
        report = verdict.build_report(
            scenario=SCENARIO_LABEL,
            repository=REPOSITORY_LABEL,
            mode=META_MODE,
            baseline=baseline,
            verification=last_verification,
            patch=last_proposal.to_dict(),
        )
        report["diagnosis"] = last_diagnosis
        return report


def _meta_error(
    message: str, reason: str, baseline: dict[str, Any], attempts: int
) -> dict[str, Any]:
    return {
        "status": "error",
        "message": message,
        "reason": reason,
        "baseline": baseline,
        "attempts": attempts,
    }
