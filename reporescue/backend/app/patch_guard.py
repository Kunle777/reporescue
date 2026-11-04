from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ALLOWED_PREFIX = "src/"
MAX_PATCH_CHARS = 4000

# Generalized guard for real repositories (Phase 3). The fixture guard above
# is intentionally narrower (src/-only) and keeps its exact behavior.
MAX_PATCHSET_FILES = 5
MAX_PATCHSET_CHARS = 8000
PATCHABLE_SUFFIX = ".py"

CONFIG_SUFFIXES = frozenset(
    {".cfg", ".ini", ".toml", ".yaml", ".yml", ".json", ".lock", ".env"}
)
CONFIG_BASENAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "tox.ini",
        "pytest.ini",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "poetry.lock",
        "Dockerfile",
        "Makefile",
        ".env",
    }
)

_PATCHSET_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".execvenv",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".expo",
        "build",
        "dist",
        ".tox",
        ".idea",
        ".vscode",
    }
)


@dataclass(frozen=True)
class PatchProposal:
    """A proposed source edit, in whatever origin (stub today, model later)."""

    file: str
    before: str
    after: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "file": self.file,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


class PatchRejected(Exception):
    """Raised when a proposed patch fails safety validation."""


def validate_and_apply(workspace: Path, patch: PatchProposal) -> None:
    """Validate a patch against the guard rules, then apply it to the workspace.

    Fails closed: any rule violation raises PatchRejected and writes nothing.
    """
    target = _resolve_target(workspace, patch.file)
    if len(patch.before) + len(patch.after) > MAX_PATCH_CHARS:
        raise PatchRejected("Patch exceeds the maximum demo patch size; refusing to apply it.")
    if not target.is_file():
        raise PatchRejected(f"Patch target '{patch.file}' does not exist; refusing to create new files.")
    source = target.read_text(encoding="utf-8")
    if patch.before not in source:
        raise PatchRejected(
            "Expected vulnerable line was not found; refusing to patch an unexpected fixture."
        )
    target.write_text(source.replace(patch.before, patch.after, 1), encoding="utf-8")


def _resolve_target(workspace: Path, rel_path: str) -> Path:
    candidate = Path(rel_path)
    posix_absolute = rel_path.replace("\\", "/").startswith("/")
    if candidate.is_absolute() or posix_absolute or ".." in candidate.parts:
        raise PatchRejected(f"Patch path '{rel_path}' is outside the repository; refusing to apply it.")
    normalized = rel_path.replace("\\", "/")
    if normalized == "tests" or normalized.startswith("tests/"):
        raise PatchRejected("Patches to the protected test suite are not allowed.")
    if not normalized.startswith(ALLOWED_PREFIX):
        raise PatchRejected(f"Patch target '{rel_path}' is outside the allowed demo scope (src/).")
    return workspace / candidate


@dataclass(frozen=True)
class FileEdit:
    """One file replacement within a patch set."""

    file: str
    before: str
    after: str

    def to_dict(self) -> dict[str, str]:
        return {"file": self.file, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class PatchSet:
    """A dynamically generated multi-file patch with its rationale."""

    edits: tuple[FileEdit, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "edits": [edit.to_dict() for edit in self.edits],
            "reason": self.reason,
        }


def _is_test_path(normalized: str) -> bool:
    name = normalized.rsplit("/", 1)[-1]
    parts = normalized.split("/")
    return (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _is_config_path(normalized: str) -> bool:
    name = normalized.rsplit("/", 1)[-1]
    lowered = normalized.lower()
    if name in CONFIG_BASENAMES:
        return True
    if any(lowered.endswith(suffix) for suffix in CONFIG_SUFFIXES):
        return True
    return name in {"Dockerfile", "Makefile", "Pipfile"}


def _resolve_patchset_target(workspace: Path, rel_path: str) -> Path:
    """Resolve a patch-set target, rejecting escapes and non-source files.

    Symlinks are rejected outright (never followed), and the resolved path
    must stay inside the workspace.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise PatchRejected("Patch contains an empty file path; refusing to apply it.")
    candidate = Path(rel_path)
    normalized = rel_path.replace("\\", "/")
    if (
        candidate.is_absolute()
        or normalized.startswith("/")
        or ".." in candidate.parts
        or ".." in normalized.split("/")
        or (len(normalized) > 1 and normalized[1] == ":")
    ):
        raise PatchRejected(f"Patch path '{rel_path}' is outside the repository; refusing to apply it.")
    if not normalized.lower().endswith(PATCHABLE_SUFFIX):
        raise PatchRejected(
            f"Patch target '{rel_path}' is not a Python source file; refusing to apply it."
        )
    if _is_test_path(normalized):
        raise PatchRejected(
            f"Patch target '{rel_path}' is part of the protected test suite; refusing to apply it."
        )
    if _is_config_path(normalized):
        raise PatchRejected(
            f"Patch target '{rel_path}' is a protected configuration file; refusing to apply it."
        )
    if _PATCHSET_EXCLUDED_DIRS.intersection(candidate.parts):
        raise PatchRejected(
            f"Patch target '{rel_path}' is inside an excluded directory; refusing to apply it."
        )
    target = workspace / candidate
    try:
        if target.is_symlink():
            raise PatchRejected(
                f"Patch target '{rel_path}' is a symlink; refusing to apply it."
            )
    except OSError:
        raise PatchRejected(f"Patch target '{rel_path}' cannot be inspected; refusing to apply it.")
    try:
        resolved_root = workspace.resolve()
        target.resolve().relative_to(resolved_root)
    except (OSError, ValueError):
        raise PatchRejected(f"Patch path '{rel_path}' escapes the repository; refusing to apply it.")
    return target


def validate_and_apply_patchset(workspace: Path, patch: PatchSet) -> list[dict[str, str]]:
    """Validate every edit, then apply all of them.

    Fails closed: any violation raises PatchRejected and writes nothing
    (validation of all edits completes before the first write). An edit is
    ambiguous unless its ``before`` block occurs exactly once.
    """
    edits = list(patch.edits)
    if not edits:
        raise PatchRejected("Patch contains no edits; refusing to apply it.")
    if len(edits) > MAX_PATCHSET_FILES:
        raise PatchRejected(
            f"Patch touches {len(edits)} files (maximum {MAX_PATCHSET_FILES}); refusing to apply it."
        )
    total = sum(len(edit.before) + len(edit.after) for edit in edits)
    if total > MAX_PATCHSET_CHARS:
        raise PatchRejected("Patch exceeds the maximum patch size; refusing to apply it.")
    seen: set[str] = set()
    for edit in edits:
        key = edit.file.replace("\\", "/")
        if key in seen:
            raise PatchRejected(f"Patch edits '{edit.file}' twice; refusing ambiguous patches.")
        seen.add(key)
    planned: list[tuple[Path, str, FileEdit]] = []
    for edit in edits:
        target = _resolve_patchset_target(workspace, edit.file)
        if not target.is_file():
            raise PatchRejected(f"Patch target '{edit.file}' does not exist; refusing to create new files.")
        if not edit.before:
            raise PatchRejected(f"Patch for '{edit.file}' has an empty 'before' block; refusing to apply it.")
        if edit.before == edit.after:
            raise PatchRejected(f"Patch for '{edit.file}' makes no change; refusing to apply it.")
        try:
            source = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise PatchRejected(f"Patch target '{edit.file}' is not readable text; refusing to apply it.")
        occurrences = source.count(edit.before)
        if occurrences == 0:
            raise PatchRejected(
                f"Expected block was not found in '{edit.file}'; refusing to patch unexpected content."
            )
        if occurrences > 1:
            raise PatchRejected(
                f"Expected block occurs {occurrences} times in '{edit.file}'; refusing ambiguous patches."
            )
        planned.append((target, source, edit))
    applied: list[dict[str, str]] = []
    for target, source, edit in planned:
        target.write_text(source.replace(edit.before, edit.after, 1), encoding="utf-8")
        applied.append({"file": edit.file, "changed": "true"})
    return applied
