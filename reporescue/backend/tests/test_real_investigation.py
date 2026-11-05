"""Tests for the real-repository investigation pipeline.

Fast cases use a scripted fake backend and stub diagnosis functions. Two
end-to-end cases run real git materialization and real pytest execution: one
via the flagged unsafe-local backend, one via the real Docker backend, each
against a temporary local repository.
"""

import shutil
import subprocess
import uuid

import pytest

from app import ingestion as ingestion_module
from app.auth import AuthenticatedUser
from app import main as main_module
from app import meta_client
from app import real_investigation as pipeline
from app.execution import (
    UNSAFE_LOCAL_FLAG,
    DockerBackend,
    ExecutionBackend,
    ExecutionResult,
    UnsafeLocalBackend,
    select_backend,
)
from app.ingestion import clear_ingestions, ingest_repository, set_ingestion_owner
from app.orchestrate import OrchestrationResult
from app.patch_guard import FileEdit, PatchSet

FAKE_SHA = "b" * 40
DIAGNOSIS = "add subtracts instead of adding."

BUGGY_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nrequires-python = ">=3.11"\n',
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": (
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    clear_ingestions()
    monkeypatch.delenv(UNSAFE_LOCAL_FLAG, raising=False)
    yield
    clear_ingestions()


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "project"
    for rel, content in BUGGY_PROJECT.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


def _ingest_copy(monkeypatch, source):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(source, destination)
        return FAKE_SHA

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    return ingest_repository("https://github.com/acme/calc")


@pytest.fixture
def materialized(monkeypatch, project_dir):
    """Stub pipeline materialization to copy the fixture project (no network)."""
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), FAKE_SHA)[1],
    )


class FakeBackend(ExecutionBackend):
    name = "fake-test"
    isolation = "fake-test-only"

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def is_available(self):
        return True, "fake"

    def run(self, request):
        self.requests.append(request)
        outcome = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return ExecutionResult(
            backend=self.name,
            isolation=self.isolation,
            status=outcome.get("status", "ok"),
            passed=outcome.get("passed"),
            exit_code=outcome.get("exit_code", 0),
            output=outcome.get("output", ""),
            duration_ms=3,
            failures=outcome.get("failures", []),
            install_ok=outcome.get("install_ok"),
            install_log=outcome.get("install_log", ""),
        )


def _baseline_failure():
    return {
        "status": "test_failure",
        "passed": False,
        "exit_code": 1,
        "output": (
            "FAILED tests/test_calc.py::test_add - assert -1 == 5\n"
            "1 failed in 0.4s"
        ),
        "failures": [
            {
                "test": "tests/test_calc.py::test_add",
                "kind": "failed",
                "message": "assert -1 == 5",
                "traceback": '  File "pkg/calc.py", line 2, in add\nE   assert -1 == 5',
            }
        ],
    }


def _verification_pass():
    return {
        "status": "ok",
        "passed": True,
        "exit_code": 0,
        "output": "1 passed in 0.3s",
        "failures": [],
    }


def _fix_diagnose(**kwargs):
    return DIAGNOSIS, PatchSet(
        edits=(FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),),
        reason="Fix the operator.",
    )


def test_unknown_ingestion_id(monkeypatch):
    result = pipeline.investigate_repository("missing", backend=FakeBackend([]))
    assert result["status"] == "error"
    assert result["reason"] == "not_found"


def test_unsupported_project_rejected(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text('{"name": "web"}\n', encoding="utf-8")
    (web / "src" / "index.js").write_text("console.log(1);\n", encoding="utf-8")
    record = _ingest_copy(monkeypatch, web)

    result = pipeline.investigate_repository(record["ingestion_id"], backend=FakeBackend([]))

    assert result["status"] == "error"
    assert result["reason"] == "unsupported_project"


def test_unsupported_test_framework_investigated_statically(tmp_path, monkeypatch):
    odd = tmp_path / "odd"
    (odd / "pkg").mkdir(parents=True)
    (odd / "tests").mkdir()
    (odd / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (odd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (odd / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (odd / "tests" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    record = _ingest_copy(monkeypatch, odd)
    assert record["profile"]["test_framework"] == "unknown"

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=FakeBackend([]), diagnose=_fix_diagnose
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["diagnosis_confidence"] == "hypothesis"
    assert result["baseline"]["status"] == "unavailable"
    assert result["verification"]["status"] == "unavailable"
    assert result["verification"]["reason"] == "no_supported_test_framework"
    assert result["patch"]["applied"] is False


def _odd_project(tmp_path):
    odd = tmp_path / "odd"
    (odd / "pkg").mkdir(parents=True)
    (odd / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (odd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (odd / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    return odd


def test_static_path_never_clones_or_executes(tmp_path, monkeypatch):
    record = _ingest_copy(monkeypatch, _odd_project(tmp_path))

    def _explode(*args, **kwargs):
        raise AssertionError("static Debug must not clone, install, or execute")

    monkeypatch.setattr(ingestion_module, "clone_and_pin", _explode)
    monkeypatch.setattr(pipeline, "clone_and_pin", _explode)
    backend = FakeBackend([])

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert result["status"] == "complete"
    assert backend.requests == []
    assert result["execution"] == {"backend": "static-only", "isolation": "no-execution"}
    assert result["guard"]["validated"] is False


def test_static_path_threads_user_message(tmp_path, monkeypatch):
    record = _ingest_copy(monkeypatch, _odd_project(tmp_path))
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _fix_diagnose(**kwargs)

    result = pipeline.investigate_repository(
        record["ingestion_id"],
        backend=FakeBackend([]),
        diagnose=_capture,
        message="Why is X wrong?",
    )

    assert result["status"] == "complete"
    assert seen["user_message"] == "Why is X wrong?"
    assert seen["evidence_files"], "static Debug must receive stored excerpts"


def test_static_path_maps_meta_errors(tmp_path, monkeypatch):
    record = _ingest_copy(monkeypatch, _odd_project(tmp_path))
    cases = [
        (meta_client.MetaRateLimitError("slow"), "rate_limit"),
        (meta_client.MetaAPIError("down"), "api_error"),
        (meta_client.MetaAPIError("boom", status_code=500), "api_error"),
        (meta_client.MetaAPIError("bad request", status_code=400), "api_rejected"),
        (meta_client.MetaRefusalError("refused"), "model_refused"),
        (meta_client.MetaResponseError("bad"), "invalid_output"),
    ]
    for error, reason in cases:
        def _broken(**kwargs):
            raise error

        result = pipeline.investigate_repository(
            record["ingestion_id"], backend=FakeBackend([]), diagnose=_broken
        )
        assert result["status"] == "error"
        assert result["reason"] == reason


def test_static_path_without_api_key_is_config_error(tmp_path, monkeypatch):
    record = _ingest_copy(monkeypatch, _odd_project(tmp_path))
    monkeypatch.delenv("META_API_KEY", raising=False)

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=FakeBackend([])
    )

    assert result["status"] == "error"
    assert result["reason"] == "config_error"


def test_debug_route_threads_message_to_orchestration(monkeypatch, project_dir):
    record = _ingest_copy(monkeypatch, project_dir)
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
        main_module.RealInvestigationRequest(
            ingestion_id=record["ingestion_id"], message="it broke"
        ),
        user,
    )
    assert result == {"status": "complete"}
    assert seen == {
        "mode": "debug", "plan_id": "debug",
        "message": "it broke", "max_attempts": 3,
    }


def test_unsupported_dependencies_rejected(tmp_path, monkeypatch):
    pip = tmp_path / "pip"
    (pip / "pkg").mkdir(parents=True)
    (pip / "tests").mkdir()
    (pip / "Pipfile").write_text("[packages]\nrequests = '*'\n", encoding="utf-8")
    (pip / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (pip / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (pip / "tests" / "test_core.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    record = _ingest_copy(monkeypatch, pip)
    assert record["profile"]["supported"] is True

    result = pipeline.investigate_repository(record["ingestion_id"], backend=FakeBackend([]))

    assert result["status"] == "error"
    assert result["reason"] == "unsupported_dependencies"


def test_runtime_unavailable_reported(project_dir, monkeypatch):
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "select_backend", lambda: (None, {"status": "error", "reason": "runtime_unavailable", "message": "no runtime"})
    )

    result = pipeline.investigate_repository(record["ingestion_id"])

    assert result["status"] == "error"
    assert result["reason"] == "runtime_unavailable"


def test_dependency_error_reported(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend(
        [{"status": "install_error", "passed": None, "output": "", "install_ok": False,
          "install_log": "ERROR: No matching distribution"}]
    )

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert result["status"] == "error"
    assert result["reason"] == "dependency_error"
    assert "baseline" in result


def test_baseline_timeout_reported(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([{"status": "timeout", "passed": False, "output": "timed out"}])

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert result["status"] == "error"
    assert result["reason"] == "timeout"


def test_green_baseline_needs_no_diagnosis(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_verification_pass()])

    def must_not_run(**kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("diagnosis must not run on a green baseline")

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=must_not_run
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["attempts"] == 0
    assert "already pass" in result["summary"]


def test_success_verifies_fail_to_pass(project_dir, monkeypatch):
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), FAKE_SHA)[1],
    )
    backend = FakeBackend([_baseline_failure(), _verification_pass()])

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["diagnosis_confidence"] == "confirmed"
    assert result["diagnosis"] == DIAGNOSIS
    assert result["baseline"]["passed"] is False
    assert result["verification"]["passed"] is True
    assert result["patch"]["edits"][0]["file"] == "pkg/calc.py"
    assert result["guard"]["validated"] is True
    assert result["attempts"] == 1
    assert result["execution"]["backend"] == "fake-test"
    assert len(backend.requests) == 2
    assert backend.requests[1].install_commands == []  # venv reused, no reinstall


def test_complete_report_carries_stage_timings(project_dir, monkeypatch):
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), FAKE_SHA)[1],
    )
    backend = FakeBackend([_baseline_failure(), _verification_pass()])

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    timings = result["timings"]
    assert set(timings) == {
        "materialize_ms", "baseline_ms", "work_copy_ms", "attempts", "total_ms",
    }
    assert all(
        isinstance(timings[key], int) and timings[key] >= 0
        for key in ("materialize_ms", "baseline_ms", "work_copy_ms", "total_ms")
    )
    assert len(timings["attempts"]) == 1
    assert set(timings["attempts"][0]) == {"diagnose_ms", "verify_ms"}
    assert timings["attempts"][0]["diagnose_ms"] >= 0


def test_green_baseline_timings_have_no_attempts(
    project_dir, monkeypatch, materialized
):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_verification_pass()])

    def must_not_run(**kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("diagnosis must not run on a green baseline")

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=must_not_run
    )

    assert result["timings"]["attempts"] == []
    assert result["timings"]["total_ms"] >= 0


def test_rejected_patch_retries_with_feedback(project_dir, monkeypatch):
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), FAKE_SHA)[1],
    )
    seen_feedback = []

    def flaky(**kwargs):
        seen_feedback.append(kwargs.get("feedback"))
        if len(seen_feedback) == 1:
            return DIAGNOSIS, PatchSet(
                edits=(FileEdit("tests/test_calc.py", "assert add(2, 3) == 5", "assert True"),),
                reason="Cheat.",
            )
        return _fix_diagnose(**kwargs)

    backend = FakeBackend([_baseline_failure(), _verification_pass()])
    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=flaky
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["attempts"] == 2
    assert seen_feedback[0] is None
    assert "guard" in seen_feedback[1].lower()
    assert len(backend.requests) == 2  # rejected patch never reached verification


def test_repeated_rejections_end_in_error(project_dir, monkeypatch):
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), FAKE_SHA)[1],
    )

    def always_cheat(**kwargs):
        return DIAGNOSIS, PatchSet(
            edits=(FileEdit("tests/test_calc.py", "assert add(2, 3) == 5", "assert True"),),
            reason="Cheat.",
        )

    backend = FakeBackend([_baseline_failure()])
    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=always_cheat, max_attempts=2
    )

    assert result["status"] == "error"
    assert result["reason"] == "patch_rejected"
    assert "verdict" not in result


def test_invalid_model_output_retries_then_errors(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_baseline_failure()])

    def invalid(**kwargs):
        raise meta_client.MetaResponseError("not json")

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=invalid, max_attempts=2
    )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_output"
    assert result["attempts"] == 2


def test_api_error_returns_error_not_success(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_baseline_failure()])

    def fail(**kwargs):
        raise meta_client.MetaAPIError("boom", status_code=500)

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=fail
    )

    assert result["status"] == "error"
    assert result["reason"] == "api_error"
    assert "verdict" not in result


def test_rejected_request_fails_fast_without_transient_reason(
    project_dir, monkeypatch, materialized
):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_baseline_failure()])

    def fail(**kwargs):
        raise meta_client.MetaAPIError("bad request", status_code=400)

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=fail
    )

    assert result["status"] == "error"
    assert result["reason"] == "api_rejected"
    assert "verdict" not in result


def test_model_refusal_fails_fast_without_retry(
    project_dir, monkeypatch, materialized
):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_baseline_failure()])
    calls = []

    def refuse(**kwargs):
        calls.append(True)
        raise meta_client.MetaRefusalError("Model refused the request.")

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=refuse, max_attempts=3
    )

    assert result["status"] == "error"
    assert result["reason"] == "model_refused"
    assert calls == [True]
    assert "verdict" not in result


def test_rate_limit_reported(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_baseline_failure()])

    def limited(**kwargs):
        raise meta_client.MetaRateLimitError("slow down")

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=limited
    )

    assert result["status"] == "error"
    assert result["reason"] == "rate_limit"


def test_failing_verification_is_inconclusive(project_dir, monkeypatch):
    """A patch that does not fix the failure must never read as success."""
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), FAKE_SHA)[1],
    )
    backend = FakeBackend([_baseline_failure(), _baseline_failure(), _baseline_failure()])
    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose, max_attempts=2
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "INCONCLUSIVE"
    assert result["verification"]["passed"] is False


def test_sha_mismatch_aborts(project_dir, monkeypatch):
    record = _ingest_copy(monkeypatch, project_dir)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(project_dir, dest), "c" * 40)[1],
    )

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=FakeBackend([]), diagnose=_fix_diagnose
    )

    assert result["status"] == "error"
    assert result["reason"] == "clone_failed"


def test_pytest_install_kept_when_venv_needed(tmp_path, monkeypatch):
    """A venv install must still provision pytest even if the host has it.

    Declared pyproject dependencies install directly; the project itself
    is never editable-installed to run its tests.
    """
    vendored = tmp_path / "vendored"
    for rel, content in BUGGY_PROJECT.items():
        if rel == "pyproject.toml":
            content = (
                '[project]\nname = "calc"\nrequires-python = ">=3.11"\n'
                'dependencies = ["requests>=2"]\n'
            )
        target = vendored / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    record = _ingest_copy(monkeypatch, vendored)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(vendored, dest), FAKE_SHA)[1],
    )

    class LocalLike(FakeBackend):
        def needs_pytest_install(self):
            return False

    backend = LocalLike([_verification_pass()])
    pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    commands = backend.requests[0].install_commands
    assert ["pip", "install", "requests>=2"] in commands
    assert ["pip", "install", "pytest"] in commands
    assert all("-e" not in command for command in commands)


def test_install_error_reported_as_setup_blocker(project_dir, monkeypatch, materialized):
    """A genuine dependency installation failure is never a test verdict."""
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend(
        [
            {
                "status": "install_error",
                "passed": None,
                "exit_code": 1,
                "output": "pip failed",
                "install_ok": False,
                "install_log": "ERROR: No matching distribution for nosuchpkg==0",
            }
        ]
    )

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert result["status"] == "error"
    assert result["reason"] == "dependency_error"
    assert "No matching distribution" in result["message"]
    assert "verdict" not in result


def test_budget_exhausted_before_materialize_starts_nothing(
    project_dir, monkeypatch, materialized
):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_verification_pass()])

    result = pipeline.investigate_repository(
        record["ingestion_id"],
        backend=backend,
        diagnose=_fix_diagnose,
        time_budget_seconds=0,
    )

    assert result["status"] == "error"
    assert result["reason"] == "budget_exhausted"
    assert "materializing" in result["message"]
    assert backend.requests == []
    assert result["attempts"] == 0


def test_budget_exhausted_mid_run_preserves_baseline(
    project_dir, monkeypatch, materialized
):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend([_baseline_failure(), _verification_pass()])
    # Budget init + pre-materialize + materialize span + pre-baseline +
    # pre-diagnose + diagnose span + work-copy span pass at t=100; the
    # pre-verification check then trips at t=10000.
    ticks = iter([100.0] * 10 + [10000.0] * 3)
    monkeypatch.setattr(pipeline, "_monotonic", lambda: next(ticks))

    result = pipeline.investigate_repository(
        record["ingestion_id"],
        backend=backend,
        diagnose=_fix_diagnose,
        time_budget_seconds=1000,
    )

    assert result["status"] == "error"
    assert result["reason"] == "budget_exhausted"
    assert "verification" in result["message"]
    assert result["baseline"]["passed"] is False
    assert result["attempts"] == 1
    assert len(backend.requests) == 1
    assert "verdict" not in result


def test_malformed_pyproject_is_setup_error(tmp_path, monkeypatch):
    """Unparseable dependency metadata fails the setup stage, loudly."""
    broken = tmp_path / "broken"
    for rel, content in BUGGY_PROJECT.items():
        if rel == "pyproject.toml":
            content = "[project\nname = oops"
        target = broken / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    record = _ingest_copy(monkeypatch, broken)

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=FakeBackend([]), diagnose=_fix_diagnose
    )

    assert result["status"] == "error"
    assert result["reason"] == "unsupported_dependencies"
    assert "could not be parsed" in result["message"]


def test_bare_pytest_install_dropped_when_backend_has_pytest(tmp_path, monkeypatch):
    """With no dependencies, a pytest-capable backend skips venv creation."""
    plain = tmp_path / "plain"
    for rel, content in BUGGY_PROJECT.items():
        if rel == "pyproject.toml":
            continue
        target = plain / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    record = _ingest_copy(monkeypatch, plain)
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(plain, dest), FAKE_SHA)[1],
    )

    class LocalLike(FakeBackend):
        def needs_pytest_install(self):
            return False

    backend = LocalLike([_verification_pass()])
    pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert backend.requests[0].install_commands == []


def test_select_evidence_prefers_failures_and_tracebacks(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (workspace / "pkg" / "big.py").write_bytes(b"\x00binary")
    stored = {"tests/test_calc.py": "def test_add():\n    assert add(2, 3) == 5\n"}
    failures = [{"test": "tests/test_calc.py::test_add", "kind": "failed",
                 "message": "m", "traceback": ""}]
    output = '  File "pkg/calc.py", line 2, in add\n  File "pkg/big.py", line 1, in x\n'

    evidence = pipeline.select_evidence(workspace, stored, failures, output)

    assert set(evidence) == {"tests/test_calc.py", "pkg/calc.py"}
    assert "return a - b" in evidence["pkg/calc.py"]


def test_select_evidence_windows_around_traceback_line(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "pkg").mkdir(parents=True)
    lines = [f"line {number:03d}" for number in range(1, 301)]
    lines[149] = "TARGET = broken_call()"
    (workspace / "pkg" / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    output = '  File "pkg/big.py", line 150, in <module>\nE   NameError\n'

    evidence = pipeline.select_evidence(workspace, {}, [], output)

    assert "TARGET = broken_call()" in evidence["pkg/big.py"]
    assert "line 001" not in evidence["pkg/big.py"]


def test_sync_debug_unsupported_project_shape(monkeypatch, tmp_path):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text('{"name": "web"}\n', encoding="utf-8")
    (web / "src" / "index.js").write_text("console.log(1);\n", encoding="utf-8")
    record = _ingest_copy(monkeypatch, web)
    user = AuthenticatedUser(id=uuid.uuid4(), email=None, claims={})
    set_ingestion_owner(record["ingestion_id"], str(user.id))

    def must_not_execute(*args, **kwargs):
        raise AssertionError("unsupported project must fail before execution")

    monkeypatch.setattr(main_module, "execute", must_not_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(ingestion_id=record["ingestion_id"]),
        user,
    )
    assert result["status"] == "error"
    assert result["reason"] == "unsupported_project"
    assert result["message"] == (
        f"Repository {record['profile']['name']} is not a supported Python project."
    )
    assert result["profile"]["commit_sha"] == FAKE_SHA


def test_api_route_registered_and_delegates(monkeypatch, project_dir):
    paths = {(route.path, tuple(route.methods or ())) for route in main_module.app.routes}
    assert ("/api/investigations", ("POST",)) in paths

    record = _ingest_copy(monkeypatch, project_dir)
    user = AuthenticatedUser(id=uuid.uuid4(), email=None, claims={})
    set_ingestion_owner(record["ingestion_id"], str(user.id))
    seen = {}

    def fake_execute(ctx, *, progress, deps):
        seen.update(max_attempts=ctx.max_attempts, mode=ctx.mode)
        return OrchestrationResult(
            status="complete", stages=[], report={"status": "complete"}
        )

    monkeypatch.setattr(main_module, "execute", fake_execute)
    result = main_module.start_real_investigation(
        main_module.RealInvestigationRequest(
            ingestion_id=record["ingestion_id"], max_attempts=99
        ),
        user,
    )
    assert result == {"status": "complete"}
    assert seen == {"max_attempts": 3, "mode": "debug"}


# --- full end-to-end with real git + real pytest (unsafe-local, flagged) ---


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_end_to_end_local_repo_with_flagged_backend(tmp_path, monkeypatch):
    """Real clone at pinned SHA, real baseline, stub fix, real verification.

    The unsafe-local backend is pinned explicitly: ambient selection would
    prefer Docker whenever a daemon is reachable, which changes the backend
    under test and makes this test depend on host state.
    """
    offline_project = {k: v for k, v in BUGGY_PROJECT.items() if k != "pyproject.toml"}
    repo = tmp_path / "repo"
    for rel, content in offline_project.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    identity = ["-c", "user.email=t@e.com", "-c", "user.name=t"]
    _git(["init", "-b", "main"], repo)
    _git([*identity, "add", "."], repo)
    _git([*identity, "commit", "-m", "init"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    real_clone = pipeline.clone_and_pin

    def local_clone(url, destination, ref=None):
        assert url == "https://github.com/acme/calc"
        return real_clone(str(repo), destination, ref)

    monkeypatch.setattr(ingestion_module, "clone_and_pin", local_clone)
    monkeypatch.setattr(pipeline, "clone_and_pin", local_clone)
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")

    record = ingest_repository("https://github.com/acme/calc")
    assert record["profile"]["commit_sha"] == sha

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=UnsafeLocalBackend(), diagnose=_fix_diagnose
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["baseline"]["passed"] is False
    assert result["verification"]["passed"] is True
    assert result["execution"]["isolation"] == "none-unsafe-local-dev-only"
    assert result["commit_sha"] == sha


# --- end-to-end: flat layout + build-system-less pyproject via real Docker ---
#
# Regression fixture for the savings-vault-ledger failure: two top-level
# directories (app/, alembic/) plus a [project] table with no [build-system]
# makes setuptools package discovery abort, so any `pip install -e .` step
# fails. The pipeline must install dependencies directly and run the tests
# from the source tree instead.


FLAT_LAYOUT_PROJECT = {
    "pyproject.toml": (
        '[project]\nname = "vault"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n'
    ),
    "app/__init__.py": "",
    "app/calc.py": "def add(a, b):\n    return a - b\n",
    "alembic/env.py": "CONFIGURED = True\n",
    "tests/test_calc.py": (
        "from app.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}


def _fix_diagnose_app(**kwargs):
    return "add subtracts instead of adding.", PatchSet(
        edits=(FileEdit("app/calc.py", "    return a - b", "    return a + b"),),
        reason="Fix the operator.",
    )


def test_end_to_end_flat_layout_pyproject_docker_no_editable(tmp_path, monkeypatch):
    """Real Docker run: ambiguous flat layout installs deps, never -e ."""
    available, reason = DockerBackend().is_available()
    if not available:
        pytest.skip(f"docker unavailable: {reason}")
    repo = tmp_path / "repo"
    for rel, content in FLAT_LAYOUT_PROJECT.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    identity = ["-c", "user.email=t@e.com", "-c", "user.name=t"]
    _git(["init", "-b", "main"], repo)
    _git([*identity, "add", "."], repo)
    _git([*identity, "commit", "-m", "init"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    real_clone = pipeline.clone_and_pin

    def local_clone(url, destination, ref=None):
        assert url == "https://github.com/acme/vault"
        return real_clone(str(repo), destination, ref)

    monkeypatch.setattr(ingestion_module, "clone_and_pin", local_clone)
    monkeypatch.setattr(pipeline, "clone_and_pin", local_clone)

    record = ingest_repository("https://github.com/acme/vault")
    assert record["profile"]["commit_sha"] == sha

    result = pipeline.investigate_repository(
        record["ingestion_id"],
        backend=DockerBackend(),
        diagnose=_fix_diagnose_app,
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["baseline"]["passed"] is False
    assert result["verification"]["passed"] is True
    assert result["execution"]["backend"] == "docker"
    assert result["commit_sha"] == sha
    install_log = result["baseline"]["install_log"]
    assert "install -e" not in install_log
    assert "pip install pytest" in install_log


# --- declared pytest pins and harness classification ---


PLUGGY_OUTPUT = """\
pluggy._manager.PluginValidationError: Plugin '/repo/tests/conftest.py' for hook 'pytest_ignore_collect'
hookimpl definition: pytest_ignore_collect(path)
Argument(s) {'path'} are declared in the hookimpl but can not be found in the hookspec
"""


def test_pipeline_respects_declared_pytest_pin(tmp_path, monkeypatch):
    pinned = tmp_path / "pinned"
    for rel, content in BUGGY_PROJECT.items():
        if rel == "pyproject.toml":
            continue
        target = pinned / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (pinned / "requirements.txt").write_text("requests\npytest==7.4.4\n", encoding="utf-8")
    record = _ingest_copy(monkeypatch, pinned)
    assert "requirements.txt" in record["profile"]["dependency_files"]
    monkeypatch.setattr(
        pipeline, "clone_and_pin",
        lambda url, dest, ref=None: (shutil.copytree(pinned, dest), FAKE_SHA)[1],
    )

    backend = FakeBackend([_verification_pass()])
    pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert backend.requests[0].install_commands == [["pip", "install", "-r", "requirements.txt"]]


def test_pipeline_keeps_pytest_ensure_without_pin(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)

    backend = FakeBackend([_verification_pass()])
    pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )

    assert ["pip", "install", "pytest"] in backend.requests[0].install_commands


def test_harness_only_baseline_exits_early(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    backend = FakeBackend(
        [{"status": "test_failure", "passed": False, "exit_code": 4,
          "output": PLUGGY_OUTPUT, "failures": []}]
    )

    def must_not_run(**kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("no source patch can fix a harness failure")

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=must_not_run
    )

    assert result["status"] == "error"
    assert result["reason"] == "harness_incompatible"
    assert "pytest_ignore_collect" in result["message"]


def test_harness_verification_annotated_not_success(project_dir, monkeypatch, materialized):
    record = _ingest_copy(monkeypatch, project_dir)
    pluggy_fail = {
        "status": "test_failure", "passed": False, "exit_code": 4,
        "output": PLUGGY_OUTPUT, "failures": [],
    }
    backend = FakeBackend([_baseline_failure(), pluggy_fail, pluggy_fail])
    seen_feedback = []

    def capture(**kwargs):
        seen_feedback.append(kwargs.get("feedback"))
        return _fix_diagnose(**kwargs)

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=capture, max_attempts=2
    )

    assert result["status"] == "complete"
    assert result["verdict"] == "INCONCLUSIVE"
    assert any("harness" in item.lower() for item in result["limitations"])
    assert seen_feedback[0] is None
    assert "test-runner" in seen_feedback[1]
