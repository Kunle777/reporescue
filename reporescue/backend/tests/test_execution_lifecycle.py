"""Lifecycle tests for cancellation, verification, evidence, and jobs.

Covers the Phase 5B execution-lifecycle guarantees: cancellation is
never mistaken for failure or success, fresh-install backends verify
independently, reports carry evidence-class labels, Explain stays
independent of cloud execution, new configuration is validated, and
multi-worker claiming survives races beyond the original 2-thread test.
"""

import shutil
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import config as config_module
from app import execution as execution_module
from app import explain as explain_module
from app import ingestion as ingestion_module
from app import modal_backend as modal_backend_module
from app import real_investigation as pipeline
from app.config import ConfigError
from app.execution import (
    DockerBackend,
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    UnsafeLocalBackend,
)
from app.explain import explain_repository
from app.ingestion import clear_ingestions, ingest_repository
from app.jobqueue import (
    cancel_job,
    claim_next_job,
    complete_job,
    create_investigation,
    fail_job,
    heartbeat,
    recover_stale_jobs,
    utcnow,
)
from app.models import Base, Investigation, Job, Profile
from app.patch_guard import FileEdit, PatchSet
from app.recorder import JobCancelled
from app.verdict import REAL_REPORT_KEYS, build_real_report

FAKE_SHA = "e" * 40

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


def _make_project(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _ingest_copy(monkeypatch, source, url="https://github.com/acme/calc"):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(source, destination)
        return FAKE_SHA

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    return ingest_repository(url)


def _materialize_copy(monkeypatch, source):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(source, destination)
        return FAKE_SHA

    monkeypatch.setattr(pipeline, "clone_and_pin", fake_clone)


class ScriptedBackend(ExecutionBackend):
    name = "scripted-test"
    isolation = "scripted-test-only"

    def __init__(self, script, *, fresh_install=False):
        self.script = list(script)
        self.requests = []
        self.requires_fresh_install = fresh_install

    def is_available(self):
        return True, "scripted"

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


def _fix_diagnose(**kwargs):
    return "Diagnosis.", PatchSet(
        edits=(FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),),
        reason="Fix the operator.",
    )


def _explode(name):
    def _raiser(*args, **kwargs):
        raise AssertionError(f"{name} must never be called here")

    return _raiser


# --- backend cancellation boundaries ---


def test_docker_cancelled_before_setup_runs_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        DockerBackend, "is_available", lambda self: (True, "up")
    )
    monkeypatch.setattr(
        DockerBackend, "_run_container", _explode("container run")
    )
    cancel = threading.Event()
    cancel.set()
    result = DockerBackend().run(
        ExecutionRequest(
            snapshot=tmp_path,
            install_commands=[["pip", "install", "pytest"]],
            cancel=cancel,
        )
    )
    assert result.status == "cancelled"
    assert result.reason == "cancelled"
    assert result.passed is None
    assert "before setup" in result.output


def test_docker_cancelled_between_setup_and_test(monkeypatch, tmp_path):
    monkeypatch.setattr(
        DockerBackend, "is_available", lambda self: (True, "up")
    )
    calls = []

    def _fake_container(self, args, timeout, name):
        calls.append(args)
        return 0, "ok", "ok"

    monkeypatch.setattr(DockerBackend, "_run_container", _fake_container)
    state = {"calls": 0}

    def _cancel_on_second_probe():
        state["calls"] += 1
        return state["calls"] >= 2

    result = DockerBackend().run(
        ExecutionRequest(snapshot=tmp_path, cancel=_cancel_on_second_probe)
    )
    assert result.status == "cancelled"
    assert result.reason == "cancelled"
    assert calls == []


def test_unsafe_local_cancelled_before_venv(monkeypatch, tmp_path):
    monkeypatch.setenv("REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION", "1")
    cancel = threading.Event()
    cancel.set()
    result = UnsafeLocalBackend().run(
        ExecutionRequest(
            snapshot=tmp_path,
            install_commands=[["pip", "install", "pytest"]],
            cancel=cancel,
        )
    )
    assert result.status == "cancelled"
    assert result.reason == "cancelled"
    assert not (tmp_path / ".execvenv").exists()


# --- pipeline cancellation and fresh verification ---


def test_pipeline_cancelled_baseline_raises_not_error(monkeypatch, tmp_path):
    source = _make_project(tmp_path / "source", CALC_PROJECT)
    record = _ingest_copy(monkeypatch, source)
    _materialize_copy(monkeypatch, source)
    backend = ScriptedBackend([{"status": "cancelled"}])
    with pytest.raises(JobCancelled):
        pipeline.investigate_repository(
            record["ingestion_id"], backend=backend, diagnose=_explode("diagnose")
        )
    assert len(backend.requests) == 1
    assert backend.requests[0].commit_sha == FAKE_SHA


def test_pipeline_cancelled_verification_raises(monkeypatch, tmp_path):
    source = _make_project(tmp_path / "source", CALC_PROJECT)
    record = _ingest_copy(monkeypatch, source)
    _materialize_copy(monkeypatch, source)
    backend = ScriptedBackend(
        [
            {
                "status": "test_failure",
                "passed": False,
                "exit_code": 1,
                "output": "FAILED tests/test_calc.py::test_add",
                "failures": [{"test": "tests/test_calc.py::test_add"}],
            },
            {"status": "cancelled"},
        ]
    )
    with pytest.raises(JobCancelled):
        pipeline.investigate_repository(
            record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
        )
    assert len(backend.requests) == 2


def test_pipeline_fresh_install_backend_verifies_independently(
    monkeypatch, tmp_path
):
    source = _make_project(tmp_path / "source", CALC_PROJECT)
    record = _ingest_copy(monkeypatch, source)
    _materialize_copy(monkeypatch, source)
    backend = ScriptedBackend(
        [
            {
                "status": "test_failure",
                "passed": False,
                "exit_code": 1,
                "output": "FAILED tests/test_calc.py::test_add",
                "failures": [{"test": "tests/test_calc.py::test_add"}],
            },
            {
                "status": "ok",
                "passed": True,
                "exit_code": 0,
                "output": "1 passed",
            },
        ],
        fresh_install=True,
    )
    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )
    assert result["status"] == "complete"
    assert result["verdict"] == "TESTS_PASSED"
    assert result["outcome"] == "verified_fix"
    assert backend.requests[0].install_commands == [["pip", "install", "pytest"]]
    assert backend.requests[1].install_commands == [["pip", "install", "pytest"]]
    assert backend.requests[1].snapshot != backend.requests[0].snapshot


def test_pipeline_reuse_backend_keeps_venv_reuse(monkeypatch, tmp_path):
    source = _make_project(tmp_path / "source", CALC_PROJECT)
    record = _ingest_copy(monkeypatch, source)
    _materialize_copy(monkeypatch, source)
    backend = ScriptedBackend(
        [
            {
                "status": "test_failure",
                "passed": False,
                "exit_code": 1,
                "output": "FAILED tests/test_calc.py::test_add",
                "failures": [{"test": "tests/test_calc.py::test_add"}],
            },
            {"status": "ok", "passed": True, "exit_code": 0, "output": "1 passed"},
        ]
    )
    pipeline.investigate_repository(
        record["ingestion_id"], backend=backend, diagnose=_fix_diagnose
    )
    assert backend.requests[1].install_commands == []


# --- report evidence labels ---


def _run(passed):
    return {
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "output": "",
        "duration_ms": 1,
    }


def _report(baseline_passed, verification_passed):
    return build_real_report(
        repository="acme/calc",
        commit_sha="a" * 40,
        mode="real-repo-meta",
        diagnosis="d",
        baseline=_run(baseline_passed),
        verification=_run(verification_passed),
        patch={"edits": [], "reason": ""},
        guard={"validated": True, "message": "ok"},
        execution={"backend": "b", "isolation": "i"},
        attempts=1,
    )


def test_report_outcome_labels_evidence_class():
    assert _report(False, True)["outcome"] == "verified_fix"
    assert _report(False, False)["outcome"] == "reproduced_unfixed"
    assert _report(True, True)["outcome"] == "already_green"
    static = build_real_report(
        repository="acme/calc",
        commit_sha="a" * 40,
        mode="real-repo-meta",
        diagnosis="d",
        baseline={"passed": None, "status": "unavailable"},
        verification={"passed": None, "status": "unavailable"},
        patch={"edits": [], "reason": ""},
        guard={"validated": False, "message": "static"},
        execution={"backend": "static-only", "isolation": "no-execution"},
        attempts=1,
    )
    assert static["outcome"] == "not_executed"
    assert static["diagnosis_confidence"] == "hypothesis"
    assert "outcome" in REAL_REPORT_KEYS
    assert set(_report(False, True)) == set(REAL_REPORT_KEYS)


# --- explain independence ---


def test_explain_works_without_modal(monkeypatch, tmp_path):
    record = _ingest_copy(
        monkeypatch, _make_project(tmp_path / "source", CALC_PROJECT)
    )
    monkeypatch.setattr(
        modal_backend_module.ModalBackend, "run", _explode("ModalBackend.run")
    )
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    result = explain_repository(
        record["ingestion_id"], summarize=lambda **kwargs: "Static summary."
    )
    assert result["status"] == "complete"
    assert result["mode"] == "explain"


# --- new configuration ---


def test_execution_config_defaults(monkeypatch):
    for name in (
        "REPORESCUE_EXECUTION_BACKEND",
        "REPORESCUE_MODAL_CPU",
        "REPORESCUE_MODAL_MEMORY_MB",
        "REPORESCUE_MODAL_APP_NAME",
        "REPORESCUE_MODAL_NODE_IMAGE",
        "REPORESCUE_MODAL_USE_SDK_CONFIG",
        "REPORESCUE_SNAPSHOT_MAX_TOTAL_BYTES",
        "REPORESCUE_SNAPSHOT_MAX_FILE_BYTES",
        "REPORESCUE_SNAPSHOT_MAX_FILES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config_module.get_execution_backend() == "auto"
    assert config_module.get_modal_cpu() == 1.0
    assert config_module.get_modal_memory_mb() == 1024
    assert config_module.get_modal_app_name() == "reporescue-executions"
    assert config_module.get_modal_node_image() == "node:22-slim"
    assert config_module.modal_sdk_config_allowed() is False
    assert config_module.get_snapshot_max_total_bytes() == 128 * 1024 * 1024
    assert config_module.get_snapshot_max_file_bytes() == 10 * 1024 * 1024
    assert config_module.get_snapshot_max_files() == 20_000


def test_execution_config_overrides(monkeypatch):
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "modal")
    monkeypatch.setenv("REPORESCUE_MODAL_CPU", "0.5")
    monkeypatch.setenv("REPORESCUE_MODAL_MEMORY_MB", "512")
    monkeypatch.setenv("REPORESCUE_MODAL_APP_NAME", "custom-app_1")
    monkeypatch.setenv("REPORESCUE_MODAL_NODE_IMAGE", "node:20-slim")
    monkeypatch.setenv("REPORESCUE_MODAL_USE_SDK_CONFIG", "1")
    monkeypatch.setenv("REPORESCUE_SNAPSHOT_MAX_FILES", "100")
    assert config_module.get_execution_backend() == "modal"
    assert config_module.get_modal_cpu() == 0.5
    assert config_module.get_modal_memory_mb() == 512
    assert config_module.get_modal_app_name() == "custom-app_1"
    assert config_module.get_modal_node_image() == "node:20-slim"
    assert config_module.modal_sdk_config_allowed() is True
    assert config_module.get_snapshot_max_files() == 100


def test_execution_config_rejects_invalid(monkeypatch):
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "bogus")
    with pytest.raises(ConfigError):
        config_module.get_execution_backend()
    monkeypatch.setenv("REPORESCUE_MODAL_CPU", "lots")
    with pytest.raises(ConfigError):
        config_module.get_modal_cpu()
    monkeypatch.setenv("REPORESCUE_MODAL_CPU", "-1")
    with pytest.raises(ConfigError):
        config_module.get_modal_cpu()
    monkeypatch.setenv("REPORESCUE_MODAL_APP_NAME", "Has Spaces!")
    with pytest.raises(ConfigError):
        config_module.get_modal_app_name()
    monkeypatch.setenv("REPORESCUE_SNAPSHOT_MAX_FILES", "-5")
    with pytest.raises(ConfigError):
        config_module.get_snapshot_max_files()


# --- multi-worker safety beyond the 2-thread race ---


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/lifecycle.db",
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
def owner(db):
    profile = Profile(id=uuid.uuid4())
    db.add(profile)
    db.commit()
    return profile.id


def _create(db, owner, **overrides):
    params = {
        "repo_url": "https://github.com/acme/calc",
        "repo_name": "acme/calc",
        "commit_sha": FAKE_SHA,
        "mode": "debug",
    }
    params.update(overrides)
    return create_investigation(db, owner_id=owner, **params)


def test_eight_way_claim_single_winner(engine, db, owner):
    _, job = _create(db, owner)
    barrier = threading.Barrier(8)
    winners = []
    lock = threading.Lock()

    def attempt(worker):
        with Session(engine) as session:
            barrier.wait(timeout=30)
            claimed = claim_next_job(session, worker)
            if claimed is not None:
                with lock:
                    winners.append((claimed.id, worker))

    threads = [
        threading.Thread(target=attempt, args=(f"w-{index}",))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(winners) == 1
    assert winners[0][0] == job.id
    with Session(engine) as session:
        assert session.get(Job, job.id).attempts == 1


def test_claim_many_jobs_many_workers_no_duplicates(engine, db, owner):
    ids = {_create(db, owner)[1].id for _ in range(6)}
    barrier = threading.Barrier(4)
    claimed = []
    lock = threading.Lock()

    def worker(worker_id):
        with Session(engine) as session:
            barrier.wait(timeout=30)
            while True:
                job = claim_next_job(session, worker_id)
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)

    threads = [
        threading.Thread(target=worker, args=(f"w-{index}",)) for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert set(claimed) == ids
    assert len(claimed) == len(ids)


def test_lease_expiry_requeues_then_fails(db, owner):
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    recovered = recover_stale_jobs(db)
    assert recovered == {"requeued": 1, "failed": 0}
    row = db.get(Job, job.id)
    assert row.status == "waiting"
    assert row.worker_id is None
    assert row.error["reason"] == "worker_lease_expired"
    row.not_before_at = utcnow() - timedelta(seconds=1)
    db.commit()
    claimed = claim_next_job(db, "w2")
    assert claimed is not None
    claimed.attempts = claimed.max_attempts
    claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    recovered = recover_stale_jobs(db)
    assert recovered == {"requeued": 0, "failed": 1}
    row = db.get(Job, job.id)
    assert row.status == "failed"
    assert db.get(Investigation, row.investigation_id).status == "failed"


def test_recovery_ignores_live_and_terminal_jobs(db, owner):
    _, live_job = _create(db, owner)
    claim_next_job(db, "w1")
    _, cancelled_job = _create(db, owner)
    cancel_job(db, db.get(Job, cancelled_job.id))
    _, done_job = _create(db, owner)
    complete_job(db, claim_next_job(db, "w1"))
    recovered = recover_stale_jobs(db)
    assert recovered == {"requeued": 0, "failed": 0}
    assert db.get(Job, live_job.id).status == "running"
    assert db.get(Job, cancelled_job.id).status == "cancelled"
    assert db.get(Job, done_job.id).status == "completed"


def test_cancelled_job_never_claimed_or_heartbeat(db, owner):
    _, job = _create(db, owner)
    cancel_job(db, db.get(Job, job.id))
    assert claim_next_job(db, "w1") is None
    assert heartbeat(db, job.id, "w1") is False


def test_fail_job_is_terminal_and_keeps_evidence(db, owner):
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    error = {"reason": "worker_lease_expired", "message": "gone"}
    fail_job(db, claimed, error)
    row = db.get(Job, job.id)
    assert row.status == "failed"
    assert row.error == error
    investigation = db.get(Investigation, row.investigation_id)
    assert investigation.status == "failed"
    assert investigation.completed_at is not None
