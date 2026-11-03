"""Tests for the authenticated jobs API (submit, poll, list, active, cancel).

Debug-path tests reuse the established fast patterns: a fixture project is
ingested through a stubbed clone, materialization copies the fixture (no
network), and execution/diagnosis come from scripted fakes. Static modes
need no seams at all.
"""

from __future__ import annotations

import shutil

import pytest

from conftest import ALICE, BOB
from app import job_runner
from app import ingestion as ingestion_module
from app import real_investigation as pipeline
from app import store
from app.execution import ExecutionBackend, ExecutionResult
from app.ingestion import clear_ingestions, ingest_repository
from app.patch_guard import FileEdit, PatchSet

FAKE_SHA = "b" * 40
DIAGNOSIS = "add subtracts instead of adding."

BUGGY_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nrequires-python = ">=3.11"\n',
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": (
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}


@pytest.fixture(autouse=True)
def _isolate(run_overrides):
    clear_ingestions()
    yield
    clear_ingestions()


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "project"
    for rel, content in BUGGY_PROJECT.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


@pytest.fixture
def ingested(monkeypatch, project_dir):
    """A real ingestion record for the fixture project (stubbed clone)."""
    monkeypatch.setattr(
        ingestion_module,
        "clone_and_pin",
        lambda url, dest, ref=None: (
            shutil.copytree(project_dir, dest),
            FAKE_SHA,
        )[1],
    )
    record = ingest_repository("https://github.com/acme/calc")
    assert record["status"] == "complete"
    return record


@pytest.fixture
def materialized(monkeypatch, project_dir):
    """Stub pipeline materialization to copy the fixture project (no network)."""
    monkeypatch.setattr(
        pipeline,
        "clone_and_pin",
        lambda url, dest, ref=None: (
            shutil.copytree(project_dir, dest),
            FAKE_SHA,
        )[1],
    )


class FakeBackend(ExecutionBackend):
    name = "fake-test"
    isolation = "fake-test-only"

    def __init__(self, script):
        self.script = list(script)

    def is_available(self):
        return True, "fake"

    def run(self, request):
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


def _baseline_failure():
    return {
        "status": "test_failure",
        "passed": False,
        "exit_code": 1,
        "output": "FAILED tests/test_calc.py::test_add - assert -1 == 5\n1 failed",
        "failures": [
            {
                "test": "tests/test_calc.py::test_add",
                "kind": "failed",
                "message": "assert -1 == 5",
                "traceback": '  File "pkg/calc.py", line 2, in add\nE   assert -1 == 5',
            }
        ],
    }


def _verification_pass():
    return {
        "status": "ok",
        "passed": True,
        "exit_code": 0,
        "output": "1 passed in 0.3s",
        "failures": [],
    }


def _fix_diagnose(**kwargs):
    return DIAGNOSIS, PatchSet(
        edits=(FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),),
        reason="Fix the operator.",
    )


def _seed_job(**overrides):
    now = store.utcnow_iso()
    row = {
        "job_id": "job-seed",
        "investigation_id": "inv-seed",
        "user_id": ALICE.id,
        "ingestion_id": "ing-seed",
        "mode": "debug",
        "max_attempts": 3,
        "status": "running",
        "progress_pct": 30,
        "repo_name": "acme/calc",
        "repo_url": "https://github.com/acme/calc",
        "commit_sha": "c" * 40,
        "repo_ref": None,
        "verification_status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    store.create_job(row)
    return row


def test_create_returns_queued_ids(client, ingested):
    response = client.post(
        "/api/jobs",
        json={"ingestion_id": ingested["ingestion_id"], "mode": "explain"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["investigation_id"]


def test_create_unknown_ingestion_404(client):
    response = client.post("/api/jobs", json={"ingestion_id": "nope", "mode": "debug"})
    assert response.status_code == 404


def test_create_missing_ingestion_422(client):
    response = client.post("/api/jobs", json={"ingestion_id": "  "})
    assert response.status_code == 422


def test_create_invalid_mode_422(client, ingested):
    response = client.post(
        "/api/jobs", json={"ingestion_id": ingested["ingestion_id"], "mode": "turbo"}
    )
    assert response.status_code == 422


def test_create_requires_auth(anon_client, ingested):
    response = anon_client.post(
        "/api/jobs", json={"ingestion_id": ingested["ingestion_id"]}
    )
    assert response.status_code == 401


def test_explain_completes_with_static_findings(client, ingested, wait_for_terminal):
    created = client.post(
        "/api/jobs",
        json={"ingestion_id": ingested["ingestion_id"], "mode": "explain"},
    ).json()
    detail = wait_for_terminal(client, created["job_id"])
    assert detail["status"] == "completed"
    assert detail["mode"] == "explain"
    assert detail["progress_pct"] == 100
    assert detail["verification_status"] == "not_applicable"
    assert detail["findings"]
    assert detail["final_report"]["mode"] == "explain"
    assert "acme/calc" in detail["final_report"]["summary"]
    assert detail["limitations"] == ["Static analysis only; nothing was executed."]
    assert detail["runs"] == []
    assert detail["repo_name"] == "acme/calc"
    assert detail["commit_sha"] == FAKE_SHA


def test_general_routes_message_to_health(client, ingested, wait_for_terminal):
    created = client.post(
        "/api/jobs",
        json={
            "ingestion_id": ingested["ingestion_id"],
            "mode": "general",
            "message": "please run a security audit",
        },
    ).json()
    detail = wait_for_terminal(client, created["job_id"])
    assert detail["status"] == "completed"
    assert detail["mode"] == "general"
    assert detail["final_report"]["routing"]["mode"] == "health"
    assert detail["final_report"]["overall"] == "healthy"


def test_debug_verifies_patch_with_fakes(
    client, ingested, materialized, run_overrides, wait_for_terminal
):
    run_overrides["backend"] = FakeBackend([_baseline_failure(), _verification_pass()])
    run_overrides["diagnose"] = _fix_diagnose
    created = client.post(
        "/api/jobs",
        json={
            "ingestion_id": ingested["ingestion_id"],
            "mode": "general",
            "message": "tests are failing",
        },
    ).json()
    detail = wait_for_terminal(client, created["job_id"])
    assert detail["status"] == "completed"
    assert detail["mode"] == "general"
    assert detail["final_report"]["routing"]["mode"] == "debug"
    assert detail["verification_status"] == "verified"
    assert [run["kind"] for run in detail["runs"]] == ["baseline", "verification"]
    assert detail["runs"][0]["passed"] is False
    assert detail["runs"][1]["passed"] is True
    assert detail["runs"][0]["failures"][0]["test"] == "tests/test_calc.py::test_add"
    assert detail["patches"][0]["status"] == "applied"
    assert detail["patches"][0]["files"][0]["file"] == "pkg/calc.py"
    assert detail["findings"][0]["kind"] == "diagnosis"
    assert DIAGNOSIS in detail["findings"][0]["detail"]
    assert detail["job"]["attempts"] >= 1
    assert detail["job"]["max_attempts"] == 3
    assert {"materialize", "baseline", "repair", "report"} <= set(
        detail["stages_completed"]
    )
    assert detail["progress_pct"] == 100
    assert detail["timings"]["total_ms"] >= 0
    assert detail["completed_at"]


def test_debug_green_baseline_not_applicable(
    client, ingested, materialized, run_overrides, wait_for_terminal
):
    run_overrides["backend"] = FakeBackend([_verification_pass()])
    run_overrides["diagnose"] = _fix_diagnose
    created = client.post(
        "/api/jobs",
        json={"ingestion_id": ingested["ingestion_id"], "mode": "debug"},
    ).json()
    detail = wait_for_terminal(client, created["job_id"])
    assert detail["status"] == "completed"
    assert detail["verification_status"] == "not_applicable"
    assert detail["findings"][0]["title"] == "Baseline already passes"
    assert detail["patches"] == []


def test_debug_install_error_fails_honestly(
    client, ingested, materialized, run_overrides, wait_for_terminal
):
    run_overrides["backend"] = FakeBackend(
        [
            {
                "status": "install_error",
                "passed": None,
                "install_ok": False,
                "install_log": "pip exploded",
                "output": "",
            }
        ]
    )
    run_overrides["diagnose"] = _fix_diagnose
    created = client.post(
        "/api/jobs",
        json={"ingestion_id": ingested["ingestion_id"], "mode": "debug"},
    ).json()
    detail = wait_for_terminal(client, created["job_id"])
    assert detail["status"] == "failed"
    assert detail["error"]["reason"] == "dependency_error"
    assert detail["runs"][0]["kind"] == "baseline"
    assert detail["verification_status"] == "not_verified"


def test_get_missing_job_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_job_ownership_404(client, current_user):
    _seed_job()
    current_user["user"] = BOB
    assert client.get("/api/jobs/job-seed").status_code == 404


def test_list_returns_newest_first_with_limit(client):
    _seed_job(
        job_id="job-old",
        investigation_id="inv-old",
        updated_at="2026-01-01T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
    )
    _seed_job(
        job_id="job-new",
        investigation_id="inv-new",
        updated_at="2026-01-02T00:00:00Z",
        created_at="2026-01-02T00:00:00Z",
    )
    body = client.get("/api/jobs?limit=10").json()
    assert [card["job_id"] for card in body["jobs"]] == ["job-new", "job-old"]
    assert len(client.get("/api/jobs?limit=1").json()["jobs"]) == 1


def test_active_empty_404(client):
    assert client.get("/api/jobs/active").status_code == 404


def test_active_returns_running_row(client):
    _seed_job()
    response = client.get("/api/jobs/active")
    assert response.status_code == 200
    assert response.json()["job"]["job_id"] == "job-seed"


def test_cancel_running_job(client):
    _seed_job()
    body = client.post("/api/jobs/job-seed/cancel").json()
    assert body["status"] == "cancelled"
    assert body["completed_at"]
    assert body["verification_status"] == "not_verified"
    again = client.post("/api/jobs/job-seed/cancel").json()
    assert again["status"] == "cancelled"


def test_cancel_terminal_unchanged(client, ingested, wait_for_terminal):
    created = client.post(
        "/api/jobs",
        json={"ingestion_id": ingested["ingestion_id"], "mode": "explain"},
    ).json()
    detail = wait_for_terminal(client, created["job_id"])
    again = client.post(f"/api/jobs/{created['job_id']}/cancel").json()
    assert again["status"] == "completed"
    assert again["completed_at"] == detail["completed_at"]


def test_cancel_missing_and_foreign_404(client, current_user):
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    _seed_job()
    current_user["user"] = BOB
    assert client.post("/api/jobs/job-seed/cancel").status_code == 404


def test_sweep_fails_stale(client):
    _seed_job()
    assert job_runner.startup_sweep() == 1
    body = client.get("/api/jobs/job-seed").json()
    assert body["status"] == "failed"
    assert body["error"]["reason"] == "server_restart"


def test_card_shape_keys(client):
    _seed_job()
    card = client.get("/api/jobs?limit=10").json()["jobs"][0]
    assert set(card) == {
        "job_id",
        "investigation_id",
        "status",
        "attempts",
        "max_attempts",
        "error",
        "created_at",
        "updated_at",
        "repo_name",
        "mode",
        "progress_pct",
    }


def test_detail_shape_keys(client):
    _seed_job()
    detail = client.get("/api/jobs/job-seed").json()
    assert set(detail) == {
        "investigation_id",
        "repo_url",
        "repo_name",
        "commit_sha",
        "ref",
        "mode",
        "user_message",
        "status",
        "stage",
        "stages_completed",
        "progress_pct",
        "partial",
        "verification_status",
        "error",
        "limitations",
        "final_report",
        "timings",
        "created_at",
        "updated_at",
        "completed_at",
        "job",
        "attempts",
        "runs",
        "findings",
        "patches",
        "messages",
    }
