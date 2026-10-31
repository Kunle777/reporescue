from __future__ import annotations

"""Python project detection and repository profiling.

A checkout is profiled from its top-level markers, dependency declarations,
source layout, and tests. A repository is NOT considered a supported Python
project merely because it contains one ``.py`` file: real project signal
(dependency metadata, a package layout, or tests) is required.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

from .repo_context import EXCLUDED_DIRS

DEPENDENCY_MARKERS = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "tox.ini",
    "pytest.ini",
)

DEPENDENCY_FORMAT_LABELS = {
    "pyproject.toml": "pyproject",
    "requirements.txt": "requirements",
    "requirements-dev.txt": "requirements",
    "requirements-test.txt": "requirements",
    "setup.py": "setup.py",
    "setup.cfg": "setup.cfg",
    "Pipfile": "pipfile",
    "tox.ini": "tox",
    "pytest.ini": "pytest",
}

KNOWN_FRAMEWORKS = (
    "fastapi",
    "starlette",
    "django",
    "flask",
    "quart",
    "sanic",
    "tornado",
    "pyramid",
    "bottle",
    "pydantic",
    "sqlalchemy",
    "alembic",
    "pytest",
    "hypothesis",
    "requests",
    "httpx",
    "aiohttp",
    "numpy",
    "pandas",
    "scipy",
    "scikit-learn",
    "torch",
    "tensorflow",
    "typer",
    "click",
    "rich",
    "uvicorn",
    "gunicorn",
    "celery",
    "redis",
    "boto3",
    "azure",
    "google-cloud",
    "psycopg",
    "asyncpg",
    "pytest-asyncio",
)

MAX_DEP_SCAN_BYTES = 200_000
MAX_TEST_IMPORT_SCAN = 20
MAX_PROFILE_WARNINGS = 12

_PYTHON_REQUIRES_QUOTED_RE = re.compile(
    r"""python_requires\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
# setup.cfg style: python_requires = >=3.9 (unquoted, optional comment).
_PYTHON_REQUIRES_BARE_RE = re.compile(
    r"""python_requires\s*=\s*([^"'\s#;][^#;\r\n]*?)(?:\s*[#;]|\s*$)""",
    re.IGNORECASE | re.MULTILINE,
)
_UNITTEST_IMPORT_RE = re.compile(r"^\s*(import\s+unittest|from\s+unittest\b)", re.MULTILINE)


def _read_text_capped(path: Path, limit: int = MAX_DEP_SCAN_BYTES) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    stack = [root]
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
                    if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                        continue
                    stack.append(entry)
                elif entry.suffix.lower() == ".py":
                    found.append(entry)
            except OSError:
                continue
    return found


def _parse_python_requires(root: Path, dep_files: list[str]) -> str | None:
    if "pyproject.toml" in dep_files:
        try:
            data = tomllib.loads((root / "pyproject.toml").read_bytes().decode("utf-8", "replace"))
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            project = data.get("project", {})
            if isinstance(project, dict) and project.get("requires-python"):
                return str(project["requires-python"])
            poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
            if isinstance(poetry, dict):
                deps = poetry.get("dependencies", {})
                if isinstance(deps, dict) and deps.get("python"):
                    return str(deps["python"])
    for name in ("setup.cfg", "setup.py"):
        if name in dep_files:
            text = _read_text_capped(root / name)
            match = _PYTHON_REQUIRES_QUOTED_RE.search(text)
            if match:
                return match.group(1).strip()
            bare = _PYTHON_REQUIRES_BARE_RE.search(text)
            if bare:
                return bare.group(1).strip()
    return None


def _detect_frameworks(root: Path, dep_files: list[str]) -> list[str]:
    corpus = "\n".join(_read_text_capped(root / name).lower() for name in dep_files)
    return sorted(framework for framework in KNOWN_FRAMEWORKS if framework in corpus)


def _test_discovery(root: Path, py_files: list[Path]) -> dict[str, Any]:
    test_dirs: set[str] = set()
    test_files: list[str] = []
    for path in py_files:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        name = path.name
        parts = rel.split("/")[:-1]
        is_test = (
            "tests" in parts
            or "test" in parts
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        )
        if is_test:
            test_files.append(rel)
            if parts:
                test_dirs.add("/".join(parts))
    return {
        "dirs": sorted(test_dirs),
        "files": sorted(test_files),
        "count": len(test_files),
    }


def _detect_test_framework(root: Path, dep_files: list[str], tests: dict[str, Any]) -> str:
    if "pytest.ini" in dep_files:
        return "pytest"
    if "tox.ini" in dep_files and "pytest" in _read_text_capped(root / "tox.ini").lower():
        return "pytest"
    corpus = "\n".join(_read_text_capped(root / name).lower() for name in dep_files)
    if "pytest" in corpus:
        return "pytest"
    names = [path.rsplit("/", 1)[-1] for path in tests["files"]]
    if "conftest.py" in names or any(
        name.startswith("test_") or name.endswith("_test.py") for name in names
    ):
        return "pytest"
    for rel in tests["files"][:MAX_TEST_IMPORT_SCAN]:
        if _UNITTEST_IMPORT_RE.search(_read_text_capped(root / rel, 8192)):
            return "unittest"
    if tests["count"]:
        return "unknown"
    return "none"


def _primary_language(root: Path, py_files: list[Path], dep_files: list[str]) -> str:
    if py_files or dep_files:
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
    top_names = {entry.name for entry in _top_level_entries(root)}
    dep_files = [marker for marker in DEPENDENCY_MARKERS if marker in top_names]
    formats = {DEPENDENCY_FORMAT_LABELS[marker] for marker in dep_files}
    if not formats:
        dependency_format = "none"
    elif len(formats) == 1:
        dependency_format = next(iter(formats))
    else:
        dependency_format = "multiple"

    py_files = _python_files(root)
    package_dirs = sorted(
        {
            path.parent.relative_to(root).as_posix()
            for path in py_files
            if path.name == "__init__.py"
            and path.parent != root
        }
    )
    source_dirs = sorted(
        {
            (path.parent.relative_to(root).as_posix() if path.parent != root else ".")
            for path in py_files
        }
    )
    has_src_layout = "src" in source_dirs or any(
        entry.startswith("src/") for entry in source_dirs
    )
    tests = _test_discovery(root, py_files)
    test_framework = _detect_test_framework(root, dep_files, tests)

    is_python = bool(
        dep_files
        or (len(py_files) >= 2 and (tests["count"] > 0 or package_dirs or has_src_layout))
    )
    primary_language = _primary_language(root, py_files, dep_files)
    frameworks = _detect_frameworks(root, dep_files)
    python_requires = _parse_python_requires(root, dep_files)

    warnings: list[str] = []
    if not dep_files:
        warnings.append("No Python dependency metadata found (no pyproject, requirements, setup, Pipfile, or tox/pytest config).")
    if dependency_format == "multiple":
        warnings.append(
            f"Multiple dependency formats detected: {', '.join(sorted(formats))}; "
            "a later phase must decide which one governs installation."
        )
    if not tests["count"]:
        warnings.append("No test files discovered; test-backed verification is unavailable.")
    if not is_python and len(py_files) == 1:
        warnings.append("Only a single Python file was found; this is not enough to treat the repository as a supported Python project.")
    if not is_python and not py_files:
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
            f"{len(py_files)} Python file(s)"
            + (f" across {len(source_dirs)} directories" if source_dirs else "")
            + (
                f"; {tests['count']} {test_framework} test file(s)"
                if tests["count"]
                else "; no tests discovered"
            )
            + (f"; frameworks: {', '.join(frameworks)}" if frameworks else "")
            + "."
        )
    else:
        summary = (
            f"Not a supported Python project (primary language: {primary_language}; "
            f"{len(py_files)} Python file(s), {len(dep_files)} dependency marker(s))."
        )

    return {
        "url": url,
        "name": name,
        "commit_sha": commit_sha,
        "ref": ref,
        "primary_language": primary_language,
        "is_python_project": is_python,
        "supported": is_python,
        "python_requires": python_requires,
        "dependency_format": dependency_format,
        "dependency_files": dep_files,
        "frameworks": frameworks,
        "test_framework": test_framework,
        "tests": tests,
        "python_file_count": len(py_files),
        "source_dirs": source_dirs,
        "package_dirs": package_dirs,
        "structure_summary": summary,
        "warnings": warnings[:MAX_PROFILE_WARNINGS],
    }
