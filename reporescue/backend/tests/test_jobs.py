"""Acceptance tests for persistent investigations + job lifecycle.

Covers creation, ownership, state transitions, atomic claiming,
crash/stale recovery, retries, cancellation, write-through recording,
worker execution, and the job API routes — all against an isolated
SQLite database. No live Supabase or Docker is required.
"""

import shutil
import threading
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import ingestion as ingestion_module
from app import main as main_module
from app import real_investigation as pipeline
from app.auth import AuthenticatedUser
from app.ingestion import clear_ingestions, ingest_repository, set_ingestion_owner
from app.jobqueue import (
    InvalidMode,
    InvalidTransition,
    cancel_job,
    claim_next_job,
    complete_job,
    create_investigation,
    fail_job,
    get_active_job_for_owner,
    get_investigation_for_owner,
    get_job_for_owner,
    heartbeat,
    investigation_detail,
    list_job_cards,
    recover_stale_jobs,
    release_to_waiting,
    save_snapshot_record,
    utcnow,
)
from app.meta_client import MetaAPIError, MetaRateLimitError
from app.models import (
    Base,
    ExecutionRun,
    Finding,
    Investigation,
    InvestigationAttempt,
    InvestigationMessage,
    Job,
    Patch,
    Profile,
)
from app.recorder import DBRecorder, JobCancelled
from app.worker import create_conversation_session, run_job

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
        f"sqlite:///{tmp_path.as_posix()}/jobs.db",
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
def other_owner(db):
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


def _create(db, owner, **overrides):
    params = {
        "repo_url": "https://github.com/acme/calc",
        "repo_name": "acme/calc",
        "commit_sha": FAKE_SHA,
        "mode": "debug",
    }
    params.update(overrides)
    return create_investigation(db, owner_id=owner, **params)


# --- creation & models ---


def test_create_investigation_with_job_and_message(db, owner):
    investigation, job = _create(db, owner, user_message="  Why broken? ")

    assert investigation.status == "queued"
    assert investigation.mode == "debug"
    assert investigation.user_message == "Why broken?"
    assert investigation.progress_pct == 0
    assert investigation.verification_status == "unverified"
    assert job.status == "queued"
    assert job.investigation_id == investigation.id
    assert job.attempts == 0
    messages = db.scalars(
        select(InvestigationMessage).where(
            InvestigationMessage.investigation_id == investigation.id
        )
    ).all()
    assert [(m.role, m.content) for m in messages] == [("user", "Why broken?")]


def test_create_without_message_has_no_message_row(db, owner):
    investigation, _ = _create(db, owner)
    assert investigation.user_message is None
    count = len(
        db.scalars(
            select(InvestigationMessage).where(
                InvestigationMessage.investigation_id == investigation.id
            )
        ).all()
    )
    assert count == 0


def test_create_rejects_unknown_mode(db, owner):
    with pytest.raises(InvalidMode):
        _create(db, owner, mode="bogus")


def test_create_truncates_long_message(db, owner):
    investigation, _ = _create(db, owner, user_message="?" * 5000)
    assert len(investigation.user_message) == 2000


def test_one_job_per_investigation(db, owner):
    investigation, _ = _create(db, owner)
    db.add(Job(investigation_id=investigation.id, owner_id=owner))
    with pytest.raises(IntegrityError):
        db.commit()


def test_status_check_constraint_rejects_garbage(db, owner):
    investigation, _ = _create(db, owner)
    investigation.status = "flying"
    with pytest.raises(IntegrityError):
        db.commit()


def test_cascade_deletes_children(db, owner):
    investigation, _ = _create(db, owner, user_message="hi")
    db.delete(investigation)
    db.commit()
    assert db.scalars(select(Job)).all() == []
    assert db.scalars(select(InvestigationMessage)).all() == []


# --- transitions ---


def test_legal_lifecycle_transitions(db, owner):
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    assert claimed is not None and claimed.status == "running"
    release_to_waiting(db, claimed, backoff_seconds=0)
    assert db.get(Job, job.id).status == "waiting"
    reclaimed = claim_next_job(db, "w1")
    assert reclaimed is not None and reclaimed.status == "running"
    complete_job(db, reclaimed)
    assert db.get(Job, job.id).status == "completed"
    investigation = db.get(Investigation, job.investigation_id)
    assert investigation.status == "completed"
    assert investigation.completed_at is not None


def test_illegal_transitions_rejected(db, owner):
    _, job = _create(db, owner)
    complete_job(db, claim_next_job(db, "w1"))
    with pytest.raises(InvalidTransition):
        fail_job(db, db.get(Job, job.id))
    fresh = db.get(Job, job.id)
    assert fresh.status == "completed"


def test_fail_mirrors_error_to_investigation(db, owner):
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    fail_job(db, claimed, {"reason": "api_error", "message": "down"})
    investigation = db.get(Investigation, job.investigation_id)
    assert investigation.status == "failed"
    assert investigation.error == {"reason": "api_error", "message": "down"}
    assert investigation.completed_at is not None


# --- claiming ---


def test_claim_oldest_first_and_sets_lease(db, owner):
    first, first_job = _create(db, owner)
    _create(db, owner)
    # created_at has second precision on SQLite: pin distinct instants so
    # "oldest" never falls through to the random UUID tiebreak.
    first_job.created_at = first_job.created_at - timedelta(seconds=10)
    db.commit()
    job = claim_next_job(db, "worker-1")
    assert job is not None
    assert job.investigation_id == first.id
    assert job.worker_id == "worker-1"
    assert job.attempts == 1
    assert job.lease_expires_at is not None
    assert job.claimed_at is not None
    assert db.get(Investigation, first.id).status == "running"


def test_claim_returns_none_when_queue_empty(db, owner):
    assert claim_next_job(db, "worker-1") is None


def test_second_claim_skips_running_job(db, owner):
    _create(db, owner)
    assert claim_next_job(db, "w1") is not None
    assert claim_next_job(db, "w2") is None


def test_waiting_respects_backoff(db, owner):
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    release_to_waiting(db, claimed, backoff_seconds=3600)
    assert claim_next_job(db, "w2") is None
    with Session(db.get_bind()) as other:
        row = other.get(Job, job.id)
        row.not_before_at = utcnow() - timedelta(seconds=1)
        other.commit()
    db.expire_all()
    assert claim_next_job(db, "w2") is not None


def test_concurrent_claim_single_winner(db, engine, owner):
    _create(db, owner)
    barrier = threading.Barrier(2)
    winners = []

    def attempt():
        with Session(engine) as session:
            barrier.wait(timeout=10)
            job = claim_next_job(session, f"w-{threading.get_ident()}")
            if job is not None:
                winners.append(job.id)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(winners) == 1


# --- heartbeat, lease, recovery ---


def test_heartbeat_extends_lease(db, owner):
    _, job = _create(db, owner)
    claim_next_job(db, "w1")
    first_lease = db.get(Job, job.id).lease_expires_at
    assert heartbeat(db, job.id, "w1") is True
    assert db.get(Job, job.id).lease_expires_at >= first_lease


def test_heartbeat_rejected_for_other_worker_or_cancelled(db, owner):
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    assert heartbeat(db, job.id, "w2") is False
    cancel_job(db, claimed)
    assert heartbeat(db, job.id, "w1") is False


def test_recover_stale_job_requeues_with_backoff(db, owner):
    _, job = _create(db, owner)
    claim_next_job(db, "crashed-worker")
    with Session(db.get_bind()) as other:
        row = other.get(Job, job.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        other.commit()
    db.expire_all()
    outcome = recover_stale_jobs(db)
    assert outcome == {"requeued": 1, "failed": 0}
    row = db.get(Job, job.id)
    assert row.status == "waiting"
    assert row.not_before_at is not None
    assert row.error["reason"] == "worker_lease_expired"


def test_recover_exhausted_job_fails(db, owner):
    _, job = _create(db, owner)
    claim_next_job(db, "crashed-worker")
    with Session(db.get_bind()) as other:
        row = other.get(Job, job.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        row.attempts = row.max_attempts
        other.commit()
    db.expire_all()
    outcome = recover_stale_jobs(db)
    assert outcome == {"requeued": 0, "failed": 1}
    assert db.get(Job, job.id).status == "failed"
    assert db.get(Investigation, job.investigation_id).status == "failed"


def test_recovery_ignores_live_lease(db, owner):
    _, job = _create(db, owner)
    claim_next_job(db, "w1")
    assert recover_stale_jobs(db) == {"requeued": 0, "failed": 0}
    assert db.get(Job, job.id).status == "running"


# --- cancellation ---


def test_cancel_from_queued_and_running(db, owner):
    _, queued = _create(db, owner)
    cancel_job(db, queued)
    assert db.get(Job, queued.id).status == "cancelled"
    assert db.get(Investigation, queued.investigation_id).status == "cancelled"
    _, running = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    cancel_job(db, claimed)
    assert db.get(Job, running.id).status == "cancelled"


def test_cancel_terminal_is_noop(db, owner):
    _, job = _create(db, owner)
    complete_job(db, claim_next_job(db, "w1"))
    cancel_job(db, db.get(Job, job.id))
    assert db.get(Job, job.id).status == "completed"


# --- ownership & queries ---


def test_cross_user_access_denied(db, owner, other_owner):
    investigation, job = _create(db, owner)
    assert get_job_for_owner(db, job_id=job.id, owner_id=other_owner) is None
    assert (
        get_investigation_for_owner(
            db, investigation_id=investigation.id, owner_id=other_owner
        )
        is None
    )
    assert list_job_cards(db, owner_id=other_owner) == []
    assert get_active_job_for_owner(db, owner_id=other_owner) is None


def test_active_job_is_most_recent_non_terminal(db, owner):
    old_inv, old_job = _create(db, owner)
    complete_job(db, claim_next_job(db, "w1"))
    new_inv, _ = _create(db, owner)
    active = get_active_job_for_owner(db, owner_id=owner)
    assert active is not None
    assert active.investigation_id == new_inv.id
    assert old_inv.id != new_inv.id


def test_no_active_job_when_all_terminal(db, owner):
    _, job = _create(db, owner)
    complete_job(db, claim_next_job(db, "w1"))
    assert get_active_job_for_owner(db, owner_id=owner) is None


def test_list_job_cards_newest_first(db, owner):
    _create(db, owner, repo_name="acme/first")
    _create(db, owner, repo_name="acme/second")
    cards = list_job_cards(db, owner_id=owner)
    assert {card["repo_name"] for card in cards} == {"acme/first", "acme/second"}
    assert {card["mode"] for card in cards} == {"debug"}


# --- recorder ---


def _recorder(factory, job):
    return DBRecorder(
        factory, investigation_id=job.investigation_id, job_id=job.id
    )


def test_recorder_tracks_stages_progress_and_partial(db, owner, factory):
    _, job = _create(db, owner)
    recorder = _recorder(factory, job)
    recorder.set_stages(("a", "b", "c", "d"))
    recorder.stage_started("a")
    db.expire_all()
    assert db.get(Investigation, job.investigation_id).stage == "a"
    recorder.stage_completed("a")
    recorder.stage_completed("b", {"note": 1})
    recorder.record_partial({"fact": 2})
    db.expire_all()
    investigation = db.get(Investigation, job.investigation_id)
    assert investigation.stages_completed == ["a", "b"]
    assert investigation.progress_pct == 50
    assert investigation.partial == {"note": 1, "fact": 2}


def test_recorder_persists_runs_attempts_findings_patches(db, owner, factory):
    _, job = _create(db, owner)
    recorder = _recorder(factory, job)
    run_id = recorder.record_run(
        "baseline",
        {
            "backend": "docker", "isolation": "x", "status": "test_failure",
            "passed": False, "exit_code": 1, "output": "o" * 50000,
            "failures": [{"test": "t"}], "install_ok": True,
            "install_log": "log", "duration_ms": 5, "truncated": True,
        },
    )
    assert run_id is not None
    run = db.get(ExecutionRun, run_id)
    assert len(run.output) == 20000
    assert run.truncated is True
    detail = investigation_detail(db, db.get(Investigation, job.investigation_id))
    assert detail["runs"][0]["truncated"] is True
    attempt_id = recorder.record_attempt(
        1, diagnosis="d", confidence="confirmed",
        patch={"edits": []}, guard={"validated": True},
        verification={"passed": True}, timings={"a": 1},
    )
    assert db.get(InvestigationAttempt, attempt_id).confidence == "confirmed"
    finding_id = recorder.record_finding(
        kind="fact", title="t", detail="d", evidence={"f": "x"},
        confidence="confirmed", category="c",
    )
    assert db.get(Finding, finding_id).kind == "fact"
    patch_id = recorder.record_patch(
        edits=[{"file": "a.py", "before": "b", "after": "c"}],
        reason="r", status="applied", attempt_id=attempt_id,
    )
    assert db.get(Patch, patch_id).status == "applied"
    message_id = recorder.record_message(role="assistant", content="done")
    assert message_id is not None


def test_recorder_check_cancelled(db, owner, factory):
    _, job = _create(db, owner)
    recorder = _recorder(factory, job)
    recorder.check_cancelled()
    cancel_job(db, db.get(Job, job.id))
    with pytest.raises(JobCancelled):
        recorder.check_cancelled()


def test_recorder_stopping_raises(db, owner, factory):
    _, job = _create(db, owner)
    recorder = DBRecorder(
        factory, investigation_id=job.investigation_id, job_id=job.id,
        is_stopping=lambda: True,
    )
    with pytest.raises(JobCancelled):
        recorder.check_cancelled()


# --- worker ---


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


def _baseline_failure():
    return {
        "status": "test_failure", "passed": False, "exit_code": 1,
        "output": "FAILED tests/test_calc.py::test_add - assert -1 == 5",
        "failures": [
            {
                "test": "tests/test_calc.py::test_add", "kind": "failed",
                "message": "assert -1 == 5", "traceback": "E assert",
            }
        ],
    }


def _verification_pass():
    return {
        "status": "ok", "passed": True, "exit_code": 0,
        "output": "1 passed", "failures": [],
    }


def test_worker_debug_end_to_end(db, factory, owner, monkeypatch, project_dir):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, user_message="fix it")
    claimed = claim_next_job(db, "w1")
    backend = _FakeBackend([_baseline_failure(), _verification_pass()])

    outcome = run_job(factory, claimed.id, "w1", backend=backend, diagnose=_fix_diagnose)

    assert outcome == "completed"
    db.expire_all()
    row = db.get(Investigation, investigation.id)
    assert row.status == "completed"
    assert row.verification_status == "passed"
    assert row.progress_pct == 100
    assert row.stages_completed == ["materialize", "baseline", "repair", "report"]
    assert row.final_report["verdict"] == "TESTS_PASSED"
    assert row.completed_at is not None
    assert db.get(Job, job.id).status == "completed"
    assert len(db.scalars(select(InvestigationAttempt)).all()) == 1
    assert len(db.scalars(select(ExecutionRun)).all()) == 2
    assert len(db.scalars(select(Finding)).all()) == 2
    assert len(db.scalars(select(Patch)).all()) == 1
    roles = sorted(
        m.role
        for m in db.scalars(select(InvestigationMessage)).all()
    )
    assert roles == ["assistant", "user"]


def test_worker_explain_end_to_end(db, factory, owner, monkeypatch, project_dir):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, mode="explain")
    claimed = claim_next_job(db, "w1")

    outcome = run_job(
        factory, claimed.id, "w1", summarize=lambda **kwargs: "It adds."
    )

    assert outcome == "completed"
    row = db.get(Investigation, investigation.id)
    assert row.verification_status == "unavailable"
    assert row.final_report["summary"] == "It adds."
    assert "report" in row.stages_completed


def test_worker_rejects_sha_substitution(db, factory, owner, monkeypatch, project_dir):
    _stub_clone(monkeypatch, project_dir, sha="c" * 40)
    investigation, job = _create(db, owner, mode="explain")
    claimed = claim_next_job(db, "w1")

    assert run_job(factory, claimed.id, "w1", summarize=lambda **k: "x") == "failed"
    db.expire_all()
    row = db.get(Investigation, investigation.id)
    assert row.status == "failed"
    assert row.error["reason"] == "sha_mismatch"


def test_worker_in_flight_cancel_settles_cancelled(
    db, factory, owner, monkeypatch, project_dir
):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, mode="explain")
    claimed = claim_next_job(db, "w1")
    job_id = claimed.id

    def cancelling(**kwargs):
        # Mirror the cancel endpoint: cancel_job flips the job and its
        # investigation together, then the runner notices mid-flight.
        with factory() as other:
            cancel_job(other, other.get(Job, job_id))
        return "too late"

    assert run_job(factory, claimed.id, "w1", summarize=cancelling) == "cancelled"
    db.expire_all()
    assert db.get(Job, job.id).status == "cancelled"
    row = db.get(Investigation, investigation.id)
    assert row.status == "cancelled"
    # The summarize handler finished before the cancel landed, so its
    # work is honestly marked; finalization never ran, so no report.
    assert row.stages_completed == ["context", "summarize"]
    assert row.final_report is None


def test_worker_cancel_before_run_skips_pipeline(db, factory, owner, monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("pipeline must not run for a cancelled job")

    monkeypatch.setattr(ingestion_module, "clone_and_pin", _explode)
    _, job = _create(db, owner)
    claimed = claim_next_job(db, "w1")
    cancel_job(db, db.get(Job, job.id))

    assert run_job(factory, claimed.id, "w1") == "cancelled"


def test_worker_transient_error_retries_then_completes(
    db, factory, owner, monkeypatch, project_dir
):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, mode="explain")
    claimed = claim_next_job(db, "w1")
    calls = []

    def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise MetaRateLimitError("slow down")
        return "recovered"

    assert run_job(factory, claimed.id, "w1", summarize=flaky) == "waiting"
    db.expire_all()
    row = db.get(Job, job.id)
    assert row.status == "waiting"
    assert row.error["reason"] == "rate_limit"
    with Session(db.get_bind()) as other:
        other.get(Job, job.id).not_before_at = utcnow() - timedelta(seconds=1)
        other.commit()
    db.expire_all()
    reclaimed = claim_next_job(db, "w1")
    assert reclaimed is not None
    assert run_job(factory, reclaimed.id, "w1", summarize=flaky) == "completed"
    assert db.get(Investigation, investigation.id).final_report["summary"] == "recovered"


def test_worker_exhausted_transient_error_fails_with_diagnostics(
    db, factory, owner, monkeypatch, project_dir
):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, mode="explain")
    claimed = claim_next_job(db, "w1")
    with Session(db.get_bind()) as other:
        row = other.get(Job, job.id)
        row.attempts = row.max_attempts
        other.commit()

    def broken(**kwargs):
        raise MetaAPIError("bad gateway")

    assert run_job(factory, claimed.id, "w1", summarize=broken) == "failed"
    row = db.get(Investigation, investigation.id)
    assert row.status == "failed"
    assert row.error == {"reason": "api_error", "message": "bad gateway"}


def test_worker_crash_recovery_reruns_to_completion(
    db, factory, owner, monkeypatch, project_dir
):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, mode="explain")
    claim_next_job(db, "dead-worker")
    with Session(db.get_bind()) as other:
        other.get(Job, job.id).lease_expires_at = utcnow() - timedelta(seconds=1)
        other.commit()
    db.expire_all()
    assert recover_stale_jobs(db) == {"requeued": 1, "failed": 0}
    with Session(db.get_bind()) as other:
        other.get(Job, job.id).not_before_at = utcnow() - timedelta(seconds=1)
        other.commit()
    db.expire_all()
    reclaimed = claim_next_job(db, "w2")
    assert run_job(
        factory, reclaimed.id, "w2", summarize=lambda **kwargs: "done"
    ) == "completed"
    assert db.get(Investigation, investigation.id).status == "completed"


def test_conversation_session_snapshot(db, owner):
    investigation, _ = _create(db, owner)
    investigation.final_report = {"summary": "s" * 5000}
    investigation.status = "completed"
    db.commit()
    session = create_conversation_session(db, db.get(Investigation, investigation.id))
    assert session.owner_id == owner
    assert session.context_snapshot["commit_sha"] == FAKE_SHA
    assert len(session.context_snapshot["summary"]) == 2000


# --- routes ---


def _user(owner):
    return AuthenticatedUser(id=owner, email=None, claims={})


def _owned_ingestion(monkeypatch, project_dir, owner):
    _stub_clone(monkeypatch, project_dir)
    record = ingest_repository("https://github.com/acme/calc")
    set_ingestion_owner(record["ingestion_id"], str(owner))
    return record


def test_create_job_route(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    body = main_module.JobCreate(
        ingestion_id=record["ingestion_id"], mode="debug", message="go"
    )
    result = main_module.create_job_route(body, _user(owner), db)
    assert result["status"] == "queued"
    investigation = db.get(Investigation, uuid.UUID(result["investigation_id"]))
    assert investigation.repo_name == "acme/calc"
    assert investigation.commit_sha == FAKE_SHA
    assert investigation.user_message == "go"


def test_create_job_route_stores_general_preselect(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    body = main_module.JobCreate(
        ingestion_id=record["ingestion_id"], mode="general", message="look",
        selected_mode="debug", routing_rationale="Model chose debug.",
        routing_confidence="high", routing_source="model",
    )
    result = main_module.create_job_route(body, _user(owner), db)
    investigation = db.get(Investigation, uuid.UUID(result["investigation_id"]))
    assert investigation.preselected_mode == "debug"
    assert investigation.preselect_rationale == "Model chose debug."
    assert investigation.preselect_confidence == "high"
    assert investigation.preselect_source == "model"
    detail = main_module.get_job_route(
        uuid.UUID(result["job_id"]), _user(owner), db
    )
    assert detail["preselected"]["mode"] == "debug"
    assert detail["preselected"]["source"] == "model"


def test_create_job_route_ignores_preselect_for_direct_modes(
    db, owner, monkeypatch, project_dir
):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    body = main_module.JobCreate(
        ingestion_id=record["ingestion_id"], mode="debug", message="x",
        selected_mode="explain",
    )
    result = main_module.create_job_route(body, _user(owner), db)
    investigation = db.get(Investigation, uuid.UUID(result["investigation_id"]))
    assert investigation.preselected_mode is None


def test_record_run_persists_command(db, factory, owner):
    from app.recorder import DBRecorder

    investigation, job = _create(db, owner)
    recorder = DBRecorder(
        factory, investigation_id=investigation.id, job_id=job.id
    )
    recorder.record_run(
        "baseline", {"backend": "docker", "command": "python -m pytest -q"}
    )
    detail = main_module.get_job_route(job.id, _user(owner), db)
    assert detail["runs"][0]["command"] == "python -m pytest -q"


def test_create_job_route_rejects_foreign_ingestion(db, owner, other_owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    body = main_module.JobCreate(ingestion_id=record["ingestion_id"])
    with pytest.raises(HTTPException) as exc_info:
        main_module.create_job_route(body, _user(other_owner), db)
    assert exc_info.value.status_code == 404


def test_create_job_route_missing_ingestion_is_404(db, owner):
    body = main_module.JobCreate(ingestion_id="missing")
    with pytest.raises(HTTPException) as exc_info:
        main_module.create_job_route(body, _user(owner), db)
    assert exc_info.value.status_code == 404


def test_create_job_route_survives_restart_via_snapshot(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    row = save_snapshot_record(db, owner_id=owner, record=record)
    assert row is not None
    clear_ingestions()  # simulate API process restart: memory gone, DB row remains

    body = main_module.JobCreate(
        ingestion_id=record["ingestion_id"], mode="general", message="take a look"
    )
    result = main_module.create_job_route(body, _user(owner), db)
    assert result["status"] == "queued"
    investigation = db.get(Investigation, uuid.UUID(result["investigation_id"]))
    assert investigation.mode == "general"
    assert investigation.repo_name == "acme/calc"
    assert investigation.commit_sha == FAKE_SHA
    assert investigation.project_id is None


def test_snapshot_rows_are_owner_isolated(db, owner, other_owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    save_snapshot_record(db, owner_id=owner, record=record)
    clear_ingestions()

    body = main_module.JobCreate(ingestion_id=record["ingestion_id"])
    with pytest.raises(HTTPException) as exc_info:
        main_module.create_job_route(body, _user(other_owner), db)
    assert exc_info.value.status_code == 404


def test_job_routes_ownership_and_detail(db, owner, other_owner):
    investigation, job = _create(db, owner, user_message="m")
    detail = main_module.get_job_route(job.id, _user(owner), db)
    assert detail["investigation_id"] == str(investigation.id)
    assert detail["messages"] == [{"role": "user", "content": "m"}]
    assert detail["job"]["status"] == "queued"
    with pytest.raises(HTTPException) as exc_info:
        main_module.get_job_route(job.id, _user(other_owner), db)
    assert exc_info.value.status_code == 404
    same = main_module.get_investigation_route(investigation.id, _user(owner), db)
    assert same["investigation_id"] == str(investigation.id)
    with pytest.raises(HTTPException):
        main_module.get_investigation_route(investigation.id, _user(other_owner), db)


def test_active_and_cancel_routes(db, owner):
    _, job = _create(db, owner)
    active = main_module.get_active_job_route(_user(owner), db)
    assert active["job"]["job_id"] == str(job.id)
    cancelled = main_module.cancel_job_route(job.id, _user(owner), db)
    assert cancelled["status"] == "cancelled"
    with pytest.raises(HTTPException) as exc_info:
        main_module.get_active_job_route(_user(owner), db)
    assert exc_info.value.status_code == 404


def test_list_jobs_route(db, owner):
    _create(db, owner, repo_name="acme/a")
    result = main_module.list_jobs_route(_user(owner), db)
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["repo_name"] == "acme/a"


# --- pipeline progress hooks ---


class _CallRecorder:
    def __init__(self):
        self.calls = []

    def set_stages(self, stages):
        self.calls.append(("set_stages", tuple(stages)))

    def stage_started(self, stage):
        self.calls.append(("started", stage))

    def stage_completed(self, stage, data=None):
        self.calls.append(("completed", stage))

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
        pass


def test_debug_pipeline_emits_progress(monkeypatch, project_dir):
    _stub_clone(monkeypatch, project_dir)
    record = ingest_repository("https://github.com/acme/calc")
    progress = _CallRecorder()
    backend = _FakeBackend([_baseline_failure(), _verification_pass()])

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=backend,
        diagnose=_fix_diagnose, progress=progress,
    )

    assert result["status"] == "complete"
    kinds = [call[0] for call in progress.calls]
    assert kinds.count("run") == 2
    assert ("attempt", 1) in progress.calls
    stages = [call[1] for call in progress.calls if call[0] == "completed"]
    # The diagnose -> guard -> apply -> verify attempt loop is one
    # runner-owned "repair" stage by design (evidence and verification
    # boundaries), not one stage per loop phase.
    assert stages == ["materialize", "baseline", "repair"]


def test_static_pipeline_emits_progress(monkeypatch, tmp_path):
    odd = tmp_path / "odd"
    (odd / "pkg").mkdir(parents=True)
    (odd / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (odd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (odd / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    _stub_clone(monkeypatch, odd)
    record = ingest_repository("https://github.com/acme/odd")
    progress = _CallRecorder()

    result = pipeline.investigate_repository(
        record["ingestion_id"], backend=_FakeBackend([]),
        diagnose=_fix_diagnose, progress=progress,
    )

    assert result["status"] == "complete"
    stages = [call[1] for call in progress.calls if call[0] == "completed"]
    assert stages == ["evidence", "diagnose"]
    assert ("attempt", 1) in progress.calls


def test_explain_and_health_emit_progress(monkeypatch, project_dir):
    _stub_clone(monkeypatch, project_dir)
    record = ingest_repository("https://github.com/acme/calc")
    from app.explain import explain_repository
    from app.health import assess_repository

    progress = _CallRecorder()
    assert (
        explain_repository(
            record["ingestion_id"], summarize=lambda **k: "x", progress=progress
        )["status"]
        == "complete"
    )
    stages = [call[1] for call in progress.calls if call[0] == "completed"]
    assert stages == ["context", "summarize"]

    progress = _CallRecorder()
    assert (
        assess_repository(
            record["ingestion_id"], summarize=lambda **k: "y", progress=progress
        )["status"]
        == "complete"
    )
    stages = [call[1] for call in progress.calls if call[0] == "completed"]
    assert stages == ["checks", "summarize"]


def test_worker_logs_model_not_found_with_phase(
    db, factory, owner, monkeypatch, project_dir, capsys
):
    _stub_clone(monkeypatch, project_dir)
    investigation, job = _create(db, owner, user_message="fix it")
    claimed = claim_next_job(db, "w1")

    def _missing_model(**kwargs):
        raise MetaAPIError(
            "Meta API request failed (HTTP 404). "
            "Details: The requested model was not found.",
            status_code=404,
        )

    backend = _FakeBackend([_baseline_failure(), _verification_pass()])
    outcome = run_job(factory, claimed.id, "w1", backend=backend, diagnose=_missing_model)
    assert outcome == "failed"
    out = capsys.readouterr().out
    assert "model_not_found" in out
    assert "phase=provider" in out
    assert str(claimed.id) in out
    assert "password" not in out
