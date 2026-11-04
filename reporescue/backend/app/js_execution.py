"""JavaScript execution helpers for isolated backends (Phase 5B).

Genuine npm-based execution planning and result parsing for JavaScript
repositories: dependency-install planning, test-command selection, and
best-effort output parsing for the common runners (node --test,
TAP, Jest, Vitest, Mocha). These helpers never execute anything themselves;
an :class:`~app.execution.ExecutionBackend` calls them to build sandbox
commands and to interpret sandbox output.

Honesty rules (same as the Python path):

- No ``package.json`` (or an invalid one) is a loud plan error, never a
  guessed install.
- A missing ``test`` script is a loud error: tests that were not run are
  reported as not run, never as passing.
- Unparseable output yields empty failures and counts; the raw output
  tail remains the evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

JS_MAX_FAILURES = 30

MODAL_NODE_IMAGE_DEFAULT = "node:22-slim"
MODAL_NODE_MAJOR_MIN = 18
MODAL_NODE_MAJOR_MAX = 24

# Test-command entry points a backend may execute for JavaScript. Argv
# lists only (never a shell); anything else is refused as unsupported.
JS_TEST_LAUNCHERS = frozenset({"npm", "npx", "node"})
JS_INSTALL_LAUNCHERS = frozenset({"npm"})

_TAP_NOT_OK_RE = re.compile(r"^not ok \d+\s*(.*)$")
_JEST_FAIL_RE = re.compile(r"^\s*[✕×]\s+(.+)$")
_JEST_SUMMARY_RE = re.compile(
    r"^Tests:\s+(?:(\d+)\s+failed,?\s*)?(?:(\d+)\s+passed,?\s*)?(.*)$"
)
_VITEST_FAIL_RE = re.compile(r"^\s*[❯›»]\s+(.+?)\s*$")
_VITEST_TESTS_RE = re.compile(
    r"^\s*Tests\s+(?:(\d+)\s+failed)?[,\s]*(?:(\d+)\s+passed)?"
)
_MOCHA_FAIL_ITEM_RE = re.compile(r"^\d+\)\s+(.+?):?\s*$")
_MOCHA_PASSING_RE = re.compile(r"^\s*(\d+)\s+passing")
_MOCHA_FAILING_RE = re.compile(r"^\s*(\d+)\s+failing")
_NODE_INFO_RE = re.compile(r"^ℹ\s+(pass|fail)\s+(\d+)\s*$")
_NODE_FAILING_HEADER_RE = re.compile(r"^✖\s+failing tests:\s*$")
_NODE_FAILING_ENTRY_RE = re.compile(r"^(?:test|suite) at (\S+)\s*$")
_NODE_FILE_FAIL_RE = re.compile(r"^✖\s+(\S.*?)\s+\([\d.]+ms\)\s*$")
_ENGINES_NODE_RE = re.compile(r"(\d+)(?:\.(\d+))?")


def plan_js_install(
    *,
    has_package_json: bool,
    manifest_error: str | None,
    manifests: list[str] | tuple[str, ...],
) -> dict[str, object]:
    """Plan npm dependency installation for a JavaScript checkout.

    Returns ``{"commands": [...], "error": None}`` or ``{"commands": [],
    "error": "..."}``. ``npm ci`` is preferred when a lockfile is
    present (reproducible); otherwise ``npm install`` resolves from
    ``package.json``. yarn/pnpm-only lockfiles are reported honestly:
    npm cannot consume them, so the plan falls back to ``npm install``
    and says so in the install log marker.
    """
    if manifest_error or not has_package_json:
        return {
            "commands": [],
            "error": (
                manifest_error
                or "No package.json found; refusing to guess dependencies."
            ),
        }
    names = set(manifests)
    if "package-lock.json" in names:
        return {"commands": [["npm", "ci"]], "error": None}
    if "yarn.lock" in names or "pnpm-lock.yaml" in names:
        return {
            "commands": [["npm", "install"]],
            "error": None,
            "note": (
                "No package-lock.json; falling back to 'npm install' "
                "(yarn/pnpm lockfiles are not consumed)."
            ),
        }
    return {"commands": [["npm", "install"]], "error": None}


def js_test_command(scripts: list[str] | tuple[str, ...]) -> dict[str, object]:
    """Select the test command for a JavaScript checkout.

    Returns ``{"command": [...], "error": None}``. The repository's own
    ``test`` script runs verbatim via ``npm test --silent``; no runner
    flags are injected (a flag correct for one runner breaks another).
    Runners that watch by default (Vitest) run once under ``CI=true``,
    which backends set in the test environment.
    """
    if "test" not in set(scripts or ()):
        return {
            "command": [],
            "error": (
                "No 'test' script in package.json; nothing was executed, "
                "so no test result is reported."
            ),
        }
    return {"command": ["npm", "test", "--silent"], "error": None}


def js_test_env() -> dict[str, str]:
    """Environment additions for JavaScript test execution."""
    return {"CI": "true"}


def validate_js_argv(argv: list[str], *, kind: str) -> str | None:
    """Validate a JS backend command. Returns an error message or None."""
    if not argv:
        return "Empty JavaScript command; refusing to execute."
    allowed = JS_INSTALL_LAUNCHERS if kind == "install" else JS_TEST_LAUNCHERS
    first = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first not in allowed:
        return (
            f"Unsupported JavaScript {kind} launcher {argv[0]!r}; "
            f"expected one of {sorted(allowed)}."
        )
    if any(not isinstance(part, str) or not part for part in argv):
        return "JavaScript command arguments must be non-empty strings."
    return None


def select_node_image(
    package_json_path: Path | str | None,
    *,
    default: str = MODAL_NODE_IMAGE_DEFAULT,
) -> str:
    """Map a ``package.json`` engines constraint to a Node image tag.

    Only the major version's lower bound is honored; anything unknown
    or out of the supported range falls back to the default image
    rather than failing the investigation.
    """
    if package_json_path is not None:
        try:
            text = Path(package_json_path).read_text(
                encoding="utf-8", errors="replace"
            )
            data = json.loads(text)
            engines = (data.get("engines") or {}).get("node", "")
        except (OSError, ValueError, AttributeError):
            engines = ""
        match = _ENGINES_NODE_RE.search(str(engines))
        if match:
            major = int(match.group(1))
            if MODAL_NODE_MAJOR_MIN <= major <= MODAL_NODE_MAJOR_MAX:
                return f"node:{major}-slim"
    return default


def parse_js_output(output: str) -> dict[str, object]:
    """Parse JavaScript test output into failures and summary counts.

    Best-effort across node --test, TAP, Jest, Vitest, and Mocha shapes. Never
    invents results: unparseable output yields an empty failure list
    and empty counts, and the caller falls back to the raw output tail.
    """
    text = output or ""
    failures: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(test: str, kind: str, message: str = "") -> None:
        test = (test or "").strip()
        if not test or test in seen or len(failures) >= JS_MAX_FAILURES:
            return
        seen.add(test)
        failures.append(
            {"test": test, "kind": kind, "message": message.strip(), "traceback": ""}
        )

    counts: dict[str, int] = {}
    in_mocha_failures = False
    in_node_failures = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        node_info = _NODE_INFO_RE.match(line)
        if node_info:
            counts["passed" if node_info.group(1) == "pass" else "failed"] = int(
                node_info.group(2)
            )
            continue
        if _NODE_FAILING_HEADER_RE.match(line):
            in_node_failures = True
            in_mocha_failures = False
            continue
        if in_node_failures:
            entry = _NODE_FAILING_ENTRY_RE.match(line)
            if entry:
                _add(entry.group(1), "failed")
                continue
            file_fail = _NODE_FILE_FAIL_RE.match(line)
            if file_fail:
                _add(file_fail.group(1), "failed")
                continue
        tap = _TAP_NOT_OK_RE.match(line)
        if tap:
            _add(tap.group(1) or "tap test", "failed")
            continue
        jest_fail = _JEST_FAIL_RE.match(line)
        if jest_fail and "PASS" not in line and "FAIL" not in line:
            _add(jest_fail.group(1), "failed")
            continue
        jest_summary = _JEST_SUMMARY_RE.match(line)
        if jest_summary:
            if jest_summary.group(1):
                counts["failed"] = int(jest_summary.group(1))
            if jest_summary.group(2):
                counts["passed"] = int(jest_summary.group(2))
            continue
        # Bare "FAIL <file>" lines name files, not tests; the per-test
        # failure lines above carry the actionable failures.
        vitest_fail = _VITEST_FAIL_RE.match(line)
        if vitest_fail and ("FAIL" in text or "failed" in line.lower()):
            _add(vitest_fail.group(1), "failed")
            continue
        vitest_tests = _VITEST_TESTS_RE.match(line)
        if vitest_tests and (vitest_tests.group(1) or vitest_tests.group(2)):
            if vitest_tests.group(1):
                counts["failed"] = int(vitest_tests.group(1))
            if vitest_tests.group(2):
                counts["passed"] = int(vitest_tests.group(2))
            continue
        passing = _MOCHA_PASSING_RE.match(line)
        if passing:
            counts["passed"] = int(passing.group(1))
            in_mocha_failures = False
            continue
        failing = _MOCHA_FAILING_RE.match(line)
        if failing:
            counts["failed"] = int(failing.group(1))
            in_mocha_failures = True
            continue
        if in_mocha_failures:
            item = _MOCHA_FAIL_ITEM_RE.match(line)
            if item:
                _add(item.group(1), "failed")
            continue
    return {"failures": failures, "summary_counts": counts}
