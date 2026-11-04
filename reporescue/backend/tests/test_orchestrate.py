"""Tests for shared orchestration (Phase 5B Tasks 2-3).

Covers context construction/validation, plan resolution, the stage
runner, migrated mode plans, safety boundaries, progress ownership,
and the derived attempt-status helper. Real pipelines run only with
stubbed clones, fake backends, and stubbed model calls: no network,
no Docker.
"""

import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app import ingestion as ingestion_module
from app import orchestrate as orch
from app import real_investigation as pipeline
from app.ingestion import clear_ingestions, ingest_repository, set_ingestion_owner
from app.jobqueue import (
    attempt_status_summary,
    create_investigation,
    investigation_detail,
)
from app.meta_client import MetaRateLimitError
from app.models import Base, ExecutionRun, Investigation, InvestigationAttempt, Profile
from app.orchestrate import (
    HANDLERS,
    PLANS,
    ContextError,
    HandlerDeps,
    InvestigationContext,
    InvestigationPlan,
    NullRecorder,
    OrchestrationError,
    RunScope,
    StageDefinition,
    StageResult,
    UnknownPlanError,
    UnsupportedProjectError,
    build_context_for_worker,
    build_context_from_record,
    execute,
    legacy_error_of,
    plan_for_mode,
    run,
)
from app.recorder import DBRecorder, JobCancelled

FAKE_SHA = "b" * 40

CALC_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nrequires-python = ">=3.11"\n',
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": (
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}


@pytest.fixture(autouse=True)
def _isolate_store():
    clear_ingestions()
    yield
    clear_ingestions()


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/orch.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture()
def owner(db):
    profile = Profile(id=uuid.uuid4())
    db.add(profile)
    db.commit()
    return profile.id


@pytest.fixture()
def project_dir(tmp_path):
    project = tmp_path / "project"
    for rel, content in CALC_PROJECT.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


def _stub_clone(monkeypatch, source, sha=FAKE_SHA):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(source, destination)
        return sha

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    monkeypatch.setattr(pipeline, "clone_and_pin", fake_clone)


def _owned_record(monkeypatch, source, owner, url="https://github.com/acme/calc"):
    _stub_clone(monkeypatch, source)
    record = ingest_repository(url)
    set_ingestion_owner(record["ingestion_id"], str(owner))
    return record


class _RecordingRecorder:
    """Fake recorder capturing the exact boundary call sequence."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._cancel_at: int | None = None
        self._checks = 0

    def cancel_on_check(self, number: int) -> None:
        self._cancel_at = number

    def set_stages(self, stages):
        self.calls.append(("set_stages", tuple(stages)))

    def stage_started(self, stage):
        self.calls.append(("started", stage))

    def stage_completed(self, stage, data=None):
        self.calls.append(("completed", stage, sorted((data or {}).keys())))

    def record_run(self, kind, result, attempt_id=None):
        self.calls.append(("run", kind))
        return None

    def record_attempt(self, attempt_no, **kwargs):
        self.calls.append(("attempt", attempt_no))
        return None

    def record_finding(self, **kwargs):
        self.calls.append(("finding",))

    def record_patch(self, **kwargs):
        self.calls.append(("patch",))

    def record_message(self, **kwargs):
        self.calls.append(("message",))

    def record_partial(self, data):
        self.calls.append(("partial", sorted(data.keys())))

    def check_cancelled(self):
        self._checks += 1
        if self._cancel_at is not None and self._checks >= self._cancel_at:
            raise JobCancelled("cancelled")


def _ok_handler(name="s", output=None):
    def _handle(ctx, progress, deps, scope):
        _ = scope
        return StageResult(stage=name, status="ok", output=output)

    return _handle


# --- context construction ---


def test_context_from_validated_record(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = build_context_from_record(
        record,
        ingestion_id=record["ingestion_id"],
        owner_id=owner,
        mode="debug",
        message="  fix it ",
        max_attempts=2,
    )
    assert isinstance(ctx, InvestigationContext)
    assert ctx.repo_name == "acme/calc"
    assert ctx.commit_sha == FAKE_SHA
    assert ctx.message == "fix it"
    assert ctx.max_attempts == 2
    assert ctx.plan_id == "debug"
    assert ctx.excerpts
    assert all(len(text) <= 4000 for text in ctx.excerpts.values())
    assert ctx.profile["commit_sha"] == ctx.commit_sha


def test_context_resolves_static_plan(monkeypatch, tmp_path, owner):
    odd = tmp_path / "odd"
    (odd / "pkg").mkdir(parents=True)
    (odd / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (odd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (odd / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    record = _owned_record(monkeypatch, odd, owner, url="https://github.com/acme/odd")
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner
    )
    assert ctx.plan_id == "debug-static"


def test_context_rejects_unowned_record(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    with pytest.raises(ContextError) as exc_info:
        build_context_from_record(
            record,
            ingestion_id=record["ingestion_id"],
            owner_id=uuid.uuid4(),
        )
    assert exc_info.value.reason == "not_owner"


def test_context_rejects_record_id_mismatch(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    other = dict(record)
    other["ingestion_id"] = "different-id"
    with pytest.raises(ContextError) as exc_info:
        build_context_from_record(
            other,
            ingestion_id=record["ingestion_id"],
            owner_id=owner,
        )
    assert exc_info.value.reason == "record_mismatch"


def test_context_max_attempts_clamp_and_reject(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)

    def build(value):
        return build_context_from_record(
            record, ingestion_id=record["ingestion_id"], owner_id=owner,
            max_attempts=value,
        ).max_attempts

    assert build(None) == 3
    assert build(0) == 3
    assert build(1) == 1
    assert build(2) == 2
    assert build(99) == 3
    assert build("2") == 2
    for bad in ("many", [2], {"n": 2}):
        with pytest.raises(ContextError) as exc_info:
            build(bad)
        assert exc_info.value.reason == "bad_attempts"


def test_context_carries_ref_as_display_only(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    record["profile"]["ref"] = "feature/branch;$(touch pwned)"
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner
    )
    # Verbatim passthrough is the contract: checkout uses the validated
    # SHA only, so hostile-looking display metadata is inert data.
    assert ctx.ref == "feature/branch;$(touch pwned)"
    assert ctx.commit_sha == FAKE_SHA


def test_context_rejects_bad_record():
    with pytest.raises(ContextError) as exc_info:
        build_context_from_record(
            {"status": "error"}, ingestion_id="x", owner_id=uuid.uuid4()
        )
    assert exc_info.value.reason == "unknown_record"


def test_context_rejects_bad_mode(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    with pytest.raises(ContextError) as exc_info:
        build_context_from_record(
            record,
            ingestion_id=record["ingestion_id"],
            owner_id=owner,
            mode="general",
        )
    assert exc_info.value.reason == "bad_plan"


def test_context_caps_excerpts_and_message(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    record["context"]["excerpts"] = {
        f"f{i}.py": "x\n" * 5000 for i in range(30)
    }
    ctx = build_context_from_record(
        record,
        ingestion_id=record["ingestion_id"],
        owner_id=owner,
        message="?" * 5000,
    )
    assert len(ctx.excerpts) == 12
    assert all(len(text) <= 4000 for text in ctx.excerpts.values())
    assert len(ctx.message) == 2000


def test_worker_reconstruction_ignores_global_store(monkeypatch, project_dir):
    _stub_clone(monkeypatch, project_dir)
    record = ingest_repository("https://github.com/acme/calc")
    profile, excerpts = record["profile"], record["context"]["excerpts"]
    clear_ingestions()

    def fake_ingest(url, ref=None):
        assert ref == FAKE_SHA
        return {
            "status": "complete",
            "profile": profile,
            "context": {"excerpts": excerpts},
        }

    ctx = build_context_for_worker(
        repo_url="https://github.com/acme/calc",
        commit_sha=FAKE_SHA,
        investigation_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        ingest_fn=fake_ingest,
    )
    assert ctx.commit_sha == FAKE_SHA
    assert ctx.excerpts


def test_worker_rejects_sha_substitution():
    def fake_ingest(url, ref=None):
        return {
            "status": "complete",
            "profile": {
                "url": url, "name": "acme/calc", "commit_sha": "c" * 40,
                "capabilities": {"static": {"available": True}},
            },
            "context": {"excerpts": {}},
        }

    with pytest.raises(ContextError) as exc_info:
        build_context_for_worker(
            repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA,
            investigation_id=uuid.uuid4(),
            owner_id=uuid.uuid4(),
            ingest_fn=fake_ingest,
        )
    assert exc_info.value.reason == "sha_mismatch"


def test_worker_rejects_bad_sha_and_failed_ingest():
    with pytest.raises(ContextError) as exc_info:
        build_context_for_worker(
            repo_url="https://github.com/acme/calc",
            commit_sha="not-a-sha",
            investigation_id=uuid.uuid4(),
            owner_id=uuid.uuid4(),
            ingest_fn=lambda url, ref=None: {},
        )
    assert exc_info.value.reason == "bad_sha"

    def failed(url, ref=None):
        return {"status": "error", "reason": "clone_failed", "message": "gone"}

    with pytest.raises(ContextError) as exc_info:
        build_context_for_worker(
            repo_url="https://github.com/acme/calc",
            commit_sha=FAKE_SHA,
            investigation_id=uuid.uuid4(),
            owner_id=uuid.uuid4(),
            ingest_fn=failed,
        )
    assert exc_info.value.reason == "clone_failed"


# --- plan resolution ---


def test_plan_for_mode_debug_variants():
    runnable = {"runnable": {"python": True, "reason": None}, "test_frameworks": ["pytest"]}
    assert plan_for_mode("debug", runnable).plan_id == "debug"
    bare = {"runnable": {"python": False, "reason": "unsupported_framework"}, "test_frameworks": []}
    assert plan_for_mode("debug", bare).plan_id == "debug-static"
    assert plan_for_mode("explain", bare).plan_id == "explain"
    assert plan_for_mode("health", bare).plan_id == "health"


def test_plan_for_mode_rejects_general_unknown_and_unrunnable():
    caps: dict = {"runnable": {"python": False, "reason": "x"}, "test_frameworks": []}
    with pytest.raises(UnknownPlanError):
        plan_for_mode("general", caps)
    with pytest.raises(UnknownPlanError):
        plan_for_mode("bogus", caps)
    nonpython = {"runnable": {"python": False, "reason": "unsupported_project"}, "test_frameworks": []}
    with pytest.raises(UnsupportedProjectError):
        plan_for_mode("debug", nonpython)


def test_plan_validation():
    with pytest.raises(OrchestrationError):
        InvestigationPlan(plan_id="empty", mode="debug", stages=())
    dup = (
        StageDefinition(name="a", handler="h"),
        StageDefinition(name="a", handler="h"),
    )
    with pytest.raises(OrchestrationError):
        InvestigationPlan(plan_id="dup", mode="debug", stages=dup)
    bad = (StageDefinition(name="a", handler="missing"),)
    with pytest.raises(OrchestrationError):
        InvestigationPlan(plan_id="bad", mode="debug", stages=bad)


# --- stage runner ---


def _ctx(**overrides):
    params = {
        "investigation_id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "repo_url": "https://github.com/acme/calc",
        "repo_name": "acme/calc",
        "commit_sha": FAKE_SHA,
        "ref": None,
        "mode": "debug",
        "plan_id": "debug",
        "message": None,
        "max_attempts": 3,
        "profile": {},
        "capabilities": {"static": {"available": True}},
        "excerpts": {},
    }
    params.update(overrides)
    return InvestigationContext(**params)


def test_runner_executes_in_order_and_reports(monkeypatch):
    calls: list[str] = []

    def make(name):
        def _handle(ctx, progress, deps, scope):
            _ = scope
            calls.append(name)
            return StageResult(stage=name, status="ok")

        return _handle

    monkeypatch.setitem(HANDLERS, "first", make("first"))
    monkeypatch.setitem(HANDLERS, "second", make("second"))
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(
            StageDefinition(name="first", handler="first"),
            StageDefinition(name="second", handler="second"),
        ),
    )
    progress = _RecordingRecorder()
    result = run(plan, _ctx(), progress=progress)
    assert result.status == "complete"
    assert calls == ["first", "second"]
    assert [c[0] for c in progress.calls].count("started") == 2
    assert progress.calls[0] == ("set_stages", ("first", "second"))


def test_runner_takes_last_output_as_report(monkeypatch):
    monkeypatch.setitem(HANDLERS, "a", _ok_handler("a", {"v": 1}))
    monkeypatch.setitem(HANDLERS, "b", _ok_handler("b", {"v": 2}))
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(
            StageDefinition(name="a", handler="a"),
            StageDefinition(name="b", handler="b"),
        ),
    )
    result = run(plan, _ctx(), progress=_RecordingRecorder())
    assert result.status == "complete"
    assert result.report == {"v": 2}


def test_runner_skips_unmet_capabilities(monkeypatch):
    monkeypatch.setitem(HANDLERS, "need", _ok_handler("need"))
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="need", handler="need", requires=("runnable.python",)),),
    )
    ran = run(plan, _ctx(), progress=_RecordingRecorder())
    assert ran.status == "complete"
    assert ran.stages[0].status == "skipped"
    assert "runnable.python" in (ran.stages[0].detail or "")


def test_runner_failure_wins_and_skips_rest(monkeypatch):
    def bad(ctx, progress, deps, scope):
        _ = scope
        progress.record_run("baseline", {})
        return StageResult(stage="bad", status="failed", detail="boom")

    def never(ctx, progress, deps, scope):  # pragma: no cover - must not run
        raise AssertionError("skipped stage ran")

    monkeypatch.setitem(HANDLERS, "bad", bad)
    monkeypatch.setitem(HANDLERS, "never", never)
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(
            StageDefinition(name="bad", handler="bad"),
            StageDefinition(name="never", handler="never"),
        ),
    )
    progress = _RecordingRecorder()
    result = run(plan, _ctx(), progress=progress)
    assert result.status == "failed"
    assert [s.status for s in result.stages] == ["failed", "skipped"]
    assert result.error == {"reason": "stage_failed", "message": "boom"}
    assert ("run", "baseline") in progress.calls
    assert ("completed", "bad", []) not in progress.calls


def test_runner_converts_exceptions(monkeypatch):
    def boom(ctx, progress, deps, scope):
        raise ValueError("kaboom")

    monkeypatch.setitem(HANDLERS, "boom", boom)
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="boom", handler="boom"),),
    )
    result = run(plan, _ctx(), progress=_RecordingRecorder())
    assert result.status == "failed"
    assert "ValueError: kaboom" in (result.error or {}).get("message", "")


def test_runner_rejects_non_result_handlers(monkeypatch):
    monkeypatch.setitem(HANDLERS, "bad", lambda ctx, p, d, s: {"not": "a result"})
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="bad", handler="bad"),),
    )
    result = run(plan, _ctx(), progress=_RecordingRecorder())
    assert result.status == "failed"
    assert "no StageResult" in (result.error or {}).get("message", "")


def test_runner_cancellation_between_stages(monkeypatch):
    calls: list[str] = []

    def make(name):
        def _handle(ctx, progress, deps, scope):
            _ = scope
            calls.append(name)
            return StageResult(stage=name, status="ok")

        return _handle

    monkeypatch.setitem(HANDLERS, "one", make("one"))
    monkeypatch.setitem(HANDLERS, "two", make("two"))
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(
            StageDefinition(name="one", handler="one"),
            StageDefinition(name="two", handler="two"),
        ),
    )
    progress = _RecordingRecorder()
    progress.cancel_on_check(2)
    result = run(plan, _ctx(), progress=progress)
    assert result.status == "cancelled"
    assert calls == ["one"]
    assert [s.status for s in result.stages] == ["ok", "skipped"]


def test_runner_recorder_failure_is_failed_not_success(monkeypatch):
    monkeypatch.setitem(HANDLERS, "a", _ok_handler("a"))

    class Broken(_RecordingRecorder):
        def stage_completed(self, stage, data=None):
            raise RuntimeError("db is down")

    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="a", handler="a"),),
    )
    result = run(plan, _ctx(), progress=Broken())
    assert result.status == "failed"
    assert "Recorder error" in (result.error or {}).get("message", "")


def test_runner_passes_through_self_managed_progress(monkeypatch):
    seen: list[str] = []

    def coarse(ctx, progress, deps, scope):
        _ = scope
        seen.append("ran")
        progress.set_stages(("inner1", "inner2"))
        progress.stage_started("inner1")
        progress.stage_completed("inner1")
        return StageResult(stage="execute", status="ok")

    monkeypatch.setitem(HANDLERS, "coarse", coarse)
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="execute", handler="coarse", manages_progress=True),),
    )
    progress = _RecordingRecorder()
    result = run(plan, _ctx(), progress=progress)
    assert result.status == "complete"
    assert seen == ["ran"]
    kinds = [c[0] for c in progress.calls]
    assert "set_stages" in kinds
    # Runner adds no wrapper boundaries of its own.
    assert ("started", "execute") not in progress.calls
    assert [c for c in progress.calls if c[0] == "completed" and c[1] == "execute"] == []


def test_null_recorder_is_silent():
    progress = NullRecorder()
    progress.set_stages(("a",))
    progress.stage_started("a")
    progress.stage_completed("a", {"x": 1})
    assert progress.record_run("baseline", {}) is None
    assert progress.record_attempt(1) is None
    assert progress.record_finding() is None
    assert progress.record_patch() is None
    assert progress.record_message(role="user", content="hi") is None
    progress.record_partial({"x": 1})
    progress.check_cancelled()


# --- safety boundaries ---


def test_static_plans_ignore_execution(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)

    def exploding_backend(*args, **kwargs):
        raise AssertionError("static plan touched execution")

    for mode in ("explain", "health"):
        ctx = build_context_from_record(
            record, ingestion_id=record["ingestion_id"], owner_id=owner, mode=mode
        )
        result = run(
            PLANS[mode], ctx,
            progress=NullRecorder(),
            deps=HandlerDeps(backend=exploding_backend, summarize=lambda **k: "ok"),
        )
        assert result.status == "complete", mode
        assert result.report is not None


def test_orchestrate_imports_no_execution_machinery():
    namespace = vars(orch)
    for name in (
        "ExecutionBackend", "DockerBackend", "UnsafeLocalBackend",
        "select_backend", "clone_and_pin", "subprocess", "plan_install",
    ):
        assert name not in namespace, name


def test_demo_and_orchestrator_stay_decoupled():
    root = Path(orch.__file__).parent
    orchestrate_src = (root / "orchestrate.py").read_text(encoding="utf-8")
    assert "orchestrator" not in orchestrate_src
    demo_src = (root / "orchestrator.py").read_text(encoding="utf-8")
    assert "orchestrate" not in demo_src


# --- progress compatibility: pipelines under the runner ---


class _FakeBackend:
    name = "fake-test"
    isolation = "fake-test-only"

    def __init__(self, script):
        self.script = list(script)

    def needs_pytest_install(self):
        return False

    def run(self, request):
        from app.execution import ExecutionResult

        outcome = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return ExecutionResult(
            backend=self.name, isolation=self.isolation,
            status=outcome.get("status", "ok"),
            passed=outcome.get("passed"), exit_code=outcome.get("exit_code", 0),
            output=outcome.get("output", ""), duration_ms=3,
            failures=outcome.get("failures", []),
            install_ok=outcome.get("install_ok"),
            install_log=outcome.get("install_log", ""),
        )


def _fix_diagnose(**kwargs):
    from app.patch_guard import FileEdit, PatchSet

    return "add subtracts.", PatchSet(
        edits=(FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),),
        reason="Fix the operator.",
    )


def _scripted():
    return [
        {
            "status": "test_failure", "passed": False, "exit_code": 1,
            "output": "FAILED tests/test_calc.py::test_add",
            "failures": [
                {
                    "test": "tests/test_calc.py::test_add", "kind": "failed",
                    "message": "assert", "traceback": "E assert",
                }
            ],
        },
        {"status": "ok", "passed": True, "exit_code": 0, "output": "1 passed", "failures": []},
    ]


def test_runner_preserves_debug_granular_progress(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    direct = _RecordingRecorder()
    direct_result = pipeline.investigate_repository(
        record["ingestion_id"], backend=_FakeBackend(_scripted()),
        diagnose=_fix_diagnose, progress=direct,
    )
    assert direct_result["status"] == "complete"

    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug"
    )
    assert ctx.plan_id == "debug"
    wrapped = _RecordingRecorder()
    outcome = run(
        PLANS[ctx.plan_id], ctx, progress=wrapped,
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose),
    )
    assert outcome.status == "complete"
    assert outcome.report["verdict"] == "TESTS_PASSED"
    assert wrapped.calls == direct.calls
    assert not [c for c in wrapped.calls if len(c) > 1 and c[1] == "execute"]


def test_runner_preserves_static_granular_progress(monkeypatch, tmp_path, owner):
    odd = tmp_path / "odd"
    (odd / "pkg").mkdir(parents=True)
    (odd / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (odd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (odd / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    record = _owned_record(monkeypatch, odd, owner, url="https://github.com/acme/odd")
    direct = _RecordingRecorder()
    assert (
        pipeline.investigate_repository(
            record["ingestion_id"], backend=_FakeBackend([]),
            diagnose=_fix_diagnose, progress=direct,
        )["status"]
        == "complete"
    )
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug"
    )
    assert ctx.plan_id == "debug-static"
    wrapped = _RecordingRecorder()
    outcome = run(
        PLANS[ctx.plan_id], ctx, progress=wrapped,
        deps=HandlerDeps(diagnose=_fix_diagnose),
    )
    assert outcome.status == "complete"
    assert wrapped.calls == direct.calls


def test_runner_preserves_explain_health_progress(monkeypatch, project_dir, owner):
    from app.explain import explain_repository
    from app.health import assess_repository

    record = _owned_record(monkeypatch, project_dir, owner)
    for mode, fn in (("explain", explain_repository), ("health", assess_repository)):
        direct = _RecordingRecorder()
        assert (
            fn(record["ingestion_id"], summarize=lambda **k: "x", progress=direct)[
                "status"
            ]
            == "complete"
        )
        ctx = build_context_from_record(
            record, ingestion_id=record["ingestion_id"], owner_id=owner, mode=mode
        )
        wrapped = _RecordingRecorder()
        outcome = run(
            PLANS[mode], ctx, progress=wrapped,
            deps=HandlerDeps(summarize=lambda **k: "x"),
        )
        assert outcome.status == "complete"
        assert wrapped.calls == direct.calls, mode


def test_runner_db_progress_matches_direct(db, factory, owner, monkeypatch, project_dir):
    from app.health import assess_repository

    record = _owned_record(monkeypatch, project_dir, owner)
    direct_inv, direct_job = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="health",
    )
    direct_recorder = DBRecorder(
        factory, investigation_id=direct_inv.id, job_id=direct_job.id
    )
    assert (
        assess_repository(
            record["ingestion_id"], summarize=lambda **k: "x", progress=direct_recorder
        )["status"]
        == "complete"
    )
    db.expire_all()
    direct_row = db.get(Investigation, direct_inv.id)

    wrapped_inv, wrapped_job = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="health",
    )
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="health",
        investigation_id=wrapped_inv.id,
    )
    wrapped_recorder = DBRecorder(
        factory, investigation_id=wrapped_inv.id, job_id=wrapped_job.id
    )
    outcome = run(
        PLANS["health"], ctx, progress=wrapped_recorder,
        deps=HandlerDeps(summarize=lambda **k: "x"),
    )
    assert outcome.status == "complete"
    db.expire_all()
    wrapped_row = db.get(Investigation, wrapped_inv.id)
    assert wrapped_row.stages_completed == direct_row.stages_completed
    assert wrapped_row.progress_pct == direct_row.progress_pct
    assert "execute" not in wrapped_row.stages_completed


# --- derived attempt status ---


def _attempt(**overrides):
    params = {
        "investigation_id": uuid.uuid4(), "attempt_no": 1,
        "diagnosis": "d", "confidence": "hypothesis",
        "patch": None, "guard": None, "verification": None, "timings": None,
    }
    params.update(overrides)
    return InvestigationAttempt(**params)


def test_attempt_status_summary():
    assert attempt_status_summary(_attempt()) == "proposed"
    assert attempt_status_summary(_attempt(verification={"passed": True})) == "verified"
    assert attempt_status_summary(_attempt(verification={"passed": False})) == "failed"
    assert (
        attempt_status_summary(
            _attempt(patch={"applied": ["a.py"], "edits": []})
        )
        == "applied"
    )


def test_detail_serializer_carries_derived_status(db, owner):
    investigation, _ = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="debug",
    )
    db.add(
        InvestigationAttempt(
            investigation_id=investigation.id, attempt_no=1,
            diagnosis="d", confidence="confirmed",
            patch={"applied": True}, guard={"validated": True},
            verification={"passed": True},
        )
    )
    db.commit()
    detail = investigation_detail(db, db.get(Investigation, investigation.id))
    assert detail["attempts"][0]["status"] == "verified"


# --- migrated plans (Task 3) ---


def test_migrated_plans_have_explicit_runner_owned_stages():
    expected = {
        "debug": (
            ("materialize", "debug_materialize"),
            ("baseline", "debug_baseline"),
            ("repair", "debug_repair"),
        ),
        "debug-static": (
            ("evidence", "static_evidence"),
            ("diagnose", "static_diagnose"),
        ),
        "explain": (
            ("context", "explain_context"),
            ("summarize", "explain_summarize"),
        ),
        "health": (
            ("checks", "health_checks"),
            ("summarize", "health_summarize"),
        ),
    }
    assert set(PLANS) == set(expected)
    for plan_id, stages in expected.items():
        plan = PLANS[plan_id]
        assert [(s.name, s.handler) for s in plan.stages] == list(stages)
        assert all(not s.manages_progress for s in plan.stages), plan_id
        assert all(not s.requires for s in plan.stages), plan_id


def test_production_handlers_resolve_lazily(monkeypatch):
    from app.orchestrate import HANDLER_PATHS, _resolve_handler

    assert set(HANDLER_PATHS) == {
        "debug_materialize", "debug_baseline", "debug_repair",
        "static_evidence", "static_diagnose",
        "explain_context", "explain_summarize",
        "health_checks", "health_summarize",
    }
    # A directly registered handler wins over the lazy path (test seam).
    marker = _ok_handler("marker")
    monkeypatch.setitem(HANDLERS, "explain_context", marker)
    assert _resolve_handler("explain_context") is marker
    assert _resolve_handler("no_such_handler") is None
    resolved = _resolve_handler("health_checks")
    assert callable(resolved)
    assert HANDLERS["health_checks"] is resolved


def test_runner_marks_interrupted_stage_cancelled(monkeypatch):
    def cancel_midway(ctx, progress, deps, scope):
        _ = (deps, scope)
        progress.record_run("baseline", {})
        raise JobCancelled("stop now")

    def never(ctx, progress, deps, scope):  # pragma: no cover - must not run
        raise AssertionError("skipped stage ran")

    monkeypatch.setitem(HANDLERS, "first", cancel_midway)
    monkeypatch.setitem(HANDLERS, "second", never)
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(
            StageDefinition(name="first", handler="first"),
            StageDefinition(name="second", handler="second"),
        ),
    )
    progress = _RecordingRecorder()
    result = run(plan, _ctx(), progress=progress)
    assert result.status == "cancelled"
    assert [s.status for s in result.stages] == ["cancelled", "skipped"]
    assert result.report is None
    # The interrupted stage kept its data record but was never completed.
    assert ("run", "baseline") in progress.calls
    assert ("completed", "first", []) not in progress.calls


def test_runner_cancel_before_start_skips_everything(monkeypatch):
    monkeypatch.setitem(HANDLERS, "a", _ok_handler("a"))
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="a", handler="a"),),
    )
    progress = _RecordingRecorder()
    progress.cancel_on_check(1)
    result = run(plan, _ctx(), progress=progress)
    assert result.status == "cancelled"
    assert [s.status for s in result.stages] == ["skipped"]


def test_failed_reason_flows_to_result_error(monkeypatch):
    def bad(ctx, progress, deps, scope):
        _ = (progress, deps, scope)
        return StageResult(
            stage="bad", status="failed", reason="clone_failed",
            detail="clone went away",
            output={"legacy_error": {"status": "error", "reason": "clone_failed"}},
        )

    monkeypatch.setitem(HANDLERS, "bad", bad)
    plan = InvestigationPlan(
        plan_id="t", mode="debug",
        stages=(StageDefinition(name="bad", handler="bad"),),
    )
    result = run(plan, _ctx(), progress=_RecordingRecorder())
    assert result.status == "failed"
    assert result.error == {"reason": "clone_failed", "message": "clone went away"}
    assert legacy_error_of(result) == {
        "status": "error", "reason": "clone_failed",
    }


def test_legacy_error_of_ignores_non_failed_stages():
    result = orch.OrchestrationResult(
        status="complete",
        stages=[StageResult(stage="a", status="ok", output={"legacy_error": {}})],
        report={},
    )
    assert legacy_error_of(result) is None


def test_execute_rejects_unknown_plan_id():
    ctx = _ctx(plan_id="nope")
    result = execute(ctx, progress=NullRecorder())
    assert result.status == "failed"
    assert result.stages == []
    assert (result.error or {}).get("reason") == "bad_plan"


def test_execute_runs_ctx_plan(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="explain",
    )
    result = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(summarize=lambda **k: "static summary"),
    )
    assert result.status == "complete"
    assert result.report is not None
    assert result.report["mode"] == "explain"
    assert [s.stage for s in result.stages] == ["context", "summarize"]
    assert [s.status for s in result.stages] == ["ok", "ok"]


def test_context_carries_static_file_lists(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="health",
    )
    for key in ("entry_points", "configs", "tests", "docs"):
        stored = [p for p in record["context"].get(key, []) if isinstance(p, str)]
        assert ctx.file_lists.get(key, []) == stored, key
    assert set(ctx.file_lists) <= {"entry_points", "configs", "tests", "docs"}


def test_migrated_path_writes_no_bridge_records(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug",
    )
    outcome = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose),
    )
    assert outcome.status == "complete"
    assert outcome.report["verdict"] == "TESTS_PASSED"
    assert [key for key in ingestion_module._store if key.startswith("ctx-")] == []


def test_scope_workspace_removed_on_success_and_failure(monkeypatch, project_dir, owner):
    import tempfile

    record = _owned_record(monkeypatch, project_dir, owner)
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug",
    )
    outcome = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose),
    )
    assert outcome.status == "complete"
    assert created, "materialize must create the scope workspace"
    assert all(not Path(path).exists() for path in created)

    failing = _FakeBackend([{"status": "install_error", "install_log": "pip blew up"}])
    failed = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=failing, diagnose=_fix_diagnose),
    )
    assert failed.status == "failed"
    assert all(not Path(path).exists() for path in created)


def test_handler_recorder_failure_is_failed_not_success(
    monkeypatch, project_dir, owner
):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug"
    )

    class BrokenRun(_RecordingRecorder):
        def record_run(self, kind, result, attempt_id=None):
            raise RuntimeError("db is down")

    result = run(
        PLANS["debug"], ctx, progress=BrokenRun(),
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose),
    )
    assert result.status == "failed"
    assert result.report is None
    assert [s.status for s in result.stages] == ["ok", "failed", "skipped"]
    assert "RuntimeError" in (result.error or {}).get("message", "")


def test_partial_records_survive_later_failure(
    db, factory, owner, monkeypatch, project_dir
):
    record = _owned_record(monkeypatch, project_dir, owner)
    investigation, job = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="debug",
    )
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug",
        investigation_id=investigation.id,
    )
    recorder = DBRecorder(
        factory, investigation_id=investigation.id, job_id=job.id
    )

    def rate_limited(**kwargs):
        raise MetaRateLimitError("slow down")

    result = run(
        PLANS["debug"], ctx, progress=recorder,
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=rate_limited),
    )
    assert result.status == "failed"
    assert result.report is None
    assert legacy_error_of(result)["reason"] == "rate_limit"
    db.expire_all()
    row = db.get(Investigation, investigation.id)
    assert row.stages_completed == ["materialize", "baseline"]
    kinds = [
        run.kind for run in db.scalars(
            select(ExecutionRun).where(
                ExecutionRun.investigation_id == investigation.id
            )
        ).all()
    ]
    assert kinds == ["baseline"]


def test_debug_static_plan_never_touches_execution(monkeypatch, tmp_path, owner):
    odd = tmp_path / "odd"
    (odd / "pkg").mkdir(parents=True)
    (odd / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (odd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (odd / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    record = _owned_record(monkeypatch, odd, owner, url="https://github.com/acme/odd")

    def exploding(*args, **kwargs):
        raise AssertionError("static plan touched execution")

    monkeypatch.setattr(pipeline, "select_backend", exploding)
    monkeypatch.setattr(pipeline, "clone_and_pin", exploding)
    ctx = build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner, mode="debug"
    )
    assert ctx.plan_id == "debug-static"
    result = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=exploding, diagnose=_fix_diagnose),
    )
    assert result.status == "complete"
    assert result.report is not None
    assert result.report["execution"] == {"backend": "static-only", "isolation": "no-execution"}
