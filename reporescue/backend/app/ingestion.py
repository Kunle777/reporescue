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
import time
import uuid
from collections.abc import Callable
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


def ingest_repository(
    url: str,
    ref: str | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Ingest one public GitHub repository and return its record.

    ``on_phase`` receives "clone", "profile", then "context" as phases
    start, for liveness reporting by long-running callers. Callback
    failures never fail ingestion.

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
    ingest_start = time.perf_counter()
    timings: dict[str, int] = {}
    try:
        with temporary_workspace() as workspace:
            destination = workspace / "repo"
            try:
                step_start = time.perf_counter()
                _emit_phase(on_phase, "clone")
                sha = clone_and_pin(normalized.url, destination, cleaned_ref)
                timings["clone_ms"] = max(0, round((time.perf_counter() - step_start) * 1000))
            except IngestionError as exc:
                return {
                    "status": "error",
                    "reason": exc.reason,
                    "message": str(exc),
                    "url": normalized.url,
                    "name": name,
                }
            step_start = time.perf_counter()
            _emit_phase(on_phase, "profile")
            profile = build_profile(
                destination,
                url=normalized.url,
                name=name,
                commit_sha=sha,
                ref=cleaned_ref,
            )
            timings["profile_ms"] = max(0, round((time.perf_counter() - step_start) * 1000))
            step_start = time.perf_counter()
            _emit_phase(on_phase, "context")
            inventory = inventory_repository(destination)
            timings["inventory_ms"] = max(0, round((time.perf_counter() - step_start) * 1000))
            step_start = time.perf_counter()
            context = extract_context(destination, inventory)
            timings["context_ms"] = max(0, round((time.perf_counter() - step_start) * 1000))
    except OSError as exc:
        return {
            "status": "error",
            "reason": "workspace_error",
            "message": f"Could not prepare a temporary workspace: {exc}.",
            "url": normalized.url,
            "name": name,
        }

    timings["total_ms"] = max(0, round((time.perf_counter() - ingest_start) * 1000))
    record = {
        "status": "complete",
        "ingestion_id": uuid.uuid4().hex[:12],
        "profile": profile,
        "context": context,
        "warnings": list(profile.get("warnings", [])),
        "timings": timings,
    }
    with _store_lock:
        while len(_store) >= MAX_STORED_RECORDS:
            _store.pop(next(iter(_store)))
        _store[record["ingestion_id"]] = record
    return record


def _emit_phase(on_phase: Callable[[str], None] | None, phase: str) -> None:
    if on_phase is None:
        return
    try:
        on_phase(phase)
    except Exception:
        pass


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


def set_ingestion_owner(ingestion_id: str, owner_id: str) -> bool:
    """Tag a stored record with its owner's UUID string.

    Records created before ownership (or in tests) carry no tag; only the
    authenticated HTTP routes set it. Returns False for unknown ids.
    """
    with _store_lock:
        record = _store.get(ingestion_id.strip() if isinstance(ingestion_id, str) else "")
        if record is None:
            return False
        record["owner_id"] = owner_id
        return True


def get_ingestion_owner(ingestion_id: str) -> str | None:
    """Return the owner's UUID string for a record, or None when untagged."""
    if not isinstance(ingestion_id, str) or not ingestion_id.strip():
        return None
    with _store_lock:
        record = _store.get(ingestion_id.strip())
    if record is None:
        return None
    owner = record.get("owner_id")
    return owner if isinstance(owner, str) else None
