"""Authenticated unified-diff export of an investigation's guarded patch.

Reads the persisted ``Patch``/``PatchFile`` rows for one investigation,
re-materializes the pinned repository snapshot read-only (clone only via
:func:`github_ingest.clone_and_pin`; repository code is never executed),
and renders a standard unified Git diff with :mod:`difflib` that applies
with ``git apply``.

Safety properties:

- Every stored ``before`` block must occur exactly once in the pinned
  file before a hunk is emitted. Anything else is HTTP 422: a
  misleading patch is never generated silently.
- ``applied`` does not mean ``verified``. The verified/unverified
  designation comes from the investigation's ``verification_status``
  corroborated by its attempt records, and unverified downloads carry
  an explicit warning in the patch header, the filename, and the
  response headers.
- Error details never include temporary filesystem paths.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .github_ingest import (
    IngestionError,
    clone_and_pin,
    normalize_github_url,
    temporary_workspace,
)
from .models import Investigation, InvestigationAttempt, Patch, PatchFile

MAX_PATCH_BYTES = 1_000_000

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DIFF_PATH_QUOTE_RE = re.compile(r"[^A-Za-z0-9._/+-]")


class PatchDownloadError(Exception):
    """Base error with an HTTP mapping for the download route."""

    status_code: int = 500
    reason: str = "patch_error"


class PatchNotFoundError(PatchDownloadError):
    """No applicable patch is stored for the investigation."""

    status_code: int = 404
    reason: str = "patch_not_found"


class PatchUnrenderableError(PatchDownloadError):
    """Stored patch cannot be faithfully rendered against the pinned snapshot."""

    status_code: int = 422
    reason: str = "patch_unrenderable"


class PatchTooLargeError(PatchDownloadError):
    """Rendered patch exceeds the download size limit."""

    status_code: int = 413
    reason: str = "patch_too_large"


def select_downloadable_patch(db: Session, investigation_id: UUID) -> Patch | None:
    """Return the latest applied patch for an investigation, if any.

    Only ``applied`` patches qualify: they passed the guard and were
    written to a verification work copy. Proposed or validated-only
    patches were never applied and are not downloadable.
    """
    return db.scalars(
        select(Patch)
        .where(
            Patch.investigation_id == investigation_id,
            Patch.status == "applied",
        )
        .order_by(Patch.created_at.desc())
    ).first()


def patch_files_for(db: Session, patch_id: UUID) -> list[PatchFile]:
    """Return a patch's file edits in deterministic filename order."""
    return list(
        db.scalars(
            select(PatchFile)
            .where(PatchFile.patch_id == patch_id)
            .order_by(PatchFile.file.asc())
        ).all()
    )


def is_verified(db: Session, investigation: Investigation) -> bool:
    """Whether the investigation's patch passed verification.

    The persisted ``verification_status`` is authoritative; attempt
    records corroborate it when present. Anything other than an
    explicit pass is unverified.
    """
    if investigation.verification_status != "passed":
        return False
    attempts = list(
        db.scalars(
            select(InvestigationAttempt).where(
                InvestigationAttempt.investigation_id == investigation.id
            )
        ).all()
    )
    if not attempts:
        return True
    for attempt in attempts:
        verification = attempt.verification
        if isinstance(verification, dict) and verification.get("passed") is True:
            return True
    return False


def build_filename(
    repo_name: str | None, commit_sha: str, investigation_id: UUID, verified: bool
) -> str:
    """Build an ASCII-only attachment filename for the patch."""
    safe_repo = _FILENAME_SAFE_RE.sub("-", repo_name or "repo").strip("-.") or "repo"
    tag = "verified" if verified else "unverified"
    return (
        f"reporescue-{safe_repo[:60]}-{commit_sha[:7]}"
        f"-{str(investigation_id)[:8]}-{tag}.patch"
    )


def _resolve_render_target(root: Path, rel_path: str) -> Path:
    """Resolve a stored repo-relative path inside the snapshot.

    Containment only (no policy): the download must faithfully render
    what was applied. Rejects absolute paths, escapes, symlinks, and
    non-files. Error text carries only the repo-relative path.
    """
    if not isinstance(rel_path, str) or not rel_path or rel_path != rel_path.strip():
        raise PatchUnrenderableError("Stored patch contains an invalid file path.")
    candidate = root / rel_path
    try:
        if candidate.is_symlink():
            raise PatchUnrenderableError(
                f"Refusing to render symlinked path {rel_path!r}."
            )
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        raise PatchUnrenderableError(
            f"Could not resolve {rel_path!r} in the pinned snapshot."
        ) from None
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PatchUnrenderableError(
            f"Stored path {rel_path!r} escapes the repository."
        )
    if not resolved.is_file():
        raise PatchUnrenderableError(
            f"Stored path {rel_path!r} is not a file in the pinned snapshot."
        )
    return resolved


def _quote_diff_path(rel_posix: str) -> str:
    """Quote a diff path the way Git does when it needs quoting."""
    if not _DIFF_PATH_QUOTE_RE.search(rel_posix):
        return f"a/{rel_posix}", f"b/{rel_posix}"
    quoted = rel_posix.replace("\\", "\\\\").replace('"', '\\"')
    return f'"a/{quoted}"', f'"b/{quoted}"'


def _unified_file_diff(rel_posix: str, old_text: str, new_text: str) -> str:
    """Render one file's unified diff with git-compatible headers."""
    from_path, to_path = _quote_diff_path(rel_posix)
    lines = [f"diff --git {from_path} {to_path}\n"]
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    for line in difflib.unified_diff(
        old_lines, new_lines, fromfile=from_path, tofile=to_path
    ):
        lines.append(line)
        if not line.endswith("\n") and not line.startswith(("---", "+++", "@@")):
            lines.append("\n\\ No newline at end of file\n")
    return "".join(lines)


def _read_snapshot_text(path: Path, rel_posix: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PatchUnrenderableError(
            f"Could not read {rel_posix!r} as text in the pinned snapshot."
        ) from None


def render_patch_text(
    *,
    repo_root: Path,
    files: list[PatchFile],
    repo_url: str,
    commit_sha: str,
    investigation_id: UUID,
    verified: bool,
) -> str:
    """Render stored edits as a unified diff against the pinned snapshot.

    Raises :class:`PatchUnrenderableError` unless every ``before``
    block occurs exactly once, and :class:`PatchTooLargeError` when
    the render exceeds :data:`MAX_PATCH_BYTES`.
    """
    if not files:
        raise PatchUnrenderableError("Stored patch has no file edits.")
    header = [
        "# RepoRescue patch export",
        f"# Repository: {repo_url}",
        f"# Commit: {commit_sha}",
        f"# Investigation: {investigation_id}",
    ]
    if verified:
        header.append("# Verification: PASSED")
    else:
        header.append("# Verification: UNVERIFIED - verification did not pass")
        header.append(
            "# WARNING: this patch was not verified. Review it carefully; "
            "downloading is not applying."
        )
    chunks = ["\n".join(header) + "\n"]
    total = len(chunks[0].encode("utf-8"))
    for edit in files:
        rel = edit.file if isinstance(edit.file, str) else ""
        before = edit.before if isinstance(edit.before, str) else None
        after = edit.after if isinstance(edit.after, str) else None
        if not rel or before is None or before == "" or after is None:
            raise PatchUnrenderableError("Stored patch contains an incomplete edit.")
        if len(before) + len(after) > MAX_PATCH_BYTES:
            raise PatchTooLargeError("Patch exceeds the 1 MB download limit.")
        rel_posix = rel.replace("\\", "/")
        target = _resolve_render_target(repo_root, rel_posix)
        source = _read_snapshot_text(target, rel_posix)
        if source.count(before) != 1:
            raise PatchUnrenderableError(
                f"Original content for {rel_posix!r} was not found exactly "
                "once in the pinned snapshot."
            )
        diff = _unified_file_diff(rel_posix, source, source.replace(before, after, 1))
        total += len(diff.encode("utf-8"))
        if total > MAX_PATCH_BYTES:
            raise PatchTooLargeError("Patch exceeds the 1 MB download limit.")
        chunks.append(diff)
    return "".join(chunks)


def build_patch_download(
    db: Session, investigation: Investigation
) -> tuple[str, str, bool]:
    """Build (content, filename, verified) for an investigation's patch.

    Re-materializes the pinned snapshot read-only; the temporary
    workspace is always removed, including on errors. Raises
    :class:`PatchNotFoundError`, :class:`PatchUnrenderableError`, or
    :class:`PatchTooLargeError`.
    """
    patch = select_downloadable_patch(db, investigation.id)
    if patch is None:
        raise PatchNotFoundError("No downloadable patch for this investigation.")
    files = patch_files_for(db, patch.id)
    verified = is_verified(db, investigation)
    try:
        normalized = normalize_github_url(investigation.repo_url)
    except IngestionError:
        raise PatchUnrenderableError(
            "Investigation repository URL is invalid."
        ) from None
    if not isinstance(investigation.commit_sha, str) or len(investigation.commit_sha) != 40:
        raise PatchUnrenderableError("Investigation commit SHA is invalid.")
    with temporary_workspace(prefix="reporescue-patch-") as workspace:
        destination = workspace / "repo"
        try:
            pinned = clone_and_pin(
                normalized.url, destination, investigation.commit_sha
            )
        except IngestionError as exc:
            raise PatchUnrenderableError(
                f"Pinned snapshot is unavailable ({exc.reason})."
            ) from None
        if pinned != investigation.commit_sha:
            raise PatchUnrenderableError(
                "Materialized revision does not match the pinned SHA."
            )
        content = render_patch_text(
            repo_root=destination,
            files=files,
            repo_url=normalized.url,
            commit_sha=investigation.commit_sha,
            investigation_id=investigation.id,
            verified=verified,
        )
    filename = build_filename(
        investigation.repo_name, investigation.commit_sha, investigation.id, verified
    )
    return content, filename, verified
