"""Conversation reliability: success-only charging and safe retry.

A provider failure must mark the idempotency claim failed (never
completed), refund quota, persist no canned answer, and leave the same
key retryable. Success replays must not charge again.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import main as main_module
from app.auth import AuthenticatedUser
from app.jobqueue import create_investigation
from app.meta_client import MetaAPIError, MetaRateLimitError
from app.models import Base, IdempotencyRecord, InvestigationMessage, Profile
from app.usage_quota import usage_status

FAKE_SHA = "c" * 40
IDEMPOTENCY_KEY = "key-" + "k" * 28


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("REPORESCUE_AI_MODE", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("REPORESCUE_AI_MODE", "meta")
    monkeypatch.setenv("META_API_KEY", "test-key-not-real")


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path.as_posix()}/conversation.db",
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
def investigation(db, owner):
    investigation, _ = create_investigation(
        db,
        owner_id=owner,
        repo_url="https://github.com/acme/calc",
        repo_name="acme/calc",
        commit_sha=FAKE_SHA,
        mode="debug",
    )
    return investigation


def _user(owner):
    return AuthenticatedUser(id=owner, email=None, claims={})


def _send(db, investigation, owner, message="Why did it fail?", key=IDEMPOTENCY_KEY):
    body = main_module.ConversationMessageRequest(message=message)
    return main_module.send_investigation_message(
        investigation.id, body, _user(owner), db, key
    )


def _messages(db, investigation):
    return db.scalars(
        select(InvestigationMessage)
        .where(InvestigationMessage.investigation_id == investigation.id)
        .order_by(InvestigationMessage.created_at)
    ).all()


def _record(db, owner, investigation, key=IDEMPOTENCY_KEY):
    return db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.owner_id == owner,
            IdempotencyRecord.scope == f"conversation:{investigation.id}",
            IdempotencyRecord.request_key == key,
        )
    )


def _ok_provider(monkeypatch, answer="Because the discount is uncapped."):
    def fake_reply(*, context, history, question, api_key=None):
        assert api_key == "test-key-not-real"
        return {"answer": answer, "evidence_ids": []}

    monkeypatch.setattr(
        main_module.meta_client_module, "conversation_reply", fake_reply
    )


def _failing_provider(monkeypatch, error):
    def fake_reply(*, context, history, question, api_key=None):
        raise error

    monkeypatch.setattr(
        main_module.meta_client_module, "conversation_reply", fake_reply
    )


def test_success_consumes_one_use_and_persists_both_messages(
    db, owner, investigation, monkeypatch
):
    _ok_provider(monkeypatch)
    response = _send(db, investigation, owner)
    assert response["assistant_message"]["content"].startswith("Because")
    assert response["assistant_message"]["grounding_status"] == "unverified"
    assert [(m.role, m.content) for m in _messages(db, investigation)] == [
        ("user", "Why did it fail?"),
        ("assistant", "Because the discount is uncapped."),
    ]
    assert _record(db, owner, investigation).status == "completed"
    status = usage_status(db, user_id=str(owner))
    assert (status["ai"]["used"], status["debug"]["used"]) == (1, 0)


def test_provider_error_returns_502_without_charging_or_persisting(
    db, owner, investigation, monkeypatch
):
    _failing_provider(monkeypatch, MetaAPIError("Meta API request failed (503)."))
    with pytest.raises(HTTPException) as exc_info:
        _send(db, investigation, owner)
    assert exc_info.value.status_code == 502
    assert _record(db, owner, investigation).status == "failed"
    assert _messages(db, investigation) == []
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_provider_rate_limit_returns_429_without_charging(
    db, owner, investigation, monkeypatch
):
    _failing_provider(monkeypatch, MetaRateLimitError("Meta API rate limited."))
    with pytest.raises(HTTPException) as exc_info:
        _send(db, investigation, owner)
    assert exc_info.value.status_code == 429
    assert _record(db, owner, investigation).status == "failed"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_unexpected_exception_marks_failed_and_refunds(
    db, owner, investigation, monkeypatch
):
    _failing_provider(monkeypatch, RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        _send(db, investigation, owner)
    assert _record(db, owner, investigation).status == "failed"
    assert _messages(db, investigation) == []
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0


def test_retry_after_failure_succeeds_with_single_charge(
    db, owner, investigation, monkeypatch
):
    _failing_provider(monkeypatch, MetaAPIError("Meta API request failed."))
    with pytest.raises(HTTPException):
        _send(db, investigation, owner)
    _ok_provider(monkeypatch)
    response = _send(db, investigation, owner)
    assert response["assistant_message"]["content"].startswith("Because")
    assert len(_messages(db, investigation)) == 2
    assert _record(db, owner, investigation).status == "completed"
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_replay_after_success_consumes_nothing(
    db, owner, investigation, monkeypatch
):
    _ok_provider(monkeypatch)
    first = _send(db, investigation, owner)
    second = _send(db, investigation, owner)
    assert second == first
    assert len(_messages(db, investigation)) == 2
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_conflicting_key_reuse_rejected(db, owner, investigation, monkeypatch):
    _ok_provider(monkeypatch)
    _send(db, investigation, owner, message="First question?")
    with pytest.raises(HTTPException) as exc_info:
        _send(db, investigation, owner, message="Different question?")
    assert exc_info.value.status_code == 409
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_stale_processing_claim_reclaimed_with_refund(
    db, owner, investigation, monkeypatch
):
    stale = IdempotencyRecord(
        owner_id=owner,
        scope=f"conversation:{investigation.id}",
        request_key=IDEMPOTENCY_KEY,
        request_hash=main_module._request_fingerprint({"message": "Why did it fail?"}),
        status="processing",
        quota_kind="user",
        quota_refunded=False,
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    db.add(stale)
    db.commit()
    from app.usage_quota import reserve_operation

    reserve_operation(db, user_id=str(owner))
    _ok_provider(monkeypatch)
    _send(db, investigation, owner)
    assert _record(db, owner, investigation).status == "completed"
    # The orphaned slot was refunded before the retry reserved again.
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_non_meta_mode_rejects_without_charging(
    db, owner, investigation, monkeypatch
):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "stub")
    with pytest.raises(HTTPException) as exc_info:
        _send(db, investigation, owner)
    assert exc_info.value.status_code == 503
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 0
    # The rolled-back claim leaves the key reusable.
    monkeypatch.setenv("REPORESCUE_AI_MODE", "meta")
    _ok_provider(monkeypatch)
    _send(db, investigation, owner)
    assert usage_status(db, user_id=str(owner))["ai"]["used"] == 1


def test_answer_sanitized_before_persist(db, owner, investigation, monkeypatch):
    _ok_provider(monkeypatch, answer="Set password=hunter2 to reproduce.")
    response = _send(db, investigation, owner)
    assert "hunter2" not in response["assistant_message"]["content"]
    stored = _messages(db, investigation)[1]
    assert "hunter2" not in stored.content
    assert "[REDACTED]" in stored.content
