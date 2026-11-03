"""SQLite persistence for jobs, messages, projects, and profiles.

Standard-library ``sqlite3`` only: one file database (``backend/reporescue.db``
by default, ``REPORESCUE_DB_PATH`` to override), one connection per call, all
access serialized through a module lock so background job threads share the
store safely. Every read is ownership-scoped: rows owned by another user read
back as missing so routes answer 404 without leaking existence.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from . import config

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    investigation_id TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    ingestion_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    resolved_mode TEXT,
    status TEXT NOT NULL,
    progress_pct INTEGER NOT NULL DEFAULT 0,
    stage TEXT,
    stages_completed TEXT NOT NULL DEFAULT '[]',
    repo_name TEXT NOT NULL DEFAULT '',
    repo_url TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    repo_ref TEXT,
    user_message TEXT,
    error TEXT,
    verification_status TEXT NOT NULL DEFAULT 'pending',
    runs TEXT NOT NULL DEFAULT '[]',
    attempts TEXT NOT NULL DEFAULT '[]',
    findings TEXT NOT NULL DEFAULT '[]',
    patches TEXT NOT NULL DEFAULT '[]',
    timings TEXT,
    partial TEXT,
    final_report TEXT,
    routing TEXT,
    limitations TEXT NOT NULL DEFAULT '[]',
    cancelled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_updated ON jobs (user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_investigation ON messages (investigation_id, created_at, id);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    github_owner TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    github_url TEXT NOT NULL,
    default_branch TEXT,
    visibility TEXT NOT NULL DEFAULT 'public',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, github_owner, repository_name)
);
CREATE INDEX IF NOT EXISTS idx_projects_user_created ON projects (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE,
    display_name TEXT,
    avatar_url TEXT,
    github_username TEXT,
    github_user_id TEXT,
    github_avatar_url TEXT,
    github_display_name TEXT,
    github_connected_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        config.get_db_path(), check_same_thread=False, timeout=5.0
    )
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _dump_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


# --- jobs ---


def create_job(row: dict[str, Any]) -> None:
    """Insert a job row. ``row`` keys must match the jobs columns."""
    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            connection.commit()
        finally:
            connection.close()


def get_job(job_id: str, user_id: str) -> dict[str, Any] | None:
    """Fetch one owned job, or None when missing or foreign."""
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            connection.close()


def update_job(job_id: str, fields: dict[str, Any]) -> None:
    """Update job columns (plus ``updated_at``). No ownership check."""
    assignments = ", ".join(f"{name} = ?" for name in fields) + ", updated_at = ?"
    values = tuple(fields.values()) + (utcnow_iso(),)
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                values + (job_id,),
            )
            connection.commit()
        finally:
            connection.close()


def list_jobs(user_id: str, limit: int) -> list[dict[str, Any]]:
    """Newest-first job rows for one user (update recency)."""
    with _LOCK:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE user_id = ? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")


def get_active_job(user_id: str) -> dict[str, Any] | None:
    """Most recently updated non-terminal job, or None."""
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND status NOT IN "
                "('completed', 'failed', 'cancelled') "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            connection.close()


def sweep_stale_jobs(reason: str, message: str) -> int:
    """Fail every non-terminal job (server restarts abandon live threads).

    Returns the number of rows swept.
    """
    now = utcnow_iso()
    with _LOCK:
        connection = _connect()
        try:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'failed', error = ?, "
                "verification_status = 'not_verified', "
                "updated_at = ?, completed_at = ? "
                "WHERE status NOT IN ('completed', 'failed', 'cancelled')",
                (_dump_json({"reason": reason, "message": message}), now, now),
            )
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()


# --- messages ---


def add_message(row: dict[str, Any]) -> None:
    """Insert one conversation message row."""
    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                f"INSERT INTO messages ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            connection.commit()
        finally:
            connection.close()


def list_messages(
    investigation_id: str, user_id: str, limit: int
) -> list[dict[str, Any]]:
    """Latest ``limit`` owned messages, oldest first (never creates)."""
    with _LOCK:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT * FROM (SELECT *, rowid AS _rid FROM messages "
                "WHERE investigation_id = ? AND user_id = ? "
                "ORDER BY created_at DESC, _rid DESC LIMIT ?) "
                "ORDER BY created_at ASC, _rid ASC",
                (investigation_id, user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


def get_job_by_investigation(
    investigation_id: str, user_id: str
) -> dict[str, Any] | None:
    """Fetch one owned job by investigation id, or None."""
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE investigation_id = ? AND user_id = ?",
                (investigation_id, user_id),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            connection.close()


# --- projects ---


def create_project(row: dict[str, Any]) -> None:
    """Insert a project row. Raises sqlite3.IntegrityError on duplicates."""
    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                f"INSERT INTO projects ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            connection.commit()
        finally:
            connection.close()


def get_project(project_id: str, user_id: str) -> dict[str, Any] | None:
    """Fetch one owned project, or None when missing or foreign."""
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            connection.close()


def list_projects(user_id: str) -> list[dict[str, Any]]:
    """Newest-first projects for one user."""
    with _LOCK:
        connection = _connect()
        try:
            rows = connection.execute(
                "SELECT * FROM projects WHERE user_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


def update_project(
    project_id: str, user_id: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    """Update one owned project; None when missing or foreign."""
    assignments = ", ".join(f"{name} = ?" for name in fields) + ", updated_at = ?"
    values = tuple(fields.values()) + (utcnow_iso(),)
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                f"UPDATE projects SET {assignments} WHERE id = ? AND user_id = ?",
                values + (project_id, user_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            return _row_to_dict(row)
        finally:
            connection.close()


def delete_project(project_id: str, user_id: str) -> bool:
    """Delete one owned project; False when missing or foreign."""
    with _LOCK:
        connection = _connect()
        try:
            cursor = connection.execute(
                "DELETE FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()


# --- profiles ---


def get_profile(user_id: str) -> dict[str, Any] | None:
    """Fetch one profile row, or None when never provisioned."""
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _row_to_dict(row)
        finally:
            connection.close()


def save_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Upsert one profile row by user_id; returns the stored row.

    ``created_at`` is supplied on first insert and never overwritten by
    later updates; ``updated_at`` defaults to now when the caller omits it.
    """
    row = dict(row)
    now = utcnow_iso()
    row.setdefault("created_at", now)
    row.setdefault("updated_at", now)
    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    updates = ", ".join(
        f"{name} = excluded.{name}"
        for name in row
        if name not in ("user_id", "created_at")
    )
    with _LOCK:
        connection = _connect()
        try:
            connection.execute(
                f"INSERT INTO profiles ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT (user_id) DO UPDATE SET {updates}",
                tuple(row.values()),
            )
            connection.commit()
            stored = connection.execute(
                "SELECT * FROM profiles WHERE user_id = ?", (row["user_id"],)
            ).fetchone()
            result = _row_to_dict(stored)
            assert result is not None
            return result
        finally:
            connection.close()


def username_taken(username: str, user_id: str) -> bool:
    """True when another user already owns the normalized username."""
    with _LOCK:
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT user_id FROM profiles WHERE username = ? AND user_id != ?",
                (username, user_id),
            ).fetchone()
            return row is not None
        finally:
            connection.close()
