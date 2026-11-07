"""Provider diagnostics: safe startup and failure telemetry.

Covers key fingerprinting (comparison without secret exposure),
provider/deployment snapshots, and the worker failure log format.
"""
import pytest

from app import meta_client
from app.worker import failure_phase, format_job_failure

FAKE_KEY = "test-key-material-not-real-12345"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in (
        "REPORESCUE_AI_MODE",
        "META_API_KEY",
        "RAILWAY_SERVICE_NAME",
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_GIT_COMMIT_SHA",
        "RAILWAY_GIT_BRANCH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_key_fingerprint_is_stable_and_secret_free():
    first = meta_client.key_fingerprint(FAKE_KEY)
    assert first == meta_client.key_fingerprint(FAKE_KEY)
    assert FAKE_KEY not in first
    assert "test-key-material" not in first
    assert first.startswith(f"len={len(FAKE_KEY)} sha256=")
    assert first != meta_client.key_fingerprint(FAKE_KEY + "x")


def test_provider_info_reports_present_key_without_value(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "meta")
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    info = meta_client.provider_info()
    assert info["provider"] == "meta"
    assert info["model"] == meta_client.MODEL
    assert info["endpoint"] == meta_client.API_URL
    assert info["ai_mode"] == "meta"
    assert info["key_present"] is True
    assert info["key_fingerprint"] == meta_client.key_fingerprint(FAKE_KEY)
    assert FAKE_KEY not in str(info)


def test_provider_info_without_key_never_raises():
    info = meta_client.provider_info()
    assert info["key_present"] is False
    assert info["key_fingerprint"] is None
    assert info["ai_mode"] == "stub"


def test_provider_info_invalid_mode_never_raises(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "bogus")
    info = meta_client.provider_info()
    assert info["ai_mode"] == "invalid"
    assert info["key_present"] is False


def test_deployment_info_defaults_to_local_unknown():
    deploy = meta_client.deployment_info()
    assert deploy == {
        "service": "local",
        "environment": "local",
        "commit": "unknown",
        "branch": "unknown",
    }


def test_deployment_info_reads_railway_env(monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "reporescue-worker")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "4e8811bf44a3ef63c2eaa36653c14a18ac2a4294")
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    deploy = meta_client.deployment_info()
    assert deploy["service"] == "reporescue-worker"
    assert deploy["environment"] == "production"
    assert deploy["commit"] == "4e8811bf44a3"
    assert deploy["branch"] == "main"


def test_startup_line_never_contains_key(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "meta")
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    line = meta_client.startup_diagnostic_line("[worker w1]")
    assert "[worker w1]" in line
    assert meta_client.MODEL in line
    assert "key_present=True" in line
    assert FAKE_KEY not in line


def test_failure_phase_mapping():
    assert failure_phase("config_error") == "pre-request"
    assert failure_phase("provider_auth") == "provider"
    assert failure_phase("model_not_found") == "provider"
    assert failure_phase("rate_limit") == "provider"
    assert failure_phase("api_error") == "provider"
    assert failure_phase("api_rejected") == "provider"
    assert failure_phase("invalid_output") == "response"
    assert failure_phase("model_refused") == "response"
    assert failure_phase("worker_error") == "unknown"
    assert failure_phase("something-new") == "unknown"


def test_format_job_failure_redacts_and_truncates():
    import uuid

    line = format_job_failure(
        worker_id="w1",
        job_id=uuid.UUID("4834cb26-fcab-49d2-a0f9-e16dfbaae077"),
        mode="debug",
        reason="model_not_found",
        message="Meta API request failed (HTTP 404).\nDetails: password=hunter2 leaked",
    )
    assert "4834cb26-fcab-49d2-a0f9-e16dfbaae077" in line
    assert "mode=debug" in line
    assert "reason=model_not_found" in line
    assert "phase=provider" in line
    assert "hunter2" not in line
    assert "[REDACTED]" in line
    assert "\n" not in line
    long_line = format_job_failure(
        worker_id="w1",
        job_id=uuid.UUID("4834cb26-fcab-49d2-a0f9-e16dfbaae077"),
        mode="debug",
        reason="api_error",
        message="x" * 2000,
    )
    assert len(long_line) < 600
