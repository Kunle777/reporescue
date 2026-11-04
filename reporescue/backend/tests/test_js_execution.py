"""Tests for JavaScript execution helpers (Phase 5B).

Covers npm install planning, test-command selection, Node image
selection, and output parsing, plus a real end-to-end lifecycle check
with the host Node.js runtime (no network, no install needed).
"""

import json
import os
import shutil
import subprocess

import pytest

from app.js_execution import (
    js_test_command,
    js_test_env,
    parse_js_output,
    plan_js_install,
    select_node_image,
    validate_js_argv,
)


def test_plan_js_install_requires_manifest():
    planned = plan_js_install(
        has_package_json=False, manifest_error=None, manifests=[]
    )
    assert planned["commands"] == []
    assert "package.json" in str(planned["error"])


def test_plan_js_install_propagates_manifest_error():
    planned = plan_js_install(
        has_package_json=True,
        manifest_error="package.json is missing, oversized, or not valid JSON.",
        manifests=["package.json"],
    )
    assert planned["commands"] == []
    assert "not valid JSON" in str(planned["error"])


def test_plan_js_install_prefers_ci_with_lockfile():
    planned = plan_js_install(
        has_package_json=True,
        manifest_error=None,
        manifests=["package.json", "package-lock.json"],
    )
    assert planned["error"] is None
    assert planned["commands"] == [["npm", "ci"]]


def test_plan_js_install_falls_back_without_lockfile():
    planned = plan_js_install(
        has_package_json=True, manifest_error=None, manifests=["package.json"]
    )
    assert planned["error"] is None
    assert planned["commands"] == [["npm", "install"]]


def test_plan_js_install_reports_yarn_pnpm_honestly():
    planned = plan_js_install(
        has_package_json=True,
        manifest_error=None,
        manifests=["package.json", "yarn.lock"],
    )
    assert planned["error"] is None
    assert planned["commands"] == [["npm", "install"]]
    assert "yarn" in str(planned.get("note", ""))


def test_js_test_command_requires_test_script():
    planned = js_test_command(["build", "lint"])
    assert planned["command"] == []
    assert "nothing was executed" in str(planned["error"])


def test_js_test_command_runs_repo_script_verbatim():
    planned = js_test_command(["build", "test"])
    assert planned["error"] is None
    assert planned["command"] == ["npm", "test", "--silent"]


def test_js_test_env_requests_single_run():
    assert js_test_env() == {"CI": "true"}


def test_validate_js_argv():
    assert validate_js_argv([], kind="test") is not None
    assert validate_js_argv(["sh", "-c", "x"], kind="test") is not None
    assert validate_js_argv(["python", "-m", "pytest"], kind="test") is not None
    assert validate_js_argv(["npm", "test"], kind="test") is None
    assert validate_js_argv(["npx", "vitest"], kind="test") is None
    assert validate_js_argv(["node", "--test"], kind="test") is None
    assert validate_js_argv(["npm", "ci"], kind="install") is None
    assert validate_js_argv(["node", "x.js"], kind="install") is not None


def test_select_node_image_honors_engines_major(tmp_path):
    manifest = tmp_path / "package.json"
    manifest.write_text(
        json.dumps({"engines": {"node": ">=18.12"}}), encoding="utf-8"
    )
    assert select_node_image(manifest) == "node:18-slim"
    manifest.write_text(
        json.dumps({"engines": {"node": "^20.9.0"}}), encoding="utf-8"
    )
    assert select_node_image(manifest) == "node:20-slim"


def test_select_node_image_falls_back_honestly(tmp_path):
    assert select_node_image(None) == "node:22-slim"
    assert select_node_image(tmp_path / "missing.json") == "node:22-slim"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert select_node_image(broken) == "node:22-slim"
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"engines": {"node": ">=16"}}), encoding="utf-8")
    assert select_node_image(odd) == "node:22-slim"
    future = tmp_path / "future.json"
    future.write_text(
        json.dumps({"engines": {"node": ">=99"}}), encoding="utf-8"
    )
    assert select_node_image(future) == "node:22-slim"
    none = tmp_path / "none.json"
    none.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert select_node_image(none) == "node:22-slim"


def test_parse_js_output_tap():
    parsed = parse_js_output(
        "TAP version 13\nok 1 adds numbers\nnot ok 2 subtracts numbers\n"
    )
    assert parsed["summary_counts"] == {}
    assert [failure["test"] for failure in parsed["failures"]] == [
        "subtracts numbers"
    ]
    assert parsed["failures"][0]["kind"] == "failed"


def test_parse_js_output_jest():
    parsed = parse_js_output(
        "FAIL ./calc.test.js\n"
        "  ✕ adds two numbers\n"
        "  ✓ subtracts two numbers\n"
        "Tests: 1 failed, 1 passed, 2 total\n"
    )
    assert parsed["summary_counts"] == {"failed": 1, "passed": 1}
    assert [failure["test"] for failure in parsed["failures"]] == [
        "adds two numbers"
    ]


def test_parse_js_output_mocha():
    parsed = parse_js_output(
        "  2 passing\n  1 failing\n\n  1) calc adds:\n     AssertionError\n"
    )
    assert parsed["summary_counts"] == {"passed": 2, "failed": 1}
    assert parsed["failures"][0]["test"] == "calc adds"


def test_parse_js_output_node_test_runner():
    parsed = parse_js_output(
        "✖ calc.test.js (127.3537ms)\n"
        "ℹ tests 1\n"
        "ℹ suites 0\n"
        "ℹ pass 0\n"
        "ℹ fail 1\n"
        "ℹ cancelled 0\n"
        "ℹ skipped 0\n"
        "ℹ todo 0\n"
        "ℹ duration_ms 162.9475\n"
        "\n"
        "✖ failing tests:\n"
        "\n"
        "test at calc.test.js:1:1\n"
        "✖ calc.test.js (127.3537ms)\n"
        "  'test failed'\n"
    )
    assert parsed["summary_counts"] == {"passed": 0, "failed": 1}
    assert [failure["test"] for failure in parsed["failures"]] == [
        "calc.test.js:1:1",
        "calc.test.js",
    ]


def test_parse_js_output_unparseable_is_empty():
    parsed = parse_js_output("some banner\nno results here\n")
    assert parsed == {"failures": [], "summary_counts": {}}
    assert parse_js_output("") == {"failures": [], "summary_counts": {}}


def test_parse_js_output_caps_failures():
    lines = "".join(f"not ok {index} case {index}\n" for index in range(100))
    parsed = parse_js_output(lines)
    assert len(parsed["failures"]) == 30


def _run_planned_npm(command, package):
    """Run the planned npm command on this host (test helper only)."""
    if os.name == "nt":
        # npm ships as npm.cmd on Windows, which needs a shell; the
        # command is a fixed planner string, never user input.
        return subprocess.run(
            " ".join(command),
            cwd=package,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            shell=True,
        )
    return subprocess.run(
        [str(part) for part in command],
        cwd=package,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _write_node_package(root, *, passing):
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "calc",
                "version": "1.0.0",
                "scripts": {"test": "node --test"},
            }
        ),
        encoding="utf-8",
    )
    expected = "5" if passing else "6"
    (root / "calc.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('adds', () => { assert.strictEqual(2 + 3, " + expected + "); });\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    shutil.which("npm") is None or shutil.which("node") is None,
    reason="nodejs is not installed",
)
def test_real_npm_test_lifecycle_and_parsing(tmp_path):
    """Run the planned npm test command for real and parse its output."""
    package = tmp_path / "calc"
    package.mkdir()
    _write_node_package(package, passing=False)

    planned = js_test_command(["test"])
    assert planned["error"] is None
    command = planned["command"]
    assert command[0] == "npm"

    completed = _run_planned_npm(command, package)
    combined = ((completed.stdout or "") + "\n" + (completed.stderr or ""))
    parsed = parse_js_output(combined)
    assert completed.returncode != 0
    assert parsed["failures"], "a failing suite must parse to failures"

    _write_node_package(package, passing=True)
    completed = _run_planned_npm(command, package)
    combined = ((completed.stdout or "") + "\n" + (completed.stderr or ""))
    assert completed.returncode == 0
    assert parse_js_output(combined)["failures"] == []
