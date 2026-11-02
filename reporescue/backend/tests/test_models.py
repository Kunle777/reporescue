"""Model and constraint checks against an isolated SQLite database."""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Profile, Project


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/m.db")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session_:
        yield session_
    engine.dispose()


def _project_kwargs(**overrides):
    values = {
        "github_owner": "acme",
        "repository_name": "calc",
        "display_name": "Calc",
        "github_url": "https://github.com/acme/calc",
    }
    values.update(overrides)
    return values


def test_profile_defaults(session):
    profile = Profile(id=uuid.uuid4())
    session.add(profile)
    session.commit()
    session.refresh(profile)
    assert profile.display_name is None
    assert profile.created_at is not None
    assert profile.updated_at is not None


def test_project_defaults_and_id(session):
    owner = Profile(id=uuid.uuid4())
    session.add(owner)
    session.commit()
    project = Project(owner_id=owner.id, **_project_kwargs())
    session.add(project)
    session.commit()
    session.refresh(project)
    assert project.id is not None
    assert project.visibility == "public"
    assert project.github_repo_id is None
    assert project.default_branch is None


def test_duplicate_repo_per_owner_rejected(session):
    owner = Profile(id=uuid.uuid4())
    session.add(owner)
    session.commit()
    session.add(Project(owner_id=owner.id, **_project_kwargs()))
    session.commit()
    session.add(Project(owner_id=owner.id, **_project_kwargs()))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_repo_different_owners_allowed(session):
    first = Profile(id=uuid.uuid4())
    second = Profile(id=uuid.uuid4())
    session.add_all([first, second])
    session.commit()
    session.add(Project(owner_id=first.id, **_project_kwargs()))
    session.add(Project(owner_id=second.id, **_project_kwargs()))
    session.commit()


def test_project_requires_existing_owner(session):
    session.add(Project(owner_id=uuid.uuid4(), **_project_kwargs()))
    with pytest.raises(IntegrityError):
        session.commit()


def test_visibility_check_rejects_non_public(session):
    owner = Profile(id=uuid.uuid4())
    session.add(owner)
    session.commit()
    session.add(Project(owner_id=owner.id, visibility="private", **_project_kwargs()))
    with pytest.raises(IntegrityError):
        session.commit()


def test_owner_delete_cascades_projects(session):
    owner = Profile(id=uuid.uuid4())
    session.add(owner)
    session.commit()
    session.add(Project(owner_id=owner.id, **_project_kwargs()))
    session.commit()
    session.delete(owner)
    session.commit()
    assert session.query(Project).count() == 0
