"""Shared fixtures for the authenticated API suites.

Every fixture is opt-in: the pre-existing suites are unaffected. Tests talk
to the real FastAPI app with ``get_current_user`` overridden (no network
auth) and a per-test SQLite file via ``REPORESCUE_DB_PATH``.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import job_runner
from app.auth import AuthenticatedUser, get_current_user
from app.main import app

ALICE_ID = "00000000-0000-0000-0000-000000000001"
BOB_ID = "00000000-0000-0000-0000-000000000002"

ALICE = AuthenticatedUser(
    id=ALICE_ID,
    email="alice@example.com",
    claims={"sub": ALICE_ID, "email": "alice@example.com"},
)
BOB = AuthenticatedUser(
    id=BOB_ID, email="bob@example.com", claims={"sub": BOB_ID}
)


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    """Point the store at a per-test SQLite file."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("REPORESCUE_DB_PATH", str(path))
    return str(path)


@pytest.fixture
def current_user():
    """Mutable auth box: tests swap the caller by reassigning ``["user"]``."""
    return {"user": ALICE}


@pytest.fixture
def client(db_path, current_user):
    """TestClient authenticated as ``current_user["user"]`` (lifespan on)."""
    app.dependency_overrides[get_current_user] = lambda: current_user["user"]
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def anon_client(db_path):
    """TestClient with real auth (no override): no token means 401."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def run_overrides():
    """Resettable job-runner test seams (backend/diagnose injection)."""
    old = dict(job_runner._RUN_OVERRIDES)
    job_runner._RUN_OVERRIDES.update({"backend": None, "diagnose": None})
    yield job_runner._RUN_OVERRIDES
    job_runner._RUN_OVERRIDES.update(old)


@pytest.fixture
def wait_for_terminal():
    """Poll ``GET /api/jobs/{id}`` until the job reaches a terminal state."""

    def _wait(api_client, job_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = api_client.get(f"/api/jobs/{job_id}")
            assert response.status_code == 200, response.text
            detail = response.json()
            if detail["status"] in ("completed", "failed", "cancelled"):
                return detail
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not reach a terminal state")

    return _wait
