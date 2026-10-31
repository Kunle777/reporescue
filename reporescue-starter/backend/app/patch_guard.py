from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ALLOWED_PREFIX = "src/"
MAX_PATCH_CHARS = 4000


@dataclass(frozen=True)
class PatchProposal:
    """A proposed source edit, in whatever origin (stub today, model later)."""

    file: str
    before: str
    after: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "file": self.file,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


class PatchRejected(Exception):
    """Raised when a proposed patch fails safety validation."""


def validate_and_apply(workspace: Path, patch: PatchProposal) -> None:
    """Validate a patch against the guard rules, then apply it to the workspace.

    Fails closed: any rule violation raises PatchRejected and writes nothing.
    """
    target = _resolve_target(workspace, patch.file)
    if len(patch.before) + len(patch.after) > MAX_PATCH_CHARS:
        raise PatchRejected("Patch exceeds the maximum demo patch size; refusing to apply it.")
    if not target.is_file():
        raise PatchRejected(f"Patch target '{patch.file}' does not exist; refusing to create new files.")
    source = target.read_text(encoding="utf-8")
    if patch.before not in source:
        raise PatchRejected(
            "Expected vulnerable line was not found; refusing to patch an unexpected fixture."
        )
    target.write_text(source.replace(patch.before, patch.after, 1), encoding="utf-8")


def _resolve_target(workspace: Path, rel_path: str) -> Path:
    candidate = Path(rel_path)
    posix_absolute = rel_path.replace("\\", "/").startswith("/")
    if candidate.is_absolute() or posix_absolute or ".." in candidate.parts:
        raise PatchRejected(f"Patch path '{rel_path}' is outside the repository; refusing to apply it.")
    normalized = rel_path.replace("\\", "/")
    if normalized == "tests" or normalized.startswith("tests/"):
        raise PatchRejected("Patches to the protected test suite are not allowed.")
    if not normalized.startswith(ALLOWED_PREFIX):
        raise PatchRejected(f"Patch target '{rel_path}' is outside the allowed demo scope (src/).")
    return workspace / candidate
