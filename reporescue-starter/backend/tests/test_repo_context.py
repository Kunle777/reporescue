"""Tests for safe file inventory and bounded context extraction."""

import pytest

from app import repo_context
from app.repo_context import (
    _is_within_root,
    classify_file,
    extract_context,
    inventory_repository,
    render_ai_context,
)


def write_files(root, files):
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return root


BASE_PROJECT = {
    "pyproject.toml": "[project]\nname='x'\n",
    "src/app.py": "VALUE = 1\n",
    "src/main.py": "print('hi')\n",
    "tests/test_app.py": "def test_ok():\n    assert True\n",
    "README.md": "# demo\n",
}


def test_inventory_lists_project_files(tmp_path):
    write_files(tmp_path, BASE_PROJECT)

    inventory = inventory_repository(tmp_path)

    paths = {item["path"] for item in inventory["files"]}
    assert {"pyproject.toml", "src/app.py", "src/main.py", "tests/test_app.py", "README.md"} <= paths
    assert inventory["truncated"] is False


@pytest.mark.parametrize(
    "junk",
    [
        ".git/objects/ab/cd",
        "__pycache__/app.pyc",
        ".pytest_cache/CACHEDIR.TAG",
        ".venv/lib/x.py",
        "venv/lib/x.py",
        "env/lib/x.py",
        "node_modules/dep/index.js",
        "build/out.js",
        "dist/out.js",
        "demo.egg-info/PKG-INFO",
        ".tox/py311/x.py",
    ],
)
def test_inventory_excludes_irrelevant_dirs(tmp_path, junk):
    write_files(tmp_path, {**BASE_PROJECT, junk: "junk\n"})

    inventory = inventory_repository(tmp_path)

    paths = {item["path"] for item in inventory["files"]}
    assert junk not in paths
    assert "src/app.py" in paths


def test_gitignore_patterns_respected(tmp_path):
    write_files(
        tmp_path,
        {
            **BASE_PROJECT,
            ".gitignore": "*.log\nsecret_dir/\n",
            "debug.log": "noise\n",
            "secret_dir/hidden.py": "X = 1\n",
            "keep.py": "Y = 2\n",
        },
    )

    inventory = inventory_repository(tmp_path)

    paths = {item["path"] for item in inventory["files"]}
    assert "debug.log" not in paths
    assert "secret_dir/hidden.py" not in paths
    assert "keep.py" in paths


def test_binary_files_excluded_from_excerpts(tmp_path):
    write_files(tmp_path, {**BASE_PROJECT, "data/blob.bin": b"\x00\x01\x02binary"})

    inventory = inventory_repository(tmp_path)
    context = extract_context(tmp_path, inventory)

    assert "data/blob.bin" not in context["excerpts"]
    assert "data/blob.bin" in context["unreadable"]


def test_oversize_files_excluded_from_excerpts(tmp_path):
    write_files(tmp_path, {**BASE_PROJECT, "src/huge.py": "x = 1\n" * 50_000})

    inventory = inventory_repository(tmp_path)
    context = extract_context(tmp_path, inventory)

    assert "src/huge.py" not in context["excerpts"]
    assert "src/huge.py" in context["unreadable"]
    assert any("oversized" in warning for warning in context["warnings"])


def test_secret_files_never_read(tmp_path):
    marker = "AKIA-SECRET-MARKER-12345"
    write_files(
        tmp_path,
        {
            **BASE_PROJECT,
            ".env": f"META_API_KEY={marker}\n",
            "deploy.pem": "private-key-bytes\n",
        },
    )

    inventory = inventory_repository(tmp_path)
    context = extract_context(tmp_path, inventory)

    assert ".env" not in context["excerpts"]
    assert "deploy.pem" not in context["excerpts"]
    rendered = render_ai_context(context)
    assert marker not in rendered
    assert "private-key-bytes" not in rendered


def test_symlinks_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("OUTSIDE-CONTENT-MARKER", encoding="utf-8")
    repo = tmp_path / "repo"
    write_files(repo, BASE_PROJECT)
    link = repo / "evil-link.txt"
    target = repo / "src" / "inner-link.py"
    try:
        link.symlink_to(secret)
        target.symlink_to(repo / "src" / "app.py")
    except OSError:
        pytest.skip("symlinks require elevated privileges on this platform")

    inventory = inventory_repository(repo)
    context = extract_context(repo, inventory)

    assert inventory["skipped"].get("symlink") == 2
    assert "evil-link.txt" not in context["excerpts"]
    assert "OUTSIDE-CONTENT-MARKER" not in render_ai_context(context)


def test_paths_cannot_escape_root(tmp_path):
    assert _is_within_root(tmp_path, tmp_path / "src" / "app.py") is True
    assert _is_within_root(tmp_path, tmp_path / ".." / "escape.py") is False


def test_inventory_paths_all_within_root(tmp_path):
    write_files(tmp_path, BASE_PROJECT)

    inventory = inventory_repository(tmp_path)

    for item in inventory["files"]:
        assert ".." not in item["path"].split("/")
        assert _is_within_root(tmp_path, tmp_path / item["path"])


def test_inventory_truncation_is_reported(tmp_path, monkeypatch):
    write_files(tmp_path, BASE_PROJECT)
    monkeypatch.setattr(repo_context, "MAX_INVENTORY_FILES", 2)

    inventory = inventory_repository(tmp_path)

    assert inventory["truncated"] is True
    assert any("truncated" in warning.lower() for warning in inventory["warnings"])


def test_classification():
    assert classify_file("pyproject.toml") == "config"
    assert classify_file("requirements.txt") == "config"
    assert classify_file("src/main.py") == "entry"
    assert classify_file("manage.py") == "entry"
    assert classify_file("tests/test_app.py") == "test"
    assert classify_file("src/app_test.py") == "test"
    assert classify_file("src/conftest.py") == "test"
    assert classify_file("tests/testserver/server.py") == "test"
    assert classify_file("src/app.py") == "source"
    assert classify_file("app.py") == "entry"
    assert classify_file("README.md") == "doc"
    assert classify_file("notes.txt") == "doc"


def test_extract_context_prioritizes_entries_and_configs(tmp_path):
    write_files(tmp_path, BASE_PROJECT)

    inventory = inventory_repository(tmp_path)
    context = extract_context(tmp_path, inventory)

    assert context["entry_points"] == ["src/main.py"]
    assert "pyproject.toml" in context["configs"]
    assert context["tests"] == ["tests/test_app.py"]
    assert set(context["excerpt_files"]) >= {"src/main.py", "pyproject.toml"}
    assert context["excerpts"]["src/app.py"] == "VALUE = 1\n"


def test_excerpt_budgets_enforced(tmp_path):
    files = dict(BASE_PROJECT)
    for index in range(70):
        files[f"src/mod_{index:02d}.py"] = f"VALUE_{index} = {index}\n"
    write_files(tmp_path, files)

    inventory = inventory_repository(tmp_path)
    context = extract_context(tmp_path, inventory)

    assert len(context["excerpt_files"]) <= repo_context.MAX_EXCERPT_FILES
    assert context["excerpt_bytes"] <= repo_context.MAX_TOTAL_EXCERPT_BYTES
    assert any("truncated" in warning.lower() for warning in context["warnings"])


def test_rendered_ai_context_is_bounded(tmp_path):
    files = dict(BASE_PROJECT)
    for index in range(30):
        files[f"src/mod_{index:02d}.py"] = "PAYLOAD = 'x'\n" * 200
    write_files(tmp_path, files)

    inventory = inventory_repository(tmp_path)
    context = extract_context(tmp_path, inventory)
    rendered = render_ai_context(context)

    assert len(rendered) <= repo_context.MAX_AI_CONTEXT_CHARS + 200
    assert "truncated" in rendered
