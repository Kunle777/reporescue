"""Tests for the in-memory API rate limiter."""

import pytest
from fastapi import HTTPException

from app import rate_limit
from app.auth import AuthenticatedUser


def _user(uid: str) -> AuthenticatedUser:
    return AuthenticatedUser(id=uid, email=None, claims={})


def test_allows_calls_within_budget():
    for _ in range(5):
        rate_limit.check_rate_limit("jobs", _user("u1"))


def test_rejects_beyond_budget_with_429(monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, "jobs", (2, 60))
    rate_limit.check_rate_limit("jobs", _user("u1"))
    rate_limit.check_rate_limit("jobs", _user("u1"))
    with pytest.raises(HTTPException) as exc_info:
        rate_limit.check_rate_limit("jobs", _user("u1"))
    assert exc_info.value.status_code == 429


def test_buckets_are_per_user_and_per_route(monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, "jobs", (1, 60))
    rate_limit.check_rate_limit("jobs", _user("u1"))
    rate_limit.check_rate_limit("jobs", _user("u2"))
    rate_limit.check_rate_limit("classify", _user("u1"))
    with pytest.raises(HTTPException):
        rate_limit.check_rate_limit("jobs", _user("u1"))


def test_window_expiry_frees_budget(monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, "jobs", (1, 60))
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    rate_limit.check_rate_limit("jobs", _user("u1"))
    now[0] += 61.0
    rate_limit.check_rate_limit("jobs", _user("u1"))


def test_message_size_caps_reject_oversize_bodies():
    from pydantic import ValidationError

    from app.main import ClassifyRequest, JobCreate, RealInvestigationRequest

    big = "x" * 8001
    with pytest.raises(ValidationError):
        JobCreate(ingestion_id="a", message=big)
    with pytest.raises(ValidationError):
        RealInvestigationRequest(ingestion_id="a", message=big)
    with pytest.raises(ValidationError):
        ClassifyRequest(message=big)
    assert JobCreate(ingestion_id="a", message="ok").message == "ok"
