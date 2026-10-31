from __future__ import annotations

"""Isolated execution of untrusted repository test suites (Phase 3).

Design
------
An :class:`ExecutionBackend` materializes a pinned repository snapshot,
installs its dependencies, and runs its tests, returning a structured
:class:`ExecutionResult`. Two backends exist:

- :class:`DockerBackend` -- the only backend that provides a genuine
  isolation boundary (separate filesystem, network namespace, and resource
  limits). It is selected automatically whenever Docker is available.
- :class:`UnsafeLocalBackend` -- a development-only fallback that runs
  repository code on the host. It is DISABLED by default, requires an
  explicit opt-in environment variable, and every result it produces is
  labelled ``none-unsafe-local-dev-only``. It is never selected silently.

There is intentionally no silent fallback: without Docker and without the
explicit flag, real investigations fail closed with ``runtime_unavailable``.
"""

import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .github_ingest import scrub_env_for_subprocess

INSTALL_TIMEOUT_SECONDS = 240
TEST_TIMEOUT_SECONDS = 120
DOCKER_PROBE_TIMEOUT_SECONDS = 15
DOCKER_RM_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 8000
MAX_FAILURES = 30
TRACEBACK_CHARS_PER_TEST = 800
MAX_INSTALL_COMMANDS = 6

DOCKER_IMAGE_DEFAULT = "python:3.12-slim"
DOCKER_CPUS = "1"
DOCKER_MEMORY = "1g"
DOCKER_PIDS_LIMIT = "256"

UNSAFE_LOCAL_FLAG = "REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION"

_FAILED_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$")
_SHORT_SUMMARY_BOUNDARY_RE = re.compile(r"^=+\s+(short test summary info|FAILURES|ERRORS)\s+=+$")
_TRACEBACK_HEADER_RE = re.compile(r"^_{3,}\s*(.+?)\s*_{3,}\s*$")
_UNITTEST_FAIL_RE = re.compile(r"^(FAIL|ERROR):\s+(\S+?)(?:\s+\(([^)]+)\))?$")
_TRACEBACK_FILE_RE = re.compile(r'^\s*File\s+"([^"]+\.py)",\s+line\s+(\d+)')
_SHORT_FRAME_RE = re.compile(r"^(.+?\.py):(\d+): in ")
_CONFTEST_ERROR_RE = re.compile(r"^ImportError while loading conftest '(.+?)'\.?$")
_COLLECT_ERROR_RE = re.compile(r"^ERROR\s+(?:collecting\s+)?(\S+)")
_SUMMARY_COUNT_RE = re.compile(r"(\d+)\s+(failed|passed|error|skipped|deselected|xfailed|xpassed|warning)")


@dataclass
class ExecutionRequest:
    """One backend execution: install commands then a test command."""

    snapshot: Path
    test_command: list[str] = field(default_factory=lambda: ["python", "-m", "pytest", "-q", "--tb=short"])
    install_commands: list[list[str]] = field(default_factory=list)
    framework: str = "pytest"
    test_timeout: int = TEST_TIMEOUT_SECONDS
    install_timeout: int = INSTALL_TIMEOUT_SECONDS
    python_requires: str | None = None


@dataclass
class ExecutionResult:
    """Structured outcome of one backend execution."""

    backend: str
    isolation: str
    status: str  # ok | test_failure | install_error | timeout | unsupported | error
    passed: bool | None
    exit_code: int | None
    output: str
    duration_ms: int
    failures: list[dict[str, str]] = field(default_factory=list)
    summary_counts: dict[str, int] = field(default_factory=dict)
    install_ok: bool | None = None
    install_log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "isolation": self.isolation,
            "status": self.status,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "output": self.output,
            "duration_ms": self.duration_ms,
            "failures": self.failures,
            "summary_counts": self.summary_counts,
            "install_ok": self.install_ok,
            "install_log": self.install_log,
        }


class ExecutionBackend:
    """Base class for execution backends."""

    name = "base"
    isolation = "unknown"

    def is_available(self) -> tuple[bool, str]:
        """Return (available, human-readable reason)."""
        raise NotImplementedError

    def needs_pytest_install(self) -> bool:
        """Whether the bare ``pip install pytest`` plan step is required."""
        return True

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        raise NotImplementedError


def select_python_image(python_requires: str | None) -> str:
    """Map a requires-python constraint to a container image tag.

    Only the lower bound's minor version is honored; anything unknown falls
    back to the default image rather than failing the investigation.
    """
    match = re.search(r"3\.(\d+)", python_requires or "")
    if match:
        minor = int(match.group(1))
        if 9 <= minor <= 13:
            return f"python:3.{minor}-slim"
    return DOCKER_IMAGE_DEFAULT


def plan_install(profile: dict[str, Any]) -> dict[str, Any]:
    """Plan dependency installation from a Phase 2 repository profile.

    Returns ``{"commands": [...], "error": None}`` or
    ``{"commands": [], "error": "..."}`` for formats with no honest
    installer mapping. Commands use bare ``pip``/``python`` names; the
    backend rewrites them to the snapshot-local venv executables.
    """
    dep_files = profile.get("dependency_files", [])
    framework = profile.get("test_framework", "none")
    commands: list[list[str]] = []
    for marker in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        if marker in dep_files:
            commands.append(["pip", "install", "-r", marker])
    if "pyproject.toml" in dep_files or "setup.py" in dep_files or "setup.cfg" in dep_files:
        commands.append(["pip", "install", "-e", "."])
    if not commands and "Pipfile" in dep_files:
        return {
            "commands": [],
            "error": "Pipfile-only projects are not supported for isolated "
            "installation; add a requirements file or pyproject section.",
        }
    if framework == "pytest":
        commands.append(["pip", "install", "pytest"])
    if len(commands) > MAX_INSTALL_COMMANDS:
        commands = commands[:MAX_INSTALL_COMMANDS]
    return {"commands": commands, "error": None}


def _extract_traceback_sections(output: str) -> dict[str, str]:
    """Map failing-test headers (``___ name ___``) to short tracebacks."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in output.splitlines():
        header = _TRACEBACK_HEADER_RE.match(line)
        if header:
            if current is not None:
                sections[current] = "\n".join(buffer)[-TRACEBACK_CHARS_PER_TEST:]
            current = header.group(1).strip()
            buffer = []
        elif _SHORT_SUMMARY_BOUNDARY_RE.match(line):
            if current is not None:
                sections[current] = "\n".join(buffer)[-TRACEBACK_CHARS_PER_TEST:]
            current = None
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer)[-TRACEBACK_CHARS_PER_TEST:]
    return sections


def _summary_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines():
        if " in " not in line or "===" not in line and "failed" not in line and "passed" not in line:
            continue
        for match in _SUMMARY_COUNT_RE.finditer(line):
            counts[match.group(2)] = int(match.group(1))
    return counts


def parse_test_output(output: str, framework: str) -> dict[str, Any]:
    """Parse test output into failing tests with messages and tracebacks.

    Never invents results: unparseable output yields an empty failure list
    and the caller falls back to the raw output tail.
    """
    text = output or ""
    sections = _extract_traceback_sections(text)
    failures: list[dict[str, str]] = []
    seen: set[str] = set()
    if framework == "unittest":
        for line in text.splitlines():
            match = _UNITTEST_FAIL_RE.match(line.strip())
            if match and match.group(2) not in seen:
                seen.add(match.group(2))
                failures.append(
                    {
                        "test": match.group(2),
                        "kind": match.group(1).lower(),
                        "message": match.group(3) or "",
                        "traceback": "",
                    }
                )
    else:  # pytest-style (also used for "unknown": FAILED lines are generic)
        for line in text.splitlines():
            stripped = line.strip()
            collect = _COLLECT_ERROR_RE.match(stripped)
            if collect and "collecting" in stripped.split()[:3]:
                if collect.group(1) not in seen:
                    seen.add(collect.group(1))
                    failures.append(
                        {"test": collect.group(1), "kind": "error",
                         "message": "collection error", "traceback": ""}
                    )
                continue
            match = _FAILED_LINE_RE.match(stripped)
            if match and match.group(2) not in seen:
                seen.add(match.group(2))
                test_id = match.group(2)
                short_name = test_id.split("::")[-1]
                failures.append(
                    {
                        "test": test_id,
                        "kind": match.group(1).lower(),
                        "message": (match.group(3) or "").strip(),
                        "traceback": sections.get(short_name, sections.get(test_id, "")),
                    }
                )
                continue
            conftest = _CONFTEST_ERROR_RE.match(stripped)
            if conftest:
                key = f"conftest:{conftest.group(1)}"
                if key not in seen:
                    seen.add(key)
                    failures.append(
                        {"test": conftest.group(1), "kind": "error",
                         "message": "conftest import failed", "traceback": ""}
                    )
    return {
        "failures": failures[:MAX_FAILURES],
        "summary_counts": _summary_counts(text),
    }


def _normalize_traceback_path(raw: str) -> str | None:
    """Normalize a traceback path to repo-relative form, or None to skip."""
    path = raw.replace("\\", "/")
    lowered = path.lower()
    if (
        "site-packages" in lowered
        or "/usr/lib/" in lowered
        or "importlib" in lowered
        or "<frozen" in lowered
        or ".execvenv" in lowered
        or "/venv/" in lowered
        or "unittest/" in lowered
        or "_pytest/" in lowered
    ):
        return None
    candidate = path
    for prefix in ("/repo/", "repo/"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
    # Absolute host paths (e.g. C:/Temp/xyz/repo/pkg/a.py): keep the tail
    # from the first plausible source root so evidence matching works.
    if "/" in candidate and (candidate.startswith("/") or ":" in candidate.split("/")[0]):
        parts = candidate.lstrip("/").split("/")
        for anchor in ("repo", "tests", "src", "pkg"):
            if anchor in parts:
                candidate = "/".join(parts[parts.index(anchor) + (0 if anchor in ("tests", "src", "pkg") else 1):])
                break
        else:
            candidate = parts[-1] if len(parts) > 1 else candidate
    candidate = candidate.lstrip("/")
    if not candidate or ".." in candidate.split("/"):
        return None
    return candidate


def traceback_locations(output: str) -> list[tuple[str, int]]:
    """Extract ``(repo-relative file, line)`` pairs from tracebacks.

    Supports classic ``File "x.py", line N`` frames and pytest's short
    ``x.py:N: in func`` frames. Non-repository paths are excluded.
    """
    locations: list[tuple[str, int]] = []
    for line in (output or "").splitlines():
        raw: str | None = None
        lineno = 0
        classic = _TRACEBACK_FILE_RE.match(line)
        if classic:
            raw, lineno = classic.group(1), int(classic.group(2))
        else:
            short = _SHORT_FRAME_RE.match(line.strip())
            if short:
                raw, lineno = short.group(1), int(short.group(2))
        if raw is None:
            continue
        candidate = _normalize_traceback_path(raw)
        if candidate and (candidate, lineno) not in locations:
            locations.append((candidate, lineno))
    return locations


_HOOK_NAME_RE = re.compile(r"for hook '([^']+)'")
_PYTEST_PIN_RE = re.compile(r"^\s*pytest(\[[^\]]+\])?\s*(==|~=|<=|<|!=)")


def detect_harness_incompatibility(output: str) -> str | None:
    """Classify a pytest/pluggy test-harness incompatibility, if present.

    Returns a human-readable note when the output shows the runner itself
    rejecting the repository's test harness (e.g. a repo-era hook signature
    removed from the installed pytest) — a failure no source patch can fix
    (the repair would require editing tests or changing the runner
    version). Returns None otherwise. Deliberately narrow to avoid
    misclassifying ordinary test failures.
    """
    text = output or ""
    if "PluginValidationError" in text and "can not be found in the hookspec" in text:
        hook = _HOOK_NAME_RE.search(text)
        name = f" {hook.group(1)}" if hook else ""
        return (
            "test-harness incompatibility: pytest/pluggy rejected the"
            f"{name} hook declared by the repository's test harness "
            "(repo-era hook vs installed pytest version). No source patch "
            "can fix this; it would require editing tests or selecting an "
            "older pytest."
        )
    return None


def requirements_pin_pytest(root: Path, dep_files: list[str]) -> bool:
    """Check whether declared requirements pin pytest to a bounded version.

    When a repository declares ``pytest==x`` (or another upper-bounded
    specifier), the install plan must not append a bare ``pip install
    pytest`` that would override the declaration with the latest release.
    """
    for marker in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        if marker not in dep_files:
            continue
        try:
            content = (root / marker).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            stripped = line.split("#", 1)[0].strip()
            if _PYTEST_PIN_RE.match(stripped):
                return True
    return False


def traceback_source_files(output: str) -> list[str]:
    """Extract repo-relative ``.py`` files referenced by tracebacks.

    Site-packages, stdlib, frozen modules, and virtualenv paths are
    excluded so only repository sources are returned.
    """
    files: list[str] = []
    for path, _ in traceback_locations(output):
        if path not in files:
            files.append(path)
    return files


def _tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    return (text or "")[-limit:]


class DockerBackend(ExecutionBackend):
    """Genuine isolation via Docker containers.

    Dependencies install in a networked container; tests run in a second
    container with ``--network none``, CPU/memory/PID limits, dropped
    capabilities, and no host secrets in the environment. The snapshot is
    bind-mounted at ``/repo`` with a venv created inside it so installed
    packages survive across the two runs.
    """

    name = "docker"
    isolation = "docker-container"

    def __init__(self, image: str | None = None):
        self.image = image

    def is_available(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
                check=False,
                env=scrub_env_for_subprocess(),
            )
        except FileNotFoundError:
            return False, "docker is not installed; cannot provide isolated execution."
        except subprocess.TimeoutExpired:
            return False, "docker did not respond; the daemon may be down."
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[-300:]
            return False, f"docker daemon is unavailable ({detail or 'unknown error'})."
        version = (result.stdout or "").strip()
        return True, f"docker daemon reachable (server {version or 'unknown'})."

    @staticmethod
    def _new_container_name() -> str:
        return f"reporescue-{uuid.uuid4().hex[:12]}"

    def _base_args(self, snapshot: Path, name: str) -> list[str]:
        return [
            "docker", "run", "--rm",
            "--name", name,
            "--cpus", DOCKER_CPUS,
            "--memory", DOCKER_MEMORY,
            "--pids-limit", DOCKER_PIDS_LIMIT,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{snapshot}:/repo",
            "-w", "/repo",
        ]

    def _force_remove(self, name: str) -> None:
        """Best-effort ``docker rm --force`` for a timed-out container.

        Killing the ``docker run`` client on timeout does NOT stop the
        container (``--rm`` only applies when the container exits), so the
        container must be removed explicitly or it would keep running
        unbounded. Failures are swallowed: a missing container (e.g. the
        timeout hit during image pull) is a fine outcome here.
        """
        try:
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True, text=True,
                timeout=DOCKER_RM_TIMEOUT_SECONDS, check=False,
                env=scrub_env_for_subprocess(),
            )
        except Exception:
            pass

    def _run_container(
        self, args: list[str], timeout: int, name: str
    ) -> tuple[int | None, str, str]:
        env = scrub_env_for_subprocess()
        env["PIP_NO_INPUT"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
                check=False, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            partial = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
            self._force_remove(name)
            return None, partial, "timeout"
        combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return result.returncode, combined, "ok"

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        import time

        started = time.perf_counter()
        available, reason = self.is_available()
        if not available:
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="error",
                passed=None, exit_code=None, output=reason,
                duration_ms=0, install_ok=None,
            )
        if not request.snapshot.is_dir():
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="error",
                passed=None, exit_code=None,
                output="Execution snapshot directory is missing; refusing to mount it.",
                duration_ms=round((time.perf_counter() - started) * 1000),
                install_ok=None,
            )
        image = self.image or select_python_image(request.python_requires)
        install_log: list[str] = []
        if request.install_commands:
            venv_name = self._new_container_name()
            venv_args = self._base_args(request.snapshot, venv_name) + [
                image, "python", "-m", "venv", "/repo/.execvenv",
            ]
            code, out, state = self._run_container(venv_args, request.install_timeout, venv_name)
            install_log.append(f"$ python -m venv /repo/.execvenv\n{out}")
            if state == "timeout" or code != 0:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="timeout" if state == "timeout" else "install_error",
                    passed=None, exit_code=code,
                    output=_tail("\n".join(install_log)),
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    install_ok=False, install_log=_tail("\n".join(install_log)),
                )
            for command in request.install_commands:
                rewritten = self._rewrite_venv(command)
                step_name = self._new_container_name()
                args = self._base_args(request.snapshot, step_name) + [image, *rewritten]
                code, out, state = self._run_container(args, request.install_timeout, step_name)
                install_log.append(f"$ {' '.join(rewritten)}\n{out}")
                if state == "timeout" or code != 0:
                    return ExecutionResult(
                        backend=self.name, isolation=self.isolation,
                        status="install_error" if state != "timeout" else "timeout",
                        passed=None, exit_code=code,
                        output=_tail("\n".join(install_log)),
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        install_ok=False, install_log=_tail("\n".join(install_log)),
                    )
        test_command = list(request.test_command)
        has_venv = bool(request.install_commands) or (request.snapshot / ".execvenv").is_dir()
        if has_venv and test_command[:1] == ["python"]:
            test_command[0] = "/repo/.execvenv/bin/python"
        test_name = self._new_container_name()
        args = (
            self._base_args(request.snapshot, test_name)
            + ["--network", "none", image, *test_command]
        )
        code, out, state = self._run_container(args, request.test_timeout, test_name)
        elapsed = round((time.perf_counter() - started) * 1000)
        if state == "timeout":
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="timeout",
                passed=False, exit_code=None, output=_tail(f"Test run timed out.\n{out}"),
                duration_ms=elapsed, install_ok=True if request.install_commands else None,
                install_log=_tail("\n".join(install_log)),
            )
        parsed = parse_test_output(out, request.framework)
        passed = code == 0
        return ExecutionResult(
            backend=self.name, isolation=self.isolation,
            status="ok" if passed else "test_failure",
            passed=passed, exit_code=code, output=_tail(out),
            duration_ms=elapsed, failures=parsed["failures"],
            summary_counts=parsed["summary_counts"],
            install_ok=True if request.install_commands else None,
            install_log=_tail("\n".join(install_log)),
        )

    @staticmethod
    def _rewrite_venv(command: list[str]) -> list[str]:
        if command[:1] == ["pip"]:
            return ["/repo/.execvenv/bin/python", "-m", "pip", *command[1:]]
        if command[:1] == ["python"]:
            return ["/repo/.execvenv/bin/python", *command[1:]]
        return command


class UnsafeLocalBackend(ExecutionBackend):
    """Development-only host execution. NOT a sandbox.

    Runs only when ``REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION=1`` is set
    explicitly. Dependency installs go into a venv inside the disposable
    snapshot (never the project or system interpreter); without install
    commands the backend Python runs pytest with a scrubbed environment.
    Every result carries ``isolation="none-unsafe-local-dev-only"``.
    """

    name = "unsafe-local"
    isolation = "none-unsafe-local-dev-only"

    def is_available(self) -> tuple[bool, str]:
        if os.environ.get(UNSAFE_LOCAL_FLAG) == "1":
            return True, (
                "unsafe local execution explicitly enabled; repository code "
                "will run on the host without isolation."
            )
        return False, (
            f"unsafe local execution is disabled by default; set "
            f"{UNSAFE_LOCAL_FLAG}=1 to acknowledge host execution without "
            "isolation (development only)."
        )

    def needs_pytest_install(self) -> bool:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--version"],
                capture_output=True, text=True, timeout=30, check=False,
                env=scrub_env_for_subprocess(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        return result.returncode != 0

    def _venv_python(self, snapshot: Path) -> Path:
        venv = snapshot / ".execvenv"
        scripts = venv / "Scripts" / "python.exe"
        if scripts.exists():
            return scripts
        return venv / "bin" / "python"

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        import time

        started = time.perf_counter()
        available, reason = self.is_available()
        if not available:
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="error",
                passed=None, exit_code=None, output=reason,
                duration_ms=0, install_ok=None,
            )
        if not request.snapshot.is_dir():
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="error",
                passed=None, exit_code=None,
                output="Execution snapshot directory is missing.",
                duration_ms=0, install_ok=None,
            )
        install_log: list[str] = []
        python_exe = Path(sys.executable)
        existing_venv = self._venv_python(request.snapshot)
        if not request.install_commands and existing_venv.exists():
            python_exe = existing_venv
        if request.install_commands:
            venv_result = subprocess.run(
                [sys.executable, "-m", "venv", str(request.snapshot / ".execvenv")],
                capture_output=True, text=True, timeout=request.install_timeout,
                check=False, env=scrub_env_for_subprocess(), cwd=request.snapshot,
            )
            if venv_result.returncode != 0:
                detail = ((venv_result.stdout or "") + "\n" + (venv_result.stderr or "")).strip()
                install_log.append(f"$ python -m venv .execvenv\n{detail}")
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="install_error", passed=None,
                    exit_code=venv_result.returncode,
                    output=_tail("\n".join(install_log)),
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    install_ok=False, install_log=_tail("\n".join(install_log)),
                )
            python_exe = self._venv_python(request.snapshot)
            for command in request.install_commands:
                rewritten = self._rewrite(command, python_exe)
                try:
                    result = subprocess.run(
                        [str(part) for part in rewritten],
                        capture_output=True, text=True,
                        timeout=request.install_timeout, check=False,
                        env=_pip_env(), cwd=request.snapshot,
                    )
                except subprocess.TimeoutExpired as exc:
                    partial = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
                    install_log.append(f"$ {' '.join(command)}\n{partial}")
                    return ExecutionResult(
                        backend=self.name, isolation=self.isolation,
                        status="timeout", passed=None, exit_code=None,
                        output=_tail("\n".join(install_log)),
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        install_ok=False, install_log=_tail("\n".join(install_log)),
                    )
                combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
                install_log.append(f"$ {' '.join(command)}\n{combined}")
                if result.returncode != 0:
                    return ExecutionResult(
                        backend=self.name, isolation=self.isolation,
                        status="install_error", passed=None,
                        exit_code=result.returncode,
                        output=_tail("\n".join(install_log)),
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        install_ok=False, install_log=_tail("\n".join(install_log)),
                    )
        test_command = self._rewrite(list(request.test_command), python_exe)
        try:
            result = subprocess.run(
                [str(part) for part in test_command],
                capture_output=True, text=True, timeout=request.test_timeout,
                check=False, env=scrub_env_for_subprocess(), cwd=request.snapshot,
            )
        except subprocess.TimeoutExpired as exc:
            partial = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="timeout",
                passed=False, exit_code=None,
                output=_tail(f"Test run timed out.\n{partial}"),
                duration_ms=round((time.perf_counter() - started) * 1000),
                install_ok=True if request.install_commands else None,
                install_log=_tail("\n".join(install_log)),
            )
        elapsed = round((time.perf_counter() - started) * 1000)
        combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        parsed = parse_test_output(combined, request.framework)
        passed = result.returncode == 0
        return ExecutionResult(
            backend=self.name, isolation=self.isolation,
            status="ok" if passed else "test_failure",
            passed=passed, exit_code=result.returncode, output=_tail(combined),
            duration_ms=elapsed, failures=parsed["failures"],
            summary_counts=parsed["summary_counts"],
            install_ok=True if request.install_commands else None,
            install_log=_tail("\n".join(install_log)),
        )

    @staticmethod
    def _rewrite(command: list[str], python_exe: Path) -> list[str]:
        if command[:1] == ["pip"]:
            return [str(python_exe), "-m", "pip", *command[1:]]
        if command[:1] == ["python"]:
            return [str(python_exe), *command[1:]]
        return command


def _pip_env() -> dict[str, str]:
    env = scrub_env_for_subprocess()
    env["PIP_NO_INPUT"] = "1"
    return env


def select_backend() -> tuple[ExecutionBackend | None, dict[str, Any] | None]:
    """Select the execution backend. Never falls back silently.

    Returns (backend, None) on success or (None, error_dict) when no
    backend is available, distinguishing a missing Docker runtime from a
    disabled dev fallback.
    """
    docker = DockerBackend()
    available, _ = docker.is_available()
    if available:
        return docker, None
    local = UnsafeLocalBackend()
    local_available, local_reason = local.is_available()
    if local_available:
        return local, None
    return None, {
        "status": "error",
        "reason": "runtime_unavailable",
        "message": (
            "No execution runtime is available: docker is not installed or "
            "its daemon is unreachable. Install Docker to run real "
            "investigations with isolation, or set "
            f"{UNSAFE_LOCAL_FLAG}=1 to acknowledge unisolated host "
            "execution (development only, never for untrusted code)."
        ),
    }
