import pytest

from app.verdict import REPORT_KEYS, VERDICT_INCONCLUSIVE, VERDICT_PASSED, build_report, decide_verdict


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
