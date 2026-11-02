"""Project routes: CRUD, ownership isolation, cross-user rejection.

Route functions are called directly with an explicit user and a throwaway
SQLite session; no live database or Supabase Auth is required.
"""

import shutil
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import ingestion as ingestion_module
from app import main as main_module
from app.auth import AuthenticatedUser
from app.ingestion import clear_ingestions, ingest_repository
from app.models import Base


@pytest.fixture(autouse=True)
def _isolate_store():
    clear_ingestions()
    yield
    clear_ingestions()


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/proj.db")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def local_project(monkeypatch, tmp_path):
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_core.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    def fake_clone(url, destination, ref=None):
        shutil.copytree(project, destination)
        return "f" * 40

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    return project


def _user():
    return AuthenticatedUser(id=uuid.uuid4(), email="tester@example.com", claims={})


def _create(db, user, **overrides):
    body = {"github_url": "https://github.com/acme/calc"}
    body.update(overrides)
    return main_module.create_project_route(
        main_module.ProjectCreate(**body), user, db
    )


def test_create_and_get_project(db):
    user = _user()
    created = _create(db, user, display_name="My Calc")
    assert created.display_name == "My Calc"
    assert created.github_owner == "acme"
    assert created.repository_name == "calc"
    assert created.github_url == "https://github.com/acme/calc"
    assert created.visibility == "public"

    fetched = main_module.get_project_route(created.id, user, db)
    assert fetched.id == created.id
    assert [row.id for row in main_module.list_projects_route(user, db)] == [created.id]


def test_create_rejects_non_github_host(db):
    with pytest.raises(HTTPException) as exc_info:
        _create(db, _user(), github_url="https://example.com/acme/calc")
    assert exc_info.value.status_code == 422


def test_client_supplied_owner_is_ignored(db):
    user = _user()
    other_id = str(uuid.uuid4())
    body = main_module.ProjectCreate.model_validate(
        {"github_url": "https://github.com/acme/calc", "owner_id": other_id}
    )
    created = main_module.create_project_route(body, user, db)
    assert created.id is not None
    fetched = main_module.get_project_route(created.id, user, db)
    assert fetched.id == created.id


def test_duplicate_repo_same_owner_rejected(db):
    user = _user()
    _create(db, user)
    with pytest.raises(HTTPException) as exc_info:
        _create(db, user)
    assert exc_info.value.status_code == 409


def test_same_repo_other_owner_allowed(db):
    first = _create(db, _user())
    second = _create(db, _user())
    assert first.id != second.id


def test_cross_user_access_rejected(db):
    owner = _user()
    stranger = _user()
    created = _create(db, owner)

    with pytest.raises(HTTPException) as exc_info:
        main_module.get_project_route(created.id, stranger, db)
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        main_module.update_project_route(
            created.id, main_module.ProjectUpdate(display_name="Hijacked"), stranger, db
        )
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        main_module.delete_project_route(created.id, stranger, db)
    assert exc_info.value.status_code == 404

    assert main_module.list_projects_route(stranger, db) == []
    assert main_module.get_project_route(created.id, owner, db).display_name != "Hijacked"


def test_patch_display_name_only(db):
    user = _user()
    created = _create(db, user)
    updated = main_module.update_project_route(
        created.id, main_module.ProjectUpdate(display_name="Renamed"), user, db
    )
    assert updated.display_name == "Renamed"
    assert updated.github_owner == "acme"
    assert updated.repository_name == "calc"
    assert updated.github_url == "https://github.com/acme/calc"


def test_patch_rejects_blank_display_name(db):
    user = _user()
    created = _create(db, user)
    with pytest.raises(Exception, match="display_name"):
        main_module.ProjectUpdate(display_name="   ")
    assert main_module.get_project_route(created.id, user, db).id == created.id


def test_delete_project(db):
    user = _user()
    created = _create(db, user)
    assert main_module.delete_project_route(created.id, user, db) is None
    with pytest.raises(HTTPException) as exc_info:
        main_module.get_project_route(created.id, user, db)
    assert exc_info.value.status_code == 404


def test_ingest_tags_owner_and_enforces_it(local_project):
    owner = _user()
    stranger = _user()
    record = main_module.ingest_repo(
        main_module.IngestRequest(url="https://github.com/acme/local"), owner
    )
    assert record["status"] == "complete"
    assert (
        main_module.get_repo(record["ingestion_id"], owner)["ingestion_id"]
        == record["ingestion_id"]
    )
    with pytest.raises(HTTPException) as exc_info:
        main_module.get_repo(record["ingestion_id"], stranger)
    assert exc_info.value.status_code == 404


def test_investigation_skips_foreign_ingestion(monkeypatch, local_project):
    owner = _user()
    stranger = _user()
    record = main_module.ingest_repo(
        main_module.IngestRequest(url="https://github.com/acme/local"), owner
    )

    def must_not_run(*args, **kwargs):
        raise AssertionError("foreign ingestion must not execute")

    monkeypatch.setattr(main_module, "execute", must_not_run)
    with pytest.raises(HTTPException) as exc_info:
        main_module.start_real_investigation(
            main_module.RealInvestigationRequest(
                ingestion_id=record["ingestion_id"]
            ),
            stranger,
        )
    assert exc_info.value.status_code == 404


def test_untagged_record_inaccessible_through_routes(monkeypatch, local_project):
    """Legacy records without an owner tag are denied on protected routes."""
    record = ingest_repository("https://github.com/acme/local")
    assert record["status"] == "complete"
    user = _user()

    with pytest.raises(HTTPException) as exc_info:
        main_module.get_repo(record["ingestion_id"], user)
    assert exc_info.value.status_code == 404

    def must_not_run(*args, **kwargs):
        raise AssertionError("untagged ingestion must not execute")

    monkeypatch.setattr(main_module, "execute", must_not_run)
    with pytest.raises(HTTPException) as exc_info:
        main_module.start_real_investigation(
            main_module.RealInvestigationRequest(
                ingestion_id=record["ingestion_id"]
            ),
            user,
        )
    assert exc_info.value.status_code == 404
