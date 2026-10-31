import pytest

from app import orchestrator, repository


@pytest.fixture(autouse=True)
def _force_stub_mode(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "stub")
    monkeypatch.delenv("META_API_KEY", raising=False)


def test_full_investigation_verifies_fix_and_preserves_fixture():
    result = orchestrator.investigate("negative-discount")

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["baseline"]["passed"] is False
    assert result["verification"]["passed"] is True
    assert result["patch"]["file"] == "src/pricing.py"

    original = (repository.FIXTURE_DIR / "src" / "pricing.py").read_text(encoding="utf-8")
    assert "    return price - discount\n" in original
    assert "max(0" not in original


def test_unknown_scenario_is_rejected():
    assert orchestrator.investigate("no-such-scenario") == {
        "status": "error",
        "message": "Unknown demo scenario.",
    }
