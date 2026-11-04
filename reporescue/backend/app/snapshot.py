"""Secure repository snapshot staging for cloud execution (Phase 5B).

Builds a bounded, secret-free copy of a materialized repository snapshot
for transfer to an isolated execution sandbox (Modal). The staging tree is
the only repository content that leaves the trusted worker.

Security properties (mirroring :mod:`app.repo_context` and
:mod:`app.github_ingest`):

- Git history and other VCS metadata never enter the snapshot.
- Local environments (``.execvenv``, ``venv``, ``node_modules``) never
  enter: the sandbox installs dependencies into its own fresh environment.
- Secret-bearing files (``.env*``, ``credentials.json``, ``*.pem``,
  ...) never enter and are never read beyond their names.
- Symlinks are never followed and never copied, so a malicious checkout
  cannot make the worker read outside the snapshot.
- Per-file, total-size, and file-count caps bound the transfer; oversized
  snapshots fail loudly instead of partially transferring.
- A manifest (SHA-256 per file) plus a commit-identity file lets the
  sandbox side verify it received the expected content.

There is deliberately no archive *extraction* on the trusted worker: the
worker only builds a vetted tree (and optionally a gzip-compressed tar of
exactly that tree for upload). Traversal attacks have no surface here —
every entry written comes from a safe relative path the worker computed
itself.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .repo_context import (
    EXCLUDED_DIRS,
    SECRET_BASENAMES,
    SECRET_NAME_HINTS,
    SECRET_SUFFIXES,
)

DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_FILES = 20_000

COMMIT_FILENAME = ".reporescue-commit"
MANIFEST_FILENAME = ".reporescue-manifest.json"

# Reserved names are written by the worker after staging; a repository file
# with one of these names is skipped so it can never spoof snapshot
# identity or the manifest.
RESERVED_BASENAMES = frozenset({COMMIT_FILENAME, MANIFEST_FILENAME})

# Snapshot-only exclusions on top of repo_context.EXCLUDED_DIRS.
SNAPSHOT_EXCLUDED_DIRS = frozenset(EXCLUDED_DIRS) | frozenset(
    {".execvenv", ".reporescue-stage"}
)


class SnapshotError(Exception):
    """A snapshot could not be built safely, with a machine-readable reason."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SnapshotManifest:
    """Identity and integrity record for one staged snapshot."""

    commit_sha: str | None
    files: tuple[tuple[str, int, str], ...] = ()
    total_bytes: int = 0
    excluded: dict[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def to_dict(self) -> dict:
        return {
            "commit_sha": self.commit_sha,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "excluded": dict(self.excluded),
            "files": [
                {"path": rel, "size": size, "sha256": digest}
                for rel, size, digest in self.files
            ],
        }


def _is_secret_path(name: str) -> bool:
    """Whether ``name`` (a basename) must never leave the worker.

    Mirrors :func:`app.repo_context._is_secret_name` and additionally
    treats every ``.env*`` file as secret: ``.env.development``,
    ``.env.test``, and friends are as credential-bearing as ``.env``.
    """
    lowered = name.lower()
    if lowered in SECRET_BASENAMES:
        return True
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if any(lowered.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return True
    return any(hint in lowered for hint in SECRET_NAME_HINTS)


def _is_excluded_dir(name: str) -> bool:
    return name in SNAPSHOT_EXCLUDED_DIRS


def build_snapshot(
    source: Path | str,
    staging_dir: Path | str,
    *,
    commit_sha: str | None = None,
    max_total_bytes: int = DEFAULT_SNAPSHOT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_SNAPSHOT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_SNAPSHOT_MAX_FILES,
) -> SnapshotManifest:
    """Stage a bounded, secret-free copy of ``source`` into ``staging_dir``.

    ``staging_dir`` must not exist; it is created by this function. Files
    are copied by content (hardlinks are naturally broken: the copy is a
    fresh regular file). Returns the snapshot manifest. Raises
    :class:`SnapshotError` when the source is unusable or any bound is
    exceeded — never a partial snapshot presented as complete.
    """
    src = Path(source)
    staging = Path(staging_dir)
    if not src.is_dir() or src.is_symlink():
        raise SnapshotError(
            "Snapshot source is not a directory; refusing to stage.",
            "snapshot_source_invalid",
        )
    if staging.exists():
        raise SnapshotError(
            "Snapshot staging directory already exists; refusing to overwrite.",
            "snapshot_destination_exists",
        )
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise SnapshotError(
            f"Could not create snapshot staging directory: {exc}.",
            "snapshot_workspace_error",
        ) from exc

    entries: list[tuple[str, int, str]] = []
    excluded: dict[str, int] = {}
    total = 0

    def _skip(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    try:
        stack: list[Path] = [src]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda p: p.name)
            except OSError:
                _skip("unreadable")
                continue
            for entry in children:
                try:
                    if entry.is_symlink():
                        _skip("symlink")
                        continue
                    if entry.is_dir():
                        if _is_excluded_dir(entry.name):
                            _skip("excluded_dir")
                            continue
                        stack.append(entry)
                        continue
                    if not entry.is_file():
                        _skip("special")
                        continue
                except OSError:
                    _skip("unreadable")
                    continue
                try:
                    rel = entry.relative_to(src).as_posix()
                except ValueError:
                    _skip("outside_tree")
                    continue
                if (
                    entry.name in RESERVED_BASENAMES
                    or "/.git/" in f"/{rel}"
                    or rel == ".git"
                    or rel.startswith(".git/")
                ):
                    _skip("reserved_or_vcs")
                    continue
                if _is_secret_path(entry.name):
                    _skip("secret")
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    _skip("unreadable")
                    continue
                if size > max_file_bytes:
                    raise SnapshotError(
                        f"Snapshot file {rel!r} ({size} bytes) exceeds the "
                        f"per-file limit ({max_file_bytes} bytes); refusing "
                        "a partial snapshot.",
                        "snapshot_file_too_large",
                    )
                if len(entries) >= max_files:
                    raise SnapshotError(
                        f"Snapshot exceeds the file-count limit ({max_files} "
                        "files); refusing a partial snapshot.",
                        "snapshot_too_many_files",
                    )
                if total + size > max_total_bytes:
                    raise SnapshotError(
                        f"Snapshot exceeds the total-size limit "
                        f"({max_total_bytes} bytes); refusing a partial "
                        "snapshot.",
                        "snapshot_too_large",
                    )
                try:
                    digest = hashlib.sha256()
                    target = staging / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        open(entry, "rb") as handle_in,
                        open(target, "wb") as handle_out,
                    ):
                        while True:
                            chunk = handle_in.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            handle_out.write(chunk)
                except OSError as exc:
                    raise SnapshotError(
                        f"Could not stage snapshot file {rel!r}: {exc}.",
                        "snapshot_workspace_error",
                    ) from exc
                entries.append((rel, size, digest.hexdigest()))
                total += size
    except SnapshotError:
        remove_staging(staging)
        raise

    manifest = SnapshotManifest(
        commit_sha=commit_sha, files=tuple(entries), total_bytes=total,
        excluded=excluded,
    )
    try:
        (staging / COMMIT_FILENAME).write_text(
            f"{commit_sha or 'unknown'}\n", encoding="utf-8"
        )
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
    except OSError as exc:
        remove_staging(staging)
        raise SnapshotError(
            f"Could not write snapshot identity files: {exc}.",
            "snapshot_workspace_error",
        ) from exc
    return manifest


def build_archive(staging_dir: Path | str, archive_path: Path | str) -> dict[str, object]:
    """Compress a staged snapshot into a reproducible ``.tar.gz`` archive.

    Entries are stored under relative POSIX names in sorted order with
    fixed metadata so identical trees produce identical bytes. ``staging``
    must be a directory previously built by :func:`build_snapshot`
    (symlinks are refused defensively even though staging never
    contains any). Returns ``{"path", "size_bytes", "sha256"}``.
    """
    staging = Path(staging_dir)
    archive = Path(archive_path)
    if not staging.is_dir() or staging.is_symlink():
        raise SnapshotError(
            "Snapshot staging directory is invalid; refusing to archive.",
            "snapshot_source_invalid",
        )
    if archive.exists():
        raise SnapshotError(
            "Snapshot archive path already exists; refusing to overwrite.",
            "snapshot_destination_exists",
        )
    names: list[str] = []
    for root, dirs, files in os.walk(staging, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not Path(root, d).is_symlink())
        for name in sorted(files):
            full = Path(root) / name
            if full.is_symlink() or not full.is_file():
                raise SnapshotError(
                    "Snapshot staging tree changed during archival; refusing "
                    "to archive.",
                    "snapshot_changed",
                )
            names.append(full.relative_to(staging).as_posix())
    try:
        with open(archive, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=raw) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for rel in sorted(names):
                        full = staging / rel
                        info = tarfile.TarInfo(rel)
                        info.size = full.stat().st_size
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o644
                        with open(full, "rb") as handle:
                            tar.addfile(info, handle)
    except OSError as exc:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        raise SnapshotError(
            f"Could not write snapshot archive: {exc}.",
            "snapshot_workspace_error",
        ) from exc
    digest = hashlib.sha256()
    try:
        with open(archive, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        size = archive.stat().st_size
    except OSError as exc:
        raise SnapshotError(
            f"Could not read back snapshot archive: {exc}.",
            "snapshot_workspace_error",
        ) from exc
    return {"path": str(archive), "size_bytes": size, "sha256": digest.hexdigest()}


def create_staging_parent(prefix: str = "reporescue-snapshot-") -> Path:
    """Create a unique temporary parent directory for snapshot staging."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def remove_staging(path: Path | str) -> None:
    """Best-effort removal of a staging tree or archive created here."""
    import shutil

    target = Path(path)
    try:
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    except OSError:
        pass
