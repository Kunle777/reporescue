"""Tests for the generalized multi-file patch guard (real repositories).

The fixture-only guard keeps its own tests in test_patch_guard.py; these
cover traversal, symlink escapes, protected tests/configs, ambiguity, and
all-or-nothing application.
"""

import pytest

from app.patch_guard import (
    FileEdit,
    PatchRejected,
    PatchSet,
    validate_and_apply_patchset,
)

CALC_SOURCE = "def add(a, b):\n    return a - b\n"
OTHER_SOURCE = "VALUE = 1\n"
TEST_SOURCE = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def workspace(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "calc.py").write_text(CALC_SOURCE, encoding="utf-8")
    (pkg / "other.py").write_text(OTHER_SOURCE, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(TEST_SOURCE, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return tmp_path


def patchset(*edits, reason="fix"):
    return PatchSet(edits=tuple(edits), reason=reason)


def test_valid_single_edit_applied(workspace):
    applied = validate_and_apply_patchset(
        workspace,
        patchset(FileEdit("pkg/calc.py", "    return a - b", "    return a + b")),
    )

    assert applied == [{"file": "pkg/calc.py", "changed": "true"}]
    assert "return a + b" in (workspace / "pkg" / "calc.py").read_text(encoding="utf-8")


def test_valid_multi_file_patchset_applied(workspace):
    validate_and_apply_patchset(
        workspace,
        patchset(
            FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),
            FileEdit("pkg/other.py", "VALUE = 1", "VALUE = 2"),
        ),
    )

    assert "return a + b" in (workspace / "pkg" / "calc.py").read_text(encoding="utf-8")
    assert "VALUE = 2" in (workspace / "pkg" / "other.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    [
        "pkg/../../evil.py",
        "../evil.py",
        "/tmp/evil.py",
        "C:/evil.py",
        "pkg/calc.py/../../evil.py",
        "",
        "   ",
    ],
)
def test_rejects_escape_paths(workspace, path):
    with pytest.raises(PatchRejected, match="outside the repository|empty file path"):
        validate_and_apply_patchset(workspace, patchset(FileEdit(path, "a", "b")))
    assert (workspace / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_SOURCE


def test_rejects_symlink_target(workspace):
    outside = workspace / "outside.py"
    outside.write_text("OUTSIDE = 1\n", encoding="utf-8")
    link = workspace / "pkg" / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks require elevated privileges on this platform")

    with pytest.raises(PatchRejected, match="symlink"):
        validate_and_apply_patchset(
            workspace, patchset(FileEdit("pkg/linked.py", "OUTSIDE = 1", "OUTSIDE = 2"))
        )
    assert outside.read_text(encoding="utf-8") == "OUTSIDE = 1\n"


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_calc.py",
        "pkg/test_helper.py",
        "pkg/calc_test.py",
        "pkg/conftest.py",
        "test/test_calc.py",
    ],
)
def test_rejects_test_suite_modifications(workspace, path):
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("assert add(2, 3) == 5\n", encoding="utf-8")

    with pytest.raises(PatchRejected, match="protected test suite"):
        validate_and_apply_patchset(
            workspace, patchset(FileEdit(path, "assert add(2, 3) == 5", "assert True"))
        )
    assert target.read_text(encoding="utf-8") == "assert add(2, 3) == 5\n"


@pytest.mark.parametrize("path", ["pyproject.toml", "setup.py", "tox.ini", "Pipfile"])
def test_rejects_config_modifications(workspace, path):
    target = workspace / path
    target.write_text("content\n", encoding="utf-8")

    with pytest.raises(PatchRejected, match="not a Python source file|protected configuration"):
        validate_and_apply_patchset(workspace, patchset(FileEdit(path, "content", "changed")))


def test_rejects_non_python_files(workspace):
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")

    with pytest.raises(PatchRejected, match="not a Python source file"):
        validate_and_apply_patchset(workspace, patchset(FileEdit("README.md", "hello", "bye")))


def test_rejects_excluded_directories(workspace):
    target = workspace / ".git" / "hook.py"
    target.parent.mkdir(parents=True)
    target.write_text("X = 1\n", encoding="utf-8")

    with pytest.raises(PatchRejected, match="excluded directory"):
        validate_and_apply_patchset(workspace, patchset(FileEdit(".git/hook.py", "X = 1", "X = 2")))


def test_rejects_ambiguous_before_block(workspace):
    (workspace / "pkg" / "dup.py").write_text("X = 1\nX = 1\n", encoding="utf-8")

    with pytest.raises(PatchRejected, match="occurs 2 times"):
        validate_and_apply_patchset(workspace, patchset(FileEdit("pkg/dup.py", "X = 1", "X = 2")))


def test_rejects_missing_block(workspace):
    with pytest.raises(PatchRejected, match="not found"):
        validate_and_apply_patchset(
            workspace, patchset(FileEdit("pkg/calc.py", "    return a * b", "    return a + b"))
        )


def test_rejects_noop_edit(workspace):
    with pytest.raises(PatchRejected, match="makes no change"):
        validate_and_apply_patchset(
            workspace, patchset(FileEdit("pkg/calc.py", "    return a - b", "    return a - b"))
        )


def test_rejects_duplicate_file_edits(workspace):
    with pytest.raises(PatchRejected, match="twice"):
        validate_and_apply_patchset(
            workspace,
            patchset(
                FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),
                FileEdit("pkg/calc.py", "def add", "def add"),
            ),
        )


def test_rejects_too_many_files(workspace):
    edits = [FileEdit(f"pkg/f{i}.py", "a", "b") for i in range(6)]

    with pytest.raises(PatchRejected, match="maximum"):
        validate_and_apply_patchset(workspace, patchset(*edits))


def test_rejects_oversize_patchset(workspace):
    with pytest.raises(PatchRejected, match="maximum patch size"):
        validate_and_apply_patchset(
            workspace, patchset(FileEdit("pkg/calc.py", "    return a - b", "x" * 9000))
        )
    assert (workspace / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_SOURCE


def test_rejects_empty_patchset(workspace):
    with pytest.raises(PatchRejected, match="no edits"):
        validate_and_apply_patchset(workspace, patchset())


def test_rejects_missing_target(workspace):
    with pytest.raises(PatchRejected, match="does not exist"):
        validate_and_apply_patchset(
            workspace, patchset(FileEdit("pkg/missing.py", "a", "b"))
        )


def test_application_is_all_or_nothing(workspace):
    with pytest.raises(PatchRejected):
        validate_and_apply_patchset(
            workspace,
            patchset(
                FileEdit("pkg/calc.py", "    return a - b", "    return a + b"),
                FileEdit("pkg/missing.py", "a", "b"),
            ),
        )

    assert (workspace / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_SOURCE


def test_patchset_serializes(workspace):
    parsed = patchset(
        FileEdit("pkg/calc.py", "b", "a"), reason="why"
    ).to_dict()
    assert parsed == {
        "edits": [{"file": "pkg/calc.py", "before": "b", "after": "a"}],
        "reason": "why",
    }
