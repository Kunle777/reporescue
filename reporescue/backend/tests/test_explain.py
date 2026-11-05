"""Tests for the static Explain path (Phase 5A Step 2).

Explain consumes stored ingestion profile/context data only. These tests
prove it never clones, never installs dependencies, and never invokes the
Docker execution backend, while the Debug pipeline stays unchanged.
"""

import json
import shutil
import urllib.request
import uuid

import pytest
from pydantic import ValidationError

from app import execution as execution_module
from app import explain as explain_module
from app import ingestion as ingestion_module
from app import main as main_module
from app import real_investigation as pipeline
from app.auth import AuthenticatedUser
from app.explain import explain_repository
from app.ingestion import clear_ingestions, ingest_repository, set_ingestion_owner
from app.orchestrate import OrchestrationResult
from app.meta_client import (
    MAX_EXPLAIN_MESSAGE_CHARS,
    MetaAPIError,
    MetaRateLimitError,
    MetaRefusalError,
    MetaResponseError,
    build_explain_messages,
    explain_from_context,
)


def test_explain_prompt_requests_structured_sections():
    system = build_explain_messages(
        profile_summary="profile",
        structure="lists",
        excerpts={},
        user_message="how does auth work?",
    )[0]["content"]
    for section in (
        "Overview",
        "How it works",
        "Important files",
        "How the components connect",
        "How to use it",
        "Security considerations",
        "Next steps",
    ):
        assert section in system
    assert "Omit sections that do not apply" in system
    assert "no JSON" in system
    assert "not executed or verified" in system

FAKE_SHA = "c" * 40
EXPLANATION = "This repo adds numbers."

PYTHON_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nrequires-python = ">=3.11"\n',
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": (
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
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
        raise AssertionError(f"{name} must never be called by Explain")

    return _raiser


def test_explain_complete_uses_stored_profile_and_context(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    seen = {}

    def fake_summarize(**kwargs):
        seen.update(kwargs)
        return EXPLANATION

    result = explain_repository(
        record["ingestion_id"], message="What does this do?", summarize=fake_summarize
    )
    assert result["status"] == "complete"
    assert result["mode"] == "explain"
    assert result["summary"] == EXPLANATION
    assert result["message"] == "What does this do?"
    assert result["commit_sha"] == FAKE_SHA
    assert result["profile"] == record["profile"]
    assert any("static-only" in item for item in result["limitations"])
    assert set(result["timings"]) == {"total_ms"}
    assert isinstance(result["timings"]["total_ms"], int)
    assert "calc" in seen["profile_summary"]
    assert seen["excerpts"], "Explain must receive the stored source excerpts"
    assert seen["user_message"] == "What does this do?"


def test_explain_never_clones_installs_or_runs_docker(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    monkeypatch.setattr(ingestion_module, "clone_and_pin", _explode("clone_and_pin"))
    monkeypatch.setattr(pipeline, "clone_and_pin", _explode("pipeline clone"))
    monkeypatch.setattr(
        execution_module.DockerBackend, "run", _explode("DockerBackend.run")
    )
    monkeypatch.setattr(
        execution_module.UnsafeLocalBackend, "run", _explode("UnsafeLocalBackend.run")
    )
    monkeypatch.setattr(
        execution_module, "select_backend", _explode("select_backend")
    )
    result = explain_repository(
        record["ingestion_id"], summarize=lambda **kwargs: EXPLANATION
    )
    assert result["status"] == "complete"
    assert result["summary"] == EXPLANATION


def test_explain_message_is_optional(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    result = explain_repository(
        record["ingestion_id"], summarize=lambda **kwargs: EXPLANATION
    )
    assert result["status"] == "complete"
    assert result["message"] is None


def test_explain_truncates_long_message(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    seen = {}

    def fake_summarize(**kwargs):
        seen.update(kwargs)
        return EXPLANATION

    result = explain_repository(
        record["ingestion_id"],
        message="?" * (MAX_EXPLAIN_MESSAGE_CHARS + 500),
        summarize=fake_summarize,
    )
    assert result["status"] == "complete"
    assert len(seen["user_message"]) == MAX_EXPLAIN_MESSAGE_CHARS
    assert result["message"] == seen["user_message"]


def test_explain_js_repo_reports_runtime_unavailable(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch,
        _make_project(tmp_path, JS_PROJECT),
        url="https://github.com/acme/widget",
    )
    result = explain_repository(
        record["ingestion_id"], summarize=lambda **kwargs: EXPLANATION
    )
    assert result["status"] == "complete"
    assert result["profile"]["capabilities"]["languages"]["javascript"]["files"] == 1
    assert any("Runtime verification is unavailable" in item for item in result["limitations"])


def test_explain_empty_repo_has_nothing_to_explain(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch,
        _make_project(tmp_path, EMPTY_PROJECT),
        url="https://github.com/acme/empty",
    )
    result = explain_repository(
        record["ingestion_id"], summarize=lambda **kwargs: EXPLANATION
    )
    assert result["status"] == "error"
    assert result["reason"] == "nothing_to_explain"


def test_explain_missing_record_is_not_found():
    result = explain_repository("nope", summarize=lambda **kwargs: EXPLANATION)
    assert result["status"] == "error"
    assert result["reason"] == "not_found"


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
def test_explain_maps_meta_errors(monkeypatch, tmp_path, error, reason):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))

    def broken(**kwargs):
        raise error

    result = explain_repository(record["ingestion_id"], summarize=broken)
    assert result["status"] == "error"
    assert result["reason"] == reason


def test_explain_without_api_key_is_config_error(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    monkeypatch.delenv("META_API_KEY", raising=False)
    result = explain_repository(record["ingestion_id"])
    assert result["status"] == "error"
    assert result["reason"] == "config_error"


def test_mode_defaults_to_debug_for_existing_clients():
    request = main_module.RealInvestigationRequest(ingestion_id="abc")
    assert request.mode == "debug"
    assert request.message is None


def test_route_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        main_module.RealInvestigationRequest(ingestion_id="abc", mode="bogus")


def test_route_dispatches_explain_through_shared_entry(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
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
            report={"status": "complete", "mode": "explain"},
        )

    monkeypatch.setattr(main_module, "execute", fake_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(
            ingestion_id=record["ingestion_id"], mode="explain", message="why?"
        ),
        user,
    )
    assert result == {"status": "complete", "mode": "explain"}
    assert seen == {
        "mode": "explain",
        "plan_id": "explain",
        "message": "why?",
        "max_attempts": 3,
        "owner_id": user.id,
        "sha": FAKE_SHA,
    }


def test_route_debug_default_unchanged(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    user = AuthenticatedUser(id=uuid.uuid4(), email=None, claims={})
    set_ingestion_owner(record["ingestion_id"], str(user.id))
    seen = {}

    def fake_execute(ctx, *, progress, deps):
        seen.update(
            mode=ctx.mode, plan_id=ctx.plan_id,
            message=ctx.message, max_attempts=ctx.max_attempts,
        )
        return OrchestrationResult(
            status="complete", stages=[], report={"status": "complete"}
        )

    monkeypatch.setattr(main_module, "execute", fake_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(ingestion_id=record["ingestion_id"]),
        user,
    )
    assert result == {"status": "complete"}
    assert seen == {
        "mode": "debug", "plan_id": "debug",
        "message": None, "max_attempts": 3,
    }


def test_route_returns_failed_legacy_error_verbatim(monkeypatch, tmp_path):
    from app.orchestrate import StageResult

    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    user = AuthenticatedUser(id=uuid.uuid4(), email=None, claims={})
    set_ingestion_owner(record["ingestion_id"], str(user.id))
    legacy = {
        "status": "error", "mode": "explain",
        "reason": "rate_limit", "message": "slow down",
    }

    def fake_execute(ctx, *, progress, deps):
        return OrchestrationResult(
            status="failed",
            stages=[
                StageResult(
                    stage="summarize", status="failed", reason="rate_limit",
                    detail="slow down", output={"legacy_error": legacy},
                )
            ],
            error={"reason": "rate_limit", "message": "slow down"},
        )

    monkeypatch.setattr(main_module, "execute", fake_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(
            ingestion_id=record["ingestion_id"], mode="explain"
        ),
        user,
    )
    assert result == legacy


def test_route_failed_record_is_not_found(monkeypatch, tmp_path):
    record = _ingest_copy(monkeypatch, _make_project(tmp_path, PYTHON_PROJECT))
    record["status"] = "error"
    record["reason"] = "clone_failed"
    record["message"] = "clone went away"
    user = AuthenticatedUser(id=uuid.uuid4(), email=None, claims={})
    set_ingestion_owner(record["ingestion_id"], str(user.id))

    def must_not_execute(*args, **kwargs):
        raise AssertionError("failed record must not execute")

    monkeypatch.setattr(main_module, "execute", must_not_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(
            ingestion_id=record["ingestion_id"], mode="explain"
        ),
        user,
    )
    assert result == {
        "status": "error",
        "mode": "explain",
        "reason": "not_found",
        "message": "Repository is not ready for explanation.",
    }


def test_build_explain_messages_bounds_excerpts_and_message():
    excerpts = {f"src/file{i}.py": "x = 1\n" * 2000 for i in range(20)}
    messages = build_explain_messages(
        profile_summary="profile",
        structure="structure",
        excerpts=excerpts,
        user_message="?" * (MAX_EXPLAIN_MESSAGE_CHARS + 100),
    )
    assert messages[0]["role"] == "system"
    user_text = messages[1]["content"]
    assert "profile" in user_text
    assert "truncated: remaining excerpts omitted" in user_text
    assert "?" * (MAX_EXPLAIN_MESSAGE_CHARS + 100) not in user_text
    assert "?" * MAX_EXPLAIN_MESSAGE_CHARS in user_text


def test_build_explain_messages_without_user_message():
    messages = build_explain_messages(
        profile_summary="profile", structure="structure", excerpts={}
    )
    assert "no source excerpts available" in messages[1]["content"]
    assert "user asked" not in messages[1]["content"]


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_explain_from_context_returns_prose(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {"choices": [{"message": {"content": "  It adds.  "}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text = explain_from_context(
        profile_summary="profile",
        structure="structure",
        excerpts={"pkg/calc.py": "def add(a, b):\n    return a + b\n"},
        user_message="What?",
        api_key="test-key-not-real",
    )
    assert text == "It adds."
    payload = seen["payload"]
    assert payload["model"]
    assert payload["messages"][0]["role"] == "system"
    assert "max_completion_tokens" in payload
    assert "reasoning_effort" in payload


def test_explain_from_context_empty_response_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(
            {"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MetaResponseError, match="no message content"):
        explain_from_context(
            profile_summary="profile",
            structure="structure",
            excerpts={},
            api_key="test-key-not-real",
        )


def test_explain_module_imports_no_execution_backend():
    import sys

    assert "app.explain" in sys.modules
    module = sys.modules["app.explain"]
    assert not hasattr(module, "DockerBackend")
    assert not hasattr(module, "select_backend")
    assert not hasattr(module, "clone_and_pin")
