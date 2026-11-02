from __future__ import annotations

"""Central backend configuration (Phase 4).

Environment-first: every accessor reads ``os.environ`` at call time so tests
can override values with ``monkeypatch``. ``backend/.env`` is loaded once at
import (without overriding real environment variables) so local development
picks up developer-configured values. Secret values are never logged here;
use :func:`redact_database_url` / :func:`describe_database_target` for output.
"""

import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL_ENV_VAR = "SUPABASE_URL"
SUPABASE_ANON_KEY_ENV_VAR = "SUPABASE_ANON_KEY"
DATABASE_URL_ENV_VAR = "DATABASE_URL"
SUPABASE_JWT_AUDIENCE_ENV_VAR = "SUPABASE_JWT_AUDIENCE"

DB_POOL_SIZE_ENV_VAR = "DB_POOL_SIZE"
DB_MAX_OVERFLOW_ENV_VAR = "DB_MAX_OVERFLOW"
DB_POOL_TIMEOUT_ENV_VAR = "DB_POOL_TIMEOUT"
DB_POOL_RECYCLE_ENV_VAR = "DB_POOL_RECYCLE"

MAX_CLONE_BYTES_ENV_VAR = "REPORESCUE_MAX_CLONE_BYTES"
INVESTIGATION_TIMEOUT_ENV_VAR = "REPORESCUE_INVESTIGATION_TIMEOUT_SECONDS"
WORKER_POLL_ENV_VAR = "REPORESCUE_WORKER_POLL_SECONDS"
WORKER_LEASE_ENV_VAR = "REPORESCUE_WORKER_LEASE_SECONDS"
WORKER_HEARTBEAT_ENV_VAR = "REPORESCUE_WORKER_HEARTBEAT_SECONDS"

DEFAULT_JWT_AUDIENCE = "authenticated"
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10
DEFAULT_POOL_TIMEOUT = 30
DEFAULT_POOL_RECYCLE = 1800
DEFAULT_MAX_CLONE_BYTES = 256 * 1024 * 1024
DEFAULT_INVESTIGATION_TIMEOUT_SECONDS = 1200
DEFAULT_WORKER_POLL_SECONDS = 2
DEFAULT_WORKER_LEASE_SECONDS = 60
DEFAULT_WORKER_HEARTBEAT_SECONDS = 15


class ConfigError(Exception):
    """Required configuration is missing or invalid. Never carries secrets."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set; configure it in the backend environment."
        )
    return value


def get_supabase_url() -> str:
    """Return the Supabase project URL (no trailing slash)."""
    return _required(SUPABASE_URL_ENV_VAR).rstrip("/")


def get_supabase_anon_key() -> str:
    """Return the Supabase publishable/anon key for Auth API calls."""
    return _required(SUPABASE_ANON_KEY_ENV_VAR)


def get_database_url() -> str:
    """Return a SQLAlchemy-compatible sync psycopg URL.

    A bare ``postgresql://`` developer connection string is adapted to the
    ``postgresql+psycopg://`` dialect; anything else is passed through
    unchanged. The secret value must never be logged; see
    :func:`redact_database_url`.
    """
    raw = _required(DATABASE_URL_ENV_VAR)
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


def get_jwt_audience() -> str:
    """Return the expected access-token audience (Supabase default shown)."""
    return os.environ.get(SUPABASE_JWT_AUDIENCE_ENV_VAR, "").strip() or DEFAULT_JWT_AUDIENCE


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.")
    if value < 0:
        raise ConfigError(f"{name} must be >= 0, got {value}.")
    return value


def get_max_clone_bytes() -> int:
    """Return the maximum checked-out repository size in bytes.

    Repositories exceeding this after cloning are rejected before
    profiling. Zero disables the guard (not recommended).
    """
    return _positive_int(MAX_CLONE_BYTES_ENV_VAR, DEFAULT_MAX_CLONE_BYTES)


def get_investigation_timeout_seconds() -> int:
    """Return the total wall-clock budget for one investigation in seconds.

    Generous by design (measured end-to-end runs take a few minutes): it
    bounds pathological runs, not typical ones. Per-operation timeouts
    still apply inside the budget.
    """
    return _positive_int(
        INVESTIGATION_TIMEOUT_ENV_VAR, DEFAULT_INVESTIGATION_TIMEOUT_SECONDS
    )


def get_worker_poll_seconds() -> int:
    """Idle poll interval for the background worker in seconds."""
    return _positive_int(WORKER_POLL_ENV_VAR, DEFAULT_WORKER_POLL_SECONDS)


def get_worker_lease_seconds() -> int:
    """Lease duration a worker holds on a claimed job in seconds."""
    return _positive_int(WORKER_LEASE_ENV_VAR, DEFAULT_WORKER_LEASE_SECONDS)


def get_worker_heartbeat_seconds() -> int:
    """Heartbeat interval that extends a held job lease in seconds."""
    return _positive_int(WORKER_HEARTBEAT_ENV_VAR, DEFAULT_WORKER_HEARTBEAT_SECONDS)


def get_pool_settings() -> dict[str, int]:
    """Return SQLAlchemy pool settings from the environment with defaults."""
    return {
        "pool_size": _positive_int(DB_POOL_SIZE_ENV_VAR, DEFAULT_POOL_SIZE),
        "max_overflow": _positive_int(DB_MAX_OVERFLOW_ENV_VAR, DEFAULT_MAX_OVERFLOW),
        "pool_timeout": _positive_int(DB_POOL_TIMEOUT_ENV_VAR, DEFAULT_POOL_TIMEOUT),
        "pool_recycle": _positive_int(DB_POOL_RECYCLE_ENV_VAR, DEFAULT_POOL_RECYCLE),
    }


def redact_database_url(url: str) -> str:
    """Return a log-safe database URL with the password replaced by ``***``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "(unparseable database URL)"
    if not parsed.hostname:
        return "(unparseable database URL)"
    user = parsed.username or "(no user)"
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "(no database)"
    return f"{parsed.scheme}://{user}:***@{host}{port}/{database}"


def describe_database_target(url: str) -> dict[str, str | None]:
    """Return non-secret connection facts for migration confirmations."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return {"url": "(unparseable database URL)"}
    if not parsed.hostname:
        return {"url": "(unparseable database URL)"}
    return {
        "scheme": parsed.scheme or None,
        "host": parsed.hostname,
        "port": str(parsed.port) if parsed.port else None,
        "user": parsed.username,
        "database": parsed.path.lstrip("/") or None,
    }
