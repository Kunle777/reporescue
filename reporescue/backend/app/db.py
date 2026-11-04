from __future__ import annotations

"""Synchronous SQLAlchemy engine and session management (Phase 4).

The engine is cached per resolved database URL (engines are designed to be
long-lived); sessions are always short-lived and request-scoped via
:func:`get_db`, which rolls back on error and closes reliably. Write paths
commit explicitly; the dependency never commits implicitly.

Long-running work (cloning, Docker runs, Meta API calls) must never execute
inside an open session/transaction: open a session, do bounded reads/writes,
commit, and close at stage boundaries instead.
"""

import threading
from collections.abc import Iterator
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import config

_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def _is_server_database(database_url: str) -> bool:
    """True for client/server URLs; False for local SQLite URLs.

    Pool sizing and connect timeouts only apply to server databases (SQLite
    uses a fixed single-thread pool and a timeout-less DBAPI).
    """
    return not database_url.startswith("sqlite:")


def create_engine_for(database_url: str) -> Engine:
    """Build a new engine for ``database_url`` with configured pooling."""
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if _is_server_database(database_url):
        kwargs.update(config.get_pool_settings())
    return create_engine(database_url, **kwargs)


def get_engine() -> Engine:
    """Return the cached engine for the configured database URL."""
    url = config.get_database_url()
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            engine = create_engine_for(url)
            _engines[url] = engine
        return engine


def dispose_engines() -> None:
    """Dispose all cached engines. Used by tests to isolate cases."""
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


def get_session_factory() -> sessionmaker[Session]:
    """Return a session factory bound to the configured engine."""
    return sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a request-scoped session, then close it."""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _strip_password(detail: str, url: str) -> str:
    try:
        password = urlparse(url).password
    except ValueError:
        password = None
    if password:
        detail = detail.replace(password, "***")
    return detail


def check_database_connection(timeout_seconds: int = 10) -> dict[str, object]:
    """Run ``SELECT 1`` against the configured database.

    Returns ``{"ok": True, "target": {...}}`` or ``{"ok": False, "target":
    {...}, "error": ...}``. Credentials are never included in the output.
    """
    url = config.get_database_url()
    target = config.describe_database_target(url)
    connect_args: dict[str, object] = (
        {"connect_timeout": timeout_seconds} if _is_server_database(url) else {}
    )
    try:
        engine = create_engine(url, connect_args=connect_args)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception as exc:
        return {
            "ok": False,
            "target": target,
            "error": _strip_password(f"{type(exc).__name__}: {exc}", url)[:300],
        }
    return {"ok": True, "target": target}
