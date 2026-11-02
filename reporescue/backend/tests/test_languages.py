"""Acceptance tests for language adapters (Phase 5A Step 4).

Adapters are read-only detection: no installs, no execution, no Docker.
The JavaScript adapter is inspection-only and must never report runtime
or test/verification capability.
"""

import json
import sys

from app.languages import detect_all
from app.languages import javascript as js_adapter
from app.languages import python as py_adapter
from app.languages.base import AdapterResult
from app.python_detect import build_profile

FAKE_SHA = "e" * 40

# project1 shape: JavaScript-only repository with test tooling present.
JS_PROJECT = {
    "package.json": json.dumps(
        {
            "name": "widget",
            "version": "1.0.0",
            "type": "module",
            "scripts": {"test": "jest", "start": "node index.js"},
            "dependencies": {"commander": "^12.0.0"},
            "devDependencies": {"jest": "^29.0.0"},
        }
    ),
    "index.js": "export function add(a, b) {\n  return a + b;\n}\n",
    "src/util.js": "export const X = 1;\n",
}

PYTHON_PROJECT = {
    "pyproject.toml": '[project]\nname = "calc"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n',
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": (
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}

BARE_PYTHON = {
    "app.py": "print('hi')\n",
    "lib.py": "X = 1\n",
    "tests/helper.py": "X = 1\n",
}


def _write(tmp_path, files):
    project = tmp_path / "project"
    for rel, content in files.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


def test_python_adapter_exposes_contract(tmp_path):
    result = py_adapter.detect(_write(tmp_path, PYTHON_PROJECT))
    assert isinstance(result, AdapterResult)
    assert result.language == "python"
    assert result.file_count == 3
    assert result.manifests == ("pyproject.toml",)
    assert result.project == {"name": "calc", "version": "0.1.0"}
    assert result.dependencies["status"] == "pyproject"
    assert result.test_frameworks == ("pytest",)
    assert result.static_available is True
    assert result.runtime_available is True
    assert result.runtime_reason is None
    assert result.details["test_framework"] == "pytest"
    assert result.details["is_python"] is True


def test_javascript_adapter_detects_project1_shape(tmp_path):
    result = js_adapter.detect(_write(tmp_path, JS_PROJECT))
    assert isinstance(result, AdapterResult)
    assert result.language == "javascript"
    assert result.file_count == 2
    assert result.manifests == ("package.json",)
    assert result.project == {"name": "widget", "version": "1.0.0", "type": "module"}
    assert result.dependencies == {"status": "manifest", "files": ["package.json"]}
    assert result.test_frameworks == ()
    assert result.static_available is True
    assert result.runtime_available is False
    assert result.runtime_reason == "execution_not_supported"
    assert any("inspection-only" in item for item in result.limitations)
    assert result.details["dependencies"] == {"commander": "^12.0.0"}
    assert result.details["dev_dependencies"] == {"jest": "^29.0.0"}
    assert result.details["scripts"] == ["start", "test"]
    assert result.details["test_signals"] == ["jest"]
    assert result.details["has_test_script"] is True


def test_javascript_never_reports_runtime_or_verification(tmp_path):
    # Even with test tooling installed and a test script, execution stays
    # unavailable: detection must never imply verification capability.
    result = js_adapter.detect(_write(tmp_path, JS_PROJECT))
    assert result.runtime_available is False
    assert result.test_frameworks == ()
    assert any("cannot be executed" in item for item in result.limitations)


def test_mixed_repo_reports_both_languages(tmp_path):
    merged = dict(PYTHON_PROJECT)
    merged.update({f"web/{rel}": content for rel, content in JS_PROJECT.items()})
    results = detect_all(_write(tmp_path, merged))
    assert set(results) == {"python", "javascript"}
    assert results["python"].file_count == 3
    assert results["python"].runtime_available is True
    assert results["javascript"].file_count == 2
    assert results["javascript"].static_available is True
    assert results["javascript"].runtime_available is False


def test_missing_manifests_and_frameworks_keep_static_inspection(tmp_path):
    results = detect_all(_write(tmp_path, BARE_PYTHON))
    py = results["python"]
    assert py.manifests == ()
    assert py.test_frameworks == ()
    assert py.dependencies["status"] == "none"
    assert py.static_available is True
    assert py.runtime_available is False
    assert py.runtime_reason == "unsupported_framework"


def test_malformed_package_json_reported_honestly(tmp_path):
    files = dict(JS_PROJECT)
    files["package.json"] = "{not json"
    result = js_adapter.detect(_write(tmp_path, files))
    assert result.dependencies["status"] == "invalid"
    assert result.details["manifest_error"] is not None
    assert result.static_available is True
    assert result.runtime_available is False


def test_profile_capabilities_come_from_adapters(tmp_path):
    root = _write(tmp_path, JS_PROJECT)
    profile = build_profile(
        root, url="https://github.com/acme/widget", name="acme/widget",
        commit_sha=FAKE_SHA,
    )
    capabilities = profile["capabilities"]
    assert capabilities["languages"]["javascript"] == {"files": 2, "manifest": "package.json"}
    assert capabilities["languages"]["python"] == {"files": 0}
    assert capabilities["dependencies"]["javascript"] == {
        "status": "manifest",
        "files": ["package.json"],
    }
    assert capabilities["static"] == {"available": True}
    assert capabilities["runnable"] == {"python": False, "reason": "unsupported_project"}
    assert profile["primary_language"] == "javascript"


def test_adapters_import_no_execution_machinery():
    for module in (py_adapter, js_adapter):
        namespace = vars(module)
        assert "select_backend" not in namespace
        assert "DockerBackend" not in namespace
        assert "clone_and_pin" not in namespace
        assert "subprocess" not in namespace
        assert "plan_install" not in namespace
    # Adapters must not pull the execution backend in as a side effect.
    assert "app.languages.python" in sys.modules
    assert "app.languages.javascript" in sys.modules
    for name, module in sys.modules.items():
        if name in ("app.languages.python", "app.languages.javascript"):
            for obj in vars(module).values():
                assert getattr(obj, "__name__", "") != "ExecutionBackend"
