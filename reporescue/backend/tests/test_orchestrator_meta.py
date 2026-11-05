import pytest

from app import meta_client, orchestrator, repository
from app.meta_client import (
    MetaAPIError,
    MetaRateLimitError,
    MetaRefusalError,
    MetaResponseError,
)
from app.patch_guard import PatchProposal

FAKE_KEY = "test-key-not-real"
DIAGNOSIS = "final_price returns a negative value when discount exceeds price."


@pytest.fixture(autouse=True)
def _meta_env(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "meta")
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)


def _clamp_patch():
    return PatchProposal(
        file="src/pricing.py",
        before="    return price - discount",
        after="    return max(0, price - discount)",
        reason="Clamp the final price at zero.",
    )


def _useless_patch():
    return PatchProposal(
        file="src/pricing.py",
        before="    return price - discount",
        after="    return price - discount + 0",
        reason="No-op change that keeps the failure.",
    )


def _test_patch():
    return PatchProposal(
        file="tests/test_pricing.py",
        before="final_price",
        after="patched",
        reason="Forbidden target.",
    )


def _assert_fixture_original_untouched():
    original = (repository.FIXTURE_DIR / "src" / "pricing.py").read_text(encoding="utf-8")
    assert "    return price - discount\n" in original
    assert "max(0" not in original


def test_meta_success_verifies_with_real_tests(monkeypatch):
    monkeypatch.setattr(
        meta_client, "diagnose_and_propose", lambda **kwargs: (DIAGNOSIS, _clamp_patch())
    )

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["mode"] == orchestrator.META_MODE
    assert result["diagnosis"] == DIAGNOSIS
    assert result["baseline"]["passed"] is False
    assert result["verification"]["passed"] is True
    assert result["patch"]["file"] == "src/pricing.py"
    _assert_fixture_original_untouched()


def test_meta_api_error_returns_error_not_success(monkeypatch):
    def fail(**kwargs):
        raise MetaAPIError("Meta API request failed (HTTP 500).", status_code=500)

    monkeypatch.setattr(meta_client, "diagnose_and_propose", fail)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "api_error"
    assert result["attempts"] == 1
    assert "verdict" not in result
    assert result["baseline"]["passed"] is False
    _assert_fixture_original_untouched()


def test_meta_rejected_request_is_not_transient(monkeypatch):
    def fail(**kwargs):
        raise MetaAPIError("Meta API request failed (HTTP 400).", status_code=400)

    monkeypatch.setattr(meta_client, "diagnose_and_propose", fail)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "api_rejected"
    assert "verdict" not in result


def test_meta_refusal_fails_fast(monkeypatch):
    calls = []

    def refuse(**kwargs):
        calls.append(True)
        raise MetaRefusalError("Model refused the request.")

    monkeypatch.setattr(meta_client, "diagnose_and_propose", refuse)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "model_refused"
    assert calls == [True]


def test_meta_rate_limit_is_reported(monkeypatch):
    def limited(**kwargs):
        raise MetaRateLimitError("Meta API rate limited the request (HTTP 429).")

    monkeypatch.setattr(meta_client, "diagnose_and_propose", limited)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "rate_limit"
    assert "429" in result["message"]


def test_meta_invalid_output_retries_then_errors(monkeypatch):
    calls = []

    def invalid(**kwargs):
        calls.append(kwargs.get("feedback"))
        raise MetaResponseError("Meta API output was not valid JSON.")

    monkeypatch.setattr(meta_client, "diagnose_and_propose", invalid)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "invalid_output"
    assert result["attempts"] == orchestrator.MAX_LIVE_CALLS
    assert len(calls) == orchestrator.MAX_LIVE_CALLS
    assert calls[0] is None
    assert "invalid" in calls[1].lower()


def test_meta_rejected_patch_is_never_applied(monkeypatch):
    calls = []

    def rejected(**kwargs):
        calls.append(True)
        return DIAGNOSIS, _test_patch()

    monkeypatch.setattr(meta_client, "diagnose_and_propose", rejected)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "patch_rejected"
    assert "protected test suite" in result["message"]
    assert len(calls) == orchestrator.MAX_LIVE_CALLS
    _assert_fixture_original_untouched()


def test_meta_failing_verification_reports_inconclusive(monkeypatch):
    monkeypatch.setattr(
        meta_client, "diagnose_and_propose", lambda **kwargs: (DIAGNOSIS, _useless_patch())
    )

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "complete"
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["mode"] == orchestrator.META_MODE
    assert result["diagnosis"] == DIAGNOSIS
    assert result["baseline"]["passed"] is False
    assert result["verification"]["passed"] is False
    _assert_fixture_original_untouched()


def test_meta_missing_key_is_config_error(monkeypatch):
    monkeypatch.delenv("META_API_KEY")

    def must_not_run(**kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("live client must not run without a key")

    monkeypatch.setattr(meta_client, "diagnose_and_propose", must_not_run)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "config_error"
    assert "META_API_KEY" in result["message"]


def test_invalid_ai_mode_is_config_error(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "bogus")

    def must_not_run(**kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("live client must not run with an invalid mode")

    monkeypatch.setattr(meta_client, "diagnose_and_propose", must_not_run)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "error"
    assert result["reason"] == "config_error"


def test_stub_mode_never_calls_live_client(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "stub")

    def must_not_run(**kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("stub mode must not call the live client")

    monkeypatch.setattr(meta_client, "diagnose_and_propose", must_not_run)

    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["mode"] == orchestrator.MODE
    assert "diagnosis" not in result
