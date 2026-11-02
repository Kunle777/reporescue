"""Python language adapter (Phase 5A Step 4).

Owns Python-specific repository detection and planning inputs: file
detection, project classification, manifest and dependency discovery,
framework detection, and runtime eligibility. Moved verbatim from
``app/python_detect.py``; behavior is unchanged. Read-only: never
installs dependencies, never executes repository code, never invokes
Docker.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from .base import AdapterResult, scan_suffixes, top_level_names

LANGUAGE = "python"

DEPENDENCY_MARKERS = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
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

# Optional-dependency extras installed alongside runtime dependencies.
# Only these recognized test/development names are honored; every other
# extra is ignored rather than installed indiscriminately.
TEST_EXTRA_NAMES = ("test", "tests", "testing", "dev")

REQUIREMENTS_FILENAMES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
)

SUPPORTED_TEST_FRAMEWORKS = ("pytest", "unittest")

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
    return scan_suffixes(root, frozenset({".py"}), skip_egg_info=True)


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


def _validate_dep_spec(value: Any, source: str) -> str | None:
    """Return an error message when a dependency entry is unsafe, else None.

    Entries are passed to pip verbatim (one argv element each, markers
    preserved), so anything that is not a plain requirement string — or
    that pip would parse as an option — is a loud setup error, never a
    silent skip. Values are never echoed: entries may embed credentials.
    """
    if not isinstance(value, str) or not value.strip():
        return (
            f"invalid dependency entry in {source}: expected a non-empty "
            "requirement string; refusing to guess dependencies."
        )
    if value.strip().startswith("-"):
        return (
            f"invalid dependency entry in {source}: entries starting with "
            "'-' would be parsed as pip options; refusing to guess "
            "dependencies."
        )
    return None


def _parse_project_dependencies(
    root: Path, dep_files: list[str]
) -> tuple[list[str] | None, list[str], str | None]:
    """Parse installable dependency metadata from ``pyproject.toml``.

    Returns ``(runtime_deps, test_extra_deps, error)``. ``runtime_deps``
    is None when no ``[project]`` table exists (not an installable
    distribution: the installer must skip project installation).
    ``error`` is a human-readable setup-stage message; malformed metadata
    never crashes and never silently drops entries.
    """
    if "pyproject.toml" not in dep_files:
        return None, [], None
    try:
        raw = (root / "pyproject.toml").read_bytes().decode("utf-8", "replace")
        data = tomllib.loads(raw)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return (
            None,
            [],
            f"pyproject.toml could not be parsed ({exc}); "
            "refusing to guess dependencies.",
        )
    if not isinstance(data, dict):
        return (
            None,
            [],
            "pyproject.toml has an invalid top-level table; "
            "refusing to guess dependencies.",
        )
    project = data.get("project")
    if project is None:
        return None, [], None
    if not isinstance(project, dict):
        return (
            None,
            [],
            "pyproject.toml [project] table is malformed; "
            "refusing to guess dependencies.",
        )
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list):
        return (
            None,
            [],
            "pyproject.toml [project] dependencies must be a list; "
            "refusing to guess dependencies.",
        )
    runtime: list[str] = []
    for index, entry in enumerate(dependencies):
        invalid = _validate_dep_spec(entry, f"[project] dependencies[{index}]")
        if invalid:
            return None, [], invalid
        runtime.append(entry)
    test_deps: list[str] = []
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        return (
            None,
            [],
            "pyproject.toml [project] optional-dependencies must be a table; "
            "refusing to guess dependencies.",
        )
    for extra in TEST_EXTRA_NAMES:
        entries = optional.get(extra, [])
        if not isinstance(entries, list):
            return (
                None,
                [],
                f"pyproject.toml optional-dependencies.{extra} must be a "
                "list; refusing to guess dependencies.",
            )
        for index, entry in enumerate(entries):
            invalid = _validate_dep_spec(
                entry, f"optional-dependencies.{extra}[{index}]"
            )
            if invalid:
                return None, [], invalid
            test_deps.append(entry)
    return runtime, test_deps, None


_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")


def _unsafe_reference_reason(text: str) -> str | None:
    """Return a rejection reason when text holds a hostile reference form."""
    if text.startswith("-"):
        return (
            "pip options and directives (lines starting with '-') are not "
            "supported; list plain package requirements instead."
        )
    if "://" in text or " @ " in text:
        return (
            "direct URL references are not supported; list plain package "
            "requirements instead."
        )
    lowered = text.lower()
    if lowered.startswith(_VCS_PREFIXES) or lowered.startswith("file:"):
        return (
            "VCS and local-path references are not supported; list plain "
            "package requirements instead."
        )
    if text.startswith((".", "/", "~")):
        return (
            "local-path references are not supported; list plain package "
            "requirements instead."
        )
    return None


def _validate_requirement_line(line: str, source: str, lineno: int) -> str | None:
    """Validate one logical requirements line. None when acceptable.

    Only plain PEP 508 package requirements (names, extras, specifiers,
    markers) pass. pip options/directives, nested includes, editable
    installs, URLs, VCS references, and local paths are rejected: every
    accepted line must be safe to hand to ``pip install`` verbatim.
    Messages name the file and line, never the entry text (entries may
    embed credentials).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    text = stripped.split("#", 1)[0].strip()
    if not text:
        return None
    reason = _unsafe_reference_reason(text)
    if reason:
        return f"{source} line {lineno}: {reason}"
    try:
        Requirement(text)
    except InvalidRequirement:
        return (
            f"{source} line {lineno}: not a plain package requirement; "
            "expected 'name[extras]specifier; marker'."
        )
    return None


def _physical_line_scan(text: str, source: str) -> str | None:
    """Reject hostile content on any single physical line.

    Backslash continuations and comments could otherwise smuggle a pip
    directive into a logical line that reads as harmless (e.g. a comment
    line ending in ``\\`` swallowing the next line). Any non-comment
    physical line carrying an option, URL, VCS, or path reference fails
    the whole file, independent of how continuations join.
    """
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = stripped.split("#", 1)[0].rstrip("\\").strip()
        if not candidate:
            continue
        reason = _unsafe_reference_reason(candidate)
        if reason:
            return f"{source} line {lineno}: {reason}"
    return None


def validate_requirements_file(path: Path, display: str) -> str | None:
    """Fail-closed validation of one requirements file. None when clean.

    Oversized files are rejected rather than partially validated: the
    installer would read the whole file, so validating a prefix could
    miss a hostile directive further down.
    """
    try:
        if path.stat().st_size > MAX_DEP_SCAN_BYTES:
            return (
                f"{display} is larger than {MAX_DEP_SCAN_BYTES} bytes; "
                "split it into smaller requirements files."
            )
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rejected = _physical_line_scan(text, display)
    if rejected:
        return rejected
    pending: str | None = None
    pending_lineno = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        chunk = raw.strip()
        if chunk.endswith("\\"):
            chunk = chunk[:-1]
            if pending is None:
                pending, pending_lineno = chunk, lineno
            else:
                pending += chunk
            continue
        if pending is not None:
            chunk, lineno, pending = pending + chunk, pending_lineno, None
        error = _validate_requirement_line(chunk, display, lineno)
        if error:
            return error
    if pending is not None:
        return _validate_requirement_line(pending, display, pending_lineno)
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


def _project_identity(root: Path, dep_files: list[str]) -> dict[str, str]:
    """Best-effort project name/version from ``pyproject.toml``.

    Read-only display metadata for the adapter contract; never affects
    eligibility. Malformed or missing data yields an empty mapping.
    """
    if "pyproject.toml" not in dep_files:
        return {}
    try:
        data = tomllib.loads(
            (root / "pyproject.toml").read_bytes().decode("utf-8", "replace")
        )
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    project = data.get("project")
    if not isinstance(project, dict):
        return {}
    identity: dict[str, str] = {}
    for key in ("name", "version"):
        value = project.get(key)
        if isinstance(value, str) and value.strip():
            identity[key] = value.strip()
    return identity


def detect(root: Path) -> AdapterResult:
    """Run the full Python analysis for one checkout ``root``.

    Returns the adapter-contract result; ``details`` carries everything
    the capability profile needs beyond the contract fields.
    """
    root = Path(root)
    top_names = top_level_names(root)
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
    frameworks = _detect_frameworks(root, dep_files)
    python_requires = _parse_python_requires(root, dep_files)
    project_dependencies, project_test_dependencies, dependencies_error = (
        _parse_project_dependencies(root, dep_files)
    )
    requirements_error: str | None = None
    for marker in REQUIREMENTS_FILENAMES:
        if marker in dep_files:
            requirements_error = validate_requirements_file(root / marker, marker)
            if requirements_error:
                break

    dependency_error = dependencies_error or requirements_error
    # Runtime eligibility mirrors the investigation gates exactly (project
    # support, then test framework) plus dependency validity.
    if not is_python:
        runnable, runnable_reason = False, "unsupported_project"
    elif test_framework not in SUPPORTED_TEST_FRAMEWORKS:
        runnable, runnable_reason = False, "unsupported_framework"
    elif dependency_error:
        runnable, runnable_reason = False, "invalid_dependencies"
    else:
        runnable, runnable_reason = True, None

    limitations: list[str] = []
    if not runnable and runnable_reason:
        limitations.append(f"Python runtime is unavailable ({runnable_reason}).")

    return AdapterResult(
        language=LANGUAGE,
        file_count=len(py_files),
        manifests=tuple(dep_files),
        project=_project_identity(root, dep_files),
        dependencies={
            "status": "invalid" if dependency_error else dependency_format,
            "files": dep_files,
            "error": dependency_error,
        },
        test_frameworks=(
            (test_framework,) if test_framework in SUPPORTED_TEST_FRAMEWORKS else ()
        ),
        static_available=bool(py_files or dep_files),
        runtime_available=runnable,
        runtime_reason=runnable_reason,
        limitations=tuple(limitations),
        details={
            "is_python": is_python,
            "dependency_format": dependency_format,
            "dependency_formats": sorted(formats),
            "tests": tests,
            "test_framework": test_framework,
            "frameworks": frameworks,
            "python_requires": python_requires,
            "project_dependencies": project_dependencies,
            "project_test_dependencies": project_test_dependencies,
            "dependencies_error": dependencies_error,
            "requirements_error": requirements_error,
            "source_dirs": source_dirs,
            "package_dirs": package_dirs,
        },
    )
