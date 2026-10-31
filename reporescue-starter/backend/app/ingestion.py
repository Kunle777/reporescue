from __future__ import annotations

"""Phase 2 ingestion workflow: GitHub URL to repository profile and context.

Pipeline::

    GitHub URL -> validate -> clone + pin SHA -> detect Python project
        -> profile -> extract bounded context -> stored record

The temporary workspace is always removed before this module returns, so a
record contains only the profile and bounded excerpts -- never a live path
into a removed checkout. Records live in a small in-memory FIFO store: this
phase adds no database, queue, or background worker. Nothing in the cloned
repository is executed here; isolated execution belongs to a later phase.
"""

import threading
import uuid
from typing import Any

from .github_ingest import (
    IngestionError,
    clone_and_pin,
    normalize_github_url,
    temporary_workspace,
    validate_ref,
)
from .python_detect import build_profile
from .repo_context import extract_context, inventory_repository

MAX_STORED_RECORDS = 50

_store: dict[str, dict[str, Any]] = {}
_store_lock = threading.Lock()


def ingest_repository(url: str, ref: str | None = None) -> dict[str, Any]:
    """Ingest one public GitHub repository and return its record.

    Returns ``{"status": "complete", ...}`` with the profile and context, or
    ``{"status": "error", "reason": ..., "message": ...}`` following the
    existing orchestrator error conventions. Unsupported (non-Python)
    repositories still return ``complete`` with ``supported: false`` and
    warnings; only infrastructure failures are errors.
    """
    try:
        normalized = normalize_github_url(url)
        cleaned_ref = validate_ref(ref)
    except IngestionError as exc:
        return {"status": "error", "reason": exc.reason, "message": str(exc)}

    name = f"{normalized.owner}/{normalized.repo}"
    try:
        with temporary_workspace() as workspace:
            destination = workspace / "repo"
            try:
                sha = clone_and_pin(normalized.url, destination, cleaned_ref)
            except IngestionError as exc:
                return {
                    "status": "error",
                    "reason": exc.reason,
                    "message": str(exc),
                    "url": normalized.url,
                    "name": name,
                }
            profile = build_profile(
                destination,
                url=normalized.url,
                name=name,
                commit_sha=sha,
                ref=cleaned_ref,
            )
            inventory = inventory_repository(destination)
            context = extract_context(destination, inventory)
    except OSError as exc:
        return {
            "status": "error",
            "reason": "workspace_error",
            "message": f"Could not prepare a temporary workspace: {exc}.",
            "url": normalized.url,
            "name": name,
        }

    record = {
        "status": "complete",
        "ingestion_id": uuid.uuid4().hex[:12],
        "profile": profile,
        "context": context,
        "warnings": list(profile.get("warnings", [])),
    }
    with _store_lock:
        while len(_store) >= MAX_STORED_RECORDS:
            _store.pop(next(iter(_store)))
        _store[record["ingestion_id"]] = record
    return record


def get_ingestion(ingestion_id: str) -> dict[str, Any]:
    """Return a previously stored ingestion record, or an error dict."""
    if not isinstance(ingestion_id, str) or not ingestion_id.strip():
        return {"status": "error", "message": "Ingestion id is empty."}
    with _store_lock:
        record = _store.get(ingestion_id.strip())
    if record is None:
        return {
            "status": "error",
            "reason": "not_found",
            "message": f"No ingestion record {ingestion_id.strip()!r}; "
            "records are kept in memory only.",
        }
    return record


def clear_ingestions() -> None:
    """Drop all stored records. Used by tests to isolate cases."""
    with _store_lock:
        _store.clear()
