"""Input validation: strip-normalized messages and structured auth errors.

Message models strip surrounding whitespace before validation so
required fields reject whitespace-only input, optional fields stay
explicit, and idempotency fingerprints are canonical across padding
variants. Auth failures carry stable codes and never touch quota.
"""
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import ingestion as ingestion_module
from app import main as main_module
from app.auth import AuthenticatedUser, get_current_user
from app.ingestion import set_ingestion_owner
from app.models import Base, Investigation, Profile
from app.usage_quota import usage_status


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/validation.db",
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


def _user(owner):
    return AuthenticatedUser(id=owner, email=None, claims={})


def test_conversation_rejects_whitespace_only():
    with pytest.raises(ValidationError):
        main_module.ConversationMessageRequest(message="   \n  ")
    assert main_module.ConversationMessageRequest(message="  hi  ").message == "hi"


def test_classify_rejects_whitespace_only():
    with pytest.raises(ValidationError):
        main_module.ClassifyRequest(message="  ")
    assert main_module.ClassifyRequest(message="  what? ").message == "what?"


def test_job_message_stripped_but_optional():
    assert (
        main_module.JobCreate(ingestion_id=str(uuid.uuid4()), message="  fix  ").message
        == "fix"
    )
    assert (
        main_module.JobCreate(ingestion_id=str(uuid.uuid4()), message=None).message
        is None
    )
    # Empty stays empty (General without objective shows guidance, uncharged).
    assert (
        main_module.JobCreate(ingestion_id=str(uuid.uuid4()), message="").message == ""
    )


def test_sync_message_stripped():
    request = main_module.RealInvestigationRequest(
        ingestion_id=str(uuid.uuid4()), message="  explain  "
    )
    assert request.message == "explain"


def test_padded_retry_replays_same_job(db, owner, monkeypatch, tmp_path):
    import shutil

    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "calc"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )

    def fake_clone(url, destination, ref=None):
        shutil.copytree(project, destination)
        return "f" * 40

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    record = ingestion_module.ingest_repository("https://github.com/acme/calc")
    set_ingestion_owner(record["ingestion_id"], str(owner))

    key = "pad-" + "p" * 28
    first = main_module.create_job_route(
        main_module.JobCreate(
            ingestion_id=record["ingestion_id"], mode="debug", message="fix it"
        ),
        _user(owner),
        db,
        key,
    )
    second = main_module.create_job_route(
        main_module.JobCreate(
            ingestion_id=record["ingestion_id"], mode="debug", message="  fix it\n"
        ),
        _user(owner),
        db,
        key,
    )
    assert second == first
    assert len(db.scalars(select(Investigation)).all()) == 1
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_missing_credentials_use_structured_code():
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "NOT_AUTHENTICATED"
