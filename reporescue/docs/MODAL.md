# Modal Cloud Execution (Phase 5B)

RepoRescue runs untrusted repository code in isolated **Modal cloud
sandboxes**. Modal is only the execution provider: scheduling,
persistence, ingestion, AI diagnosis, Patch Guard, and reporting stay
in RepoRescue's own backend.

## How it works

One backend run (`ExecutionBackend.run(request) -> ExecutionResult`):

1. The worker stages the repository snapshot locally: bounded,
   secret-free, symlink-free, with a commit-identity file
   (`app/snapshot.py`). Git history, local environments (`.execvenv`,
   `venv`, `node_modules`), and secret files never leave the worker.
2. Dependencies bake into a Modal **Image** — the explicitly
   controlled installation stage. Python uses `debian-slim` plus
   `pip install` steps translated from the validated install plan;
   JavaScript uses a `node:slim` image plus `npm ci`/`npm install`.
3. A fresh sandbox starts from that image with CPU/memory limits and
   **`block_network=True`**: tests run with no network access.
4. The snapshot archive uploads over the SDK file API, extracts into
   `/repo`, and the commit identity round-trips before tests run. A
   mismatch refuses execution — a different commit is never silently
   substituted.
5. The test command executes with a timeout while output streams
   through a capped collector (tail kept, truncation explicitly
   marked). Cancellation terminates the sandbox promptly and reports
   `cancelled`, never a fake pass or fail.
6. The sandbox is terminated and the staging tree removed in a
   `finally` block: cleanup never depends on reporting.

Baseline and verification each get a fresh sandbox from a freshly
resolved image, so verification never inherits untrusted baseline
state. Modal content-caches image layers, so unchanged dependencies
do not reinstall.

## Requirements

- Python 3.10+ backend with `modal==1.6.3` (pinned in
  `backend/requirements.txt`; implementation verified against this
  release only).
- A Modal account with API token credentials.

## Token setup

Create a token at [modal.com](https://modal.com) (account settings →
API tokens). The worker needs two values:

- `MODAL_TOKEN_ID`
- `MODAL_TOKEN_SECRET`

Set them as environment variables in the **trusted worker
environment** (process environment, container secrets, or your cloud
provider's secret manager). Never commit them, never bake them into
images, never pass them to repository code, and never print them:
RepoRescue checks only their *presence* and fails closed without
them.

```powershell
# Local worker shell (Windows, values never echoed back)
$env:MODAL_TOKEN_ID = "<token id>"
$env:MODAL_TOKEN_SECRET = "<token secret>"
python -m app.worker
```

For local development only, `REPORESCUE_MODAL_USE_SDK_CONFIG=1` also
honors `~/.modal.toml` (`modal setup`). Workers should leave this
off and use explicit environment credentials.

## Configuration

All in `backend/.env` (see `backend/.env.example`); every value has a
safe default:

| Variable | Default | Meaning |
|---|---|---|
| `REPORESCUE_EXECUTION_BACKEND` | `auto` | `auto`, `modal`, `docker`, or `unsafe-local` (below). |
| `REPORESCUE_MODAL_CPU` | `1` | Sandbox CPU request, fractional cores. |
| `REPORESCUE_MODAL_MEMORY_MB` | `1024` | Sandbox memory request, MiB. |
| `REPORESCUE_MODAL_APP_NAME` | `reporescue-executions` | Modal App sandboxes attach to. |
| `REPORESCUE_MODAL_NODE_IMAGE` | `node:22-slim` | Explicit override for JS sandboxes; otherwise `engines.node` selects `node:<major>-slim` (18–24) with the same default. |
| `REPORESCUE_SNAPSHOT_MAX_TOTAL_BYTES` | `134217728` | Max staged snapshot size (128 MiB). |
| `REPORESCUE_SNAPSHOT_MAX_FILE_BYTES` | `10485760` | Max single staged file (10 MiB). |
| `REPORESCUE_SNAPSHOT_MAX_FILES` | `20000` | Max staged file count. |

Python sandbox versions follow the repository's `requires-python`
(3.9–3.13, default 3.12), mirroring the Docker backend.

## Backend selection

`REPORESCUE_EXECUTION_BACKEND`:

- `auto` (default): Modal when its credentials are configured, else
  Docker when its daemon is reachable, else unisolated host execution
  only with `REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION=1`. Otherwise
  investigations fail closed with `runtime_unavailable`.
- `modal`: cloud sandboxes or a loud `modal_unavailable` error — never
  a quiet substitution.
- `docker`: preserved local isolated path (bind-mounted containers,
  network-blocked test stage).
- `unsafe-local`: development only; results are labelled
  `none-unsafe-local-dev-only`.

There is no silent fallback: an unavailable explicit choice is an
error naming that backend.

## Network policy

- Test execution: network **blocked** (`block_network=True`).
- Dependency installation: networked **image build** only. The build
  runs `pip`/`npm` install steps from the already-validated plan
  (plain package specs; URLs, VCS references, local paths, and pip
  options are rejected at planning time). Package setup scripts
  execute in Modal's ephemeral build environment, never on the
  worker host, and the image is discarded after use.
- The worker host never forwards its environment into sandboxes: test
  processes receive a fixed safe dict (`PIP_NO_INPUT`,
  `PYTHONDONTWRITEBYTECODE`, plus `CI=true` for JavaScript).

## Resource limits and timeouts

- Sandbox CPU/memory from the configuration above; Modal enforces
  them per container.
- Per-command timeouts: install translation is bounded by the image
  build; snapshot transfer/setup commands get 120 s; the test command
  gets the request's `test_timeout`.
- Sandbox lifetime is bounded (`test_timeout + 300 s`, minimum
  600 s): a crashed worker's sandboxes die on their own.
- Output is capped (8000 chars tail, truncation marked); memory stays
  bounded while streaming.

## Heavy repositories

Oversized snapshots fail fast with `snapshot_too_large`,
`snapshot_file_too_large`, or `snapshot_too_many_files` — never a
partial transfer presented as complete. The ingestion clone limit
(`REPORESCUE_MAX_CLONE_BYTES`, default 256 MiB) still applies
first. Raise the snapshot caps only alongside sandbox memory.

## Local development

```powershell
cd reporescue/backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# copy .env.example to .env and fill in values (never commit .env)
python -m pytest tests/test_modal_backend.py tests/test_snapshot.py
```

The automated suite uses deterministic **mocked** Modal tests — no
credentials, no network, no cost.

## Controlled real-cloud acceptance

`backend/verify_modal_phase5b.py` performs one small bounded Python
run (and optionally a small JavaScript run) against real Modal:

```powershell
cd reporescue/backend
$env:MODAL_TOKEN_ID = "<token id>"
$env:MODAL_TOKEN_SECRET = "<token secret>"
python verify_modal_phase5b.py            # Python acceptance
python verify_modal_phase5b.py --include-js   # also JavaScript
```

Exit codes: `0` all checks passed, `1` a check failed, `3`
credentials/SDK unavailable (nothing was run). Each run creates one
sandbox, executes seconds of tests, and terminates it; expected cost
is negligible, but production deployment and sustained usage still
require owner authorization.

## Cloud deployment (trusted worker)

1. Deploy the backend + worker as usual (FastAPI + `python -m
   app.worker`, PostgreSQL, Supabase Auth).
2. Provide `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` via the platform's
   secret mechanism (not baked into images, not in client bundles).
3. Set `REPORESCUE_EXECUTION_BACKEND=modal` to pin cloud execution
   (or leave `auto` to allow Docker where present).
4. Tune `REPORESCUE_MODAL_CPU`/`REPORESCUE_MODAL_MEMORY_MB` and the
   snapshot caps for your workload.
5. Run `verify_modal_phase5b.py` once from the worker environment to
   prove authentication, transfer, execution, and cleanup.

## Troubleshooting

| Symptom | Reason | Action |
|---|---|---|
| `modal_unavailable` | No credentials or SDK | Set token env vars; `pip install -r requirements.txt`. |
| `auth_error` | Wrong/revoked token | Rotate the token; check for truncation. Values are never logged — verify presence only. |
| `connection_error` | Network/API unreachable | Check egress and Modal status; retry. |
| `quota_exhausted` | Out of credits/concurrency | Check plan/quota in Modal dashboard. |
| `image_build_failed` | Dependency install failed | Inspect `install_log`; the repo's declared deps are uninstallable. |
| `snapshot_too_large` / `snapshot_file_too_large` | Repo exceeds caps | Raise caps deliberately, or slim the repo. |
| `snapshot_mismatch` | Transfer corruption | Retry; never bypass — this guard prevents executing unexpected content. |
| `exec_timeout` / `timeout` | Slow tests/install | Check test time; raise `test_timeout` via request budgets. |
| `unsupported` | Non-Python/JS command | Expected: only Python and JavaScript commands execute. |

## Safe rollback

Set `REPORESCUE_EXECUTION_BACKEND=docker` (needs Docker Desktop
reachable from the worker) to return to the preserved local isolated
path with no code changes. `auto` does this automatically when Modal
credentials are removed.
