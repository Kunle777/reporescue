"""Tests for the Modal cloud execution backend (Phase 5B).

The Modal SDK is fully mocked: a FakeModal module records image builds,
sandbox creation, file transfer, and command execution without any
network access. Real-cloud acceptance lives in
``verify_modal_phase5b.py`` and runs only with owner credentials.
"""

import threading
import time

import pytest

from app import modal_backend as modal_backend_module
from app.execution import (
    ExecutionRequest,
    UnsafeLocalBackend,
    cancel_requested,
    select_backend,
)
from app.modal_backend import (
    ModalBackend,
    classify_modal_error,
    infer_language,
    select_modal_python_version,
)

FAKE_SHA = "d" * 40

PYTEST_FAILURE_OUTPUT = """\
============================= test session starts ==============================
collected 2 items

tests/test_calc.py::test_add FAILED                                    [ 50%]
tests/test_calc.py::test_sub PASSED                                    [100%]

=================================== FAILURES ===================================
_______________________________ test_add ________________________________

    def test_add():
>       assert add(2, 3) == 5
E       assert 1 == 5

tests/test_calc.py:4: AssertionError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert 1 == 5
========================= 1 failed, 1 passed in 0.02s ==========================
"""


def _named_error(name):
    return type(name, (Exception,), {"__module__": "modal.exception"})


FakeAuthError = _named_error("AuthError")
FakeConnectionError = _named_error("ConnectionError")
FakeQuotaError = _named_error("ResourceExhaustedError")
FakeExecTimeout = _named_error("ExecTimeoutError")
FakeSandboxTimeout = _named_error("SandboxTimeoutError")
FakeInvalidError = _named_error("InvalidError")


class FakeStream:
    def __init__(self, chunks=(), exc=None):
        self._chunks = list(chunks)
        self._exc = exc

    def __iter__(self):
        yield from self._chunks
        if self._exc is not None:
            raise self._exc

    def read(self):
        if self._exc is not None:
            raise self._exc
        return "".join(self._chunks)


class FakeProcess:
    def __init__(self, stdout=(), stderr=(), code=0, stdout_exc=None, wait_exc=None):
        self.stdout = FakeStream(stdout, stdout_exc)
        self.stderr = FakeStream(stderr)
        self._code = code
        self._wait_exc = wait_exc

    def wait(self):
        if self._wait_exc is not None:
            raise self._wait_exc
        return self._code


class FakeFilesystem:
    def __init__(self):
        self.copies = []
        self.exc = None

    def copy_from_local(self, local_path, remote_path):
        if self.exc is not None:
            raise self.exc
        self.copies.append((str(local_path), remote_path))


class FakeSandbox:
    def __init__(self, exec_handler=None):
        self.filesystem = FakeFilesystem()
        self.exec_calls = []
        self.terminated = False
        self._handler = exec_handler or (lambda *a, **k: FakeProcess(code=0))

    def exec(self, *args, **kwargs):
        self.exec_calls.append((tuple(args), dict(kwargs)))
        return self._handler(*args, **kwargs)

    def terminate(self, *args, **kwargs):
        self.terminated = True


class FakeSandboxClass:
    def __init__(self, sandbox=None, create_exc=None):
        self.create_calls = []
        self._sandbox = sandbox
        self._create_exc = create_exc

    def create(self, *args, **kwargs):
        self.create_calls.append((tuple(args), dict(kwargs)))
        if self._create_exc is not None:
            raise self._create_exc
        return self._sandbox if self._sandbox is not None else FakeSandbox()


class FakeImageNode:
    def __init__(self, log, desc):
        self._log = log
        log.append(desc)

    def pip_install(self, *specs):
        return FakeImageNode(self._log, f"pip_install:{specs}")

    def pip_install_from_requirements(self, path):
        return FakeImageNode(self._log, f"pip_install_from_requirements:{path}")

    def add_local_file(self, local, remote, copy=False):
        return FakeImageNode(
            self._log, f"add_local_file:{local}->{remote}:copy={copy}"
        )

    def add_local_dir(self, local, remote, copy=False):
        return FakeImageNode(
            self._log, f"add_local_dir:{local}->{remote}:copy={copy}"
        )

    def run_commands(self, *cmds):
        return FakeImageNode(self._log, f"run_commands:{cmds}")


class FakeImageModule:
    def __init__(self, log):
        self._log = log

    def debian_slim(self, python_version=None):
        return FakeImageNode(self._log, f"debian_slim:{python_version}")

    def from_registry(self, tag):
        return FakeImageNode(self._log, f"from_registry:{tag}")


class FakeAppModule:
    def __init__(self, log):
        self._log = log

    def lookup(self, name, create_if_missing=False):
        self._log.append(f"lookup:{name}:{create_if_missing}")
        return object()


class FakeModal:
    def __init__(self, sandbox=None, create_exc=None):
        self.image_log = []
        self.app_log = []
        self.Image = FakeImageModule(self.image_log)
        self.App = FakeAppModule(self.app_log)
        self.Sandbox = FakeSandboxClass(sandbox, create_exc)


def _write_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def modal_env(monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-token-id-value")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "test-token-secret-value")
    monkeypatch.delenv("REPORESCUE_MODAL_USE_SDK_CONFIG", raising=False)
    monkeypatch.delenv("REPORESCUE_EXECUTION_BACKEND", raising=False)
    monkeypatch.delenv("REPORESCUE_MODAL_CPU", raising=False)
    monkeypatch.delenv("REPORESCUE_MODAL_MEMORY_MB", raising=False)
    monkeypatch.delenv("REPORESCUE_MODAL_APP_NAME", raising=False)
    monkeypatch.delenv("REPORESCUE_MODAL_NODE_IMAGE", raising=False)
    monkeypatch.delenv("REPORESCUE_SNAPSHOT_MAX_TOTAL_BYTES", raising=False)
    monkeypatch.delenv("REPORESCUE_SNAPSHOT_MAX_FILE_BYTES", raising=False)
    monkeypatch.delenv("REPORESCUE_SNAPSHOT_MAX_FILES", raising=False)


def _install_fake(monkeypatch, sandbox=None, create_exc=None):
    fake = FakeModal(sandbox, create_exc)
    monkeypatch.setattr(modal_backend_module, "modal", fake)
    return fake


def _standard_handler(commit=FAKE_SHA, test_chunks=(), test_code=1):
    def handler(*args, **kwargs):
        argv = list(args)
        if argv[:2] == ["mkdir", "-p"]:
            return FakeProcess(code=0)
        if argv[0] == "tar":
            return FakeProcess(code=0)
        if argv[0] == "cat":
            return FakeProcess(stdout=[commit + "\n"], code=0)
        return FakeProcess(stdout=list(test_chunks), code=test_code)

    return handler


# --- availability and credential handling ---


def test_modal_unavailable_without_sdk(monkeypatch):
    monkeypatch.setattr(modal_backend_module, "modal", None)
    available, reason = ModalBackend().is_available()
    assert available is False
    assert "not installed" in reason


def test_modal_unavailable_without_credentials(monkeypatch):
    _install_fake(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    available, reason = ModalBackend().is_available()
    assert available is False
    assert "MODAL_TOKEN_ID" in reason


def test_modal_available_via_environment_never_logs_values(
    monkeypatch, modal_env
):
    _install_fake(monkeypatch)
    available, reason = ModalBackend().is_available()
    assert available is True
    assert "test-token-id-value" not in reason
    assert "test-token-secret-value" not in reason


def test_modal_sdk_config_opt_in(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    (tmp_path / ".modal.toml").write_text(
        '[default]\ntoken_id = "ak-x"\ntoken_secret = "as-y"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        modal_backend_module.Path, "home", classmethod(lambda cls: tmp_path)
    )
    available, _ = ModalBackend().is_available()
    assert available is False
    monkeypatch.setenv("REPORESCUE_MODAL_USE_SDK_CONFIG", "1")
    available, reason = ModalBackend().is_available()
    assert available is True
    assert "config file" in reason


def test_modal_partial_credentials_are_not_enough(monkeypatch):
    _install_fake(monkeypatch)
    monkeypatch.setenv("MODAL_TOKEN_ID", "only-id")
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    available, _ = ModalBackend().is_available()
    assert available is False


# --- backend selection ---


def _docker_down(monkeypatch):
    from app.execution import DockerBackend

    monkeypatch.setattr(
        DockerBackend, "is_available", lambda self: (False, "down")
    )


def _docker_up(monkeypatch):
    from app.execution import DockerBackend

    monkeypatch.setattr(
        DockerBackend, "is_available", lambda self: (True, "up")
    )


def test_select_modal_explicit_with_credentials(monkeypatch, modal_env):
    _install_fake(monkeypatch)
    _docker_up(monkeypatch)
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "modal")
    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, ModalBackend)


def test_select_modal_explicit_without_credentials(monkeypatch):
    _install_fake(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "modal")
    backend, error = select_backend()
    assert backend is None
    assert error["reason"] == "modal_unavailable"


def test_select_docker_explicit(monkeypatch, modal_env):
    _install_fake(monkeypatch)
    _docker_up(monkeypatch)
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "docker")
    from app.execution import DockerBackend

    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, DockerBackend)


def test_select_docker_explicit_unavailable(monkeypatch, modal_env):
    _install_fake(monkeypatch)
    _docker_down(monkeypatch)
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "docker")
    backend, error = select_backend()
    assert backend is None
    assert error["reason"] == "runtime_unavailable"


def test_select_unsafe_local_explicit(monkeypatch):
    _install_fake(monkeypatch)
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "unsafe-local")
    monkeypatch.delenv("REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION", raising=False)
    backend, error = select_backend()
    assert backend is None
    assert error["reason"] == "unsafe_local_disabled"
    monkeypatch.setenv("REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION", "1")
    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, UnsafeLocalBackend)


def test_select_auto_prefers_modal_then_docker(monkeypatch, modal_env):
    _install_fake(monkeypatch)
    _docker_up(monkeypatch)
    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, ModalBackend)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    from app.execution import DockerBackend

    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, DockerBackend)


def test_select_auto_none_mentions_modal(monkeypatch):
    _install_fake(monkeypatch)
    _docker_down(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    backend, error = select_backend()
    assert backend is None
    assert error["reason"] == "runtime_unavailable"
    assert "Modal" in error["message"]


def test_select_invalid_preference_is_config_error(monkeypatch, modal_env):
    _install_fake(monkeypatch)
    monkeypatch.setenv("REPORESCUE_EXECUTION_BACKEND", "kubernetes")
    backend, error = select_backend()
    assert backend is None
    assert error["reason"] == "config_error"


# --- language and version mapping ---


def test_infer_language_prefers_declaration(tmp_path):
    request = ExecutionRequest(
        snapshot=tmp_path, test_command=["npm", "test"], language="python"
    )
    assert infer_language(request) == "python"


def test_infer_language_from_commands(tmp_path):
    python_request = ExecutionRequest(
        snapshot=tmp_path, test_command=["python", "-m", "pytest"]
    )
    assert infer_language(python_request) == "python"
    js_request = ExecutionRequest(
        snapshot=tmp_path,
        test_command=["npm", "test"],
        install_commands=[["npm", "ci"]],
    )
    assert infer_language(js_request) == "javascript"
    install_only = ExecutionRequest(
        snapshot=tmp_path,
        test_command=["custom-runner"],
        install_commands=[["pip", "install", "pytest"]],
    )
    assert infer_language(install_only) == "python"
    unknown = ExecutionRequest(snapshot=tmp_path, test_command=["go", "test"])
    assert infer_language(unknown) == "unknown"


def test_select_modal_python_version():
    assert select_modal_python_version(">=3.11") == "3.11"
    assert select_modal_python_version(">=3.9") == "3.9"
    assert select_modal_python_version("==3.13.*") == "3.13"
    assert select_modal_python_version(">=3.8") == "3.12"
    assert select_modal_python_version(">=3.14") == "3.12"
    assert select_modal_python_version(None) == "3.12"
    assert select_modal_python_version("garbage") == "3.12"


def test_classify_modal_error_distinct_reasons():
    assert classify_modal_error(FakeAuthError("x")) == ("error", "auth_error")
    assert classify_modal_error(FakeConnectionError("x")) == (
        "error",
        "connection_error",
    )
    assert classify_modal_error(FakeQuotaError("x")) == (
        "error",
        "quota_exhausted",
    )
    assert classify_modal_error(FakeExecTimeout("x")) == ("timeout", "exec_timeout")
    assert classify_modal_error(FakeSandboxTimeout("x")) == (
        "timeout",
        "sandbox_timeout",
    )
    assert classify_modal_error(ValueError("x")) == ("error", "internal_error")


# --- successful runs ---


def test_modal_python_run_end_to_end_mocked(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    sandbox = FakeSandbox(
        _standard_handler(test_chunks=[PYTEST_FAILURE_OUTPUT], test_code=1)
    )
    fake = _install_fake(monkeypatch, sandbox)
    monkeypatch.setenv("REPORESCUE_MODAL_CPU", "2")
    monkeypatch.setenv("REPORESCUE_MODAL_MEMORY_MB", "2048")

    request = ExecutionRequest(
        snapshot=project,
        test_command=["python", "-m", "pytest", "-q"],
        install_commands=[
            ["pip", "install", "-r", "requirements.txt"],
            ["pip", "install", "pytest"],
        ],
        framework="pytest",
        commit_sha=FAKE_SHA,
        repo_name="acme/calc",
        python_requires=">=3.11",
    )
    result = ModalBackend().run(request)

    assert result.backend == "modal"
    assert result.isolation == "modal-sandbox"
    assert result.status == "test_failure"
    assert result.passed is False
    assert result.exit_code == 1
    assert result.failures
    assert result.failures[0]["test"] == "tests/test_calc.py::test_add"
    assert result.summary_counts == {"failed": 1, "passed": 1}
    assert result.install_ok is True
    assert result.setup_runs == 1
    assert result.reason is None
    assert result.truncated is False
    assert "pip install -r requirements.txt" in result.install_log
    assert "pip install pytest" in result.install_log
    payload = result.to_dict()
    assert payload["reason"] is None
    assert payload["truncated"] is False

    assert fake.image_log[0] == "debian_slim:3.11"
    assert any(
        entry.startswith("pip_install_from_requirements:") for entry in fake.image_log
    )
    assert "pip_install:('pytest',)" in fake.image_log

    assert len(fake.Sandbox.create_calls) == 1
    _, create_kwargs = fake.Sandbox.create_calls[0]
    assert create_kwargs["block_network"] is True
    assert create_kwargs["cpu"] == 2.0
    assert create_kwargs["memory"] == 2048
    assert create_kwargs["workdir"] == "/repo"
    assert fake.app_log == ["lookup:reporescue-executions:True"]

    assert len(sandbox.filesystem.copies) == 1
    local_archive, remote_archive = sandbox.filesystem.copies[0]
    assert remote_archive == "/tmp/snapshot.tar.gz"
    assert local_archive.endswith(".tar.gz")

    kinds = [list(args) for args, _ in sandbox.exec_calls]
    assert ["mkdir", "-p", "/repo"] in kinds
    assert ["tar", "-xzf", "/tmp/snapshot.tar.gz", "-C", "/repo"] in kinds
    test_calls = [
        (args, kwargs)
        for args, kwargs in sandbox.exec_calls
        if list(args)[:2] == ["python", "-m"]
    ]
    assert len(test_calls) == 1
    _, test_kwargs = test_calls[0]
    assert test_kwargs["workdir"] == "/repo"
    assert test_kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "MODAL_TOKEN_ID" not in test_kwargs["env"]
    assert "MODAL_TOKEN_SECRET" not in test_kwargs["env"]

    assert sandbox.terminated is True


def test_modal_python_run_passing_suite(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    sandbox = FakeSandbox(_standard_handler(test_chunks=["2 passed"], test_code=0))
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest", "-q"],
            install_commands=[["pip", "install", "pytest"]],
            framework="pytest",
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "ok"
    assert result.passed is True
    assert sandbox.terminated is True


def test_modal_editable_install_bakes_snapshot(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    (project / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
    sandbox = FakeSandbox(_standard_handler(test_chunks=["1 passed"], test_code=0))
    fake = _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest", "-q"],
            install_commands=[["pip", "install", "-e", "."]],
            framework="pytest",
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "ok"
    assert any(entry.startswith("add_local_dir:") for entry in fake.image_log)
    assert any("pip install -e /repo" in entry for entry in fake.image_log)


def test_modal_javascript_run_mocked(monkeypatch, modal_env, tmp_path):
    project = tmp_path / "jsproj"
    project.mkdir()
    (project / "package.json").write_text(
        '{"name": "calc", "engines": {"node": ">=20"}, '
        '"scripts": {"test": "node --test"}}',
        encoding="utf-8",
    )
    (project / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (project / "calc.test.js").write_text(
        "const test = require('node:test');\ntest('x', () => {});\n",
        encoding="utf-8",
    )
    node_output = "ℹ pass 1\nℹ fail 0\n"
    sandbox = FakeSandbox(_standard_handler(test_chunks=[node_output], test_code=0))
    fake = _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["npm", "test", "--silent"],
            install_commands=[["npm", "ci"]],
            framework="npm",
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "ok"
    assert result.passed is True
    assert fake.image_log[0] == "from_registry:node:20-slim"
    assert any("npm ci" in entry for entry in fake.image_log)
    test_calls = [
        (args, kwargs)
        for args, kwargs in sandbox.exec_calls
        if list(args)[:1] == ["npm"]
    ]
    assert len(test_calls) == 1
    assert test_calls[0][1]["env"]["CI"] == "true"
    assert sandbox.filesystem.copies
    assert sandbox.terminated is True


def test_modal_node_image_env_override_wins(monkeypatch, modal_env, tmp_path):
    project = tmp_path / "jsproj"
    project.mkdir()
    (project / "package.json").write_text(
        '{"engines": {"node": ">=18"}}', encoding="utf-8"
    )
    sandbox = FakeSandbox(_standard_handler(test_chunks=["ok"], test_code=0))
    fake = _install_fake(monkeypatch, sandbox)
    monkeypatch.setenv("REPORESCUE_MODAL_NODE_IMAGE", "node:22-slim")
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["npm", "test"],
            install_commands=[],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "ok"
    assert fake.image_log[0] == "from_registry:node:22-slim"


# --- unsupported and invalid inputs ---


def test_modal_run_without_credentials_fails_closed(monkeypatch, tmp_path):
    project = _write_project(tmp_path / "project")
    fake = _install_fake(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    result = ModalBackend().run(
        ExecutionRequest(snapshot=project, test_command=["python", "-m", "pytest"])
    )
    assert result.status == "error"
    assert result.reason == "credentials_missing"
    assert fake.Sandbox.create_calls == []


def test_modal_unknown_language_is_unsupported(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    fake = _install_fake(monkeypatch)
    result = ModalBackend().run(
        ExecutionRequest(snapshot=project, test_command=["go", "test"])
    )
    assert result.status == "unsupported"
    assert result.reason == "unsupported_language"
    assert result.passed is None
    assert fake.Sandbox.create_calls == []


def test_modal_python_launcher_allowlist(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    fake = _install_fake(monkeypatch)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["sh", "-c", "pytest"],
            language="python",
        )
    )
    assert result.status == "unsupported"
    assert result.reason == "unsupported_command"
    assert fake.Sandbox.create_calls == []


def test_modal_missing_snapshot(monkeypatch, modal_env, tmp_path):
    fake = _install_fake(monkeypatch)
    result = ModalBackend().run(
        ExecutionRequest(snapshot=tmp_path / "gone")
    )
    assert result.status == "error"
    assert result.reason == "snapshot_missing"
    assert fake.Sandbox.create_calls == []


def test_modal_snapshot_too_large_never_creates_sandbox(
    monkeypatch, modal_env, tmp_path
):
    project = _write_project(tmp_path / "project")
    fake = _install_fake(monkeypatch)
    monkeypatch.setenv("REPORESCUE_SNAPSHOT_MAX_TOTAL_BYTES", "10")
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "error"
    assert result.reason == "snapshot_too_large"
    assert fake.Sandbox.create_calls == []


def test_modal_snapshot_identity_mismatch(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    sandbox = FakeSandbox(_standard_handler(commit="e" * 40))
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "error"
    assert result.reason == "snapshot_mismatch"
    assert "refusing to execute" in result.output
    assert sandbox.terminated is True


def test_modal_transfer_failure(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    sandbox = FakeSandbox(_standard_handler())
    sandbox.filesystem.exc = FakeConnectionError("network down")
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "error"
    assert result.reason == "connection_error"
    assert sandbox.terminated is True


# --- infrastructure failures ---


def test_modal_auth_error_distinct(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    fake = _install_fake(
        monkeypatch, create_exc=FakeAuthError("bad token")
    )
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "error"
    assert result.reason == "auth_error"
    assert "auth_error" in result.output
    assert "test-token-id-value" not in result.output
    assert result.install_ok is None
    assert fake.Sandbox.create_calls != []


def test_modal_quota_error_distinct(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    _install_fake(monkeypatch, create_exc=FakeQuotaError("out of credits"))
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "error"
    assert result.reason == "quota_exhausted"


def test_modal_build_failure_is_install_error(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    _install_fake(monkeypatch, create_exc=FakeInvalidError("bad image"))
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            install_commands=[["pip", "install", "pytest"]],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "install_error"
    assert result.reason == "image_build_failed"
    assert result.install_ok is False
    assert "pip install pytest" in result.install_log


def test_modal_exec_timeout(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")

    def handler(*args, **kwargs):
        argv = list(args)
        if argv[:2] == ["mkdir", "-p"] or argv[0] == "tar":
            return FakeProcess(code=0)
        if argv[0] == "cat":
            return FakeProcess(stdout=[FAKE_SHA + "\n"], code=0)
        return FakeProcess(
            stdout=["partial output"], code=0,
            stdout_exc=FakeExecTimeout("timed out"),
        )

    sandbox = FakeSandbox(handler)
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
            test_timeout=5,
        )
    )
    assert result.status == "timeout"
    assert result.reason == "exec_timeout"
    assert result.passed is False
    assert "timed out" in result.output
    assert sandbox.terminated is True


@pytest.mark.parametrize("killed_code", [137, -1])
def test_modal_killed_exit_code_after_timeout_is_timeout(
    monkeypatch, modal_env, tmp_path, killed_code
):
    """A full-budget run ending in a killed exit code reports timeout."""
    project = _write_project(tmp_path / "project")

    class KilledProcess(FakeProcess):
        def wait(self):
            time.sleep(1.2)
            return killed_code

    def handler(*args, **kwargs):
        argv = list(args)
        if argv[:2] == ["mkdir", "-p"] or argv[0] == "tar":
            return FakeProcess(code=0)
        if argv[0] == "cat":
            return FakeProcess(stdout=[FAKE_SHA + "\n"], code=0)
        return KilledProcess(stdout=["partial"], code=killed_code)

    sandbox = FakeSandbox(handler)
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
            test_timeout=1,
        )
    )
    assert result.status == "timeout"
    assert result.reason == "exec_timeout"
    assert result.passed is False
    assert sandbox.terminated is True


def test_modal_fast_killed_code_is_not_timeout(monkeypatch, modal_env, tmp_path):
    """A quick non-zero exit is a test failure even with odd codes."""
    project = _write_project(tmp_path / "project")
    sandbox = FakeSandbox(
        _standard_handler(test_chunks=["FAILED x"], test_code=1)
    )
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
            test_timeout=120,
        )
    )
    assert result.status == "test_failure"
    assert result.reason is None


def test_modal_cancellation_terminates_sandbox(
    monkeypatch, modal_env, tmp_path
):
    project = _write_project(tmp_path / "project")
    cancel = threading.Event()

    def slow_stdout():
        for _ in range(60):
            time.sleep(0.05)
            yield "working...\n"

    class SlowStream:
        def __iter__(self):
            return slow_stdout()

        def read(self):
            return "".join(slow_stdout())

    class SlowProcess(FakeProcess):
        def __init__(self):
            self.stdout = SlowStream()
            self.stderr = FakeStream([])

        def wait(self):
            return 0

    def handler(*args, **kwargs):
        argv = list(args)
        if argv[:2] == ["mkdir", "-p"] or argv[0] == "tar":
            return FakeProcess(code=0)
        if argv[0] == "cat":
            return FakeProcess(stdout=[FAKE_SHA + "\n"], code=0)
        return SlowProcess()

    sandbox = FakeSandbox(handler)
    _install_fake(monkeypatch, sandbox)

    def _cancel_soon():
        time.sleep(0.4)
        cancel.set()

    trigger = threading.Thread(target=_cancel_soon)
    trigger.start()
    try:
        result = ModalBackend().run(
            ExecutionRequest(
                snapshot=project,
                test_command=["python", "-m", "pytest"],
                commit_sha=FAKE_SHA,
                cancel=cancel,
            )
        )
    finally:
        trigger.join(timeout=30)
    assert result.status == "cancelled"
    assert result.reason == "cancelled"
    assert result.passed is None
    assert "cancelled" in result.output.lower()
    assert sandbox.terminated is True


def test_modal_pre_cancelled_never_creates_sandbox(
    monkeypatch, modal_env, tmp_path
):
    project = _write_project(tmp_path / "project")
    fake = _install_fake(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            cancel=cancel,
        )
    )
    assert result.status == "cancelled"
    assert fake.Sandbox.create_calls == []


def test_modal_cleanup_on_unexpected_error(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")

    def handler(*args, **kwargs):
        raise RuntimeError("boom")

    sandbox = FakeSandbox(handler)
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "error"
    assert result.reason == "internal_error"
    assert sandbox.terminated is True


def test_modal_truncates_huge_output(monkeypatch, modal_env, tmp_path):
    project = _write_project(tmp_path / "project")
    big = "x" * 20000
    sandbox = FakeSandbox(_standard_handler(test_chunks=[big], test_code=0))
    _install_fake(monkeypatch, sandbox)
    result = ModalBackend().run(
        ExecutionRequest(
            snapshot=project,
            test_command=["python", "-m", "pytest"],
            commit_sha=FAKE_SHA,
        )
    )
    assert result.status == "ok"
    assert result.truncated is True
    assert "truncated" in result.output
    assert len(result.output) < 20000
    assert result.to_dict()["truncated"] is True


def test_modal_backend_contract_attributes():
    backend = ModalBackend()
    assert backend.name == "modal"
    assert backend.isolation == "modal-sandbox"
    assert backend.requires_fresh_install is True
    assert backend.needs_pytest_install() is True


def test_cancel_requested_semantics():
    assert cancel_requested(None) is False
    assert cancel_requested(lambda: False) is False
    assert cancel_requested(lambda: True) is True
    event = threading.Event()
    assert cancel_requested(event) is False
    event.set()
    assert cancel_requested(event) is True

    def _boom():
        raise RuntimeError("probe failed")

    assert cancel_requested(_boom) is True
    assert cancel_requested(object()) is False
