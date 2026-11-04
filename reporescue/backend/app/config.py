from __future__ import annotations

"""Central backend configuration (Phase 4).

Environment-first: every accessor reads ``os.environ`` at call time so tests
can override values with ``monkeypatch``. ``backend/.env`` is loaded once at
import (without overriding real environment variables) so local development
picks up developer-configured values. Secret values are never logged here;
use :func:`redact_database_url` / :func:`describe_database_target` for output.
"""

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL_ENV_VAR = "SUPABASE_URL"
SUPABASE_ANON_KEY_ENV_VAR = "SUPABASE_ANON_KEY"
DATABASE_URL_ENV_VAR = "DATABASE_URL"
SUPABASE_JWT_AUDIENCE_ENV_VAR = "SUPABASE_JWT_AUDIENCE"
CORS_ORIGINS_ENV_VAR = "REPORESCUE_CORS_ORIGINS"
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

DB_POOL_SIZE_ENV_VAR = "DB_POOL_SIZE"
DB_MAX_OVERFLOW_ENV_VAR = "DB_MAX_OVERFLOW"
DB_POOL_TIMEOUT_ENV_VAR = "DB_POOL_TIMEOUT"
DB_POOL_RECYCLE_ENV_VAR = "DB_POOL_RECYCLE"

MAX_CLONE_BYTES_ENV_VAR = "REPORESCUE_MAX_CLONE_BYTES"
INVESTIGATION_TIMEOUT_ENV_VAR = "REPORESCUE_INVESTIGATION_TIMEOUT_SECONDS"
WORKER_POLL_ENV_VAR = "REPORESCUE_WORKER_POLL_SECONDS"
WORKER_LEASE_ENV_VAR = "REPORESCUE_WORKER_LEASE_SECONDS"
WORKER_HEARTBEAT_ENV_VAR = "REPORESCUE_WORKER_HEARTBEAT_SECONDS"
EXECUTION_BACKEND_ENV_VAR = "REPORESCUE_EXECUTION_BACKEND"
MODAL_CPU_ENV_VAR = "REPORESCUE_MODAL_CPU"
MODAL_MEMORY_MB_ENV_VAR = "REPORESCUE_MODAL_MEMORY_MB"
MODAL_APP_NAME_ENV_VAR = "REPORESCUE_MODAL_APP_NAME"
MODAL_NODE_IMAGE_ENV_VAR = "REPORESCUE_MODAL_NODE_IMAGE"
MODAL_SDK_CONFIG_ENV_VAR = "REPORESCUE_MODAL_USE_SDK_CONFIG"
SNAPSHOT_MAX_TOTAL_ENV_VAR = "REPORESCUE_SNAPSHOT_MAX_TOTAL_BYTES"
SNAPSHOT_MAX_FILE_ENV_VAR = "REPORESCUE_SNAPSHOT_MAX_FILE_BYTES"
SNAPSHOT_MAX_FILES_ENV_VAR = "REPORESCUE_SNAPSHOT_MAX_FILES"

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
DEFAULT_EXECUTION_BACKEND = "auto"
EXECUTION_BACKEND_CHOICES = ("auto", "modal", "docker", "unsafe-local")
DEFAULT_MODAL_CPU = 1.0
DEFAULT_MODAL_MEMORY_MB = 1024
DEFAULT_MODAL_APP_NAME = "reporescue-executions"
DEFAULT_MODAL_NODE_IMAGE = "node:22-slim"
DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_FILES = 20_000


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


def get_cors_origins() -> list[str]:
    """Return exact browser origins permitted to call the API.

    Production must configure its frontend origin explicitly. A wildcard is
    rejected because authenticated API access uses the Authorization header.
    """
    raw = os.environ.get(CORS_ORIGINS_ENV_VAR, "").strip()
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        parsed = urlparse(origin)
        if (
            not origin
            or origin == "*"
            or parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ConfigError(
                f"{CORS_ORIGINS_ENV_VAR} must be a comma-separated list of exact http(s) origins."
            )
        if origin not in origins:
            origins.append(origin)
    return origins


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


def get_execution_backend() -> str:
    """Return the configured execution backend selection.

    One of ``auto`` (default), ``modal``, ``docker``, ``unsafe-local``.
    ``auto`` prefers Modal when its credentials are configured, then
    Docker, and unisolated host execution only with its explicit flag.
    """
    raw = os.environ.get(EXECUTION_BACKEND_ENV_VAR, "").strip().lower()
    if not raw:
        return DEFAULT_EXECUTION_BACKEND
    if raw not in EXECUTION_BACKEND_CHOICES:
        raise ConfigError(
            f"{EXECUTION_BACKEND_ENV_VAR} must be one of "
            f"{', '.join(EXECUTION_BACKEND_CHOICES)}, got {raw!r}."
        )
    return raw


def _non_negative_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.")
    if value < 0:
        raise ConfigError(f"{name} must be >= 0, got {value}.")
    return value


def get_modal_cpu() -> float:
    """Return the Modal sandbox CPU request in fractional cores."""
    return _non_negative_float(MODAL_CPU_ENV_VAR, DEFAULT_MODAL_CPU)


def get_modal_memory_mb() -> int:
    """Return the Modal sandbox memory request in MiB."""
    return _positive_int(MODAL_MEMORY_MB_ENV_VAR, DEFAULT_MODAL_MEMORY_MB)


def get_modal_app_name() -> str:
    """Return the Modal App name execution sandboxes attach to."""
    raw = os.environ.get(MODAL_APP_NAME_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MODAL_APP_NAME
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", raw):
        raise ConfigError(
            f"{MODAL_APP_NAME_ENV_VAR} must be a valid Modal app name "
            f"(lowercase letters, digits, '_', '-', '.'), got {raw!r}."
        )
    return raw


def get_modal_node_image() -> str:
    """Return the container image tag for JavaScript execution sandboxes."""
    raw = os.environ.get(MODAL_NODE_IMAGE_ENV_VAR, "").strip()
    return raw or DEFAULT_MODAL_NODE_IMAGE


def modal_sdk_config_allowed() -> bool:
    """Whether Modal may fall back to the SDK's own credential chain.

    Off by default: the trusted worker authenticates from
    ``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET`` environment variables only.
    Local development may set this flag to ``1`` to also honor
    ``~/.modal.toml`` (``modal setup``).
    """
    return os.environ.get(MODAL_SDK_CONFIG_ENV_VAR, "").strip() == "1"


def get_snapshot_max_total_bytes() -> int:
    """Return the maximum staged snapshot size in bytes for transfer."""
    return _positive_int(
        SNAPSHOT_MAX_TOTAL_ENV_VAR, DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES
    )


def get_snapshot_max_file_bytes() -> int:
    """Return the maximum single-file size in bytes for snapshot transfer."""
    return _positive_int(
        SNAPSHOT_MAX_FILE_ENV_VAR, DEFAULT_SNAPSHOT_MAX_FILE_BYTES
    )


def get_snapshot_max_files() -> int:
    """Return the maximum file count for snapshot transfer."""
    return _positive_int(SNAPSHOT_MAX_FILES_ENV_VAR, DEFAULT_SNAPSHOT_MAX_FILES)


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
