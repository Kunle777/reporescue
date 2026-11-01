"""Tests for the ingestion workflow and its API endpoints.

End-to-end ingestion is exercised with a stubbed clone step that materializes
a prepared local project (no network). Real clone behavior is covered in
test_github_ingest.py; the checkout-fixture regression is covered by the
pre-existing orchestrator tests.
"""

import shutil
import subprocess

import pytest

from app import ingestion, main
from app.github_ingest import IngestionError
from app.ingestion import clear_ingestions, get_ingestion, ingest_repository

FAKE_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _isolate_store():
    clear_ingestions()
    yield
    clear_ingestions()


@pytest.fixture
def python_project(tmp_path):
    project = tmp_path / "project"
    files = {
        "pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.11"\ndependencies = ["fastapi"]\n',
        "src/demo/__init__.py": "",
        "src/demo/main.py": "print('hi')\n",
        "tests/test_demo.py": "def test_ok():\n    assert True\n",
    }
    for rel, content in files.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


@pytest.fixture
def stub_clone(python_project, monkeypatch):
    seen = {}

    def fake_clone(source, destination, ref=None):
        seen["source"] = source
        seen["ref"] = ref
        shutil.copytree(python_project, destination)
        return FAKE_SHA

    monkeypatch.setattr(ingestion, "clone_and_pin", fake_clone)
    return seen


def test_ingest_complete_record(stub_clone):
    record = ingest_repository("https://github.com/acme/demo.git", ref="v1.0.0")

    assert record["status"] == "complete"
    assert record["ingestion_id"]
    profile = record["profile"]
    assert profile["url"] == "https://github.com/acme/demo"
    assert profile["name"] == "acme/demo"
    assert profile["commit_sha"] == FAKE_SHA
    assert profile["ref"] == "v1.0.0"
    assert profile["supported"] is True
    assert profile["dependency_format"] == "pyproject"
    assert stub_clone["source"] == "https://github.com/acme/demo"
    assert stub_clone["ref"] == "v1.0.0"
    context = record["context"]
    assert context["entry_points"] == ["src/demo/main.py"]
    assert "src/demo/main.py" in context["excerpts"]


def test_ingest_record_retrievable(stub_clone):
    record = ingest_repository("https://github.com/acme/demo")

    fetched = get_ingestion(record["ingestion_id"])

    assert fetched["status"] == "complete"
    assert fetched["profile"]["commit_sha"] == FAKE_SHA


def test_ingest_unknown_id_reports_not_found():
    result = get_ingestion("missing-id")

    assert result["status"] == "error"
    assert result["reason"] == "not_found"


def test_ingest_empty_id_rejected():
    assert get_ingestion("  ")["status"] == "error"


def test_ingest_invalid_url_never_clones(monkeypatch):
    def must_not_run(*args, **kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("clone must not run for an invalid URL")

    monkeypatch.setattr(ingestion, "clone_and_pin", must_not_run)

    result = ingest_repository("not-a-url")

    assert result["status"] == "error"
    assert result["reason"] == "invalid_url"


def test_ingest_unsupported_host_never_clones(monkeypatch):
    def must_not_run(*args, **kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("clone must not run for an unsupported host")

    monkeypatch.setattr(ingestion, "clone_and_pin", must_not_run)

    result = ingest_repository("https://gitlab.com/acme/demo")

    assert result["status"] == "error"
    assert result["reason"] == "unsupported_host"


def test_ingest_invalid_ref_never_clones(monkeypatch):
    def must_not_run(*args, **kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("clone must not run for an invalid ref")

    monkeypatch.setattr(ingestion, "clone_and_pin", must_not_run)

    result = ingest_repository("https://github.com/acme/demo", ref="../evil")

    assert result["status"] == "error"
    assert result["reason"] == "invalid_ref"


def test_ingest_clone_failure_reports_error(monkeypatch):
    def fail_clone(*args, **kwargs):
        raise IngestionError("Could not clone repository.", "clone_failed")

    monkeypatch.setattr(ingestion, "clone_and_pin", fail_clone)

    result = ingest_repository("https://github.com/acme/demo")

    assert result["status"] == "error"
    assert result["reason"] == "clone_failed"
    assert result["url"] == "https://github.com/acme/demo"


def test_ingest_unsupported_project_still_completes(tmp_path, monkeypatch):
    project = tmp_path / "web"
    (project / "src").mkdir(parents=True)
    (project / "package.json").write_text('{"name": "web"}\n', encoding="utf-8")
    (project / "src" / "index.js").write_text("console.log(1);\n", encoding="utf-8")

    def fake_clone(source, destination, ref=None):
        shutil.copytree(project, destination)
        return FAKE_SHA

    monkeypatch.setattr(ingestion, "clone_and_pin", fake_clone)

    record = ingest_repository("https://github.com/acme/web")

    assert record["status"] == "complete"
    assert record["profile"]["supported"] is False
    assert record["profile"]["primary_language"] == "javascript"
    assert record["warnings"]


def test_ingest_evicts_oldest_records(stub_clone, monkeypatch):
    monkeypatch.setattr(ingestion, "MAX_STORED_RECORDS", 2)

    first = ingest_repository("https://github.com/acme/one")
    ingest_repository("https://github.com/acme/two")
    ingest_repository("https://github.com/acme/three")

    assert get_ingestion(first["ingestion_id"])["reason"] == "not_found"


def test_ingest_with_real_local_clone_end_to_end(tmp_path, monkeypatch):
    """Full pipeline with a real git clone of a local repo (no network).

    The URL validator only admits github.com, so this test monkeypatches the
    validator to redirect one magic URL to a local fixture; the clone,
    profile, context, and store steps all run for real.
    """
    from app import github_ingest

    repo = tmp_path / "source"
    (repo / "pkg").mkdir(parents=True)
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    identity = ["-c", "user.email=t@e.com", "-c", "user.name=t"]

    def git(args):
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return result

    git(["init", "-b", "main"])
    git([*identity, "add", "."])
    git([*identity, "commit", "-m", "init"])
    expected_sha = git(["rev-parse", "HEAD"]).stdout.strip()

    real_normalize = github_ingest.normalize_github_url

    def redirect(url):
        normalized = real_normalize(url)
        assert normalized.url == "https://github.com/acme/local"
        return normalized

    real_clone = github_ingest.clone_and_pin

    def local_clone(source, destination, ref=None):
        assert source == "https://github.com/acme/local"
        return real_clone(str(repo), destination, ref)

    monkeypatch.setattr(github_ingest, "normalize_github_url", redirect)
    monkeypatch.setattr(ingestion, "clone_and_pin", local_clone)

    record = ingest_repository("https://github.com/acme/local")

    assert record["status"] == "complete"
    assert record["profile"]["commit_sha"] == expected_sha
    assert record["profile"]["supported"] is True
    assert "pkg/core.py" in record["context"]["excerpts"]
    assert get_ingestion(record["ingestion_id"])["status"] == "complete"


# --- API route wiring ---


def test_api_routes_registered():
    paths = {(route.path, tuple(route.methods or ())) for route in main.app.routes}
    assert ("/api/repos/ingest", ("POST",)) in paths
    assert ("/api/repos/{ingestion_id}", ("GET",)) in paths
    # Pre-existing contracts are untouched.
    assert ("/api/health", ("GET",)) in paths
    assert ("/api/demo/investigate", ("POST",)) in paths


def test_api_ingest_endpoint_delegates(monkeypatch):
    seen = {}

    def fake_ingest(url, ref=None):
        seen["url"] = url
        seen["ref"] = ref
        return {"status": "complete"}

    monkeypatch.setattr(main, "ingest_repository", fake_ingest)

    result = main.ingest_repo(main.IngestRequest(url="https://github.com/a/b", ref="main"))

    assert result == {"status": "complete"}
    assert seen == {"url": "https://github.com/a/b", "ref": "main"}


def test_api_get_endpoint_delegates(monkeypatch):
    monkeypatch.setattr(main, "get_ingestion", lambda ingestion_id: {"status": "complete", "id": ingestion_id})

    assert main.get_repo("abc123") == {"status": "complete", "id": "abc123"}


def test_api_ingest_request_defaults():
    request = main.IngestRequest(url="https://github.com/a/b")
    assert request.ref is None
