from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TEST_TIMEOUT_SECONDS = 20
MAX_OUTPUT_CHARS = 6000


def run_tests(workspace: Path) -> dict[str, Any]:
    """Run the bundled pytest suite inside an isolated workspace copy."""
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
            check=False,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "output": output[-MAX_OUTPUT_CHARS:],
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
        return {
            "passed": False,
            "exit_code": None,
            "output": f"Test run timed out.\n{output}"[-MAX_OUTPUT_CHARS:],
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
