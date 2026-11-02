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


def test_project_dependencies_parsed(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "src/demo/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["project_dependencies"] == ["fastapi", "uvicorn"]
    assert profile["project_test_dependencies"] == ["pytest"]
    assert profile["project_dependencies_error"] is None


def test_all_recognized_extras_included_unrecognized_ignored(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": """\
[project]
name = "demo"
dependencies = []

[project.optional-dependencies]
test = ["pytest"]
tests = ["pytest-xdist"]
testing = ["tox"]
dev = ["ruff"]
docs = ["sphinx"]
""",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["project_dependencies"] == []
    assert profile["project_test_dependencies"] == [
        "pytest",
        "pytest-xdist",
        "tox",
        "ruff",
    ]
    assert profile["project_dependencies_error"] is None


def test_pyproject_without_project_table_has_no_dependencies(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n',
            "pkg/__init__.py": "",
            "pkg/a.py": "X = 1\n",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["project_dependencies"] is None
    assert profile["project_test_dependencies"] == []
    assert profile["project_dependencies_error"] is None


def test_pep517_build_backend_ignored_for_dependency_parsing(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
dependencies = ["requests>=2"]
""",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["project_dependencies"] == ["requests>=2"]
    assert profile["project_dependencies_error"] is None


def test_no_pyproject_has_no_dependencies(tmp_path):
    write_project(
        tmp_path,
        {
            "requirements.txt": "requests\n",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["project_dependencies"] is None
    assert profile["project_test_dependencies"] == []
    assert profile["project_dependencies_error"] is None


@pytest.mark.parametrize(
    ("pyproject", "fragment"),
    [
        ("[project\nname = oops", "could not be parsed"),
        ('[project]\nname = "demo"\ndependencies = "requests"\n', "must be a list"),
        (
            '[project]\nname = "demo"\ndependencies = ["ok", 42]\n',
            "dependencies[1]",
        ),
        (
            '[project]\nname = "demo"\ndependencies = ["", "ok"]\n',
            "dependencies[0]",
        ),
        (
            '[project]\nname = "demo"\ndependencies = ["-r evil.txt"]\n',
            "parsed as pip options",
        ),
        (
            '[project]\nname = "demo"\ndependencies = []\noptional-dependencies = ["x"]\n',
            "optional-dependencies must be a table",
        ),
        (
            '[project]\nname = "demo"\n[project.optional-dependencies]\ntest = "pytest"\n',
            "optional-dependencies.test must be a list",
        ),
        (
            '[project]\nname = "demo"\n[project.optional-dependencies]\ntest = ["-e ."]\n',
            "optional-dependencies.test[0]",
        ),
    ],
)
def test_malformed_dependency_metadata_is_loud_error(tmp_path, pyproject, fragment):
    write_project(
        tmp_path,
        {
            "pyproject.toml": pyproject,
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["project_dependencies"] is None
    assert profile["project_test_dependencies"] == []
    assert profile["project_dependencies_error"] is not None
    assert fragment in profile["project_dependencies_error"]


def test_requirements_marker_detected(tmp_path):
    write_project(
        tmp_path,
        {
            "requirements-test.txt": "pytest\n",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["dependency_files"] == ["requirements-test.txt"]
    assert profile["dependency_format"] == "requirements"
    assert profile["requirements_error"] is None


def test_requirements_plain_specs_accepted(tmp_path):
    write_project(
        tmp_path,
        {
            "requirements.txt": (
                "# pinned web stack\n"
                "requests>=2,<3\n"
                "uvicorn[standard]>=0.30\n"
                'importlib-metadata>=6; python_version < "3.12"\n'
                "PyJWT  # inline comment\n"
                "\n"
                "sqlalchemy>=2.0,\\\n"
                "<2.1\n"
            ),
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    assert profile_of(tmp_path)["requirements_error"] is None


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("-r other.txt\n", "starting with '-'"),
        ("-e .\n", "starting with '-'"),
        ("-e git+https://example.com/x.git\n", "starting with '-'"),
        ("--index-url https://evil.example/simple\n", "starting with '-'"),
        ("-i https://evil.example/simple\n", "starting with '-'"),
        ("--extra-index-url https://evil.example/simple\n", "starting with '-'"),
        ("--trusted-host evil.example\n", "starting with '-'"),
        ("--pre\nrequests\n", "starting with '-'"),
        ("pkg @ https://example.com/pkg.zip\n", "direct URL"),
        ("https://example.com/pkg.zip\n", "direct URL"),
        ("git+https://example.com/repo.git\n", "direct URL"),
        ("Git+https://example.com/repo.git\n", "direct URL"),
        ("git+ssh://git@example.com/repo.git\n", "direct URL"),
        ("git+git@example.com:owner/repo.git\n", "VCS"),
        ("./local-pkg\n", "local-path"),
        ("../sibling\n", "local-path"),
        ("/abs/path\n", "local-path"),
        ("file:pkg\n", "VCS and local-path"),
        ("not a requirement!!!\n", "not a plain package requirement"),
    ],
)
def test_requirements_hostile_lines_rejected(tmp_path, content, fragment):
    write_project(
        tmp_path,
        {
            "requirements.txt": f"requests\n{content}",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    error = profile_of(tmp_path)["requirements_error"]

    assert error is not None
    assert "requirements.txt line 2" in error
    assert fragment in error
    assert "evil.example" not in error


@pytest.mark.parametrize(
    ("content", "lineno", "fragment"),
    [
        (
            "requests>=2 \\\n--index-url https://evil.example/simple\n",
            3,
            "starting with '-'",
        ),
        (
            "# pinned \\\nhttps://evil.example/pkg.zip\n",
            3,
            "direct URL",
        ),
        (
            "requests\n-e git+https://evil.example/x.git#egg=y\n",
            3,
            "starting with '-'",
        ),
    ],
)
def test_requirements_continuation_smuggling_rejected(
    tmp_path, content, lineno, fragment
):
    write_project(
        tmp_path,
        {
            "requirements.txt": f"requests\n{content}",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    error = profile_of(tmp_path)["requirements_error"]

    assert error is not None
    assert f"requirements.txt line {lineno}" in error
    assert fragment in error
    assert "evil.example" not in error


def test_requirements_oversized_rejected_not_truncated(tmp_path, monkeypatch):
    import app.languages.python as detect

    monkeypatch.setattr(detect, "MAX_DEP_SCAN_BYTES", 16)
    write_project(
        tmp_path,
        {
            "requirements.txt": "requests\n--index-url https://evil.example/simple\n",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    error = profile_of(tmp_path)["requirements_error"]

    assert error is not None
    assert "larger than" in error


def test_capabilities_many_scripts_no_manifest_no_tests(tmp_path):
    """Mini-Python-Projects shape: inspectable, never runnable."""
    write_project(
        tmp_path,
        {
            "games/guess.py": "X = 1\n",
            "tools/calc.py": "Y = 2\n",
            "misc/notes.py": "Z = 3\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["capabilities"] == {
        "languages": {
            "python": {"files": 3},
            "javascript": {"files": 0, "manifest": None},
        },
        "dependencies": {
            "python": {"status": "none", "files": [], "error": None},
            "javascript": {"status": "none", "files": []},
        },
        "test_frameworks": [],
        "static": {"available": True},
        "runnable": {"python": False, "reason": "unsupported_project"},
    }
    assert profile["supported"] is False


def test_capabilities_requirements_without_tests(tmp_path):
    """cv-evaluator shape: supported project, runnable blocked on tests."""
    write_project(
        tmp_path,
        {
            "requirements.txt": "fastapi\nhttpx\n",
            "app/main.py": "X = 1\n",
            "app/__init__.py": "",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["capabilities"]["dependencies"]["python"]["status"] == (
        "requirements"
    )
    assert profile["capabilities"]["test_frameworks"] == []
    assert profile["capabilities"]["static"] == {"available": True}
    assert profile["capabilities"]["runnable"] == {
        "python": False,
        "reason": "unsupported_framework",
    }
    assert profile["supported"] is True


def test_capabilities_javascript_inspection_only(tmp_path):
    """project1 shape: JS detected, inspection available, no execution."""
    write_project(
        tmp_path,
        {
            "package.json": '{"name": "demo"}\n',
            "server.js": "console.log(1)\n",
            "public/app.ts": "const x = 1;\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["capabilities"]["languages"]["javascript"] == {
        "files": 2,
        "manifest": "package.json",
    }
    assert profile["capabilities"]["dependencies"]["javascript"] == {
        "status": "manifest",
        "files": ["package.json"],
    }
    assert profile["capabilities"]["static"] == {"available": True}
    assert profile["capabilities"]["runnable"] == {
        "python": False,
        "reason": "unsupported_project",
    }
    assert profile["primary_language"] == "javascript"


def test_capabilities_runnable_pytest_project(tmp_path):
    """starter-repo shape: full test-backed execution retained."""
    write_project(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT,
            "src/demo/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["capabilities"]["test_frameworks"] == ["pytest"]
    assert profile["capabilities"]["static"] == {"available": True}
    assert profile["capabilities"]["runnable"] == {"python": True, "reason": None}
    assert profile["supported"] is True


def test_capabilities_invalid_dependencies_block_runnable(tmp_path):
    write_project(
        tmp_path,
        {
            "requirements.txt": "requests\n--index-url https://evil.example/simple\n",
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    profile = profile_of(tmp_path)

    assert profile["capabilities"]["dependencies"]["python"]["status"] == "invalid"
    assert profile["capabilities"]["runnable"] == {
        "python": False,
        "reason": "invalid_dependencies",
    }


def test_capabilities_empty_repo_has_no_static(tmp_path):
    profile = profile_of(tmp_path)

    assert profile["capabilities"]["static"] == {"available": False}
    assert profile["capabilities"]["runnable"]["python"] is False


def test_malformed_entry_value_never_echoed(tmp_path):
    write_project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "demo"\ndependencies = ["-r https://u:p@h/x"]\n',
            "pkg/__init__.py": "",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        },
    )

    error = profile_of(tmp_path)["project_dependencies_error"]

    assert error is not None
    assert "u:p@h" not in error
