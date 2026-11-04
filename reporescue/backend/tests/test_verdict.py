import pytest

from app.verdict import (
    REAL_REPORT_KEYS,
    REPORT_KEYS,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASSED,
    build_real_report,
    build_report,
    decide_verdict,
)


def run(passed):
    return {"passed": passed, "exit_code": 0 if passed else 1, "output": "", "duration_ms": 1}


@pytest.mark.parametrize(
    ("baseline_passed", "verification_passed", "expected"),
    [
        (False, True, VERDICT_PASSED),
        (True, True, VERDICT_INCONCLUSIVE),
        (False, False, VERDICT_INCONCLUSIVE),
        (True, False, VERDICT_INCONCLUSIVE),
    ],
)
def test_decide_verdict_requires_fail_to_pass_transition(
    baseline_passed, verification_passed, expected
):
    assert decide_verdict(run(baseline_passed), run(verification_passed)) == expected


def test_build_report_keeps_api_contract_keys():
    report = build_report(
        scenario="Negative discount result",
        repository="checkout-service-demo",
        mode="deterministic-local-demo",
        baseline=run(False),
        verification=run(True),
        patch={"file": "src/pricing.py", "before": "b", "after": "a", "reason": "r"},
    )
    assert set(report) == set(REPORT_KEYS)
    assert report["status"] == "complete"
    assert report["verdict"] == VERDICT_PASSED
    assert "failure" in report["summary"]


def _real_report(baseline_passed, verification_passed):
    return build_real_report(
        repository="acme/calc",
        commit_sha="a" * 40,
        mode="real-repo-meta",
        diagnosis="d",
        baseline=run(baseline_passed),
        verification=run(verification_passed),
        patch={"edits": [], "reason": ""},
        guard={"validated": True, "message": "ok"},
        execution={"backend": "b", "isolation": "i"},
        attempts=1,
    )


def test_build_real_report_confirms_only_verified_transition():
    confirmed = _real_report(False, True)
    assert set(confirmed) == set(REAL_REPORT_KEYS)
    assert confirmed["verdict"] == VERDICT_PASSED
    assert confirmed["diagnosis_confidence"] == "confirmed"

    hypothesis = _real_report(False, False)
    assert hypothesis["verdict"] == VERDICT_INCONCLUSIVE
    assert hypothesis["diagnosis_confidence"] == "hypothesis"


def test_build_report_marks_non_transition_inconclusive():
    report = build_report(
        scenario="Negative discount result",
        repository="checkout-service-demo",
        mode="deterministic-local-demo",
        baseline=run(False),
        verification=run(False),
        patch={"file": "src/pricing.py", "before": "b", "after": "a", "reason": "r"},
    )
    assert report["verdict"] == VERDICT_INCONCLUSIVE
    assert "did not occur" in report["summary"]
