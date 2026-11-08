"""Job quota lifecycle: reserve at admission, refund on failure.

Covers async jobs (charged flags + worker terminal transitions),
the sync investigation path, and concurrent over-admission safety.
"""
import shutil
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import ingestion as ingestion_module
from app import main as main_module
from app import real_investigation as pipeline
from app.auth import AuthenticatedUser
from app.ingestion import clear_ingestions, ingest_repository, set_ingestion_owner
from app.jobqueue import (
    cancel_job,
    claim_next_job,
    complete_job,
    fail_job,
    recover_stale_jobs,
)
from app.models import Base, Investigation
from app.orchestrate import OrchestrationResult
from app.usage_quota import reserve_operation, usage_status

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
        f"sqlite:///{tmp_path.as_posix()}/jobquota.db",
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


def _user(owner):
    return AuthenticatedUser(id=owner, email=None, claims={})


def _owned_ingestion(monkeypatch, project_dir, owner):
    def fake_clone(url, destination, ref=None):
        shutil.copytree(project_dir, destination)
        return FAKE_SHA

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    monkeypatch.setattr(pipeline, "clone_and_pin", fake_clone)
    record = ingest_repository("https://github.com/acme/calc")
    set_ingestion_owner(record["ingestion_id"], str(owner))
    return record


def _create_job(db, owner, record, mode="debug", message="fix the failure"):
    body = main_module.JobCreate(
        ingestion_id=record["ingestion_id"], mode=mode, message=message
    )
    return main_module.create_job_route(body, _user(owner), db)


def _investigation(db, job_result):
    return db.get(Investigation, uuid.UUID(job_result["investigation_id"]))


def test_create_job_reserves_and_marks_charged(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record)
    investigation = _investigation(db, result)
    assert investigation.ai_quota_charged is True
    status = usage_status(db, user_id=str(owner))
    assert status["ai"]["used"] == 1


def test_general_without_route_reserves_nothing(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record, mode="general", message="")
    investigation = _investigation(db, result)
    assert investigation.ai_quota_charged is False
    assert investigation.debug_quota_charged is False
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_fail_job_refunds_reserved_quota(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record)
    investigation = _investigation(db, result)
    job = claim_next_job(db, worker_id="w1")
    fail_job(db, job, {"reason": "stage_failed", "message": "boom"})
    db.refresh(investigation)
    assert investigation.ai_quota_charged is False
    assert investigation.debug_quota_charged is False
    status = usage_status(db, user_id=str(owner))
    assert status["ai"]["used"] == 0
    assert status["debug"]["used"] == 0


def test_fail_job_without_reservation_refunds_nothing(
    db, owner, monkeypatch, project_dir
):
    from app.jobqueue import get_job_by_investigation

    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record, mode="general", message="")
    job = claim_next_job(db, worker_id="w1")
    fail_job(db, job, {"reason": "stage_failed", "message": "boom"})
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_cancel_queued_job_refunds(db, owner, monkeypatch, project_dir):
    from app.jobqueue import get_job_by_investigation

    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record)
    investigation = _investigation(db, result)
    job = get_job_by_investigation(db, investigation.id)
    cancel_job(db, job)
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_complete_job_keeps_charge_and_clears_flags(
    db, owner, monkeypatch, project_dir
):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record)
    investigation = _investigation(db, result)
    job = claim_next_job(db, worker_id="w1")
    complete_job(db, job)
    db.refresh(investigation)
    assert investigation.ai_quota_charged is False
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1
    # Cancelling a terminal job is a no-op, never a refund.
    cancel_job(db, job)
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_recover_stale_exhausted_job_refunds(db, owner, monkeypatch, project_dir):
    from datetime import timedelta

    from app.jobqueue import claim_next_job, utcnow

    record = _owned_ingestion(monkeypatch, project_dir, owner)
    result = _create_job(db, owner, record)
    investigation = _investigation(db, result)
    job = claim_next_job(db, worker_id="w1")
    job.attempts = job.max_attempts
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    outcome = recover_stale_jobs(db)
    assert outcome == {"requeued": 0, "failed": 1}
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_recover_stale_retryable_job_keeps_reservation(
    db, owner, monkeypatch, project_dir
):
    from datetime import timedelta

    from app.jobqueue import claim_next_job, utcnow

    record = _owned_ingestion(monkeypatch, project_dir, owner)
    _create_job(db, owner, record)
    job = claim_next_job(db, worker_id="w1")
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    outcome = recover_stale_jobs(db)
    assert outcome == {"requeued": 1, "failed": 0}
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_sync_success_keeps_charge(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    monkeypatch.setattr(
        main_module,
        "execute",
        lambda *args, **kwargs: OrchestrationResult(
            status="complete", report={"status": "ok"}
        ),
    )
    request = main_module.RealInvestigationRequest(
        ingestion_id=record["ingestion_id"], mode="explain"
    )
    result = main_module.start_real_investigation(request, _user(owner), db)
    assert result == {"status": "ok"}
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_sync_failure_refunds(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)
    monkeypatch.setattr(
        main_module,
        "execute",
        lambda *args, **kwargs: OrchestrationResult(
            status="failed",
            error={"reason": "stage_failed", "message": "Stage failed."},
        ),
    )
    request = main_module.RealInvestigationRequest(
        ingestion_id=record["ingestion_id"], mode="explain"
    )
    result = main_module.start_real_investigation(request, _user(owner), db)
    assert result["status"] == "error"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_sync_exception_refunds_and_propagates(db, owner, monkeypatch, project_dir):
    record = _owned_ingestion(monkeypatch, project_dir, owner)

    def _boom(*args, **kwargs):
        raise RuntimeError("worker blew up")

    monkeypatch.setattr(main_module, "execute", _boom)
    request = main_module.RealInvestigationRequest(
        ingestion_id=record["ingestion_id"], mode="explain"
    )
    with pytest.raises(RuntimeError):
        main_module.start_real_investigation(request, _user(owner), db)
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_concurrent_reservations_never_exceed_ten(engine):
    from app.usage_quota import AI_DAILY_USER_LIMIT, UsageLimitExceeded

    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    outcomes = []

    def _attempt():
        session = factory()
        try:
            reserve_operation(session, user_id="racer")
            outcomes.append(True)
        except UsageLimitExceeded:
            outcomes.append(False)
        finally:
            session.close()

    threads = [threading.Thread(target=_attempt) for _ in range(AI_DAILY_USER_LIMIT + 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(outcomes) == AI_DAILY_USER_LIMIT
    check = factory()
    try:
        assert usage_status(check, user_id="racer")["ai"]["used"] == AI_DAILY_USER_LIMIT
    finally:
        check.close()
