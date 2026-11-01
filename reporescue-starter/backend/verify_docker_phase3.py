"""Phase 3 Docker runtime verification (run where the daemon is reachable).

Runs the REAL RepoRescue Docker backend against a controlled buggy Python
repository and checks the Task B evidence points:

 1. Docker selected as the execution backend (no silent fallback).
 2. Repository snapshot mounted (baseline executes the snapshot's tests).
 3. Baseline tests execute inside Docker.
 4. stdout/stderr/exit codes/timeouts captured (incl. a real timeout run).
 5. Diagnosis grounded in real repository evidence (stub asserts this).
 6. Patch safety validation before application (cheat patch rejected first).
 7. Patch applied only in the disposable workspace (source repo untouched).
 8. Verification tests execute independently inside Docker.
 9. Final verdict reflects the verification outcome.
10. Containers cleaned up after success and after timeout.

Diagnosis uses a clearly identified TEST STUB by default (isolated pipeline
verification, not AI). Pass --live-meta to run one additional investigation
with the live Meta API (needs META_API_KEY); its outcome is reported as-is.

Exit codes: 0 all checks passed, 1 a check failed, 3 daemon unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

from app import ingestion as ingestion_module  # noqa: E402
from app import real_investigation as pipeline  # noqa: E402
from app.execution import (  # noqa: E402
    DockerBackend,
    ExecutionRequest,
    select_backend,
)
from app.ingestion import clear_ingestions, ingest_repository  # noqa: E402
from app.patch_guard import FileEdit, PatchSet  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result


def make_buggy_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    identity = ["-c", "user.email=t@e.com", "-c", "user.name=t"]
    git(["init", "-b", "main"], repo)
    git([*identity, "add", "."], repo)
    git([*identity, "commit", "-m", "init"], repo)
    return repo


def reporescue_containers() -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=reporescue-",
         "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return sorted(n for n in result.stdout.split() if n)


def load_dotenv() -> None:
    env_file = BACKEND / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main(live_meta: bool = False) -> int:
    real_run = subprocess.run
    docker_runs: list[list[str]] = []

    def spy(*args, **kwargs):
        argv = list(args[0]) if args else []
        if argv[:2] == ["docker", "run"]:
            docker_runs.append(argv)
        return real_run(*args, **kwargs)

    # Precheck: daemon must be reachable from THIS user.
    available, reason = DockerBackend().is_available()
    print(f"docker available: {available} ({reason})")
    if not available:
        print("SKIP: Docker daemon is not reachable from this user.")
        return 3

    tmp = Path(tempfile.mkdtemp(prefix="reporescue-verify-"))
    try:
        repo = make_buggy_repo(tmp)
        before = (repo / "pkg" / "calc.py").read_bytes()

        real_clone = pipeline.clone_and_pin

        def local_clone(url, destination, ref=None):
            assert url == "https://github.com/acme/calc"
            return real_clone(str(repo), destination, ref)

        ingestion_module.clone_and_pin = local_clone
        pipeline.clone_and_pin = local_clone

        # 1. Docker genuinely selected (ambient selection, no forced backend).
        backend, error = select_backend()
        check(
            "1. docker selected as execution backend",
            backend is not None and backend.name == "docker" and error is None,
            f"backend={getattr(backend, 'name', None)}",
        )
        if backend is None or backend.name != "docker":
            return 1

        clear_ingestions()
        record = ingest_repository("https://github.com/acme/calc")
        assert record["status"] == "complete", record

        calls = {"n": 0}

        def stub_diagnose(**kwargs):
            """TEST STUB (not Meta): asserts real evidence, cheats once."""
            calls["n"] += 1
            evidence = kwargs.get("evidence_files") or {}
            failures = kwargs.get("failures") or []
            tail = kwargs.get("baseline_tail") or ""
            assert evidence, "stub received no evidence files"
            assert any("test_calc" in f.get("test", "") for f in failures), (
                f"stub failures do not reference the real test: {failures}"
            )
            assert "FAILED" in tail or "failed" in tail, (
                "stub baseline tail lacks failure output"
            )
            if calls["n"] == 1:
                return "cheat attempt", PatchSet(
                    edits=(FileEdit(
                        "tests/test_calc.py",
                        "assert add(2, 3) == 5",
                        "assert True",
                    ),),
                    reason="Cheat.",
                )
            return "add subtracts instead of adding.", PatchSet(
                edits=(FileEdit(
                    "pkg/calc.py", "    return a - b", "    return a + b"
                ),),
                reason="Fix the operator.",
            )

        subprocess.run = spy  # type: ignore[assignment]
        try:
            result = pipeline.investigate_repository(
                record["ingestion_id"], diagnose=stub_diagnose
            )
        finally:
            subprocess.run = real_run  # type: ignore[assignment]

        print(f"[info] result status={result.get('status')} "
              f"reason={result.get('reason')} "
              f"attempts={result.get('attempts')} "
              f"verdict={result.get('verdict')}")
        if result.get("status") != "complete":
            print(f"[info] message={(result.get('message') or '')[:800]}")
        print(f"[info] baseline keys={sorted(result.get('baseline', {}).keys())} "
              f"verification keys={sorted(result.get('verification', {}).keys())}")

        baseline = result.get("baseline", {})
        verification = result.get("verification", {})
        bl_failures = baseline.get("failures", [])

        # 2/3. Snapshot mounted + baseline ran in Docker.
        check(
            "2. snapshot mounted (baseline ran the snapshot tests)",
            any("test_calc" in f.get("test", "") for f in bl_failures),
            f"failures={[f.get('test') for f in bl_failures]}",
        )
        check(
            "3. baseline executed inside Docker",
            baseline.get("backend") == "docker"
            and baseline.get("isolation") == "docker-container",
            f"backend={baseline.get('backend')}",
        )
        # 4. stdout/stderr/exit captured (timeout covered separately below).
        check(
            "4. stdout/stderr/exit captured on baseline",
            baseline.get("exit_code") == 1
            and "FAILED" in (baseline.get("output") or ""),
            f"exit={baseline.get('exit_code')}",
        )
        # 5. Diagnosis based on real evidence (stub asserts; reaching attempt 2
        # proves the assertions held on attempt 1).
        check(
            "5. diagnosis grounded in real evidence (stub asserts)",
            calls["n"] >= 2 and result.get("status") == "complete",
            f"diagnose_calls={calls['n']}",
        )
        # 6. Guard validated before application (cheat rejected, then success).
        check(
            "6. cheat patch rejected, real patch guarded",
            result.get("attempts") == 2
            and (result.get("guard") or {}).get("validated") is True,
            f"attempts={result.get('attempts')}",
        )
        # 7. Patch only in the disposable workspace.
        after = (repo / "pkg" / "calc.py").read_bytes()
        applied = (result.get("patch") or {}).get("applied", [])
        check(
            "7. source repo untouched; patch applied to work copy only",
            after == before
            and [a.get("file") for a in applied] == ["pkg/calc.py"],
            f"applied={applied}",
        )
        # 8. Independent verification inside Docker.
        check(
            "8. verification ran independently inside Docker",
            verification.get("backend") == "docker"
            and verification.get("passed") is True,
            f"passed={verification.get('passed')}",
        )
        # 9. Verdict reflects the outcome.
        check(
            "9. verdict reflects baseline->verification transition",
            result.get("status") == "complete"
            and result.get("verdict") == "TESTS_PASSED"
            and baseline.get("passed") is False
            and verification.get("passed") is True,
            f"verdict={result.get('verdict')}",
        )
        # 10a. Cleanup after success.
        leftovers = reporescue_containers()
        check("10a. containers cleaned after success", leftovers == [],
              f"leftovers={leftovers}")

        # 4b/10b. Real timeout run + cleanup after timeout.
        slow = tmp / "slow"
        (slow / "tests").mkdir(parents=True)
        (slow / "tests" / "test_slow.py").write_text(
            "import time\n\n\ndef test_slow():\n    time.sleep(60)\n",
            encoding="utf-8",
        )
        timeout_result = DockerBackend().run(
            ExecutionRequest(
                snapshot=slow,
                install_commands=[["pip", "install", "pytest"]],
                test_timeout=8,
            )
        )
        check(
            "4b. timeout captured (status/exit)",
            timeout_result.status == "timeout"
            and timeout_result.exit_code is None
            and timeout_result.passed is False,
            f"status={timeout_result.status}",
        )
        leftovers = reporescue_containers()
        check("10b. containers cleaned after timeout", leftovers == [],
              f"leftovers={leftovers}")

        # Informational: hardening flags actually emitted on real runs.
        if docker_runs:
            argv = docker_runs[-1]
            for flag in ("--label", "--cpus", "--memory", "--pids-limit",
                         "--cap-drop", "--network"):
                print(f"[info] emitted {flag}: {flag in argv}")
            print(f"[info] docker run invocations observed: {len(docker_runs)}")
        else:  # pragma: no cover - defensive
            print("[info] no docker run invocations were observed")

        if live_meta:
            print("--- live Meta investigation (actual API diagnosis) ---")
            load_dotenv()
            has_key = bool(os.environ.get("META_API_KEY", "").strip())
            print(f"META_API_KEY present: {has_key} (value never printed)")
            clear_ingestions()
            record2 = ingest_repository("https://github.com/acme/calc")
            live = pipeline.investigate_repository(record2["ingestion_id"])
            print(f"live status={live.get('status')} "
                  f"reason={live.get('reason')} verdict={live.get('verdict')} "
                  f"attempts={live.get('attempts')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"--- {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed ---")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(live_meta="--live-meta" in sys.argv[1:]))
