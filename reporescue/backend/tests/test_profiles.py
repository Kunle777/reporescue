"""Profile creation: idempotent, keyed by the verified Auth UUID."""

import uuid

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models import Base, Profile
from app.profiles import get_or_create_profile, get_profile


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/prof.db")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


import pytest


@pytest.fixture()
def session(tmp_path):
    yield from _session(tmp_path)


def test_get_profile_missing_returns_none(session):
    assert get_profile(session, uuid.uuid4()) is None


def test_get_or_create_profile_is_idempotent(session):
    user_id = uuid.uuid4()
    first = get_or_create_profile(session, user_id=user_id, display_name="Tester")
    second = get_or_create_profile(session, user_id=user_id, display_name="Other")
    assert first.id == user_id == second.id
    assert session.query(Profile).count() == 1


def test_concurrent_first_request_returns_winner(session, monkeypatch):
    user_id = uuid.uuid4()
    session.add(Profile(id=user_id, display_name="Winner"))
    session.commit()
    session.expunge_all()

    calls = {"n": 0}
    original_get = Session.get

    def flaky_get(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pretend missing so the insert collides
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(Session, "get", flaky_get)
    result = get_or_create_profile(session, user_id=user_id)
    assert result.id == user_id
    assert result.display_name == "Winner"
    assert session.query(Profile).count() == 1
