"""Tests for secure snapshot staging and archival (Phase 5B).

The snapshot builder is the trust boundary for cloud execution: only the
staged tree (plus its archive) may leave the worker. These tests prove
secrets, history, environments, symlinks, and oversized content never
enter, and that identity/manifest files are worker-controlled.
"""

import gzip
import hashlib
import json
import os
import tarfile

import pytest

from app import snapshot as snapshot_module
from app.snapshot import (
    COMMIT_FILENAME,
    MANIFEST_FILENAME,
    SnapshotError,
    build_archive,
    build_snapshot,
    create_staging_parent,
    remove_staging,
)


def _write(root, rel, content="x = 1\n"):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _try_symlink(source, link):
    try:
        os.symlink(source, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted on this machine")


def test_basic_staging_manifest_and_identity(tmp_path):
    src = tmp_path / "src"
    _write(src, "pkg/__init__.py", "")
    _write(src, "pkg/app.py", "print('hi')\n")
    _write(src, "tests/test_app.py", "def test_x():\n    assert True\n")

    staging_parent = create_staging_parent()
    try:
        manifest = build_snapshot(
            src, staging_parent / "tree", commit_sha="a" * 40
        )
        assert manifest.file_count == 3
        assert manifest.commit_sha == "a" * 40
        assert manifest.total_bytes > 0
        assert manifest.excluded == {}
        staged = staging_parent / "tree"
        assert (staged / "pkg" / "app.py").read_text() == "print('hi')\n"
        assert (staged / COMMIT_FILENAME).read_text().strip() == "a" * 40
        on_disk = json.loads((staged / MANIFEST_FILENAME).read_text())
        assert on_disk["file_count"] == 3
        assert on_disk["commit_sha"] == "a" * 40
        recorded = {entry["path"] for entry in on_disk["files"]}
        assert recorded == {"pkg/__init__.py", "pkg/app.py", "tests/test_app.py"}
        for entry in on_disk["files"]:
            digest = hashlib.sha256(
                (staged / entry["path"]).read_bytes()
            ).hexdigest()
            assert entry["sha256"] == digest
    finally:
        remove_staging(staging_parent)


def test_vcs_and_environment_dirs_excluded(tmp_path):
    src = tmp_path / "src"
    _write(src, "pkg/app.py")
    _write(src, ".git/objects/abc", "history")
    _write(src, ".execvenv/bin/python", "binary")
    _write(src, "node_modules/dep/index.js", "dep")
    _write(src, "venv/bin/python", "binary")
    _write(src, "__pycache__/app.pyc", "bytecode")
    _write(src, "frontend/.expo/web/cache/production/images/icon.png", "cached")

    staging_parent = create_staging_parent()
    try:
        manifest = build_snapshot(src, staging_parent / "tree")
        assert manifest.file_count == 1
        assert manifest.excluded.get("excluded_dir", 0) >= 5
        staged = staging_parent / "tree"
        assert not (staged / ".git").exists()
        assert not (staged / ".execvenv").exists()
        assert not (staged / "node_modules").exists()
        assert not (staged / "venv").exists()
        assert not (staged / "__pycache__").exists()
        assert not (staged / "frontend" / ".expo").exists()
    finally:
        remove_staging(staging_parent)


def test_secret_files_excluded_and_never_copied(tmp_path):
    src = tmp_path / "src"
    _write(src, "pkg/app.py")
    secrets = [
        ".env",
        ".env.local",
        ".env.development",
        "config/.env.production",
        "credentials.json",
        "secrets.yaml",
        "deploy/key.pem",
        "deploy/id_rsa",
        "notes/private_key.txt",
        "db.p12",
    ]
    for rel in secrets:
        _write(src, rel, "SUPER-SECRET-VALUE")

    staging_parent = create_staging_parent()
    try:
        manifest = build_snapshot(src, staging_parent / "tree")
        assert manifest.file_count == 1
        assert manifest.excluded.get("secret", 0) == len(secrets)
        staged = staging_parent / "tree"
        blob = b"".join(
            path.read_bytes()
            for path in staged.rglob("*")
            if path.is_file()
        )
        assert b"SUPER-SECRET-VALUE" not in blob
    finally:
        remove_staging(staging_parent)


def test_symlinks_never_followed_or_copied(tmp_path):
    src = tmp_path / "src"
    _write(src, "pkg/app.py")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE", encoding="utf-8")
    _try_symlink(str(outside), str(src / "evil.txt"))
    (src / "realdir").mkdir()
    _write(src, "realdir/keep.py")
    _try_symlink(str(src / "realdir"), str(src / "dirlink"))

    staging_parent = create_staging_parent()
    try:
        manifest = build_snapshot(src, staging_parent / "tree")
        assert manifest.file_count == 2
        assert manifest.excluded.get("symlink", 0) >= 1
        staged = staging_parent / "tree"
        assert not (staged / "evil.txt").exists()
        assert not (staged / "dirlink").exists()
    finally:
        remove_staging(staging_parent)


def test_reserved_names_cannot_spoof_identity(tmp_path):
    src = tmp_path / "src"
    _write(src, COMMIT_FILENAME, "evil-sha\n")
    _write(src, MANIFEST_FILENAME, '{"forged": true}\n')
    _write(src, "pkg/app.py")

    staging_parent = create_staging_parent()
    try:
        manifest = build_snapshot(
            src, staging_parent / "tree", commit_sha="b" * 40
        )
        assert manifest.file_count == 1
        staged = staging_parent / "tree"
        assert (staged / COMMIT_FILENAME).read_text().strip() == "b" * 40
        assert json.loads((staged / MANIFEST_FILENAME).read_text())[
            "commit_sha"
        ] == "b" * 40
    finally:
        remove_staging(staging_parent)


def test_oversized_file_fails_closed_and_cleans_up(tmp_path):
    src = tmp_path / "src"
    big = src / "data.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"0" * 1024)

    staging_parent = create_staging_parent()
    try:
        with pytest.raises(SnapshotError) as excinfo:
            build_snapshot(
                src, staging_parent / "tree", max_file_bytes=100
            )
        assert excinfo.value.reason == "snapshot_file_too_large"
        assert not (staging_parent / "tree").exists()
    finally:
        remove_staging(staging_parent)


def test_total_size_cap_fails_closed(tmp_path):
    src = tmp_path / "src"
    _write(src, "a.py", "x" * 500)
    _write(src, "b.py", "y" * 500)

    staging_parent = create_staging_parent()
    try:
        with pytest.raises(SnapshotError) as excinfo:
            build_snapshot(src, staging_parent / "tree", max_total_bytes=600)
        assert excinfo.value.reason == "snapshot_too_large"
        assert not (staging_parent / "tree").exists()
    finally:
        remove_staging(staging_parent)


def test_file_count_cap_fails_closed(tmp_path):
    src = tmp_path / "src"
    for index in range(5):
        _write(src, f"f{index}.py")

    staging_parent = create_staging_parent()
    try:
        with pytest.raises(SnapshotError) as excinfo:
            build_snapshot(src, staging_parent / "tree", max_files=3)
        assert excinfo.value.reason == "snapshot_too_many_files"
        assert not (staging_parent / "tree").exists()
    finally:
        remove_staging(staging_parent)


def test_invalid_source_and_existing_destination(tmp_path):
    staging_parent = create_staging_parent()
    try:
        with pytest.raises(SnapshotError) as excinfo:
            build_snapshot(tmp_path / "missing", staging_parent / "tree")
        assert excinfo.value.reason == "snapshot_source_invalid"
        target = staging_parent / "tree"
        target.mkdir()
        src = tmp_path / "src"
        _write(src, "a.py")
        with pytest.raises(SnapshotError) as excinfo:
            build_snapshot(src, target)
        assert excinfo.value.reason == "snapshot_destination_exists"
    finally:
        remove_staging(staging_parent)


def test_archive_is_relative_sorted_and_reproducible(tmp_path):
    src = tmp_path / "src"
    _write(src, "b.py", "b = 2\n")
    _write(src, "a.py", "a = 1\n")
    _write(src, "sub/c.py", "c = 3\n")

    staging_parent = create_staging_parent()
    try:
        build_snapshot(src, staging_parent / "tree", commit_sha="c" * 40)
        first = build_archive(staging_parent / "tree", staging_parent / "one.tar.gz")
        second = build_archive(staging_parent / "tree", staging_parent / "two.tar.gz")
        assert first["sha256"] == second["sha256"]
        assert first["size_bytes"] == second["size_bytes"] > 0
        with tarfile.open(staging_parent / "one.tar.gz", "r:gz") as tar:
            names = tar.getnames()
        assert names == sorted(names)
        assert all(not name.startswith("/") for name in names)
        assert COMMIT_FILENAME in names
        assert MANIFEST_FILENAME in names
        with gzip.open(staging_parent / "one.tar.gz", "rb") as handle:
            handle.read()
            assert handle.mtime == 0
    finally:
        remove_staging(staging_parent)


def test_archive_refuses_changed_tree_with_symlink(tmp_path):
    src = tmp_path / "src"
    _write(src, "a.py")
    staging_parent = create_staging_parent()
    try:
        build_snapshot(src, staging_parent / "tree")
        _try_symlink("a.py", str(staging_parent / "tree" / "link.py"))
        with pytest.raises(SnapshotError) as excinfo:
            build_archive(staging_parent / "tree", staging_parent / "snap.tar.gz")
        assert excinfo.value.reason == "snapshot_changed"
    finally:
        remove_staging(staging_parent)


def test_remove_staging_is_best_effort(tmp_path):
    missing = tmp_path / "missing"
    remove_staging(missing)  # must not raise
    file_target = tmp_path / "f.txt"
    file_target.write_text("x", encoding="utf-8")
    remove_staging(file_target)
    assert not file_target.exists()


def test_snapshot_module_exports():
    assert snapshot_module.DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES > 0
    assert snapshot_module.DEFAULT_SNAPSHOT_MAX_FILE_BYTES > 0
    assert snapshot_module.DEFAULT_SNAPSHOT_MAX_FILES > 0
