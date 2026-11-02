from __future__ import annotations

"""Repository profiling assembled from language adapters.

Detection lives in :mod:`app.languages` (one adapter per language);
this module assembles the backward-compatible profile contract from
adapter results: identity, primary language, capability profile,
warnings, and the human-readable summary.
"""

import tomllib
from pathlib import Path
from typing import Any

from .languages import detect_all
from .languages.python import (
    DEPENDENCY_FORMAT_LABELS,
    DEPENDENCY_MARKERS,
    KNOWN_FRAMEWORKS,
    MAX_DEP_SCAN_BYTES,
    MAX_TEST_IMPORT_SCAN,
    REQUIREMENTS_FILENAMES,
    TEST_EXTRA_NAMES,
    validate_requirements_file,
)

__all__ = [
    "DEPENDENCY_FORMAT_LABELS",
    "DEPENDENCY_MARKERS",
    "KNOWN_FRAMEWORKS",
    "MAX_DEP_SCAN_BYTES",
    "MAX_PROFILE_WARNINGS",
    "MAX_TEST_IMPORT_SCAN",
    "REQUIREMENTS_FILENAMES",
    "TEST_EXTRA_NAMES",
    "build_profile",
    "validate_requirements_file",
]

MAX_PROFILE_WARNINGS = 12


def _primary_language(root: Path, py_file_count: int, dep_files: list[str]) -> str:
    if py_file_count or dep_files:
        return "python"
    top = {path.name for path in _top_level_entries(root)}
    if "package.json" in top:
        return "javascript"
    if "go.mod" in top:
        return "go"
    if "Cargo.toml" in top:
        return "rust"
    if "pom.xml" in top or "build.gradle" in top or "build.gradle.kts" in top:
        return "java"
    if any(name.endswith((".csproj", ".sln")) for name in top):
        return "csharp"
    if "Gemfile" in top:
        return "ruby"
    if "composer.json" in top:
        return "php"
    return "unknown"


def _top_level_entries(root: Path) -> list[Path]:
    try:
        return [entry for entry in root.iterdir() if not entry.is_symlink()]
    except OSError:
        return []


def build_profile(
    root: Path,
    *,
    url: str,
    name: str,
    commit_sha: str,
    ref: str | None = None,
) -> dict[str, Any]:
    """Build the structured repository profile for a checked-out ``root``."""
    root = Path(root)
    results = detect_all(root)
    py = results["python"]
    js = results["javascript"]
    details = py.details

    dep_files = list(py.manifests)
    dependency_format = details["dependency_format"]
    tests = details["tests"]
    test_framework = details["test_framework"]
    is_python = details["is_python"]
    primary_language = _primary_language(root, py.file_count, dep_files)
    has_package_json = "package.json" in js.manifests

    capabilities = {
        "languages": {
            "python": {"files": py.file_count},
            "javascript": {
                "files": js.file_count,
                "manifest": "package.json" if has_package_json else None,
            },
        },
        "dependencies": {
            "python": dict(py.dependencies),
            "javascript": dict(js.dependencies),
        },
        "test_frameworks": list(py.test_frameworks),
        "static": {
            "available": bool(py.static_available or js.static_available),
        },
        "runnable": {"python": py.runtime_available, "reason": py.runtime_reason},
    }

    warnings: list[str] = []
    if not dep_files:
        warnings.append("No Python dependency metadata found (no pyproject, requirements, setup, Pipfile, or tox/pytest config).")
    if dependency_format == "multiple":
        warnings.append(
            f"Multiple dependency formats detected: {', '.join(details['dependency_formats'])}; "
            "a later phase must decide which one governs installation."
        )
    if not tests["count"]:
        warnings.append("No test files discovered; test-backed verification is unavailable.")
    if not is_python and py.file_count == 1:
        warnings.append("Only a single Python file was found; this is not enough to treat the repository as a supported Python project.")
    if not is_python and not py.file_count:
        warnings.append("No Python source files were found.")
    if "pyproject.toml" in dep_files:
        try:
            data = tomllib.loads((root / "pyproject.toml").read_bytes().decode("utf-8", "replace"))
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            data = None
        if not isinstance(data, dict) or (
            "project" not in data and "poetry" not in str(data.get("tool", {}))
        ):
            warnings.append("pyproject.toml has no [project] or [tool.poetry] table; build metadata may be incomplete.")

    if is_python:
        summary = (
            f"Python project ({dependency_format} dependencies) with "
            f"{py.file_count} Python file(s)"
            + (f" across {len(details['source_dirs'])} directories" if details["source_dirs"] else "")
            + (
                f"; {tests['count']} {test_framework} test file(s)"
                if tests["count"]
                else "; no tests discovered"
            )
            + (f"; frameworks: {', '.join(details['frameworks'])}" if details["frameworks"] else "")
            + "."
        )
    else:
        summary = (
            f"Not a supported Python project (primary language: {primary_language}; "
            f"{py.file_count} Python file(s), {len(dep_files)} dependency marker(s))."
        )

    return {
        "url": url,
        "name": name,
        "commit_sha": commit_sha,
        "ref": ref,
        "primary_language": primary_language,
        "is_python_project": is_python,
        # Deprecated alias: kept byte-identical for backward compatibility
        # while modes migrate to capabilities["runnable"]. Removal planned
        # for a later Phase 5 step, not this one.
        "supported": is_python,
        "capabilities": capabilities,
        "python_requires": details["python_requires"],
        "dependency_format": dependency_format,
        "dependency_files": dep_files,
        "project_dependencies": details["project_dependencies"],
        "project_test_dependencies": details["project_test_dependencies"],
        "project_dependencies_error": details["dependencies_error"],
        "requirements_error": details["requirements_error"],
        "frameworks": details["frameworks"],
        "test_framework": test_framework,
        "tests": tests,
        "python_file_count": py.file_count,
        "source_dirs": details["source_dirs"],
        "package_dirs": details["package_dirs"],
        "structure_summary": summary,
        "warnings": warnings[:MAX_PROFILE_WARNINGS],
    }
