"""Tests for the static Health Assessment path (Phase 5A Step 3).

Health consumes stored ingestion profile/context data only: no clone, no
dependency installation, no Docker. Checks are deterministic observations;
only the prose summary comes from the model.
"""

import json
import shutil
import urllib.request
import uuid

import pytest

from app import execution as execution_module
from app import health as health_module
from app import ingestion as ingestion_module
from app import main as main_module
from app import real_investigation as pipeline
from app.auth import AuthenticatedUser
from app.health import assess_repository
from app.ingestion import clear_ingestions, ingest_repository, set_ingestion_owner
from app.orchestrate import OrchestrationResult
from app.meta_client import (
    MAX_EXPLAIN_MESSAGE_CHARS,
    MetaAPIError,
    MetaRateLimitError,
    MetaRefusalError,
    MetaResponseError,
    assess_from_context,
    build_health_messages,
)

FAKE_SHA = "d" * 40
ASSESSMENT = "This repo looks fine."

HEALTHY_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nrequires-python = ">=3.11"\n',
    "main.py": "from pkg.calc import add\n\nprint(add(2, 3))\n",
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": (
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}

NO_TESTS_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nrequires-python = ">=3.11"\n',
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a + b\n",
}

INVALID_DEPS_PROJECT = {
    "requirements.txt": "-e .\n",
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "def test_add():\n    assert True\n",
}

JS_PROJECT = {
    "package.json": '{"name": "widget", "version": "1.0.0"}\n',
    "index.js": "export function add(a, b) {\n  return a + b;\n}\n",
}

EMPTY_PROJECT = {
    "README.md": "# nothing here\n",
}


@pytest.fixture(autouse=True)
def _isolate():
    clear_ingestions()
    yield
    clear_ingestions()


def _make_project(tmp_path, files):
    project = tmp_path / "project"
    for rel, content in files.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


def _ingest_copy(monkeypatch, source, url="https://github.com/acme/calc"):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(source, destination)
        return FAKE_SHA

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    return ingest_repository(url)


def _explode(name):
    def _raiser(*args, **kwargs):
        raise AssertionError(f"{name} must never be called by Health")

    return _raiser


def test_health_complete_with_confirmed_checks(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, HEALTHY_PROJECT))
    seen = {}

    def fake_summarize(**kwargs):
        seen.update(kwargs)
        return ASSESSMENT

    result = assess_repository(
        record["ingestion_id"], message="Is this healthy?", summarize=fake_summarize
    )
    assert result["status"] == "complete"
    assert result["mode"] == "health"
    assert result["summary"] == ASSESSMENT
    assert result["message"] == "Is this healthy?"
    assert result["overall"] == "healthy"
    assert result["recommendations"] == []
    assert [check["name"] for check in result["checks"]] == [
        "dependencies",
        "tests",
        "runtime",
        "structure",
    ]
    assert {check["status"] for check in result["checks"]} == {"pass"}
    assert {check["confidence"] for check in result["checks"]} == {"confirmed"}
    assert result["profile"] == record["profile"]
    assert any("static-only" in item for item in result["limitations"])
    assert set(result["timings"]) == {"total_ms"}
    assert "[PASS]" in seen["checks_text"]
    assert seen["excerpts"], "Health must receive the stored source excerpts"


def test_health_no_tests_needs_attention(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch,
        _make_project(tmp_path, NO_TESTS_PROJECT),
        url="https://github.com/acme/plain",
    )
    result = assess_repository(
        record["ingestion_id"], summarize=lambda **kwargs: ASSESSMENT
    )
    assert result["status"] == "complete"
    assert result["overall"] == "needs_attention"
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["tests"]["status"] == "warn"
    assert by_name["runtime"]["status"] == "warn"
    assert any("test" in item.lower() for item in result["recommendations"])


def test_health_invalid_dependencies_fails_check(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch,
        _make_project(tmp_path, INVALID_DEPS_PROJECT),
        url="https://github.com/acme/badreq",
    )
    assert record["profile"]["capabilities"]["dependencies"]["python"]["status"] == "invalid"
    result = assess_repository(
        record["ingestion_id"], summarize=lambda **kwargs: ASSESSMENT
    )
    assert result["status"] == "complete"
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["dependencies"]["status"] == "fail"
    assert result["overall"] == "needs_attention"
    assert any("dependency" in item.lower() for item in result["recommendations"])


def test_health_js_repo_without_python_manifest(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch,
        _make_project(tmp_path, JS_PROJECT),
        url="https://github.com/acme/widget",
    )
    result = assess_repository(
        record["ingestion_id"], summarize=lambda **kwargs: ASSESSMENT
    )
    assert result["status"] == "complete"
    by_name = {check["name"]: check for check in result["checks"]}
    assert by_name["dependencies"]["status"] == "warn"
    assert by_name["runtime"]["status"] == "warn"


def test_health_empty_repo_has_nothing_to_assess(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch,
        _make_project(tmp_path, EMPTY_PROJECT),
        url="https://github.com/acme/empty",
    )
    result = assess_repository(
        record["ingestion_id"], summarize=lambda **kwargs: ASSESSMENT
    )
    assert result["status"] == "error"
    assert result["reason"] == "nothing_to_assess"


def test_health_missing_record_is_not_found():
    result = assess_repository("nope", summarize=lambda **kwargs: ASSESSMENT)
    assert result["status"] == "error"
    assert result["reason"] == "not_found"


def test_health_never_clones_installs_or_runs_docker(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, HEALTHY_PROJECT))
    monkeypatch.setattr(ingestion_module, "clone_and_pin", _explode("clone_and_pin"))
    monkeypatch.setattr(pipeline, "clone_and_pin", _explode("pipeline clone"))
    monkeypatch.setattr(
        execution_module.DockerBackend, "run", _explode("DockerBackend.run")
    )
    monkeypatch.setattr(
        execution_module.UnsafeLocalBackend, "run", _explode("UnsafeLocalBackend.run")
    )
    monkeypatch.setattr(execution_module, "select_backend", _explode("select_backend"))
    result = assess_repository(
        record["ingestion_id"], summarize=lambda **kwargs: ASSESSMENT
    )
    assert result["status"] == "complete"
    assert result["summary"] == ASSESSMENT


def test_health_message_is_optional_and_truncated(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, HEALTHY_PROJECT))
    seen = {}

    def fake_summarize(**kwargs):
        seen.update(kwargs)
        return ASSESSMENT

    result = assess_repository(record["ingestion_id"], summarize=fake_summarize)
    assert result["message"] is None
    assert seen["user_message"] is None

    result = assess_repository(
        record["ingestion_id"],
        message="?" * (MAX_EXPLAIN_MESSAGE_CHARS + 500),
        summarize=fake_summarize,
    )
    assert result["status"] == "complete"
    assert len(seen["user_message"]) == MAX_EXPLAIN_MESSAGE_CHARS


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (MetaRateLimitError("slow down"), "rate_limit"),
        (MetaAPIError("bad gateway"), "api_error"),
        (MetaAPIError("bad request", status_code=400), "api_rejected"),
        (MetaRefusalError("refused"), "model_refused"),
        (MetaResponseError("empty"), "invalid_output"),
    ],
)
def test_health_maps_meta_errors(monkeypatch, tmp_path, error, reason):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, HEALTHY_PROJECT))

    def broken(**kwargs):
        raise error

    result = assess_repository(record["ingestion_id"], summarize=broken)
    assert result["status"] == "error"
    assert result["reason"] == reason


def test_health_without_api_key_is_config_error(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, HEALTHY_PROJECT))
    monkeypatch.delenv("META_API_KEY", raising=False)
    result = assess_repository(record["ingestion_id"])
    assert result["status"] == "error"
    assert result["reason"] == "config_error"


def test_route_dispatches_health_through_shared_entry(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, HEALTHY_PROJECT))
    user = AuthenticatedUser(id=uuid.uuid4(), email=None, claims={})
    set_ingestion_owner(record["ingestion_id"], str(user.id))
    seen = {}

    def fake_execute(ctx, *, progress, deps):
        seen.update(
            mode=ctx.mode,
            plan_id=ctx.plan_id,
            message=ctx.message,
            max_attempts=ctx.max_attempts,
            owner_id=ctx.owner_id,
            sha=ctx.commit_sha,
        )
        return OrchestrationResult(
            status="complete", stages=[],
            report={"status": "complete", "mode": "health"},
        )

    monkeypatch.setattr(main_module, "execute", fake_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(
            ingestion_id=record["ingestion_id"], mode="health", message="checkup"
        ),
        user,
    )
    assert result == {"status": "complete", "mode": "health"}
    assert seen == {
        "mode": "health",
        "plan_id": "health",
        "message": "checkup",
        "max_attempts": 3,
        "owner_id": user.id,
        "sha": FAKE_SHA,
    }


def test_build_health_messages_bounds_and_checks():
    excerpts = {f"src/file{i}.py": "x = 1\n" * 2000 for i in range(20)}
    messages = build_health_messages(
        profile_summary="profile",
        structure="structure",
        checks_text="[PASS] tests: fine",
        excerpts=excerpts,
        user_message="?" * (MAX_EXPLAIN_MESSAGE_CHARS + 100),
    )
    assert messages[0]["role"] == "system"
    user_text = messages[1]["content"]
    assert "[PASS] tests: fine" in user_text
    assert "truncated: remaining excerpts omitted" in user_text
    assert "?" * (MAX_EXPLAIN_MESSAGE_CHARS + 100) not in user_text


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_assess_from_context_returns_prose(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {"choices": [{"message": {"content": "  Healthy.  "}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text = assess_from_context(
        profile_summary="profile",
        structure="structure",
        checks_text="[PASS] tests: fine",
        excerpts={"pkg/calc.py": "def add(a, b):\n    return a + b\n"},
        api_key="test-key-not-real",
    )
    assert text == "Healthy."
    payload = seen["payload"]
    assert payload["model"]
    assert payload["messages"][0]["role"] == "system"
    assert "max_completion_tokens" in payload
    assert "reasoning_effort" in payload


def test_assess_from_context_empty_response_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(
            {"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MetaResponseError, match="no message content"):
        assess_from_context(
            profile_summary="profile",
            structure="structure",
            checks_text="[PASS] tests: fine",
            excerpts={},
            api_key="test-key-not-real",
        )


def test_health_module_imports_no_execution_backend():
    import sys

    assert "app.health" in sys.modules
    module = sys.modules["app.health"]
    assert not hasattr(module, "DockerBackend")
    assert not hasattr(module, "select_backend")
    assert not hasattr(module, "clone_and_pin")
