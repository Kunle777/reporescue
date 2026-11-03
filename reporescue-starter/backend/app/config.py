"""Central backend configuration.

Environment-first: every accessor reads ``os.environ`` at call time so tests
can override values with ``monkeypatch``. ``backend/.env`` is loaded once at
import (without overriding real environment variables) so local development
picks up developer-configured values. Secret values are never logged here.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL_ENV_VAR = "SUPABASE_URL"
SUPABASE_ANON_KEY_ENV_VAR = "SUPABASE_ANON_KEY"
SUPABASE_JWT_AUDIENCE_ENV_VAR = "SUPABASE_JWT_AUDIENCE"
DB_PATH_ENV_VAR = "REPORESCUE_DB_PATH"

DEFAULT_JWT_AUDIENCE = "authenticated"


class ConfigError(Exception):
    """Required configuration is missing or invalid. Never carries secrets."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set; configure it in the backend environment.")
    return value


def get_supabase_url() -> str:
    """Return the Supabase project URL (no trailing slash)."""
    return _required(SUPABASE_URL_ENV_VAR).rstrip("/")


def get_supabase_anon_key() -> str:
    """Return the Supabase publishable/anon key for Auth API calls."""
    return _required(SUPABASE_ANON_KEY_ENV_VAR)


def get_jwt_audience() -> str:
    """Return the required JWT audience (defaults to ``authenticated``)."""
    return os.environ.get(SUPABASE_JWT_AUDIENCE_ENV_VAR, "").strip() or DEFAULT_JWT_AUDIENCE


def get_db_path() -> str:
    """Return the SQLite database path (override with REPORESCUE_DB_PATH)."""
    override = os.environ.get(DB_PATH_ENV_VAR, "").strip()
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / "reporescue.db")
