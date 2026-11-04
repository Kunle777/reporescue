"""Tests for engine/session management without a live database.

Engines point at throwaway SQLite files; nothing here connects to Supabase.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from app import db as db_module
from app.models import Base, Profile


@pytest.fixture(autouse=True)
def _clean_engines():
    db_module.dispose_engines()
    yield
    db_module.dispose_engines()


def _sqlite_url(tmp_path, name="t.db"):
    return f"sqlite:///{tmp_path.as_posix()}/{name}"


def test_create_engine_for_server_uses_queue_pool():
    engine = db_module.create_engine_for("postgresql+psycopg://u:p@h/db")
    try:
        assert isinstance(engine.pool, QueuePool)
    finally:
        engine.dispose()


def test_create_engine_for_sqlite_skips_pooling(tmp_path):
    engine = db_module.create_engine_for(_sqlite_url(tmp_path))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def test_get_engine_cached_per_url(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path, "a.db"))
    first = db_module.get_engine()
    assert db_module.get_engine() is first
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path, "b.db"))
    assert db_module.get_engine() is not first


def test_get_db_releases_connection_on_close(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path))
    engine = db_module.get_engine()
    gen = db_module.get_db()
    session = next(gen)
    session.execute(text("SELECT 1"))
    assert session.is_active
    assert engine.pool.checkedout() == 1
    gen.close()
    assert engine.pool.checkedout() == 0


def test_get_db_rolls_back_on_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path))
    engine = db_module.get_engine()
    Base.metadata.create_all(engine)
    gen = db_module.get_db()
    session = next(gen)
    session.add(Profile(id=uuid.uuid4()))
    with pytest.raises(RuntimeError, match="boom"):
        gen.throw(RuntimeError("boom"))
    check = db_module.get_session_factory()()
    try:
        assert check.query(Profile).count() == 0
    finally:
        check.close()


def test_check_database_connection_ok_uses_configured_url(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path))
    result = db_module.check_database_connection()
    assert result["ok"] is True


def test_check_database_connection_failure_redacts_password(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://u:s3cret-pw@127.0.0.1:1/db"
    )
    result = db_module.check_database_connection(timeout_seconds=1)
    assert result["ok"] is False
    assert "s3cret-pw" not in json.dumps(result)
