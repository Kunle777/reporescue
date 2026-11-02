"""Tests for central backend configuration.

Environment-first resolution, pool parsing, and credential redaction. These
tests never touch the real .env values: every case pins its own environment.
"""

import json

import pytest

from app import config
from app.config import ConfigError


def test_database_url_scheme_normalized_to_psycopg(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    assert config.get_database_url() == "postgresql+psycopg://u:p@h:5432/db"


def test_database_url_passthrough_when_already_qualified(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    assert config.get_database_url() == "postgresql+psycopg://u:p@h/db"


def test_database_url_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError):
        config.get_database_url()


def test_supabase_vars_required(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(ConfigError):
        config.get_supabase_url()
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    with pytest.raises(ConfigError):
        config.get_supabase_anon_key()


def test_supabase_url_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co/")
    assert config.get_supabase_url() == "https://x.supabase.co"


def test_jwt_audience_default_and_override(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_AUDIENCE", raising=False)
    assert config.get_jwt_audience() == "authenticated"
    monkeypatch.setenv("SUPABASE_JWT_AUDIENCE", "custom-aud")
    assert config.get_jwt_audience() == "custom-aud"


def test_pool_settings_defaults(monkeypatch):
    for var in (
        "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE",
    ):
        monkeypatch.delenv(var, raising=False)
    assert config.get_pool_settings() == {
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }


def test_pool_settings_from_env(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "7")
    monkeypatch.setenv("DB_POOL_RECYCLE", "60")
    assert config.get_pool_settings() == {
        "pool_size": 2,
        "max_overflow": 3,
        "pool_timeout": 7,
        "pool_recycle": 60,
    }


def test_pool_settings_reject_garbage(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "lots")
    with pytest.raises(ConfigError):
        config.get_pool_settings()
    monkeypatch.setenv("DB_POOL_SIZE", "-1")
    with pytest.raises(ConfigError):
        config.get_pool_settings()


def test_max_clone_bytes_default_and_override(monkeypatch):
    monkeypatch.delenv("REPORESCUE_MAX_CLONE_BYTES", raising=False)
    assert config.get_max_clone_bytes() == 256 * 1024 * 1024
    monkeypatch.setenv("REPORESCUE_MAX_CLONE_BYTES", "1024")
    assert config.get_max_clone_bytes() == 1024
    monkeypatch.setenv("REPORESCUE_MAX_CLONE_BYTES", "lots")
    with pytest.raises(ConfigError):
        config.get_max_clone_bytes()


def test_investigation_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("REPORESCUE_INVESTIGATION_TIMEOUT_SECONDS", raising=False)
    assert config.get_investigation_timeout_seconds() == 1200
    monkeypatch.setenv("REPORESCUE_INVESTIGATION_TIMEOUT_SECONDS", "60")
    assert config.get_investigation_timeout_seconds() == 60
    monkeypatch.setenv("REPORESCUE_INVESTIGATION_TIMEOUT_SECONDS", "-5")
    with pytest.raises(ConfigError):
        config.get_investigation_timeout_seconds()


def test_redact_database_url_hides_password():
    redacted = config.redact_database_url("postgresql://u:s3cret-pw@h:5432/db")
    assert "s3cret-pw" not in redacted
    assert redacted == "postgresql://u:***@h:5432/db"


def test_redact_database_url_unparseable():
    assert "unparseable" in config.redact_database_url("not a url")


def test_describe_database_target_has_no_password():
    target = config.describe_database_target("postgresql://u:s3cret-pw@h:5432/db")
    assert target == {
        "scheme": "postgresql",
        "host": "h",
        "port": "5432",
        "user": "u",
        "database": "db",
    }
    assert "s3cret-pw" not in json.dumps(target)
