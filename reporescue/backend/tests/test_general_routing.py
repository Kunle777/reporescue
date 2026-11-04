"""Tests for General-mode routing (MVP P1).

Covers objective classification, nested strategy execution through the
existing plans, honest failure when no objective or no applicable plan
exists, route persistence, worker effective-mode mapping, and the
ingestion liveness hook. Stubbed clones, fake backends, stubbed model
calls: no network, no Docker, no Modal.
"""

import inspect
import shutil
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import ingestion as ingestion_module
from app import real_investigation as pipeline
from app.general import route_objective
from app.ingestion import (
    clear_ingestions,
    ingest_repository,
    set_ingestion_owner,
)
from app.jobqueue import create_investigation
from app.models import Base, Investigation
from app.orchestrate import (
    PLANS,
    HandlerDeps,
    NullRecorder,
    build_context_for_worker,
    build_context_from_record,
    execute,
)
from app.recorder import DBRecorder
from app.worker import _effective_mode

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
        f"sqlite:///{tmp_path.as_posix()}/general.db",
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
    from app.models import Profile

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


class _ExplodingBackend:
    name = "exploding-test"
    isolation = "exploding-test-only"

    def needs_pytest_install(self):
        return False

    def run(self, request):
        raise AssertionError("execution backend must not be used")


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


def _general_ctx(record, owner, message):
    return build_context_from_record(
        record, ingestion_id=record["ingestion_id"], owner_id=owner,
        mode="general", message=message,
    )


# --- objective classification ---


def test_routes_failing_test_to_debug():
    routing = route_objective("tests are failing with an assertion error, please fix the bug")
    assert routing["selected_mode"] == "debug"
    assert routing["confidence"] in ("high", "medium")
    assert routing["signals"]["debug"]
    assert "debug" in routing["rationale"]


def test_routes_structure_question_to_explain():
    routing = route_objective("how is this repository structured? explain the architecture")
    assert routing["selected_mode"] == "explain"
    assert routing["signals"]["explain"]


def test_routes_assessment_to_health():
    routing = route_objective("assess code quality, security issues and dependency health")
    assert routing["selected_mode"] == "health"
    assert routing["signals"]["health"]


def test_empty_objective_is_unroutable():
    for objective in (None, "", "   "):
        routing = route_objective(objective)
        assert routing["selected_mode"] is None
        assert routing["reason"] == "needs_objective"
        assert routing["confidence"] == "none"


def test_ambiguous_objective_falls_back_to_health_honestly():
    routing = route_objective("look at this repo")
    assert routing["selected_mode"] == "health"
    assert routing["confidence"] == "low"
    assert "broad repository health assessment" in routing["rationale"]


def test_tie_breaks_debug_over_explain_with_low_confidence():
    routing = route_objective("fix and explain this")
    assert routing["selected_mode"] == "debug"
    assert routing["confidence"] == "low"
    assert "tie-break" in routing["rationale"]


def test_capabilities_never_override_objective():
    routing = route_objective(
        "the build is broken, fix the failing tests",
        {"runnable": {"python": False}, "test_frameworks": []},
    )
    assert routing["selected_mode"] == "debug"


# --- nested strategy execution ---


def test_general_debug_executes_full_pipeline(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = _general_ctx(record, owner, "tests are failing, please fix the bug")
    assert ctx.plan_id == "general"

    outcome = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose),
    )
    assert outcome.status == "complete"
    report = outcome.report
    assert report["requested_mode"] == "general"
    assert report["selected_mode"] == "debug"
    assert report["routing"]["selected_mode"] == "debug"
    assert report["verdict"] == "TESTS_PASSED"
    assert report["summary"].startswith("General routed this investigation to debug:")


def test_general_explain_runs_without_execution(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = _general_ctx(record, owner, "explain the architecture of this repo")

    outcome = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=_ExplodingBackend(), summarize=lambda **k: "ok"),
    )
    assert outcome.status == "complete"
    report = outcome.report
    assert report["requested_mode"] == "general"
    assert report["selected_mode"] == "explain"
    assert "General routed this investigation to explain:" in report["summary"]
    assert "verdict" not in report
    assert any("no code was executed" in lim for lim in report["limitations"])


def test_general_health_runs_without_execution(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = _general_ctx(record, owner, "review code quality and security")
    assert ctx.plan_id == "general"

    outcome = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=_ExplodingBackend(), summarize=lambda **k: "ok"),
    )
    assert outcome.status == "complete"
    assert outcome.report["selected_mode"] == "health"
    assert "General routed this investigation to health:" in outcome.report["summary"]
    assert any("no code was executed" in lim for lim in outcome.report["limitations"])


def test_general_without_objective_fails_honestly(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = _general_ctx(record, owner, None)

    outcome = execute(ctx, progress=NullRecorder(), deps=HandlerDeps())
    assert outcome.status == "failed"
    assert outcome.error["reason"] == "needs_objective"


def test_inapplicable_strategy_fails_without_silent_switch(monkeypatch, project_dir, owner):
    record = _owned_record(monkeypatch, project_dir, owner)
    ctx = _general_ctx(record, owner, "fix the failing tests")
    ctx = replace(
        ctx,
        capabilities={"runnable": {"reason": "unsupported_project"}, "test_frameworks": []},
    )

    outcome = execute(
        ctx, progress=NullRecorder(),
        deps=HandlerDeps(backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose),
    )
    assert outcome.status == "failed"
    assert outcome.error["reason"] == "unsupported_project"


# --- persistence ---


def test_record_routing_persists_selection(db, factory, owner):
    investigation, job = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="general",
        user_message="fix the bug",
    )
    db.commit()
    routing = route_objective("fix the bug")

    recorder = DBRecorder(
        factory, investigation_id=investigation.id, job_id=job.id
    )
    recorder.record_routing("debug", routing)
    db.expire_all()

    row = db.get(Investigation, investigation.id)
    assert row.mode == "general"
    assert row.selected_mode == "debug"
    assert row.routing["selected_mode"] == "debug"


def test_note_progress_records_liveness_without_moving_stage(db, factory, owner):
    investigation, job = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="debug",
    )
    db.commit()
    recorder = DBRecorder(
        factory, investigation_id=investigation.id, job_id=job.id
    )
    recorder.stage_started("materialize")
    recorder.note_progress("materialize: cloning repository")
    db.expire_all()

    row = db.get(Investigation, investigation.id)
    assert row.partial["stage_detail"] == "materialize: cloning repository"
    assert row.stage == "materialize"
    assert (row.stages_completed or []) == []
    assert row.progress_pct == 0


# --- worker end to end ---


def test_worker_runs_general_job_end_to_end(monkeypatch, project_dir, db, factory, owner):
    from app.jobqueue import claim_next_job, investigation_detail
    from app.models import Job
    from app.worker import run_job

    _stub_clone(monkeypatch, project_dir)
    investigation, job = create_investigation(
        db, owner_id=owner, repo_url="https://github.com/acme/calc",
        repo_name="acme/calc", commit_sha=FAKE_SHA, mode="general",
        user_message="tests are failing, please fix the bug",
    )
    db.commit()
    claimed = claim_next_job(db, "test-worker")
    assert claimed is not None and claimed.id == job.id

    status = run_job(
        factory, job.id, "test-worker",
        backend=_FakeBackend(_scripted()), diagnose=_fix_diagnose,
        summarize=lambda **k: "ok",
    )
    assert status == "completed"
    db.expire_all()

    row = db.get(Investigation, investigation.id)
    assert db.get(Job, job.id).status == "completed"
    assert row.mode == "general"
    assert row.selected_mode == "debug"
    assert row.routing["selected_mode"] == "debug"
    assert row.final_report["verdict"] == "TESTS_PASSED"
    assert row.final_report["summary"].startswith(
        "General routed this investigation to debug:"
    )
    detail = investigation_detail(db, row)
    assert detail["selected_mode"] == "debug"
    assert detail["routing"]["selected_mode"] == "debug"


# --- worker effective mode ---


def test_effective_mode_prefers_report_routing():
    investigation = SimpleNamespace(mode="general", selected_mode=None)
    report = {"routing": {"selected_mode": "debug"}}
    assert _effective_mode(investigation, report) == "debug"


def test_effective_mode_falls_back_to_row_selection():
    investigation = SimpleNamespace(mode="general", selected_mode="explain")
    assert _effective_mode(investigation, {}) == "explain"


def test_effective_mode_keeps_direct_modes():
    assert _effective_mode(SimpleNamespace(mode="debug", selected_mode=None), {}) == "debug"
    assert _effective_mode(SimpleNamespace(mode="general", selected_mode=None), {}) == "general"


# --- ingestion liveness hook ---


def test_build_context_for_worker_skips_phases_for_stub_ingest(owner):
    seen = {}

    def fake_ingest(url, ref=None, **kwargs):
        seen["kwargs"] = dict(kwargs)
        return {
            "status": "complete",
            "profile": {
                "url": "https://github.com/acme/calc",
                "name": "acme/calc",
                "commit_sha": FAKE_SHA,
                "capabilities": {
                    "runnable": {"python": True},
                    "test_frameworks": ["pytest"],
                    "static": {"available": True},
                },
            },
            "context": {"excerpts": {}},
        }

    ctx = build_context_for_worker(
        repo_url="https://github.com/acme/calc", commit_sha=FAKE_SHA,
        investigation_id=uuid.uuid4(), owner_id=owner, mode="general",
        message="fix it", ingest_fn=fake_ingest, on_phase=seen.setdefault,
    )
    assert ctx.mode == "general"
    assert "on_phase" not in seen["kwargs"]


def test_ingest_repository_accepts_phase_callback():
    assert "on_phase" in inspect.signature(ingest_repository).parameters
