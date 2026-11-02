# RepoRescue Limitation Register (Phase 5 reliability audit)

Material limitations discovered during implementation. Each entry names the
affected subsystem, impact, evidence, resolution or justified deferral, and
the covering acceptance test. This register is authoritative: anything listed
here as deferred is explicitly out of scope for the current step, not
overlooked.

## Active limitations

### L1 — Language adapters exist only for Python and JavaScript
- **Subsystem:** `backend/app/languages`, `backend/app/python_detect.py`
- **Impact:** Go, Rust, Java, C#, Ruby, and PHP repositories get only a
  manifest-name `primary_language`; no per-language capabilities.
- **Evidence:** `_primary_language` fallback branches; `detect_all` runs two
  adapters.
- **Resolution:** Deferred to a later language-expansion phase. No execution
  is ever implied for these languages.
- **Acceptance test:** none yet (recorded here).

### L2 — Python Poetry project identity not detected
- **Subsystem:** `backend/app/languages/python.py` (`_project_identity`)
- **Impact:** `AdapterResult.project` name/version come from the
  `[project]` table only; Poetry-only projects report no identity.
  Eligibility is unaffected (identity is display metadata).
- **Evidence:** `_project_identity` reads `[project]` name/version only.
- **Resolution:** Deferred; small enhancement when Poetry support expands.
- **Acceptance test:** `tests/test_languages.py`
  (`test_python_adapter_exposes_contract` covers the `[project]` case).

### L3 — JavaScript is inspection-only
- **Subsystem:** `backend/app/languages/javascript.py`, execution
- **Impact:** No npm install, no script/test execution, no JS Docker runs.
  Test tooling is reported as inspection signals only.
- **Evidence:** `runtime_available=False`, `runtime_reason=
  "execution_not_supported"` unconditionally.
- **Resolution:** Deferred to the JS execution-adapter phase. The adapter
  cannot misreport capability: the values are constants, not inferences.
- **Acceptance test:** `tests/test_languages.py`
  (`test_javascript_never_reports_runtime_or_verification`).

### L4 — Docker daemon and E2E reliability in this environment
- **Subsystem:** execution, `tests/test_real_investigation.py` Docker E2E
- **Impact:** When the Docker daemon is unreachable from the shell, the
  real-Docker E2E test skips. A skip is reported as a skip, never as a
  pass.
- **Evidence:** Skip reason `docker unavailable` in suite output; daemon
  pipe permission errors observed in sandbox shells.
- **Resolution:** Environmental. No code change; requires Docker Desktop
  reachable from the running shell.
- **Acceptance test:** Docker E2E (skipped here; must pass where Docker runs).

### L5 — Verification covers executed cycles only
- **Subsystem:** `backend/app/real_investigation.py` attempt loop
- **Impact:** Retried attempts (invalid model output, guard-rejected
  patches) produce no verification record; the verdict covers the
  executed suite only, never unrun behavior.
- **Evidence:** `continue` paths skip `record_attempt`/verification; stated
  in every report's `limitations`.
- **Resolution:** Accepted by design; retried attempts still count toward
  `attempts`. No silent success is possible.
- **Acceptance test:** existing attempt/guard tests; `tests/test_jobs.py`
  worker E2E asserts recorded attempts/runs.

### L6 — Container hardening still outstanding
- **Subsystem:** execution (Docker backend)
- **Impact:** No non-root user, read-only rootfs, tmpfs mounts, disk
  quotas, digest pinning, or host-crash reaper. Isolation is Docker
  defaults plus timeouts, cleanup, and secret scrubbing.
- **Evidence:** container arguments in `backend/app/execution.py`.
- **Resolution:** Deferred to the security-hardening phase; each flag needs
  reliability testing against venv/pip/test workloads first.
- **Acceptance test:** existing Docker cleanup/timeout tests (12/12 when
  Docker runs).

### L7 — Demo fixture report has no confidence label
- **Subsystem:** `backend/app/verdict.py` (`build_report`), orchestrator demo
- **Impact:** Only real reports carry `diagnosis_confidence`; the frozen
  deterministic demo keeps its exact legacy keys.
- **Evidence:** `REPORT_KEYS` unchanged; `test_build_report_keeps_api_contract_keys`.
- **Resolution:** Accepted; the demo is preserved legacy, and its
  limitations already state the patch is deterministic, not verified AI output.
- **Acceptance test:** `tests/test_verdict.py`.

### L8 — Enqueue requires a live ingestion record
- **Subsystem:** ingestion store, `POST /api/jobs`
- **Impact:** A server restart (or FIFO eviction) between Inspect and Start
  forces re-inspection; enqueue fails with 404/422 instead of queuing.
  Once queued, the worker re-ingests from the pinned SHA and no longer
  depends on API-process memory.
- **Evidence:** `create_job_route` owner/record checks; worker re-ingest
  from `repo_url`/`commit_sha`.
- **Resolution:** Accepted; the enqueue window is seconds, and the failure
  message tells the user to re-inspect.
- **Acceptance test:** `tests/test_jobs.py`
  (`test_create_job_route_missing_ingestion_is_404`).

### L9 — Cancellation takes effect at stage boundaries
- **Subsystem:** worker, execution backends
- **Impact:** Cancelling mid-stage does not kill an in-flight container;
  the run finishes its own per-operation timeout, existing cleanup
  applies, then the job settles as cancelled.
- **Evidence:** `check_cancelled` placement; blocking `backend.run`.
- **Resolution:** Accepted; bounded by existing timeouts, no orphaned work.
- **Acceptance test:** `tests/test_jobs.py` cancel tests.

### L10 — Multi-worker scale tested to a 2-thread race only
- **Subsystem:** `claim_next_job`
- **Impact:** The conditional-UPDATE claim is correct for N workers, but
  soak testing covers a 2-thread SQLite race plus sequential claims.
- **Evidence:** `test_concurrent_claim_single_winner`.
- **Resolution:** Deferred; single-worker deployment is the supported
  topology for this phase.
- **Acceptance test:** `tests/test_jobs.py` claiming tests.

### L11 — Conversation sessions are persistence only
- **Subsystem:** `conversation_sessions`, worker finalize
- **Impact:** Sessions with context snapshots are created at completion,
  but no conversation API or agent exists yet.
- **Evidence:** No routes reference `ConversationSession`.
- **Resolution:** Foundation for Phase 5C/5D by design.
- **Acceptance test:** `tests/test_jobs.py`
  (`test_conversation_session_snapshot`).

### L12 — Heavy-repository install budget unchanged
- **Subsystem:** execution (240s dependency-install budget)
- **Impact:** Large dependency sets (e.g. savings-vault-ledger's 12-spec
  install on a Windows bind mount) can still exceed the install budget;
  reported honestly as a setup blocker, never as test results.
- **Evidence:** Step A report; `dependency_error` classification.
- **Resolution:** Deferred to Phase 7 caching work.
- **Acceptance test:** install-timeout classification tests.
