from __future__ import annotations

import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = APP_DIR.parent / "fixtures" / "checkout_service"

_SKIP_PARTS = frozenset({"__pycache__", ".pytest_cache"})


def fixture_exists() -> bool:
    return FIXTURE_DIR.is_dir()


def copy_fixture_to(destination: Path) -> Path:
    """Copy the bundled fixture into an isolated workspace. The original is untouched."""
    shutil.copytree(FIXTURE_DIR, destination)
    return destination


def list_source_files(workspace: Path) -> list[str]:
    """Relative paths of workspace files, as repository context for patch proposals."""
    return sorted(
        str(path.relative_to(workspace)).replace("\\", "/")
        for path in workspace.rglob("*")
        if path.is_file() and _SKIP_PARTS.isdisjoint(path.parts)
    )
