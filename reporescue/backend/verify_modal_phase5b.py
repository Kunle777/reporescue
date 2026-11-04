"""Phase 5B Modal cloud acceptance (run with owner credentials).

Performs small, bounded, real-cloud executions against Modal and checks
the acceptance evidence points:

 1. Modal SDK installed and credentials present (presence only).
 2. ModalBackend selected when explicitly configured.
 3. Python baseline executes in a cloud sandbox (failure reproduced).
 4. stdout/exit codes/failures captured and parsed.
 5. Fresh verification environment executes independently (fix passes).
 6. Timeouts enforced and reported honestly.
 7. Cancellation terminates the sandbox and reports cancelled.
 8. (--include-js) JavaScript executes with npm install + node tests.

Each check uses a tiny synthetic repository and seconds of test time;
sandboxes terminate after every run. Expected cost is negligible, but
sustained usage still requires owner authorization.

Credential values are never printed. Exit codes: 0 all checks passed,
1 a check failed, 3 credentials/SDK unavailable (nothing was run).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

from app.execution import ExecutionRequest, select_backend  # noqa: E402
from app.modal_backend import ModalBackend  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def make_python_repo(root: Path, *, broken: bool) -> Path:
    repo = root / "pycalc"
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    body = "a - b" if broken else "a + b"
    (repo / "pkg" / "calc.py").write_text(
        f"def add(a, b):\n    return {body}\n", encoding="utf-8"
    )
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return repo


def make_js_repo(root: Path) -> Path:
    repo = root / "jscalc"
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    (repo / "package.json").write_text(
        '{"name": "jscalc", "version": "1.0.0", '
        '"scripts": {"test": "node --test"}}',
        encoding="utf-8",
    )
    (repo / "calc.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "test('adds', () => { assert.strictEqual(2 + 3, 6); });\n",
        encoding="utf-8",
    )
    return repo


def make_sleepy_repo(root: Path) -> Path:
    repo = root / "sleepy"
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(120)\n",
        encoding="utf-8",
    )
    return repo


def main() -> int:
    include_js = "--include-js" in sys.argv[1:]
    backend = ModalBackend()
    available, reason = backend.is_available()
    check("modal SDK installed and credentials present", available, reason)
    if not available:
        print("Modal is unavailable; nothing was run (exit 3).")
        return 3

    previous = os.environ.get("REPORESCUE_EXECUTION_BACKEND")
    os.environ["REPORESCUE_EXECUTION_BACKEND"] = "modal"
    try:
        selected, error = select_backend()
    finally:
        if previous is None:
            os.environ.pop("REPORESCUE_EXECUTION_BACKEND", None)
        else:
            os.environ["REPORESCUE_EXECUTION_BACKEND"] = previous
    check(
        "modal selected when explicitly configured",
        selected is not None and selected.name == "modal",
        (error or {}).get("reason", "") if error else "backend=modal",
    )
    if selected is None:
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="reporescue-modal-accept-"))
    try:
        # Baseline: broken repo must reproduce a parsed failure.
        repo = make_python_repo(workdir, broken=True)
        start = time.perf_counter()
        baseline = backend.run(
            ExecutionRequest(
                snapshot=repo,
                test_command=["python", "-m", "pytest", "-q", "--tb=short"],
                install_commands=[["pip", "install", "pytest"]],
                framework="pytest",
                test_timeout=180,
                language="python",
                commit_sha="0" * 40,
                repo_name="acceptance/pycalc",
            )
        )
        baseline_s = round(time.perf_counter() - start, 1)
        check(
            "python baseline reproduces failure in cloud sandbox",
            baseline.status == "test_failure" and baseline.passed is False,
            f"status={baseline.status} in {baseline_s}s",
        )
        check(
            "baseline failures parsed with exit code",
            bool(baseline.failures) and baseline.exit_code == 1,
            f"failures={len(baseline.failures)} exit={baseline.exit_code}",
        )
        check(
            "baseline ran on modal with network-blocked isolation",
            baseline.backend == "modal" and baseline.isolation == "modal-sandbox",
            f"{baseline.backend}/{baseline.isolation}",
        )

        # Verification: fixed repo in a fresh environment must pass.
        fixed = make_python_repo(workdir, broken=False)
        start = time.perf_counter()
        verification = backend.run(
            ExecutionRequest(
                snapshot=fixed,
                test_command=["python", "-m", "pytest", "-q", "--tb=short"],
                install_commands=[["pip", "install", "pytest"]],
                framework="pytest",
                test_timeout=180,
                language="python",
                commit_sha="1" * 40,
                repo_name="acceptance/pycalc",
            )
        )
        verification_s = round(time.perf_counter() - start, 1)
        check(
            "fresh verification environment passes fixed repo",
            verification.status == "ok" and verification.passed is True,
            f"status={verification.status} in {verification_s}s",
        )

        # Timeout: a sleepy suite must time out honestly.
        sleepy = make_sleepy_repo(workdir)
        start = time.perf_counter()
        timed = backend.run(
            ExecutionRequest(
                snapshot=sleepy,
                test_command=["python", "-m", "pytest", "-q"],
                install_commands=[["pip", "install", "pytest"]],
                framework="pytest",
                test_timeout=25,
                language="python",
                commit_sha="2" * 40,
            )
        )
        timed_s = round(time.perf_counter() - start, 1)
        check(
            "test timeout enforced and reported",
            timed.status == "timeout" and timed.passed is False,
            f"status={timed.status} reason={timed.reason} "
            f"exit={timed.exit_code} in {timed_s}s",
        )

        # Cancellation: a mid-run cancel must terminate and report.
        cancel = threading.Event()

        def _cancel_soon() -> None:
            time.sleep(8)
            cancel.set()

        trigger = threading.Thread(target=_cancel_soon, daemon=True)
        trigger.start()
        start = time.perf_counter()
        cancelled = backend.run(
            ExecutionRequest(
                snapshot=sleepy,
                test_command=["python", "-m", "pytest", "-q"],
                install_commands=[["pip", "install", "pytest"]],
                framework="pytest",
                test_timeout=120,
                language="python",
                commit_sha="2" * 40,
                cancel=cancel,
            )
        )
        trigger.join(timeout=60)
        cancelled_s = round(time.perf_counter() - start, 1)
        check(
            "cancellation terminates sandbox and reports cancelled",
            cancelled.status == "cancelled",
            f"status={cancelled.status} in {cancelled_s}s",
        )

        if include_js:
            js_repo = make_js_repo(workdir)
            start = time.perf_counter()
            js_result = backend.run(
                ExecutionRequest(
                    snapshot=js_repo,
                    test_command=["npm", "test", "--silent"],
                    install_commands=[["npm", "install"]],
                    framework="npm",
                    test_timeout=240,
                    language="javascript",
                    commit_sha="3" * 40,
                    repo_name="acceptance/jscalc",
                )
            )
            js_s = round(time.perf_counter() - start, 1)
            check(
                "javascript executes with parsed failure",
                js_result.status == "test_failure"
                and js_result.passed is False
                and bool(js_result.failures),
                f"status={js_result.status} "
                f"failures={len(js_result.failures)} in {js_s}s",
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
