"""Modal cloud execution backend (Phase 5B).

:class:`ModalBackend` implements the existing :class:`ExecutionBackend`
contract (``run(request) -> ExecutionResult``) on Modal cloud sandboxes.
Modal is only the execution provider here: scheduling, persistence,
ingestion, AI diagnosis, Patch Guard, and reporting stay in RepoRescue.

How one run works
-----------------
1. The repository snapshot is staged locally by :mod:`app.snapshot`
   (bounded, secret-free, symlink-free) with a commit-identity file.
2. Dependencies are installed by baking them into a Modal Image (the
   explicitly controlled installation stage). Nothing from the
   repository is executed on the trusted worker.
3. A fresh sandbox is created from that image with CPU/memory limits
   and ``block_network=True``: tests run with no network access.
4. The snapshot archive uploads over the SDK file API, extracts into
   ``/repo``, and the commit identity round-trips before tests run.
5. The test command executes with a timeout while a capped collector
   streams output; cancellation terminates the sandbox promptly.
6. The sandbox is terminated and the staging tree removed in a
   ``finally`` block: cleanup never depends on reporting.

Authentication comes from ``MODAL_TOKEN_ID``/``MODAL_TOKEN_SECRET`` in
the trusted worker environment only. Token *values* are never logged,
never baked into images, and never passed to repository code.

Tested against ``modal==1.6.3``; only SDK APIs verified in that
release are used (``App.lookup``, ``Image.debian_slim`` /
``Image.from_registry`` / ``pip_install`` /
``pip_install_from_requirements`` / ``add_local_file`` /
``add_local_dir`` / ``run_commands``, ``Sandbox.create`` / ``exec`` /
``terminate``, ``ContainerProcess.wait`` and stream iteration).
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import time
import tomllib
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    import modal  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised via monkeypatched None
    modal = None  # type: ignore[assignment]

from . import config as backend_config
from . import snapshot as snapshot_module
from .execution import (
    MAX_OUTPUT_CHARS,
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    cancel_requested,
    parse_test_output,
    redact_credentials,
)
from .js_execution import (
    js_test_env,
    parse_js_output,
    select_node_image,
    validate_js_argv,
)

MODAL_TOKEN_ID_ENV_VAR = "MODAL_TOKEN_ID"
MODAL_TOKEN_SECRET_ENV_VAR = "MODAL_TOKEN_SECRET"

MODAL_SDK_CONFIG_FILENAME = ".modal.toml"

PYTHON_TEST_LAUNCHERS = frozenset({"python", "python3", "pytest"})
PYTHON_VERSION_DEFAULT = "3.12"
PYTHON_VERSION_MIN_MINOR = 9
PYTHON_VERSION_MAX_MINOR = 13

SANDBOX_WORKDIR = "/repo"
SANDBOX_ARCHIVE_PATH = "/tmp/snapshot.tar.gz"
SANDBOX_ENTRYPOINT = ("sleep", "infinity")
SETUP_COMMAND_TIMEOUT = 120
SANDBOX_OVERHEAD_SECONDS = 300
SANDBOX_MIN_LIFETIME_SECONDS = 600
CANCEL_POLL_SECONDS = 0.2
CANCEL_JOIN_SECONDS = 120

_REQUIRES_MINOR_RE = re.compile(r"3\.(\d+)")


def select_modal_python_version(python_requires: str | None) -> str:
    """Map a requires-python constraint to a Modal base Python version.

    Only the lower bound's minor version is honored; anything unknown
    falls back to the default version rather than failing the run.
    Mirrors :func:`app.execution.select_python_image`.
    """
    match = _REQUIRES_MINOR_RE.search(python_requires or "")
    if match:
        minor = int(match.group(1))
        if PYTHON_VERSION_MIN_MINOR <= minor <= PYTHON_VERSION_MAX_MINOR:
            return f"3.{minor}"
    return PYTHON_VERSION_DEFAULT


def _launcher(argv: list[str]) -> str:
    first = (argv[0] if argv else "").replace("\\", "/").rsplit("/", 1)[-1]
    return first.lower()


def infer_language(request: ExecutionRequest) -> str:
    """Infer the execution language for a request.

    An explicit ``request.language`` wins; otherwise the test command
    (then the install commands) decide. Returns ``"python"``,
    ``"javascript"``, or ``"unknown"``.
    """
    declared = (request.language or "").strip().lower()
    if declared in ("python", "javascript"):
        return declared
    test = _launcher(list(request.test_command or []))
    if test in PYTHON_TEST_LAUNCHERS or test == "pip":
        return "python"
    if test in ("npm", "npx", "node"):
        return "javascript"
    for command in request.install_commands or []:
        head = _launcher(list(command or []))
        if head == "pip":
            return "python"
        if head == "npm":
            return "javascript"
    return "unknown"


def _sdk_config_tokens_present() -> bool:
    """Whether the SDK config file holds a complete token pair.

    Schema-agnostic: any TOML table containing both a non-empty
    ``token_id`` and ``token_secret`` counts. Values are never logged
    or returned, only their presence. Used only when the explicit
    opt-in flag allows the SDK credential chain.
    """
    path = Path.home() / MODAL_SDK_CONFIG_FILENAME
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8", "replace"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return False

    def _scan(node: Any) -> bool:
        if isinstance(node, dict):
            token_id = node.get("token_id")
            token_secret = node.get("token_secret")
            if (
                isinstance(token_id, str)
                and token_id.strip()
                and isinstance(token_secret, str)
                and token_secret.strip()
            ):
                return True
            return any(_scan(value) for value in node.values())
        if isinstance(node, list):
            return any(_scan(value) for value in node)
        return False

    try:
        return _scan(data)
    except Exception:
        return False


def modal_credentials_present() -> tuple[bool, str]:
    """Check Modal credential presence without exposing values.

    Returns (present, source) where source is ``"environment"``,
    ``"sdk-config"``, or ``"none"``.
    """
    token_id = os.environ.get(MODAL_TOKEN_ID_ENV_VAR, "").strip()
    token_secret = os.environ.get(MODAL_TOKEN_SECRET_ENV_VAR, "").strip()
    if token_id and token_secret:
        return True, "environment"
    if backend_config.modal_sdk_config_allowed() and _sdk_config_tokens_present():
        return True, "sdk-config"
    return False, "none"


# Modal SDK exception names (verified in modal 1.6.3) mapped to
# (status, reason). Classification walks the MRO so subclasses and
# test doubles with the same names resolve identically.
_MODAL_ERROR_MAP = {
    "AuthError": ("error", "auth_error"),
    "ConnectionError": ("error", "connection_error"),
    "ResourceExhaustedError": ("error", "quota_exhausted"),
    "ExecTimeoutError": ("timeout", "exec_timeout"),
    "SandboxTimeoutError": ("timeout", "sandbox_timeout"),
    "SandboxTerminatedError": ("error", "sandbox_terminated"),
    "InvalidError": ("error", "modal_invalid"),
    "VersionError": ("error", "modal_version"),
    "NotFoundError": ("error", "modal_not_found"),
    "OutputExpiredError": ("error", "output_expired"),
    "TimeoutError": ("timeout", "modal_timeout"),
    "Error": ("error", "modal_error"),
}


def classify_modal_error(exc: BaseException) -> tuple[str, str]:
    """Map anstatt exception to an (ExecutionResult status, reason) pair.

    Modal SDK failures resolve to distinct infrastructure reasons; any
    other unexpected failure is an internal error, never a test result.
    """
    for klass in type(exc).__mro__:
        hit = _MODAL_ERROR_MAP.get(klass.__name__)
        if hit is not None:
            return hit
    module = type(exc).__module__ or ""
    if module.split(".")[0] == "modal":
        return "error", "modal_error"
    return "error", "internal_error"


class _CappedCollector:
    """Accumulate streamed text keeping only the tail.

    Memory stays bounded by ``limit`` plus one chunk: old chunks drop
    from the left as new text arrives. :meth:`result` returns
    ``(tail_text, truncated)``.
    """

    def __init__(self, limit: int = MAX_OUTPUT_CHARS):
        self._limit = max(1, limit)
        self._chunks: deque[str] = deque()
        self._length = 0
        self.total = 0

    def feed(self, text: str) -> None:
        if not text:
            return
        if not isinstance(text, str):
            text = str(text)
        self.total += len(text)
        self._chunks.append(text)
        self._length += len(text)
        while self._length > self._limit and len(self._chunks) > 1:
            dropped = self._chunks.popleft()
            self._length -= len(dropped)

    def result(self) -> tuple[str, bool]:
        tail = "".join(self._chunks)[-self._limit :]
        return tail, self.total > self._limit


def cap_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """Cap ``text`` to its tail, returning (capped, truncated)."""
    if len(text or "") <= limit:
        return text or "", False
    return (text or "")[-limit:], True


def _truncate_note(total: int, limit: int) -> str:
    return f"[output truncated: showing last {limit} of {total} chars]\n"


class ModalBackend(ExecutionBackend):
    """Isolated cloud execution via Modal sandboxes.

    Dependencies bake into a content-cached image (the controlled
    installation stage); tests run in a fresh sandbox from that image
    with network blocked, CPU/memory limits, and an overall lifetime
    bound. Every run terminates its sandbox and removes its staging
    tree, on success, failure, timeout, and cancellation alike.
    """

    name = "modal"
    isolation = "modal-sandbox"
    requires_fresh_install = True

    def is_available(self) -> tuple[bool, str]:
        if modal is None:
            return False, (
                "modal SDK is not installed; cannot provide cloud execution."
            )
        present, source = modal_credentials_present()
        if not present:
            return False, (
                "Modal credentials are not configured; set "
                f"{MODAL_TOKEN_ID_ENV_VAR} and {MODAL_TOKEN_SECRET_ENV_VAR} "
                "in the trusted worker environment."
            )
        if source == "sdk-config":
            return True, (
                "Modal credentials found in the SDK config file; explicit "
                "environment credentials are recommended for workers."
            )
        return True, "Modal credentials configured via environment."

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.perf_counter()

        def _elapsed() -> int:
            return round((time.perf_counter() - started) * 1000)

        available, reason = self.is_available()
        if not available:
            mapped = "sdk_missing" if modal is None else "credentials_missing"
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="error",
                passed=None, exit_code=None, output=reason,
                duration_ms=_elapsed(), install_ok=None, reason=mapped,
            )
        snapshot = Path(request.snapshot)
        if not snapshot.is_dir() or snapshot.is_symlink():
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="error",
                passed=None, exit_code=None,
                output="Execution snapshot directory is missing; refusing a cloud run.",
                duration_ms=_elapsed(), install_ok=None,
                reason="snapshot_missing",
            )
        language = infer_language(request)
        if language not in ("python", "javascript"):
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="unsupported",
                passed=None, exit_code=None,
                output=(
                    "Modal execution supports Python and JavaScript "
                    "commands only; refusing to run an unrecognized "
                    "test launcher."
                ),
                duration_ms=_elapsed(), install_ok=None,
                reason="unsupported_language",
            )
        if language == "python":
            launcher = _launcher(list(request.test_command or []))
            if launcher not in PYTHON_TEST_LAUNCHERS:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="unsupported", passed=None, exit_code=None,
                    output=(
                        f"Unsupported Python test launcher {launcher!r}; "
                        "expected python, python3, or pytest."
                    ),
                    duration_ms=_elapsed(), install_ok=None,
                    reason="unsupported_command",
                )
        else:
            problem = validate_js_argv(
                list(request.test_command or []), kind="test"
            )
            if problem is not None:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="unsupported", passed=None, exit_code=None,
                    output=problem, duration_ms=_elapsed(), install_ok=None,
                    reason="unsupported_command",
                )
        if cancel_requested(request.cancel):
            return ExecutionResult(
                backend=self.name, isolation=self.isolation, status="cancelled",
                passed=None, exit_code=None,
                output="Execution cancelled before the cloud run started.",
                duration_ms=_elapsed(), install_ok=None, reason="cancelled",
            )

        staging_parent: Path | None = None
        sandbox: Any = None
        install_log: list[str] = []
        setup_ms = 0
        try:
            try:
                max_total = backend_config.get_snapshot_max_total_bytes()
                max_file = backend_config.get_snapshot_max_file_bytes()
                max_files = backend_config.get_snapshot_max_files()
            except backend_config.ConfigError as exc:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status="error",
                    passed=None, exit_code=None, output=str(exc),
                    duration_ms=_elapsed(), install_ok=None,
                    reason="config_error",
                )
            staging_parent = snapshot_module.create_staging_parent()
            staging = staging_parent / "tree"
            try:
                manifest = snapshot_module.build_snapshot(
                    snapshot, staging,
                    commit_sha=request.commit_sha,
                    max_total_bytes=max_total,
                    max_file_bytes=max_file,
                    max_files=max_files,
                )
            except snapshot_module.SnapshotError as exc:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status="error",
                    passed=None, exit_code=None, output=str(exc),
                    duration_ms=_elapsed(), install_ok=None,
                    reason=exc.reason,
                )
            try:
                archive = snapshot_module.build_archive(
                    staging, staging_parent / "snapshot.tar.gz"
                )
            except snapshot_module.SnapshotError as exc:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status="error",
                    passed=None, exit_code=None, output=str(exc),
                    duration_ms=_elapsed(), install_ok=None,
                    reason=exc.reason,
                )
            if cancel_requested(request.cancel):
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="cancelled", passed=None, exit_code=None,
                    output="Execution cancelled before the cloud run started.",
                    duration_ms=_elapsed(), install_ok=None, reason="cancelled",
                )

            image_start = time.perf_counter()
            try:
                image, bake_notes = self._build_image(
                    language, request, staging
                )
            except _InstallPlanError as exc:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="install_error", passed=None, exit_code=None,
                    output=str(exc), duration_ms=_elapsed(),
                    install_ok=False, install_log=str(exc),
                    setup_ms=round((time.perf_counter() - image_start) * 1000),
                    setup_runs=0, reason="install_plan_error",
                )
            install_log.extend(bake_notes)
            try:
                cpu = backend_config.get_modal_cpu()
                memory_mb = backend_config.get_modal_memory_mb()
                app_name = backend_config.get_modal_app_name()
            except backend_config.ConfigError as exc:
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status="error",
                    passed=None, exit_code=None, output=str(exc),
                    duration_ms=_elapsed(), install_ok=None,
                    reason="config_error",
                )
            lifetime = max(
                SANDBOX_MIN_LIFETIME_SECONDS,
                request.test_timeout + SANDBOX_OVERHEAD_SECONDS,
            )
            sandbox_name = f"reporescue-{uuid.uuid4().hex[:12]}"
            try:
                app = modal.App.lookup(app_name, create_if_missing=True)
                # Sandbox creation builds the image on first use, so this
                # timed block IS the controlled installation stage.
                sandbox = modal.Sandbox.create(
                    *SANDBOX_ENTRYPOINT,
                    app=app,
                    name=sandbox_name,
                    image=image,
                    workdir=SANDBOX_WORKDIR,
                    timeout=lifetime,
                    cpu=cpu if cpu > 0 else None,
                    memory=memory_mb if memory_mb > 0 else None,
                    block_network=True,
                )
            except Exception as exc:
                setup_ms = round((time.perf_counter() - image_start) * 1000)
                status, mapped = classify_modal_error(exc)
                detail = f"{str(exc)[:2000]}"
                install_log.append(f"sandbox creation failed: {detail}")
                if mapped in ("auth_error", "connection_error", "quota_exhausted"):
                    return ExecutionResult(
                        backend=self.name, isolation=self.isolation,
                        status="error", passed=None, exit_code=None,
                        output=redact_credentials(
                            f"Modal {mapped}: {detail}"
                        ),
                        duration_ms=_elapsed(), install_ok=None,
                        install_log=redact_credentials(
                            "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                        ),
                        setup_ms=setup_ms, setup_runs=0, reason=mapped,
                    )
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="install_error", passed=None, exit_code=None,
                    output=redact_credentials(
                        f"Modal image build failed: {detail}"
                    ),
                    duration_ms=_elapsed(), install_ok=False,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, reason="image_build_failed",
                )
            setup_ms = round((time.perf_counter() - image_start) * 1000)
            install_log.append(
                f"image build: ok in {setup_ms}ms "
                f"({manifest.file_count} snapshot files, "
                f"{archive['size_bytes']} byte archive)"
            )

            transfer_error = self._transfer_snapshot(sandbox, archive["path"])
            if transfer_error is not None:
                status, mapped, detail = transfer_error
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status=status,
                    passed=None, exit_code=None, output=detail,
                    duration_ms=_elapsed(), install_ok=True,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, reason=mapped,
                )
            identity_error = self._verify_snapshot_identity(
                sandbox, request.commit_sha
            )
            if identity_error is not None:
                status, mapped, detail = identity_error
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status=status,
                    passed=None, exit_code=None, output=detail,
                    duration_ms=_elapsed(), install_ok=True,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, reason=mapped,
                )
            if cancel_requested(request.cancel):
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="cancelled", passed=None, exit_code=None,
                    output="Execution cancelled before the test run started.",
                    duration_ms=_elapsed(), install_ok=True,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, reason="cancelled",
                )

            test_start = time.perf_counter()
            outcome = self._run_test_command(sandbox, request, language)
            test_ms = round((time.perf_counter() - test_start) * 1000)
            elapsed = _elapsed()
            kind = outcome["kind"]
            if kind == "cancelled":
                output = "Execution cancelled by request."
                if outcome.get("partial"):
                    capped, _ = cap_output(str(outcome["partial"]))
                    output += f"\nPartial output:\n{capped}"
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation,
                    status="cancelled", passed=None, exit_code=None,
                    output=redact_credentials(output), duration_ms=elapsed,
                    install_ok=True,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, test_ms=test_ms,
                    reason="cancelled",
                )
            if kind == "timeout":
                capped, was_truncated = cap_output(str(outcome.get("output", "")))
                output = f"Test run timed out.\n{capped}"
                if was_truncated:
                    output = _truncate_note(
                        len(str(outcome.get("output", ""))), MAX_OUTPUT_CHARS
                    ) + output
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status="timeout",
                    passed=False, exit_code=None,
                    output=redact_credentials(output), duration_ms=elapsed,
                    install_ok=True,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, test_ms=test_ms,
                    reason=str(outcome.get("reason", "exec_timeout")),
                    truncated=was_truncated,
                )
            if kind == "error":
                status, mapped = classify_modal_error(outcome["error"])
                detail = f"{str(outcome['error'])[:2000]}"
                if status == "timeout":
                    return ExecutionResult(
                        backend=self.name, isolation=self.isolation,
                        status="timeout", passed=False, exit_code=None,
                        output=redact_credentials(
                            f"Test run timed out ({mapped}).\n{detail}"
                        ),
                        duration_ms=elapsed, install_ok=True,
                        install_log=redact_credentials(
                            "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                        ),
                        setup_ms=setup_ms, setup_runs=1, test_ms=test_ms,
                        reason=mapped,
                    )
                return ExecutionResult(
                    backend=self.name, isolation=self.isolation, status="error",
                    passed=None, exit_code=None,
                    output=redact_credentials(f"Modal {mapped}: {detail}"),
                    duration_ms=elapsed, install_ok=True,
                    install_log=redact_credentials(
                        "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                    ),
                    setup_ms=setup_ms, setup_runs=1, test_ms=test_ms,
                    reason=mapped,
                )
            combined = str(outcome.get("output", ""))
            capped, cap_truncated = cap_output(combined)
            was_truncated = cap_truncated or bool(outcome.get("truncated"))
            output = capped
            if was_truncated:
                total = max(
                    len(combined), int(outcome.get("total_chars", 0) or 0)
                )
                output = _truncate_note(total, MAX_OUTPUT_CHARS) + output
            if language == "javascript":
                parsed = parse_js_output(combined)
            else:
                parsed = parse_test_output(combined, request.framework)
            code = outcome.get("exit_code")
            passed = code == 0
            return ExecutionResult(
                backend=self.name, isolation=self.isolation,
                status="ok" if passed else "test_failure",
                passed=passed, exit_code=code,
                output=redact_credentials(output), duration_ms=elapsed,
                failures=parsed["failures"],
                summary_counts=parsed["summary_counts"],
                install_ok=True,
                install_log=redact_credentials(
                    "\n".join(install_log)[-MAX_OUTPUT_CHARS:]
                ),
                setup_ms=setup_ms, setup_runs=1, test_ms=test_ms,
                reason=None, truncated=was_truncated,
            )
        finally:
            if sandbox is not None:
                _terminate_quietly(sandbox)
            if staging_parent is not None:
                snapshot_module.remove_staging(staging_parent)

    # -- image construction (controlled installation stage) --

    def _build_image(
        self, language: str, request: ExecutionRequest, staging: Path
    ) -> tuple[Any, list[str]]:
        """Define the baked dependency image; returns (image, log lines)."""
        notes: list[str] = []
        if language == "javascript":
            image, base = self._js_base_image(staging)
            notes.append(f"base image: {base}")
            for command in request.install_commands or []:
                image, note = self._bake_js_install(image, staging, list(command))
                notes.append(note)
            return image, notes
        version = select_modal_python_version(request.python_requires)
        image = modal.Image.debian_slim(python_version=version)
        notes.append(f"base image: debian-slim python {version}")
        for command in request.install_commands or []:
            image, note = self._bake_python_install(image, staging, list(command))
            notes.append(note)
        return image, notes

    def _bake_python_install(
        self, image: Any, staging: Path, command: list[str]
    ) -> tuple[Any, str]:
        if len(command) < 2 or command[0] != "pip" or command[1] != "install":
            raise _InstallPlanError(
                f"Unsupported Python install step {command!r}; refusing to guess."
            )
        args = command[2:]
        if args == ["-e", "."]:
            image = image.add_local_dir(str(staging), SANDBOX_WORKDIR, copy=True)
            image = image.run_commands(f"pip install -e {SANDBOX_WORKDIR}")
            return image, "$ pip install -e . (baked into image)"
        specs: list[str] = []
        requirement_files: list[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            if token in ("-r", "--requirement") and index + 1 < len(args):
                requirement_files.append(args[index + 1])
                index += 2
            elif token.startswith("-"):
                raise _InstallPlanError(
                    "Unsupported pip install option in install plan; refusing to guess."
                )
            else:
                specs.append(token)
                index += 1
        if not specs and not requirement_files:
            raise _InstallPlanError(
                "Empty pip install step in install plan; refusing to guess."
            )
        for filename in requirement_files:
            target = staging / filename
            if not target.is_file() or target.is_symlink():
                raise _InstallPlanError(
                    f"Install plan references missing file {filename!r}; refusing to guess."
                )
            image = image.pip_install_from_requirements(str(target))
        if specs:
            image = image.pip_install(*specs)
        display = " ".join(["pip", "install", *args])
        return image, f"$ {display} (baked into image)"

    def _js_base_image(self, staging: Path) -> tuple[Any, str]:
        override = os.environ.get(
            backend_config.MODAL_NODE_IMAGE_ENV_VAR, ""
        ).strip()
        if override:
            tag = override
        else:
            package_json = staging / "package.json"
            tag = select_node_image(
                package_json if package_json.is_file() else None,
                default=backend_config.get_modal_node_image(),
            )
        return modal.Image.from_registry(tag), tag

    def _bake_js_install(
        self, image: Any, staging: Path, command: list[str]
    ) -> tuple[Any, str]:
        problem = validate_js_argv(command, kind="install")
        if problem is not None:
            raise _InstallPlanError(problem)
        if command[1:] not in (["ci"], ["install"]):
            raise _InstallPlanError(
                f"Unsupported npm install step {command!r}; refusing to guess."
            )
        manifest = staging / "package.json"
        if not manifest.is_file() or manifest.is_symlink():
            raise _InstallPlanError(
                "npm install planned but package.json is missing from the snapshot."
            )
        image = image.add_local_file(
            str(manifest), f"{SANDBOX_WORKDIR}/package.json", copy=True
        )
        if command[1] == "ci":
            lockfile = staging / "package-lock.json"
            if not lockfile.is_file() or lockfile.is_symlink():
                raise _InstallPlanError(
                    "npm ci planned but package-lock.json is missing from the snapshot."
                )
            image = image.add_local_file(
                str(lockfile), f"{SANDBOX_WORKDIR}/package-lock.json", copy=True
            )
            image = image.run_commands(f"cd {SANDBOX_WORKDIR} && npm ci")
            return image, "$ npm ci (baked into image)"
        image = image.run_commands(f"cd {SANDBOX_WORKDIR} && npm install")
        return image, "$ npm install (baked into image)"

    # -- snapshot transfer and identity --

    def _transfer_snapshot(
        self, sandbox: Any, archive_path: str
    ) -> tuple[str, str, str] | None:
        """Upload and extract the snapshot; None on success."""
        try:
            sandbox.filesystem.copy_from_local(archive_path, SANDBOX_ARCHIVE_PATH)
            mkdir = sandbox.exec(
                "mkdir", "-p", SANDBOX_WORKDIR,
                timeout=SETUP_COMMAND_TIMEOUT,
            )
            if mkdir.wait() != 0:
                raise _SetupCommandError("Could not create /repo in the sandbox.")
            extract = sandbox.exec(
                "tar", "-xzf", SANDBOX_ARCHIVE_PATH, "-C", SANDBOX_WORKDIR,
                timeout=SETUP_COMMAND_TIMEOUT,
            )
            if extract.wait() != 0:
                detail = _read_process_text(extract)[0][:1000]
                raise _SetupCommandError(
                    f"Snapshot extraction failed in the sandbox: {detail}"
                )
            return None
        except Exception as exc:
            if isinstance(exc, _SetupCommandError):
                return "error", "snapshot_transfer_failed", redact_credentials(str(exc))
            status, mapped = classify_modal_error(exc)
            return status, mapped, redact_credentials(f"Modal {mapped}: {exc}"[:2000])

    def _verify_snapshot_identity(
        self, sandbox: Any, expected_sha: str | None
    ) -> tuple[str, str, str] | None:
        """Round-trip the commit-identity file; None when it matches."""
        expected = (expected_sha or "unknown").strip() or "unknown"
        try:
            proc = sandbox.exec(
                "cat", f"{SANDBOX_WORKDIR}/{snapshot_module.COMMIT_FILENAME}",
                timeout=SETUP_COMMAND_TIMEOUT,
            )
            text, _ = _read_process_text(proc)
            code = proc.wait()
        except Exception as exc:
            status, mapped = classify_modal_error(exc)
            return status, mapped, redact_credentials(f"Modal {mapped}: {exc}"[:2000])
        if code != 0:
            return (
                "error", "snapshot_mismatch",
                "Snapshot identity file is unreadable in the sandbox; refusing to execute.",
            )
        actual = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        if actual != expected:
            return (
                "error", "snapshot_mismatch",
                "Snapshot identity mismatch in the sandbox: refusing to "
                "execute content that does not match the expected commit.",
            )
        return None

    # -- test execution with cancellation --

    def _run_test_command(
        self, sandbox: Any, request: ExecutionRequest, language: str
    ) -> dict[str, Any]:
        """Run the test command; returns an outcome dict.

        Kinds: ``ok`` (with output/exit_code), ``timeout`` (with
        output/reason), ``cancelled`` (with partial), ``error`` (with
        error). Cancellation terminates the sandbox; it is never
        reported as a normal failure or success.
        """
        env: dict[str, str] = {
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if language == "javascript":
            env.update(js_test_env())
        try:
            proc = sandbox.exec(
                *request.test_command,
                workdir=SANDBOX_WORKDIR,
                env=env,
                timeout=request.test_timeout,
            )
        except Exception as exc:
            status, _ = classify_modal_error(exc)
            if status == "timeout":
                return {"kind": "timeout", "output": "", "reason": "exec_timeout"}
            return {"kind": "error", "error": exc}
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="modal-exec"
        ) as pool:
            future = pool.submit(
                _collect_process_output, proc, timeout=request.test_timeout
            )
            while True:
                try:
                    return future.result(timeout=CANCEL_POLL_SECONDS)
                except concurrent.futures.TimeoutError:
                    if cancel_requested(request.cancel):
                        _terminate_quietly(sandbox)
                        try:
                            outcome = future.result(timeout=CANCEL_JOIN_SECONDS)
                        except Exception:
                            outcome = {"kind": "cancelled", "partial": ""}
                        if outcome.get("kind") != "ok":
                            return {"kind": "cancelled", "partial": outcome.get("partial", "")}
                        partial = str(outcome.get("output", ""))
                        return {"kind": "cancelled", "partial": partial}


def _read_process_text(proc: Any) -> tuple[str, str]:
    """Read a setup command's stdout/stderr fully (bounded, short-lived)."""
    try:
        stdout = proc.stdout.read() if hasattr(proc.stdout, "read") else ""
    except Exception:
        stdout = ""
    try:
        stderr = proc.stderr.read() if hasattr(proc.stderr, "read") else ""
    except Exception:
        stderr = ""
    return str(stdout or ""), str(stderr or "")


# Exit codes proving a process was killed rather than failing on its own:
# 128+SIGKILL, 128+SIGTERM, their negative (signal-number) spellings,
# and -1, which the Modal SDK reports for a server-side killed exec.
_KILLED_EXIT_CODES = frozenset({137, 143, -1, -9, -15})


def _collect_process_output(proc: Any, *, timeout: int | None = None) -> dict[str, Any]:
    """Drain a test process's streams with capped memory; return outcome.

    A run that lasts the full ``timeout`` and ends in a killed exit
    code is a timeout even when the SDK reports EOF plus an exit code
    instead of raising: Modal kills the exec session server-side and
    the client observes -1 (verified against real Modal 1.6.3).
    Genuine test failures (exit 1-5 well under the budget) never match
    this rule.
    """
    started = time.perf_counter()
    stdout_collector = _CappedCollector()
    stderr_collector = _CappedCollector()
    try:
        for chunk in proc.stdout:
            stdout_collector.feed(chunk if isinstance(chunk, str) else str(chunk))
    except Exception as exc:
        status, _ = classify_modal_error(exc)
        if status == "timeout":
            combined = (stdout_collector.result()[0] + "\n" + stderr_collector.result()[0]).strip()
            return {"kind": "timeout", "output": combined, "reason": "exec_timeout"}
        return {"kind": "error", "error": exc}
    try:
        for chunk in proc.stderr:
            stderr_collector.feed(chunk if isinstance(chunk, str) else str(chunk))
    except Exception as exc:
        status, _ = classify_modal_error(exc)
        if status == "timeout":
            combined = (stdout_collector.result()[0] + "\n" + stderr_collector.result()[0]).strip()
            return {"kind": "timeout", "output": combined, "reason": "exec_timeout"}
        return {"kind": "error", "error": exc}
    try:
        code = proc.wait()
    except Exception as exc:
        status, _ = classify_modal_error(exc)
        if status == "timeout":
            combined = (stdout_collector.result()[0] + "\n" + stderr_collector.result()[0]).strip()
            return {"kind": "timeout", "output": combined, "reason": "exec_timeout"}
        return {"kind": "error", "error": exc}
    stdout_text, stdout_truncated = stdout_collector.result()
    stderr_text, stderr_truncated = stderr_collector.result()
    combined = (stdout_text + "\n" + stderr_text).strip()
    elapsed = time.perf_counter() - started
    if (
        timeout
        and elapsed >= timeout
        and code in _KILLED_EXIT_CODES
    ):
        return {"kind": "timeout", "output": combined, "reason": "exec_timeout"}
    return {
        "kind": "ok",
        "output": combined,
        "exit_code": code,
        "truncated": stdout_truncated or stderr_truncated,
        "total_chars": (
            stdout_collector.total + stderr_collector.total + 1
        ),
    }


def _terminate_quietly(sandbox: Any) -> None:
    try:
        sandbox.terminate()
    except Exception:
        pass


class _InstallPlanError(Exception):
    """An install step cannot be translated to a Modal image step."""


class _SetupCommandError(Exception):
    """A sandbox setup command (mkdir/extract) failed."""
