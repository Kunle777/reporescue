"""Shared contract for RepoRescue language adapters.

A language adapter is a module exposing ``LANGUAGE`` (e.g. ``"python"``)
and ``detect(root)`` returning an :class:`AdapterResult`. Adapters only
read the checkout: they never install dependencies, never execute
repository code, and never invoke Docker. The future General
orchestrator consumes these results to select the minimum necessary
capabilities; there is deliberately no plugin registry or dynamic
loading here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..repo_context import EXCLUDED_DIRS


@dataclass(frozen=True)
class AdapterResult:
    """One language's detection outcome for a repository checkout."""

    language: str
    file_count: int
    manifests: tuple[str, ...] = ()
    project: dict[str, Any] = field(default_factory=dict)
    dependencies: dict[str, Any] = field(default_factory=dict)
    test_frameworks: tuple[str, ...] = ()
    static_available: bool = False
    runtime_available: bool = False
    runtime_reason: str | None = None
    limitations: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


def scan_suffixes(
    root: Path, suffixes: frozenset[str], *, skip_egg_info: bool = False
) -> list[Path]:
    """Walk ``root`` collecting files whose suffix is in ``suffixes``.

    Symlinks are never followed, excluded directories are pruned, and
    unreadable entries are skipped. Pure read-only traversal.
    """
    found: list[Path] = []
    stack = [Path(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in EXCLUDED_DIRS:
                        continue
                    if skip_egg_info and entry.name.endswith(".egg-info"):
                        continue
                    stack.append(entry)
                elif entry.suffix.lower() in suffixes:
                    found.append(entry)
            except OSError:
                continue
    return found


def top_level_names(root: Path) -> set[str]:
    """Return non-symlink top-level entry names, tolerating errors."""
    try:
        return {
            entry.name for entry in Path(root).iterdir() if not entry.is_symlink()
        }
    except OSError:
        return set()
