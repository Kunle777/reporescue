"""Language adapters for repository detection.

Each adapter module exposes ``LANGUAGE`` and ``detect(root)`` returning
an :class:`~app.languages.base.AdapterResult`. :func:`detect_all` runs
every supported adapter explicitly — there is no plugin registry or
dynamic loading. Adapters are read-only: no installs, no execution, no
Docker.
"""

from __future__ import annotations

from pathlib import Path

from . import javascript, python
from .base import AdapterResult

__all__ = ["AdapterResult", "detect_all", "javascript", "python"]


def detect_all(root: Path) -> dict[str, AdapterResult]:
    """Run every supported language adapter over one checkout ``root``."""
    root = Path(root)
    results = [python.detect(root), javascript.detect(root)]
    return {result.language: result for result in results}
