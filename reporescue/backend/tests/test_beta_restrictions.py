"""Beta restriction: public GitHub repositories only.

The backend must reject private repositories before cloning, executing,
or sending context to the AI provider. A best-effort GitHub API
pre-check rejects privately-visible repos without cloning; any API
anomaly fails open to the clone attempt, and clone-level "not found"
maps to the same structured reason.
"""
import io
import json
import urllib.error
import urllib.request

import pytest

from app import ingestion as ingestion_module
from app.github_ingest import IngestionError


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
