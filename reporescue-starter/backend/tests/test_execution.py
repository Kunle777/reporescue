"""Tests for the execution interface: backends, parsing, and planning.

Docker behavior is verified through mocked subprocess calls (no daemon is
required); the unsafe-local backend is exercised for real against temporary
fixture snapshots with the explicit opt-in flag set per-test.
"""

import subprocess

import pytest

from app import execution as execution_module
from app.execution import (
    UNSAFE_LOCAL_FLAG,
    DockerBackend,
    ExecutionRequest,
    UnsafeLocalBackend,
    detect_harness_incompatibility,
    parse_test_output,
    plan_install,
    requirements_pin_pytest,
    select_backend,
    select_python_image,
    traceback_source_files,
)


@pytest.fixture(autouse=True)
def _no_unsafe_flag(monkeypatch):
    monkeypatch.delenv(UNSAFE_LOCAL_FLAG, raising=False)


@pytest.fixture
def failing_project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return tmp_path


# --- backend availability ---


def test_docker_unavailable_without_daemon(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    available, reason = DockerBackend().is_available()
    assert available is False
    assert "not installed" in reason


def test_docker_unavailable_when_daemon_down(monkeypatch):
    class Dead:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Dead())
    available, reason = DockerBackend().is_available()
    assert available is False
    assert "unavailable" in reason


def test_unsafe_local_disabled_by_default():
    available, reason = UnsafeLocalBackend().is_available()
    assert available is False
    assert UNSAFE_LOCAL_FLAG in reason


def test_unsafe_local_enabled_only_by_explicit_flag(monkeypatch):
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    available, reason = UnsafeLocalBackend().is_available()
    assert available is True
    assert "without isolation" in reason


def test_select_backend_prefers_docker(monkeypatch):
    monkeypatch.setattr(DockerBackend, "is_available", lambda self: (True, "up"))
    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, DockerBackend)


def test_select_backend_never_silently_falls_back(monkeypatch):
    monkeypatch.setattr(DockerBackend, "is_available", lambda self: (False, "down"))
    backend, error = select_backend()
    assert backend is None
    assert error["reason"] == "runtime_unavailable"
    assert "docker" in error["message"].lower()
    assert UNSAFE_LOCAL_FLAG in error["message"]


def test_select_backend_uses_flagged_local(monkeypatch):
    monkeypatch.setattr(DockerBackend, "is_available", lambda self: (False, "down"))
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    backend, error = select_backend()
    assert error is None
    assert isinstance(backend, UnsafeLocalBackend)


# --- docker command construction (mocked transport) ---


def _ok_result(stdout=""):
    class Ok:
        returncode = 0

        def __init__(self):
            self.stdout = stdout
            self.stderr = ""

    return Ok()


def test_docker_test_run_uses_network_none_and_limits(tmp_path, monkeypatch):
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        assert args[:3] == ["docker", "run", "--rm"]
        return _ok_result("1 passed in 0.1s")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert result.backend == "docker"
    assert result.isolation == "docker-container"
    assert result.passed is True
    run_args = seen[-1][0]
    assert "--network" in run_args and "none" in run_args
    assert "--cpus" in run_args and "--memory" in run_args
    assert "--pids-limit" in run_args
    assert "--cap-drop" in run_args and "ALL" in run_args
    assert "no-new-privileges" in " ".join(run_args)


def test_docker_scrubs_secrets_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "super-secret-key")
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs.get("env", {}))
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        return _ok_result("1 passed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert "META_API_KEY" not in seen
    assert "super-secret-key" not in "\n".join(seen.values())


def test_docker_reports_timeout(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert result.status == "timeout"
    assert result.passed is False
    assert result.exit_code is None


def test_docker_install_failure_aborts_before_tests(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        if "venv" in args:
            return _ok_result("venv ready")

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "ERROR: No matching distribution"

        return Failed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(
        ExecutionRequest(snapshot=tmp_path, install_commands=[["pip", "install", "nope"]])
    )

    assert result.status == "install_error"
    assert result.install_ok is False
    assert result.passed is None
    assert "--network" not in [part for call in calls for part in call]


def test_docker_prunes_venv_lib64_after_creation(tmp_path, monkeypatch):
    """The venv's lib64 symlink must be removed inside the container.

    On Windows bind mounts lib64 materializes as an entry the host can
    neither copy (WinError 1920) nor delete (WinError 5), which breaks
    work-copy creation later. The venv functions without it.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        return _ok_result("ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(
        ExecutionRequest(
            snapshot=tmp_path, install_commands=[["pip", "install", "pytest"]]
        )
    )

    assert result.status == "ok"
    rm_calls = [c for c in calls if c[-3:] == ["rm", "-f", "/repo/.execvenv/lib64"]]
    assert len(rm_calls) == 1
    venv_idx = next(i for i, c in enumerate(calls) if "venv" in c)
    pip_idx = next(i for i, c in enumerate(calls) if "pip" in " ".join(c))
    assert venv_idx < calls.index(rm_calls[0]) < pip_idx


def test_docker_lib64_prune_failure_aborts_install(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        if "venv" in args:
            return _ok_result("venv ready")

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "rm: cannot remove"

        return Failed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(
        ExecutionRequest(
            snapshot=tmp_path, install_commands=[["pip", "install", "pytest"]]
        )
    )

    assert result.status == "install_error"
    assert result.install_ok is False
    assert "lib64" in result.install_log


def test_docker_uses_snapshot_venv_for_verification(tmp_path, monkeypatch):
    (tmp_path / ".execvenv").mkdir()
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        return _ok_result("1 passed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerBackend().run(ExecutionRequest(snapshot=tmp_path, install_commands=[]))

    assert "/repo/.execvenv/bin/python" in seen[-1]


# --- unsafe-local backend (real subprocess, fixture snapshots only) ---


def test_local_backend_refuses_without_flag(failing_project):
    result = UnsafeLocalBackend().run(ExecutionRequest(snapshot=failing_project))
    assert result.status == "error"
    assert result.passed is None
    assert UNSAFE_LOCAL_FLAG in result.output


def test_local_backend_runs_real_tests(failing_project, monkeypatch):
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    result = UnsafeLocalBackend().run(ExecutionRequest(snapshot=failing_project))

    assert result.isolation == "none-unsafe-local-dev-only"
    assert result.status == "test_failure"
    assert result.passed is False
    assert result.exit_code != 0
    assert len(result.failures) == 1
    assert result.failures[0]["test"] == "tests/test_calc.py::test_add"
    assert "calc" in result.output


def test_local_backend_passing_suite(failing_project, monkeypatch):
    (failing_project / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    result = UnsafeLocalBackend().run(ExecutionRequest(snapshot=failing_project))

    assert result.status == "ok"
    assert result.passed is True


def test_local_backend_tolerates_non_locale_output(tmp_path, monkeypatch):
    """Raw output bytes outside the host locale must not crash decoding.

    Regression test for the Windows ``UnicodeDecodeError: 'charmap' codec
    can't decode byte 0x81`` crash: repository test output is arbitrary
    bytes and must be decoded lossily, never with strict locale decoding.
    Byte 0x81 is invalid under both cp1252 and strict UTF-8, so this test
    fails pre-fix on any host locale.
    """
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_raw.py").write_text(
        "def test_raw():\n"
        "    import sys\n"
        "    sys.stdout.buffer.write(b'\\x81not-locale\\n')\n"
        "    assert True\n",
        encoding="utf-8",
    )
    result = UnsafeLocalBackend().run(
        ExecutionRequest(
            snapshot=tmp_path,
            test_command=["python", "-m", "pytest", "-q", "-s", "--tb=short"],
        )
    )

    assert result.status == "ok"
    assert result.passed is True
    assert "not-locale" in result.output


def test_local_backend_runs_in_snapshot_dir(failing_project, monkeypatch):
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    real_run = subprocess.run
    seen = {}

    def spy_run(*args, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        seen["env_keys"] = set(kwargs.get("env", {}))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setenv("META_API_KEY", "super-secret-key")
    UnsafeLocalBackend().run(ExecutionRequest(snapshot=failing_project))

    assert seen["cwd"] == failing_project
    assert "META_API_KEY" not in seen["env_keys"]


def test_local_backend_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8"
    )
    result = UnsafeLocalBackend().run(ExecutionRequest(snapshot=tmp_path, test_timeout=2))

    assert result.status == "timeout"
    assert result.passed is False


def test_local_backend_install_failure(tmp_path, monkeypatch):
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    result = UnsafeLocalBackend().run(
        ExecutionRequest(
            snapshot=tmp_path,
            install_commands=[["pip", "install", "--no-index", "no-such-package-xyz"]],
        )
    )

    assert result.status == "install_error"
    assert result.install_ok is False
    assert (tmp_path / ".execvenv").is_dir()  # venv contained in the snapshot


def test_local_backend_missing_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv(UNSAFE_LOCAL_FLAG, "1")
    result = UnsafeLocalBackend().run(ExecutionRequest(snapshot=tmp_path / "gone"))
    assert result.status == "error"


# --- output parsing ---


PYTEST_SAMPLE = """\
============================= test session starts =============================
tests/test_calc.py::test_add FAILED                                       [ 50%]
tests/test_calc.py::test_sub PASSED                                       [100%]
================================== FAILURES ===================================
_______________________________ test_add ________________________________
tests/test_calc.py:5: in test_add
    assert add(2, 3) == 5
E   assert -1 == 5
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_add - assert -1 == 5
1 failed, 1 passed in 0.42s
"""

UNITTEST_SAMPLE = """\
..F
======================================================================
FAIL: test_add (tests.test_calc.CalcTests.test_add)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_calc.py", line 9, in test_add
    self.assertEqual(add(2, 3), 5)
AssertionError: -1 != 5

----------------------------------------------------------------------
Ran 3 tests in 0.010s

FAILED (failures=1)
"""


def test_parse_pytest_failures():
    parsed = parse_test_output(PYTEST_SAMPLE, "pytest")
    assert len(parsed["failures"]) == 1
    failure = parsed["failures"][0]
    assert failure["test"] == "tests/test_calc.py::test_add"
    assert failure["kind"] == "failed"
    assert "assert -1 == 5" in failure["message"]
    assert "assert -1 == 5" in failure["traceback"]
    assert parsed["summary_counts"] == {"failed": 1, "passed": 1}


def test_parse_unittest_failures():
    parsed = parse_test_output(UNITTEST_SAMPLE, "unittest")
    assert len(parsed["failures"]) == 1
    assert parsed["failures"][0]["test"] == "test_add"
    assert parsed["failures"][0]["kind"] == "fail"


def test_parse_unparseable_output_yields_no_failures():
    parsed = parse_test_output("some garbage without structure", "pytest")
    assert parsed["failures"] == []


def test_parse_conftest_import_error():
    output = (
        "ImportError while loading conftest 'C:\\proj\\repo\\tests\\conftest.py'.\n"
        "tests\\conftest.py:14: in <module>\n"
        "    from jinja2 import loaders\n"
        "E   ImportError: cannot import name 'soft_unicode' from 'markupsafe'\n"
    )
    parsed = parse_test_output(output, "pytest")
    assert len(parsed["failures"]) == 1
    assert parsed["failures"][0]["kind"] == "error"
    assert "conftest.py" in parsed["failures"][0]["test"]


def test_parse_collect_error():
    parsed = parse_test_output("ERROR collecting tests/test_debug.py", "pytest")
    assert parsed["failures"] == [
        {"test": "tests/test_debug.py", "kind": "error",
         "message": "collection error", "traceback": ""}
    ]


def test_traceback_locations_support_short_frames():
    from app.execution import traceback_locations

    output = (
        "tests\\conftest.py:14: in <module>\n"
        "    from jinja2 import loaders\n"
        "jinja2\\utils.py:642: in <module>\n"
        "    from markupsafe import Markup\n"
        "E   ImportError: cannot import name 'soft_unicode'\n"
    )
    assert traceback_locations(output) == [
        ("tests/conftest.py", 14),
        ("jinja2/utils.py", 642),
    ]


def test_traceback_locations_normalize_absolute_paths():
    from app.execution import traceback_locations

    output = (
        '  File "C:/Temp/xyz/repo/pkg/calc.py", line 2, in add\n'
        '  File "/tmp/abc/repo/tests/test_x.py", line 9, in test_x\n'
    )
    assert traceback_locations(output) == [("pkg/calc.py", 2), ("tests/test_x.py", 9)]


def test_traceback_source_files_filters_stdlib():
    output = (
        '  File "/repo/pkg/calc.py", line 2, in add\n'
        '  File "/repo/tests/test_calc.py", line 5, in test_add\n'
        '  File "/usr/lib/python3.12/unittest/case.py", line 58, in run\n'
        '  File "/repo/.execvenv/lib/site-packages/_pytest/runner.py", line 1, in x\n'
        '  File "<frozen importlib._bootstrap>", line 1, in x\n'
    )
    assert traceback_source_files(output) == ["pkg/calc.py", "tests/test_calc.py"]


# --- install planning and image selection ---


def _profile(dep_files, framework="pytest", requires=None):
    return {
        "dependency_files": dep_files,
        "test_framework": framework,
        "python_requires": requires,
    }


def test_plan_install_requirements():
    planned = plan_install(_profile(["requirements.txt"]))
    assert planned["error"] is None
    assert ["pip", "install", "-r", "requirements.txt"] in planned["commands"]
    assert ["pip", "install", "pytest"] in planned["commands"]


def test_plan_install_pyproject_editable():
    planned = plan_install(_profile(["pyproject.toml"]))
    assert ["pip", "install", "-e", "."] in planned["commands"]


def test_plan_install_unittest_needs_no_pytest():
    planned = plan_install(_profile(["requirements.txt"], framework="unittest"))
    assert ["pip", "install", "pytest"] not in planned["commands"]


def test_plan_install_pipfile_only_rejected():
    planned = plan_install(_profile(["Pipfile"]))
    assert planned["error"] is not None
    assert "Pipfile" in planned["error"]


def test_pytest_install_needed_on_docker_assumed():
    assert DockerBackend().needs_pytest_install() is True


def test_pytest_install_skipped_when_host_has_pytest():
    assert UnsafeLocalBackend().needs_pytest_install() is False


def test_select_python_image_versions():
    assert select_python_image(">=3.11") == "python:3.11-slim"
    assert select_python_image(">=3.9,<4") == "python:3.9-slim"
    assert select_python_image(None) == execution_module.DOCKER_IMAGE_DEFAULT
    assert select_python_image("garbage") == execution_module.DOCKER_IMAGE_DEFAULT


# --- container naming, cleanup, and hardening ---


def _name_of(args):
    index = args.index("--name")
    return args[index + 1]


def test_docker_names_containers_uniquely(tmp_path, monkeypatch):
    names = []

    def fake_run(args, **kwargs):
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        names.append(_name_of(args))
        return _ok_result("1 passed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerBackend().run(ExecutionRequest(snapshot=tmp_path))
    DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert len(names) == 2
    assert names[0] != names[1]
    assert all(name.startswith("reporescue-") for name in names)


def test_docker_timeout_force_removes_container(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        if args[:3] == ["docker", "rm", "--force"]:
            return _ok_result("removed")
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert result.status == "timeout"
    run_call = next(call for call in calls if call[1:3] == ["run", "--rm"])
    rm_calls = [call for call in calls if call[1:3] == ["rm", "--force"]]
    assert len(rm_calls) == 1
    assert rm_calls[0][3] == _name_of(run_call)


def test_docker_timeout_survives_failed_force_remove(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert result.status == "timeout"
    assert result.passed is False


def test_docker_success_never_force_removes(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        return _ok_result("1 passed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    assert not [call for call in calls if call[1:2] == ["rm"]]


def test_docker_run_labels_managed_containers(tmp_path, monkeypatch):
    """Every container carries the management label for orphan cleanup."""
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        return _ok_result("1 passed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    DockerBackend().run(ExecutionRequest(snapshot=tmp_path))

    run_args = seen[-1]
    assert "--label" in run_args
    assert run_args[run_args.index("--label") + 1] == "reporescue.managed=true"


def test_docker_refuses_missing_snapshot(tmp_path, monkeypatch):
    def must_not_run(*args, **kwargs):  # pragma: no cover - guard assertion
        raise AssertionError("docker must not run without a snapshot directory")

    monkeypatch.setattr(DockerBackend, "is_available", lambda self: (True, "up"))
    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = DockerBackend().run(ExecutionRequest(snapshot=tmp_path / "gone"))

    assert result.status == "error"
    assert "missing" in result.output


def test_docker_venv_timeout_reports_timeout(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        if args[:2] == ["docker", "version"]:
            return _ok_result("27.0.0")
        if args[:3] == ["docker", "rm", "--force"]:
            return _ok_result("removed")
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBackend().run(
        ExecutionRequest(snapshot=tmp_path, install_commands=[["pip", "install", "x"]])
    )

    assert result.status == "timeout"
    assert result.install_ok is False


# --- harness classification and pytest-pin detection ---


PLUGGY_SAMPLE = """\
pluggy._manager.PluginValidationError: Plugin '/repo/tests/conftest.py' for hook 'pytest_ignore_collect'
hookimpl definition: pytest_ignore_collect(path)
Argument(s) {'path'} are declared in the hookimpl but can not be found in the hookspec
"""


def test_detect_harness_incompatibility_matches_pluggy_error():
    note = detect_harness_incompatibility(PLUGGY_SAMPLE)
    assert note is not None
    assert "pytest_ignore_collect" in note
    assert "No source patch can fix this" in note


def test_detect_harness_incompatibility_ignores_ordinary_failures():
    assert detect_harness_incompatibility(PYTEST_SAMPLE) is None
    assert detect_harness_incompatibility("some PluginValidationError mention") is None
    assert detect_harness_incompatibility("") is None
    assert detect_harness_incompatibility(None) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("pytest==7.4.4\n", True),
        ("pytest == 7.4.4  # pinned\n", True),
        ("requests\npytest~=7.4\n", True),
        ("pytest<8\n", True),
        ("pytest<=8.1\n", True),
        ("pytest[asyncio]==7.4\n", True),
        ("pytest\n", False),
        ("pytest>=6\n", False),
        ("requests\n", False),
        ("# pytest==7.4\n", False),
    ],
)
def test_requirements_pin_pytest_cases(tmp_path, content, expected):
    (tmp_path / "requirements.txt").write_text(content, encoding="utf-8")
    assert requirements_pin_pytest(tmp_path, ["requirements.txt"]) is expected


def test_requirements_pin_pytest_missing_file(tmp_path):
    assert requirements_pin_pytest(tmp_path, ["requirements.txt"]) is False
    assert requirements_pin_pytest(tmp_path, []) is False
