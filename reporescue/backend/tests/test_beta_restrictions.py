"""Beta restrictions: public repos only, Debug supports Python only.

Public-only: the backend rejects private repositories before cloning,
executing, or sending context to the AI provider. A best-effort GitHub
API pre-check rejects privately-visible repos without cloning; any API
anomaly fails open to the clone attempt, and clone-level "not found"
maps to the same structured reason.

Debug Python-only: Debug mode (direct or routed) is rejected for
repositories without Python runtime evidence, before quota is touched.
Other modes stay available for supported public repositories.
"""
import io
import json
import shutil
import urllib.error
import urllib.request
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import ingestion as ingestion_module
from app import main as main_module
from app.auth import AuthenticatedUser
from app.github_ingest import IngestionError
from app.ingestion import set_ingestion_owner
from app.models import Base, Investigation, Profile
from app.usage_quota import usage_status


@pytest.fixture(autouse=True)
def _isolate_store():
    ingestion_module.clear_ingestions()
    yield
    ingestion_module.clear_ingestions()


class _FakeAPIResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _api(monkeypatch, response):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        if isinstance(response, Exception):
            raise response
        return _FakeAPIResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _http_error(code):
    return urllib.error.HTTPError(
        "https://api.github.com/repos/o/r", code, "error", {}, io.BytesIO(b"{}")
    )


def _stub_clone(monkeypatch, project_dir, sha="d" * 40):
    import shutil

    def fake_clone(url, destination, ref=None):
        shutil.copytree(project_dir, destination)
        return sha

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)


@pytest.fixture()
def project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pkg").mkdir()
    (project / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "calc"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    return project


def test_public_repo_proceeds_to_clone(monkeypatch, project_dir):
    seen = _api(monkeypatch, {"private": False})
    _stub_clone(monkeypatch, project_dir)
    record = ingestion_module.ingest_repository("https://github.com/acme/calc")
    assert record["status"] == "complete"
    assert seen["url"] == "https://api.github.com/repos/acme/calc"
    assert seen["auth"] is None


def test_private_repo_rejected_before_clone(monkeypatch, project_dir):
    _api(monkeypatch, {"private": True})

    def _must_not_clone(url, destination, ref=None):
        raise AssertionError("private repo must not be cloned")

    monkeypatch.setattr(ingestion_module, "clone_and_pin", _must_not_clone)
    record = ingestion_module.ingest_repository("https://github.com/acme/secret")
    assert record["status"] == "error"
    assert record["reason"] == "private_repo_not_supported"
    assert "public" in record["message"].lower()


def test_missing_repo_rejected_before_clone(monkeypatch, project_dir):
    _api(monkeypatch, _http_error(404))

    def _must_not_clone(url, destination, ref=None):
        raise AssertionError("missing repo must not be cloned")

    monkeypatch.setattr(ingestion_module, "clone_and_pin", _must_not_clone)
    record = ingestion_module.ingest_repository("https://github.com/acme/gone")
    assert record["status"] == "error"
    assert record["reason"] == "private_repo_not_supported"


def test_rate_limited_api_falls_back_to_clone(monkeypatch, project_dir):
    _api(monkeypatch, _http_error(403))
    _stub_clone(monkeypatch, project_dir)
    record = ingestion_module.ingest_repository("https://github.com/acme/calc")
    assert record["status"] == "complete"


def test_api_network_error_falls_back_to_clone(monkeypatch, project_dir):
    _api(monkeypatch, urllib.error.URLError("dns down"))
    _stub_clone(monkeypatch, project_dir)
    record = ingestion_module.ingest_repository("https://github.com/acme/calc")
    assert record["status"] == "complete"


def test_clone_not_found_maps_to_private_reason(monkeypatch):
    _api(monkeypatch, _http_error(403))

    def _missing(url, destination, ref=None):
        raise IngestionError("Repository not found.", "not_found")

    monkeypatch.setattr(ingestion_module, "clone_and_pin", _missing)
    record = ingestion_module.ingest_repository("https://github.com/acme/gone")
    assert record["status"] == "error"
    assert record["reason"] == "private_repo_not_supported"
    assert "public" in record["message"].lower()


# --- Debug Python-only ---


JS_PROJECT = {
    "package.json": '{"name": "app", "version": "1.0.0"}\n',
    "index.js": "console.log('hi');\n",
}


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/beta.db",
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


@pytest.fixture()
def js_dir(tmp_path):
    project = tmp_path / "jsproj"
    for rel, content in JS_PROJECT.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


def _user(owner):
    return AuthenticatedUser(id=owner, email=None, claims={})


def _ingest_owned(monkeypatch, source, owner, url="https://github.com/acme/repo"):
    def fake_clone(clone_url, destination, ref=None):
        shutil.copytree(source, destination)
        return "e" * 40

    monkeypatch.setattr(ingestion_module, "clone_and_pin", fake_clone)
    record = ingestion_module.ingest_repository(url)
    assert record["status"] == "complete"
    set_ingestion_owner(record["ingestion_id"], str(owner))
    return record


def _job(db, owner, record, **kwargs):
    params = {"ingestion_id": record["ingestion_id"], "mode": "debug"}
    params.update(kwargs)
    body = main_module.JobCreate(**params)
    return main_module.create_job_route(body, _user(owner), db)


def test_debug_on_javascript_rejected_without_quota(db, owner, monkeypatch, js_dir):
    record = _ingest_owned(monkeypatch, js_dir, owner)
    with pytest.raises(HTTPException) as exc_info:
        _job(db, owner, record)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "DEBUG_PYTHON_ONLY"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0
    assert db.scalars(select(Investigation)).all() == []


def test_debug_on_python_accepted(db, owner, monkeypatch, project_dir):
    record = _ingest_owned(monkeypatch, project_dir, owner)
    result = _job(db, owner, record, message="fix it")
    assert result["status"] == "queued"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_general_preselect_debug_on_javascript_rejected(db, owner, monkeypatch, js_dir):
    record = _ingest_owned(monkeypatch, js_dir, owner)
    with pytest.raises(HTTPException) as exc_info:
        _job(db, owner, record, mode="general", message="fix it", selected_mode="debug")
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "DEBUG_PYTHON_ONLY"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_general_routed_debug_on_javascript_rejected(db, owner, monkeypatch, js_dir):
    record = _ingest_owned(monkeypatch, js_dir, owner)
    with pytest.raises(HTTPException) as exc_info:
        _job(
            db,
            owner,
            record,
            mode="general",
            message="tests are failing with an assertion error, please fix the bug",
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "DEBUG_PYTHON_ONLY"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_explain_on_javascript_accepted(db, owner, monkeypatch, js_dir):
    record = _ingest_owned(monkeypatch, js_dir, owner)
    result = _job(db, owner, record, mode="explain", message="explain this")
    assert result["status"] == "queued"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_sync_debug_on_javascript_rejected(db, owner, monkeypatch, js_dir):
    record = _ingest_owned(monkeypatch, js_dir, owner)
    request = main_module.RealInvestigationRequest(
        ingestion_id=record["ingestion_id"], mode="debug", message="fix it"
    )
    with pytest.raises(HTTPException) as exc_info:
        main_module.start_real_investigation(request, _user(owner), db)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "DEBUG_PYTHON_ONLY"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0
