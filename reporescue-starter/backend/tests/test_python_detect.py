"""Tests for Python project detection and repository profiling."""

import pytest

from app.python_detect import build_profile

FAKE_SHA = "f" * 40


def write_project(root, files):
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def profile_of(root, **overrides):
    kwargs = {
        "url": "https://github.com/acme/demo",
        "name": "acme/demo",
        "commit_sha": FAKE_SHA,
    }
    kwargs.update(overrides)
    return build_profile(root, **kwargs)


PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastapi", "uvicorn"]

[project.optional-dependencies]
test = ["pytest"]
"""


def test_pyproject_detection(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "src/demo/__init__.py": "",
            "src/demo/app.py": "VALUE = 1\n",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["supported"] is True
    assert profile["is_python_project"] is True
    assert profile["primary_language"] == "python"
    assert profile["dependency_format"] == "pyproject"
    assert profile["dependency_files"] == ["pyproject.toml"]
    assert profile["python_requires"] == ">=3.11"
    assert "fastapi" in profile["frameworks"]
    assert "pytest" in profile["frameworks"]
    assert profile["test_framework"] == "pytest"
    assert profile["tests"]["count"] == 1
    assert profile["tests"]["files"] == ["tests/test_app.py"]
    assert profile["commit_sha"] == FAKE_SHA
    assert profile["url"] == "https://github.com/acme/demo"
    assert profile["name"] == "acme/demo"


@pytest.mark.parametrize(
    ("marker", "content", "expected_format"),
    [
        ("requirements.txt", "flask==3.0.0\npytest\n", "requirements"),
        ("requirements-dev.txt", "pytest\n", "requirements"),
        ("setup.py", 'from setuptools import setup\nsetup(name="x", python_requires=">=3.10")\n', "setup.py"),
        ("setup.cfg", "[options]\npython_requires = >=3.9\n", "setup.cfg"),
        ("Pipfile", '[packages]\ndjango = "*"\n', "pipfile"),
        ("tox.ini", "[tox]\nenvlist = py311\n[testenv]\ncommands = pytest\n", "tox"),
        ("pytest.ini", "[pytest]\ntestpaths = tests\n", "pytest"),
    ],
)
def test_dependency_formats_detected(tmp_path, marker, content, expected_format):
    write_project(
        tmp_path,
        {
            marker: content,
            "pkg/__init__.py": "",
            "pkg/core.py": "X = 1\n",
            "tests/test_core.py": "def test_x():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["supported"] is True
    assert profile["dependency_format"] == expected_format
    assert marker in profile["dependency_files"]
    if marker == "setup.py":
        assert profile["python_requires"] == ">=3.10"
    if marker == "setup.cfg":
        assert profile["python_requires"] == ">=3.9"
    if marker == "Pipfile":
        assert "django" in profile["frameworks"]
    if marker == "tox.ini":
        assert profile["test_framework"] == "pytest"


def test_multiple_formats_flagged(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "requirements.txt": "requests\n",
            "pkg/__init__.py": "",
            "pkg/core.py": "X = 1\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["dependency_format"] == "multiple"
    assert any("Multiple dependency formats" in warning for warning in profile["warnings"])
    assert profile["supported"] is True


def test_unittest_project_detected(tmp_path):
    write_project(
        tmp_path,
        {
            "setup.py": 'from setuptools import setup\nsetup(name="x")\n',
            "pkg/__init__.py": "",
            "pkg/core.py": "X = 1\n",
            "tests/suite.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["supported"] is True
    assert profile["test_framework"] == "unittest"
    assert profile["tests"]["count"] == 1


def test_project_without_tests_warns(tmp_path):
    write_project(
        tmp_path,
        {
            "requirements.txt": "requests\n",
            "pkg/__init__.py": "",
            "pkg/core.py": "X = 1\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["supported"] is True
    assert profile["test_framework"] == "none"
    assert profile["tests"] == {"dirs": [], "files": [], "count": 0}
    assert any("No test files" in warning for warning in profile["warnings"])


def test_non_python_repo_not_supported(tmp_path):
    write_project(
        tmp_path,
        {
            "package.json": '{"name": "web", "dependencies": {"react": "^18.0.0"}}\n',
            "src/index.js": "console.log('hi');\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["supported"] is False
    assert profile["is_python_project"] is False
    assert profile["primary_language"] == "javascript"
    assert profile["dependency_format"] == "none"
    assert any("No Python" in warning for warning in profile["warnings"])


@pytest.mark.parametrize(
    "files",
    [
        {"lonely.py": "X = 1\n"},
        {"lonely.py": "X = 1\n", "README.md": "hello\n"},
    ],
)
def test_single_python_file_is_not_supported(tmp_path, files):
    write_project(tmp_path, files)

    profile = profile_of(tmp_path)

    assert profile["supported"] is False
    assert any("single Python file" in warning for warning in profile["warnings"])


def test_empty_repo_not_supported(tmp_path):
    profile = profile_of(tmp_path)

    assert profile["supported"] is False
    assert profile["primary_language"] == "unknown"
    assert profile["dependency_format"] == "none"


def test_structure_summary_describes_project(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "src/demo/__init__.py": "",
            "src/demo/app.py": "VALUE = 1\n",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    summary = profile_of(tmp_path)["structure_summary"]

    assert "Python project" in summary
    assert "pyproject" in summary
    assert "pytest" in summary


def test_venv_and_caches_ignored_for_counts(tmp_path):
    write_project(
        tmp_path,
        {
            "requirements.txt": "requests\n",
            "pkg/__init__.py": "",
            "pkg/core.py": "X = 1\n",
            "venv/lib/noise.py": "X = 1\n",
            "__pycache__/noise.py": "X = 1\n",
            ".pytest_cache/noise.py": "X = 1\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["python_file_count"] == 2
    assert profile["source_dirs"] == ["pkg"]
    assert profile["package_dirs"] == ["pkg"]


def test_ref_recorded_in_profile(tmp_path):
    write_project(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "X=1\n", "pkg/b.py": "Y=2\n"})

    assert profile_of(tmp_path, ref="v1.0.0")["ref"] == "v1.0.0"
    assert profile_of(tmp_path)["ref"] is None
