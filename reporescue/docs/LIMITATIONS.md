# RepoRescue Limitation Register (Phase 5 reliability audit)

Material limitations discovered during implementation. Each entry names the
affected subsystem, impact, evidence, resolution or justified deferral, and
the covering acceptance test. This register is authoritative: anything listed
here as deferred is explicitly out of scope for the current step, not
overlooked.

Updated in Phase 5B (Modal execution work): L3, L4, L6, L7, L9, L12 carry
split status for the Modal path; L10 is resolved and recorded in place;
L13 records this PC's driver policy. Nothing was removed silently: every
original entry is preserved with its actual status.

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
- **Resolution:** Split status. The detection adapter stays inspection-only
  by design (pipeline wiring still deferred to the JS execution-adapter
  phase); the *execution layer* gained genuine, tested JavaScript support
  (npm install planning, test-command selection, node --test/TAP/Jest/
  Vitest/Mocha output parsing, Modal Node sandboxes). The adapter
  cannot misreport capability: the values are constants, not inferences.
  Execution-layer JS is covered by the tests below, not by the adapter.
- **Acceptance test:** `tests/test_languages.py`
  (`test_javascript_never_reports_runtime_or_verification`); execution
  layer: `tests/test_js_execution.py` (incl. a real npm lifecycle test),
  `tests/test_modal_backend.py` (mocked Node runs),
  `verify_modal_phase5b.py --include-js` (real cloud).

### L4 — Docker daemon and E2E reliability in this environment
- **Subsystem:** execution, `tests/test_real_investigation.py` Docker E2E
- **Impact:** When the Docker daemon is unreachable from the shell, the
  real-Docker E2E test skips. A skip is reported as a skip, never as a
  pass.
- **Evidence:** Skip reason `docker unavailable` in suite output; daemon
  pipe permission errors observed in sandbox shells. Extreme setup-time
  variance also observed on Docker Desktop/Windows bind mounts: the
  identical E2E flow measured 293s, 649s, 745s, 753s, and 906s total
  across runs (failures cluster at high durations against the 720s
  setup budget; the flow also passes with identical code, so this is
  load variance, not a code regression). Modal cloud runs of the same
  flow take ~10s with warm image layers.
- **Resolution:** Environmental. No code change; requires Docker Desktop
  reachable from the running shell. Modal cloud execution
  (`verify_modal_phase5b.py`, 9/9 real-cloud checks on 2026-11-04) now
  provides an independent E2E path needing no local daemon.
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

### L6 — Container hardening is partial (updated)
- **Subsystem:** execution (Docker backend)
- **Impact:** The Docker path now runs with `--cpus`, `--memory`,
  `--pids-limit`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  a tmpfs `/tmp`, `--network none` for test runs, and forced container
  removal with timeouts (see container arguments in
  `backend/app/execution.py`). Still outstanding on the Docker path: a
  non-root user, read-only rootfs, disk quotas, digest pinning, and a
  host-crash reaper. Do not treat Docker execution as a full sandbox
  boundary on its own.
- **Evidence:** container arguments in `backend/app/execution.py`.
- **Resolution:** Modal path (Phase 5B): provider-isolated sandboxes with
  CPU/memory limits, network-blocked test runs, networked installs
  only at image build, secret isolation (fixed safe env; tokens never
  forwarded), bounded lifetimes, mid-run cancellation via termination,
  and finally-block cleanup. Process-count limits rely on Modal
  runtime defaults (the SDK exposes no pids-limit). The hosted
  deployment should use `REPORESCUE_EXECUTION_BACKEND=modal`; never
  use `unsafe-local` for untrusted repositories.
- **Acceptance test:** existing Docker cleanup/timeout tests (12/12 when
  Docker runs). Modal: `tests/test_modal_backend.py` (mocked
  isolation/transfer) plus `verify_modal_phase5b.py` (real cloud).

### L7 — Demo fixture report has no confidence label
- **Subsystem:** `backend/app/verdict.py` (`build_report`), orchestrator demo
- **Impact:** Only real reports carry `diagnosis_confidence`; the frozen
  deterministic demo keeps its exact legacy keys. Real reports now
  additionally carry an `outcome` evidence label (`verified_fix`,
  `already_green`, `not_executed`, `reproduced_unfixed`) alongside
  `diagnosis_confidence`; error/timeout/cancel/unsupported outcomes
  keep their distinct reasons and job statuses.
- **Evidence:** `REPORT_KEYS` unchanged; `test_build_report_keeps_api_contract_keys`.
- **Resolution:** Accepted; the demo is preserved legacy, and its
  limitations already state the patch is deterministic, not verified AI output.
- **Acceptance test:** `tests/test_verdict.py`,
  `tests/test_execution_lifecycle.py` (outcome labels).

### L8 — Enqueue required a live ingestion record (resolved in MVP stabilisation)
- **Subsystem:** ingestion store, `POST /api/jobs`
- **Impact:** A server restart (or FIFO eviction) between Inspect and Start
  forces re-inspection; enqueue fails with 404/422 instead of queuing.
  Once queued, the worker re-ingests from the pinned SHA and no longer
  depends on API-process memory.
- **Evidence:** `create_job_route` owner/record checks; worker re-ingest
  from `repo_url`/`commit_sha`.
- **Resolution:** Resolved in MVP stabilisation. The ingest route persists
  a `repo_snapshots` row (migration 0006) and job creation resolves
  memory-then-database with owner checks, so enqueue survives API
  restarts and refreshes; ad-hoc URLs need no project record.
- **Acceptance test:** `tests/test_jobs.py`
  (`test_create_job_route_survives_restart_via_snapshot`,
  `test_snapshot_rows_are_owner_isolated`,
  `test_create_job_route_missing_ingestion_is_404`).

### L9 — Cancellation takes effect at stage boundaries
- **Subsystem:** worker, execution backends
- **Impact (Docker/UnsafeLocal, unchanged):** Cancelling mid-stage does not
  kill an in-flight container;
  the run finishes its own per-operation timeout, existing cleanup
  applies, then the job settles as cancelled.
  Modal path (Phase 5B): cancellation is polled during test execution;
  a fired signal terminates the sandbox and the run reports `cancelled`
  (never a normal failure or success). Boundary checks also precede
  setup and test stages on every backend.
- **Evidence:** `check_cancelled` placement; blocking `backend.run`.
- **Resolution:** Accepted for Docker/UnsafeLocal (bounded by existing
  timeouts, no orphaned work). Modal mid-run cancel is implemented;
  see acceptance tests.
- **Acceptance test:** `tests/test_jobs.py` cancel tests,
  `tests/test_execution_lifecycle.py` (cancel mapping),
  `tests/test_modal_backend.py` (mid-run cancel),
  `verify_modal_phase5b.py` (real-cloud cancel).

### L10 — Multi-worker scale testing (resolved in Phase 5B)
- **Subsystem:** `claim_next_job`
- **Impact:** The conditional-UPDATE claim is correct for N workers, but
  soak testing covers a 2-thread SQLite race plus sequential claims.
- **Evidence:** original `test_concurrent_claim_single_winner` (retained).
- **Resolution:** Resolved in Phase 5B. Claim/lease/recovery paths are
  now covered by an 8-thread single-winner race, a 4-worker/6-job
  no-duplicate race, lease-expiry requeue-then-fail cycles, and
  recovery evidence checks. Single-worker remains the supported
  deployment topology; high-concurrency soak stays operational work.
- **Acceptance test:** `tests/test_jobs.py` claiming tests,
  `tests/test_execution_lifecycle.py` (8-thread race, lease/recovery).

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
- **Resolution:** Partially addressed in Phase 5B; install budget unchanged.
  Snapshot transfer caps (total/file/count) fail fast instead of
  partially transferring, and Modal image layers cache unchanged
  dependencies across baseline/verification. The 240s per-step install
  budget and Phase 7 caching work are unchanged.
- **Acceptance test:** install-timeout classification tests;
  snapshot-cap tests in `tests/test_snapshot.py`; Modal bake in
  `tests/test_modal_backend.py` plus real-cloud acceptance.

### L13 — Native driver loads transiently blocked on this PC
- **Subsystem:** local verification environment (Application Control policy)
- **Impact:** the `psycopg` binary `pq` DLL and SQLAlchemy 2.1 C extensions
  were refused load here for part of 2026-11-04, failing
  `test_create_engine_for_server_uses_queue_pool` and forcing a temporary
  local SQLAlchemy 2.0.44 (pure Python). Enforcement later lifted with no
  code change: `psycopg` and SQLAlchemy 2.1.8 import normally and the
  suite runs against pinned requirements. If the refusal recurs, expect
  the same single-test failure; product code is unaffected.
- **Evidence:** `ImportError: DLL load failed ... Application Control policy
  has blocked this file` for `pq` and `_immutabledict_cy` on 2026-11-04.
- **Resolution:** Environmental; out of product scope. Re-run where the
  policy allows the drivers, or install a system libpq for psycopg's
  python implementation.
- **Acceptance test:** `tests/test_db.py` (green when drivers load).

### L14 — Windows long-path checkout failures (resolved in MVP stabilisation)
- **Subsystem:** `backend/app/github_ingest.py`, `backend/app/repo_context.py`
- **Impact:** Git could not check out deeply nested generated paths
  (e.g. `frontend/.expo/web/cache/...` in Kunle777/change-app),
  failing ingestion with `clone_failed` on Windows.
- **Evidence:** reported checkout failure on the nested Expo cache path.
- **Resolution:** Resolved. File-writing git commands run with
  `core.longpaths=true`, and `.expo` plus other tool-generated caches
  (`.output`, `.turbo`, `.parcel-cache`, `.vercel`, `.nyc_output`)
  join the shared `EXCLUDED_DIRS`, keeping them out of inventory,
  snapshots, and patches without touching real sources or manifests.
- **Acceptance test:** `tests/test_github_ingest.py`
  (`test_clone_checkout_commands_enable_windows_longpaths`),
  `tests/test_repo_context.py` (expo parametrize cases),
  `tests/test_snapshot.py`, `tests/test_patchset_guard.py`.

### L15 — Rate limiting is per process
- **Subsystem:** `backend/app/rate_limit.py`, expensive POST routes
- **Impact:** ingest/job/classify/demo budgets are enforced from
  in-process memory, so a multi-worker API deploy enforces per-process
  budgets rather than one exact global budget. Normal use never
  approaches the limits (20–60 calls/minute per bucket); the bound
  still caps total load by worker count.
- **Evidence:** `LIMITS` and bucket store in `backend/app/rate_limit.py`.
- **Resolution:** Accepted for the current topology. A shared store
  (Redis) would be needed for exact global limits.
- **Acceptance test:** `tests/test_rate_limit.py`.

### L16 — Migration chain requires PostgreSQL
- **Subsystem:** `backend/alembic/versions/0002_*` onward
- **Impact:** revisions after 0001 use constraint operations that
  SQLite cannot execute, so `alembic upgrade head` only runs against
  PostgreSQL (the production target). Tests build schema from
  SQLAlchemy metadata instead and are unaffected.
- **Evidence:** `NotImplementedError: No support for ALTER of
  constraints in SQLite dialect` when upgrading a scratch SQLite DB.
- **Resolution:** By design; migrations target Supabase PostgreSQL.
  Revision 0007 (pre-classification columns, run commands) was
  validated in both directions against SQLite batch mode in isolation.
- **Acceptance test:** `alembic upgrade head` on PostgreSQL; suite
  schema parity via `Base.metadata.create_all`.

### L17 — Meta API live verification (2026-11-05)
- **Subsystem:** `backend/app/meta_client.py`
- **Impact:** None outstanding. The configured endpoint
  (`https://api.meta.ai/v1/chat/completions`) and model
  (`muse-spark-1.3`) were verified live with the project key: the
  request shape (`model` + `messages` + `max_completion_tokens` +
  `reasoning_effort`) is accepted and the response shape
  (`choices[0].message.content`, `finish_reason`,
  `message.refusal`) matches the parser. Repair payloads use a 2000
  token budget (aligned with the ~8000-char patch guard); prose
  answers stay at 800.
- **Evidence:** `backend/test_meta_connection.py` probe output.
- **Resolution:** Verified; no action. The earlier `invalid_output`
  failures were most plausibly empty/truncated responses under the
  old 800-token repair budget; truncation now surfaces a specific
  actionable message.
- **Acceptance test:** `tests/test_meta_client.py` response-shape
  fixtures (string, content blocks, refusal, filter, truncation,
  error envelope).
