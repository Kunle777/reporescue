"""JavaScript/TypeScript language adapter (Phase 5A Step 4).

Inspection-only: detects JavaScript/TypeScript sources and manifests and
reports honestly what RepoRescue can do with them. There is deliberately
no execution support here — no npm installation, no package-script
execution, no test-runner execution, no Docker. Runtime and
test/verification capability always report unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AdapterResult, scan_suffixes, top_level_names

LANGUAGE = "javascript"

JS_SOURCE_SUFFIXES = frozenset(
    {".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"}
)

JS_MANIFESTS = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)

# Test runners recognized as inspection signals only; their presence never
# implies RepoRescue can execute them.
JS_TEST_RUNNERS = frozenset(
    {"jest", "vitest", "mocha", "jasmine", "ava", "tap", "karma"}
)

MAX_PACKAGE_JSON_BYTES = 200_000
MAX_RECORDED_DEPS = 100
MAX_RECORDED_SCRIPTS = 50


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """Parse ``package.json`` defensively; None when missing/unusable."""
    try:
        if path.stat().st_size > MAX_PACKAGE_JSON_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _string_map(value: Any) -> dict[str, str]:
    """Keep string-valued entries of a mapping, bounded in size."""
    if not isinstance(value, dict):
        return {}
    kept: dict[str, str] = {}
    for name in sorted(value):
        if len(kept) >= MAX_RECORDED_DEPS:
            break
        spec = value[name]
        if isinstance(name, str) and isinstance(spec, str):
            kept[name] = spec
    return kept


def detect(root: Path) -> AdapterResult:
    """Inspect one checkout ``root`` for JavaScript/TypeScript content."""
    root = Path(root)
    js_files = scan_suffixes(root, JS_SOURCE_SUFFIXES)
    top_names = top_level_names(root)
    manifests = [name for name in JS_MANIFESTS if name in top_names]
    has_package_json = "package.json" in manifests

    project: dict[str, str] = {}
    dependencies: dict[str, str] = {}
    dev_dependencies: dict[str, str] = {}
    scripts: list[str] = []
    manifest_error: str | None = None
    if has_package_json:
        manifest = _read_manifest(root / "package.json")
        if manifest is None:
            manifest_error = "package.json is missing, oversized, or not valid JSON."
        else:
            for key in ("name", "version", "type"):
                value = manifest.get(key)
                if isinstance(value, str) and value.strip():
                    project[key] = value.strip()
            dependencies = _string_map(manifest.get("dependencies"))
            dev_dependencies = _string_map(manifest.get("devDependencies"))
            raw_scripts = manifest.get("scripts")
            if isinstance(raw_scripts, dict):
                scripts = sorted(
                    name
                    for name in raw_scripts
                    if isinstance(name, str)
                )[:MAX_RECORDED_SCRIPTS]

    test_signals = sorted(
        JS_TEST_RUNNERS.intersection(dependencies).union(
            JS_TEST_RUNNERS.intersection(dev_dependencies)
        )
    )
    has_test_script = "test" in scripts

    if manifest_error:
        dep_status = "invalid"
    elif has_package_json:
        dep_status = "manifest"
    else:
        dep_status = "none"

    limitations = [
        "JavaScript is inspection-only; runtime execution and test verification are not supported."
    ]
    if test_signals or has_test_script:
        limitations.append(
            "JavaScript test tooling was detected for inspection only; it cannot be executed."
        )

    return AdapterResult(
        language=LANGUAGE,
        file_count=len(js_files),
        manifests=tuple(manifests),
        project=project,
        dependencies={
            "status": dep_status,
            "files": ["package.json"] if has_package_json else [],
        },
        test_frameworks=(),
        static_available=bool(js_files or has_package_json),
        runtime_available=False,
        runtime_reason="execution_not_supported",
        limitations=tuple(limitations),
        details={
            "manifest_error": manifest_error,
            "dependencies": dependencies,
            "dev_dependencies": dev_dependencies,
            "scripts": scripts,
            "test_signals": test_signals,
            "has_test_script": has_test_script,
        },
    )
