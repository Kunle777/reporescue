from __future__ import annotations

"""Safe repository inventory and bounded context extraction.

Phase 2 inspects real-world checkouts, so every read here is defensive:
irrelevant directories are skipped, symlinks are never followed, binary and
oversized files are excluded, and total output is capped. The extracted
context is the only repository content that later pipeline stages (including
Meta AI diagnosis) may consume; the raw checkout is never sent anywhere.
"""

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Directories that never carry useful project signal for debugging.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".sl",
        ".svn",
        ".eden",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "bower_components",
        "build",
        "dist",
        "target",
        "vendor",
        "Pods",
        ".idea",
        ".vscode",
        ".gradle",
        ".next",
        ".nuxt",
        ".output",
        ".turbo",
        ".parcel-cache",
        ".vercel",
        ".nyc_output",
        ".expo",
        "coverage",
        ".coverage",
        "htmlcov",
    }
)

EXCLUDED_SUFFIXES = frozenset(
    {
        ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".exe", ".o", ".a",
        ".lib", ".obj", ".class", ".jar", ".war", ".png", ".jpg", ".jpeg",
        ".gif", ".bmp", ".ico", ".webp", ".svg", ".mp3", ".mp4", ".wav",
        ".avi", ".mov", ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz",
        ".7z", ".rar", ".whl", ".egg", ".woff", ".woff2", ".ttf", ".otf",
        ".eot", ".sqlite", ".sqlite3", ".db", ".bin", ".dat", ".map",
    }
)

CONFIG_FILENAMES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "tox.ini",
        "pytest.ini",
        "setup.py",
        "poetry.lock",
        "conda.yaml",
        "environment.yml",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "Makefile",
        "package.json",
        "go.mod",
        "Cargo.toml",
    }
)

# Files that may hold credentials: inventoried by name only, never read.
SECRET_BASENAMES = frozenset(
    {".env", ".env.local", ".env.production", "credentials.json", "secrets.yaml",
     "secrets.yml", "secrets.json", ".npmrc", ".pypirc"}
)
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".kdbx"})
SECRET_NAME_HINTS = ("secret", "credential", "private_key", "id_rsa", "id_ed25519")

ENTRY_POINT_NAMES = frozenset(
    {"main.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py",
     "cli.py", "run.py", "server.py"}
)
# app.py usually defines the application object inside a package; it only
# counts as an entry point at the repository root (e.g. Flask-style layouts).
ROOT_ENTRY_POINT_NAMES = frozenset({"app.py"})

MAX_INVENTORY_FILES = 5000
MAX_READ_BYTES = 100_000
MAX_EXCERPT_FILES = 60
MAX_TOTAL_EXCERPT_BYTES = 400_000
MAX_AI_CONTEXT_CHARS = 12_000
_BINARY_PROBE_BYTES = 8192


@dataclass
class _IgnoreRules:
    """Minimal .gitignore support: literals, dir/ prefixes, globs, !negation."""

    patterns: list[tuple[str, bool]]

    def ignored(self, rel_posix: str, is_dir: bool) -> bool:
        ignored = False
        for pattern, negated in self.patterns:
            if _match_ignore(pattern, rel_posix, is_dir):
                ignored = not negated
        return ignored


def _match_ignore(pattern: str, rel_posix: str, is_dir: bool) -> bool:
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return rel_posix == prefix or rel_posix.startswith(prefix + "/")
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel_posix, pattern)
    return fnmatch.fnmatchcase(rel_posix.rsplit("/", 1)[-1], pattern)


def _load_ignore_rules(root: Path) -> _IgnoreRules:
    patterns: list[tuple[str, bool]] = []
    ignore_file = root / ".gitignore"
    try:
        if not ignore_file.is_file() or ignore_file.is_symlink():
            return _IgnoreRules(patterns)
        text = ignore_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _IgnoreRules(patterns)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
            if not line:
                continue
        line = line.lstrip("/")
        if line:
            patterns.append((line, negated))
    return _IgnoreRules(patterns)


def _is_within_root(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in SECRET_BASENAMES:
        return True
    if any(lowered.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return True
    return any(hint in lowered for hint in SECRET_NAME_HINTS)


def _is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_PROBE_BYTES)
    except OSError:
        return True


def classify_file(rel_posix: str) -> str:
    """Classify an inventoried file for context extraction."""
    name = rel_posix.rsplit("/", 1)[-1]
    lowered = rel_posix.lower()
    if name in CONFIG_FILENAMES or lowered.endswith((".cfg", ".ini", ".toml")) and "/" not in lowered:
        return "config"
    parts = lowered.split("/")
    if (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    ):
        return "test"
    if name in ENTRY_POINT_NAMES or (
        "/" not in rel_posix and name in ROOT_ENTRY_POINT_NAMES
    ):
        return "entry"
    if lowered.endswith(".py"):
        return "source"
    if lowered.endswith((".md", ".rst", ".txt")):
        return "doc"
    return "other"


def inventory_repository(root: Path) -> dict[str, Any]:
    """Walk ``root`` safely and return a bounded file inventory.

    Directory symlinks are never descended into and file symlinks are
    recorded as skipped, so a malicious checkout cannot pull the walker
    outside the workspace or into a loop.
    """
    root = Path(root)
    rules = _load_ignore_rules(root)
    files: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    warnings: list[str] = []
    truncated = False

    def note(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def walk(current: Path) -> None:
        nonlocal truncated
        try:
            entries = sorted(current.iterdir(), key=lambda entry: entry.name)
        except OSError:
            note("unreadable_dir")
            return
        for entry in entries:
            if len(files) + sum(skipped.values()) >= MAX_INVENTORY_FILES:
                truncated = True
                return
            try:
                rel = entry.relative_to(root).as_posix()
            except ValueError:
                note("outside_root")
                continue
            try:
                is_link = entry.is_symlink()
            except OSError:
                note("unreadable")
                continue
            if is_link:
                note("symlink")
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                note("unreadable")
                continue
            if is_dir:
                if (
                    entry.name in EXCLUDED_DIRS
                    or entry.name.endswith(".egg-info")
                    or rules.ignored(rel, True)
                ):
                    note("excluded_dir")
                    continue
                if not _is_within_root(root, entry):
                    note("outside_root")
                    continue
                walk(entry)
                if truncated:
                    return
                continue
            if not _is_within_root(root, entry):
                note("outside_root")
                continue
            if rules.ignored(rel, False):
                note("ignored")
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                note("unreadable")
                continue
            kind = classify_file(rel)
            skip_reason: str | None = None
            if entry.suffix.lower() in EXCLUDED_SUFFIXES:
                skip_reason = "binary_suffix"
            elif _is_secret_name(entry.name):
                skip_reason = "secret"
            if skip_reason:
                note(skip_reason)
                files.append(
                    {"path": rel, "size": size, "kind": kind,
                     "readable": False, "skip_reason": skip_reason}
                )
                continue
            files.append({"path": rel, "size": size, "kind": kind, "readable": True})

    walk(root)
    if truncated:
        warnings.append(
            f"Inventory truncated at {MAX_INVENTORY_FILES} entries; "
            "remaining files were omitted."
        )
    if skipped.get("symlink"):
        warnings.append(
            f"{skipped['symlink']} symlink(s) were listed but not followed."
        )
    return {
        "root": str(root),
        "files": files,
        "total_files": len(files),
        "truncated": truncated,
        "skipped": skipped,
        "warnings": warnings,
    }


def _read_text_bounded(path: Path) -> str | None:
    """Return file text, or None for binary/oversized/unreadable files."""
    try:
        if path.stat().st_size > MAX_READ_BYTES:
            return None
    except OSError:
        return None
    if _is_probably_binary(path):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def extract_context(root: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    """Select entry points, configs, sources, and tests with bounded excerpts.

    Priority order is entry points, readable configs, tests (listed with a
    few excerpts), then other sources, stopping when the file or byte budget
    is exhausted. Secret, binary, and oversized files are never read.
    """
    root = Path(root)
    kind_order = {"entry": 0, "config": 1, "test": 2, "source": 3, "doc": 4, "other": 5}
    candidates = sorted(
        inventory.get("files", []),
        key=lambda item: (kind_order.get(item.get("kind", "other"), 5), item.get("path", "")),
    )
    excerpts: dict[str, str] = {}
    excerpt_files: list[str] = []
    used = 0
    unreadable: list[str] = []
    by_kind: dict[str, list[str]] = {}
    for item in candidates:
        by_kind.setdefault(item.get("kind", "other"), []).append(item["path"])
        if not item.get("readable"):
            if item.get("skip_reason") == "binary_suffix":
                unreadable.append(item["path"])
            continue
        if len(excerpts) >= MAX_EXCERPT_FILES or used >= MAX_TOTAL_EXCERPT_BYTES:
            continue
        if item.get("size", 0) > MAX_READ_BYTES:
            unreadable.append(item["path"])
            continue
        text = _read_text_bounded(root / item["path"])
        if text is None:
            unreadable.append(item["path"])
            continue
        budget = MAX_TOTAL_EXCERPT_BYTES - used
        excerpt = text[:budget]
        excerpts[item["path"]] = excerpt
        excerpt_files.append(item["path"])
        used += len(excerpt)

    warnings = list(inventory.get("warnings", []))
    if unreadable:
        warnings.append(
            f"{len(unreadable)} file(s) were excluded as binary, oversized, "
            "or unreadable."
        )
    if len(excerpts) >= MAX_EXCERPT_FILES or used >= MAX_TOTAL_EXCERPT_BYTES:
        warnings.append("Code excerpts were truncated to fit the context budget.")

    return {
        "entry_points": by_kind.get("entry", []),
        "configs": by_kind.get("config", []),
        "sources": by_kind.get("source", []),
        "tests": by_kind.get("test", []),
        "docs": by_kind.get("doc", []),
        "other": by_kind.get("other", []),
        "excerpt_files": excerpt_files,
        "excerpts": excerpts,
        "excerpt_bytes": used,
        "unreadable": unreadable,
        "skipped": inventory.get("skipped", {}),
        "truncated": inventory.get("truncated", False),
        "warnings": warnings,
    }


def render_ai_context(extracted: dict[str, Any]) -> str:
    """Render extracted context as one bounded string for AI diagnosis.

    This is the only repository-content renderer later pipeline stages may
    use; it only sees files that already passed the safety filters above.
    """
    chunks: list[str] = []
    used = 0
    for path in extracted.get("excerpt_files", []):
        text = extracted.get("excerpts", {}).get(path, "")
        chunk = f"--- {path} ---\n{text}\n"
        if used + len(chunk) > MAX_AI_CONTEXT_CHARS:
            chunks.append("[truncated: remaining excerpts omitted to bound request size]")
            break
        chunks.append(chunk)
        used += len(chunk)
    if not chunks:
        return "[no readable source excerpts available]"
    return "\n".join(chunks)
