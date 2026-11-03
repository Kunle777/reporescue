"""Deterministic explain/health reports built from a persisted repo profile.

Both builders read ONLY the stored ``RepoProfile`` (plus the repo display
name recorded on the job row). They touch no filesystem, spawn no
subprocess, and make no model calls, so their output is a pure function of
the persisted profile.

Finding vocabulary mirrors the frontend ``Finding`` union::

    kind:       "fact" | "failed check" | "hypothesis"
    confidence: "confirmed" | "medium" | "low"
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STATIC_LIMITATION = "Static analysis only; nothing was executed."


def _join_capped(items: list[str], limit: int = 5) -> str:
    if len(items) <= limit:
        return ", ".join(items)
    return f"{', '.join(items[:limit])} (+{len(items) - limit} more)"


def _warnings_as_facts(warnings: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "fact",
            "category": "profile",
            "title": warning,
            "detail": None,
            "confidence": "confirmed",
        }
        for warning in warnings
    ]


def build_explain(record: Mapping[str, Any]) -> dict[str, Any]:
    """Narrate repository structure from the persisted ingestion profile."""
    profile = record.get("profile") or {}
    name = record.get("repo_name") or profile.get("name") or "repository"
    primary = profile.get("primary_language") or "unknown"
    py_count = profile.get("python_file_count", 0) or 0
    source_dirs = profile.get("source_dirs") or []
    tests = profile.get("tests") or {}
    test_count = tests.get("count", 0) or 0
    test_files = tests.get("files") or []
    test_dirs = tests.get("dirs") or []
    framework = tests.get("framework") or profile.get("test_framework") or ""
    dep_format = profile.get("dependency_format") or "none"
    dep_files = profile.get("dependency_files") or []
    frameworks = profile.get("frameworks") or []
    entry = profile.get("entry_points") or {}
    entry_files = entry.get("files") or []
    warnings = profile.get("warnings") or []

    findings: list[dict[str, Any]] = []
    if py_count > 0:
        findings.append(
            {
                "kind": "fact",
                "category": "structure",
                "title": f"Primary language: {primary}",
                "detail": (
                    f"{py_count} Python file(s) across: {_join_capped(source_dirs)}."
                    if source_dirs
                    else f"{py_count} Python file(s) discovered."
                ),
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "fact",
                "category": "structure",
                "title": "No Python sources detected",
                "detail": "No Python source files were discovered under the scanned roots.",
                "confidence": "confirmed",
            }
        )

    if entry_files:
        findings.append(
            {
                "kind": "fact",
                "category": "structure",
                "title": f"{len(entry_files)} entry point(s)",
                "detail": _join_capped(entry_files),
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "fact",
                "category": "structure",
                "title": "No entry points detected",
                "detail": "No __main__ blocks, cli.py, or main.py were found.",
                "confidence": "confirmed",
            }
        )

    if test_count > 0:
        label = f"{test_count} test file(s)"
        if framework and framework != "none":
            label += f" ({framework})"
        locations = _join_capped([*(test_dirs or []), *(test_files or [])])
        findings.append(
            {
                "kind": "fact",
                "category": "tests",
                "title": label,
                "detail": locations or None,
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "fact",
                "category": "tests",
                "title": "No tests discovered",
                "detail": "No test files or directories were found.",
                "confidence": "confirmed",
            }
        )

    if dep_format != "none":
        findings.append(
            {
                "kind": "fact",
                "category": "dependencies",
                "title": f"Dependencies: {dep_format}",
                "detail": _join_capped(dep_files) or None,
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "fact",
                "category": "dependencies",
                "title": "No dependency metadata",
                "detail": "No requirements, pyproject, or setup files were found.",
                "confidence": "confirmed",
            }
        )

    if frameworks:
        findings.append(
            {
                "kind": "fact",
                "category": "dependencies",
                "title": f"Frameworks: {', '.join(frameworks)}",
                "detail": None,
                "confidence": "confirmed",
            }
        )

    findings.extend(_warnings_as_facts(warnings))

    structure = profile.get("structure_summary") or "structure recorded."
    summary = f"Static explanation of {name}: {structure}"
    return {"mode": "explain", "summary": summary, "overall": "", "findings": findings}


def build_health(record: Mapping[str, Any]) -> dict[str, Any]:
    """Audit repository health from the persisted ingestion profile."""
    profile = record.get("profile") or {}
    name = record.get("repo_name") or profile.get("name") or "repository"
    tests = profile.get("tests") or {}
    test_count = tests.get("count", 0) or 0
    framework = tests.get("framework") or profile.get("test_framework") or ""
    dep_format = profile.get("dependency_format") or "none"
    dep_files = profile.get("dependency_files") or []
    requires = profile.get("python_requires")
    warnings = profile.get("warnings") or []

    findings: list[dict[str, Any]] = []
    passed = 0

    if test_count > 0:
        passed += 1
        detail = f"{test_count} test file(s) discovered"
        if framework and framework != "none":
            detail += f" ({framework})"
        findings.append(
            {
                "kind": "fact",
                "category": "tests",
                "title": "Test suite present",
                "detail": detail + ".",
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "failed check",
                "category": "tests",
                "title": "No test files",
                "detail": "No test files discovered; test-backed verification is unavailable.",
                "confidence": "confirmed",
            }
        )

    if dep_format != "none":
        passed += 1
        detail = f"Dependencies declared via {dep_format}"
        if dep_files:
            detail += f" ({_join_capped(dep_files)})"
        findings.append(
            {
                "kind": "fact",
                "category": "dependencies",
                "title": "Dependency metadata present",
                "detail": detail + ".",
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "failed check",
                "category": "dependencies",
                "title": "No dependency metadata",
                "detail": "No dependency metadata discovered; install behavior is unknown.",
                "confidence": "confirmed",
            }
        )

    if requires:
        passed += 1
        findings.append(
            {
                "kind": "fact",
                "category": "dependencies",
                "title": "Python version declared",
                "detail": f"Python requirement declared: {requires}.",
                "confidence": "confirmed",
            }
        )
    else:
        findings.append(
            {
                "kind": "failed check",
                "category": "dependencies",
                "title": "Python version undeclared",
                "detail": "No Python version requirement declared.",
                "confidence": "confirmed",
            }
        )

    if dep_format == "multiple" and dep_files:
        findings.append(
            {
                "kind": "hypothesis",
                "category": "dependencies",
                "title": "Multiple dependency formats",
                "detail": (
                    f"Multiple dependency formats present ({_join_capped(dep_files)}); "
                    "installs may disagree about versions."
                ),
                "confidence": "medium",
            }
        )

    findings.extend(_warnings_as_facts(warnings))

    failed = 3 - passed
    overall = "healthy" if failed == 0 else "needs attention"
    summary = (
        f"Health check for {name}: {passed} passed, {failed} need attention."
    )
    return {"mode": "health", "summary": summary, "overall": overall, "findings": findings}
