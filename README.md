# RepoRescue

### Investigate repositories with evidence. Verify proposed fixes with execution.

RepoRescue is an AI-powered repository investigation workbench that helps developers understand unfamiliar codebases, investigate failures, and evaluate proposed fixes using real execution evidence.

Instead of simply asking an AI model to suggest a fix, RepoRescue follows an investigation workflow: inspect the repository, collect relevant evidence, identify potential issues, validate proposed changes, and independently verify whether a fix actually works.

Choose a public GitHub repository, describe your objective, and follow the investigation from the browser — including durable progress, findings, execution evidence, and the final report.

**The principle is simple: a proposed fix is not a verified fix.**

---

## What RepoRescue does

RepoRescue provides four investigation modes:

| Mode | Purpose |
|---|---|
| **General** | Understands the investigation objective and routes it to the most appropriate mode. |
| **Debug** | Investigates test failures, evaluates proposed patches, and independently verifies changes. |
| **Explain** | Explains repository structure and source code using inspected repository context. |
| **Health** | Assesses repository health using deterministic checks and AI-assisted interpretation. |

General is the default workbench experience. It uses transparent, deterministic signals to select Debug, Explain, or Health rather than pretending that an AI classification call has been made.

## How it works

<details>
<summary><strong>Architecture overview</strong></summary>

```mermaid
flowchart TB
    subgraph CLIENT["CLIENT"]
        UI["React + TypeScript + Vite"]
    end

    subgraph CONTROL["CONTROL PLANE"]
        API["FastAPI API"]
        WORKER["Python Worker"]
        ORCHESTRATOR["Investigation Orchestration"]
        GUARD["Patch Guard"]
    end

    subgraph DATA["PERSISTENCE AND IDENTITY"]
        AUTH["Supabase Auth"]
        DB[("Supabase PostgreSQL")]
        QUEUE["Persistent Job Queue"]
    end

    subgraph EXTERNAL["EXTERNAL SERVICES"]
        GH["Public GitHub"]
        META["Meta Model API"]
    end

    subgraph EXECUTION["ISOLATED EXECUTION PLANE"]
        MODAL["Modal"]
        SANDBOX["Fresh Isolated Sandbox"]
    end

    UI <-->|Authentication| AUTH
    UI -->|REST API| API
    API -->|JWT verification| AUTH
    API <-->|Projects, snapshots, jobs and reports| DB
    API -->|Repository inspection| GH
    API -->|Create jobs| QUEUE
    QUEUE <-->|Atomic claims, leases and progress| WORKER
    WORKER --> ORCHESTRATOR
    ORCHESTRATOR -->|Diagnosis and explanations| META
    META -->|Model responses| ORCHESTRATOR
    ORCHESTRATOR --> GUARD
    WORKER -->|Execution requests| MODAL
    MODAL --> SANDBOX
    SANDBOX -->|Execution evidence| WORKER
    WORKER -->|Persist results| DB
```

### Architecture responsibilities

**Frontend — Vercel**

The React and TypeScript application provides authentication, repository selection, investigation controls, live progress, findings, and final reports.

**API — FastAPI on Railway**

The API is the control plane. It verifies Supabase access tokens, enforces ownership, handles repository ingestion, manages inspection snapshots, and exposes job and report endpoints.

**Worker — Python on Railway**

The worker is a separate long-running process. It polls PostgreSQL for queued work, atomically claims jobs, maintains leases and heartbeats, recovers interrupted work, runs investigation orchestration, and persists progress and results.

The worker is deliberately separate from the API. Investigations are not dependent on an HTTP request remaining open.

**Database and identity — Supabase**

Supabase provides authentication and PostgreSQL persistence. PostgreSQL stores projects, inspection snapshots, investigations, jobs, progress, findings, execution attempts, patches, and reports.

The database-backed queue provides durable job state across API and worker restarts.

**AI — Meta Model API**

Meta provides model-assisted diagnosis, explanations, health interpretation, and structured patch proposals.

**Execution — Modal**

Modal provides isolated execution environments for repository baseline tests and independent verification. Each execution uses a fresh sandbox.

</details>

---

## Investigation modes

### 1. General — start with an objective

General is the default entry point.

Describe what you want to investigate, and RepoRescue evaluates the objective using transparent keyword signals.

It routes the request to:

- **Debug** when the objective indicates a failure or a need to investigate and verify a fix.
- **Explain** when the objective asks for understanding or explanation.
- **Health** when the objective concerns repository quality or general assessment.

An empty objective requires clarification. When no recognized signals are found, General falls back to a low-confidence Health assessment.

Routing is deterministic, not an AI classification call. The selected mode and routing explanation are persisted with the investigation.

### 2. Debug — investigate and verify

Debug is designed for eligible Python repositories with a recognized `pytest` or `unittest` test framework.

The workflow:

1. Re-ingests the repository at the commit pinned during inspection.
2. Selects the configured execution backend and runs the baseline test command.
3. Collects failure output and relevant source evidence.
4. Sends bounded evidence to Meta for diagnosis and a structured patch proposal.
5. Passes proposed edits through Patch Guard before applying them to a separate work copy.
6. Creates a fresh execution environment for independent verification.
7. Persists the baseline, patch outcome, verification evidence, findings, and final report.

A fix is only reported as verified when the independent verification run passes.

If a patch is rejected, execution is unavailable, verification fails, or a result remains unverified, RepoRescue reports that distinction rather than presenting a proposal as a successful fix.

Repositories without a runnable test framework may receive a static diagnosis and an explicitly unverified proposal. Unsupported projects may be rejected.

### 3. Explain — understand a codebase

Explain uses inspected repository structure and bounded source excerpts to answer questions about a repository.

It can help developers understand:

- Project structure and organization.
- Important source files and their responsibilities.
- Relationships between components.
- Relevant implementation details.

Explain is static-only. It does not install dependencies, execute repository code, or run tests.

### 4. Health — assess repository quality

Health evaluates deterministic checks over ingested repository metadata and uses Meta to interpret the observations.

It provides an evidence-based assessment rather than claiming to have performed a runtime audit.

Health is static-only: it does not install dependencies, execute repository code, or run tests.

---

## Debug verification: evidence before conclusions

The most important distinction in RepoRescue is between a suggested change and a verified result.

```mermaid
flowchart TD
    A["Inspect pinned repository commit"] --> B["Run baseline tests"]
    B --> C["Collect failure and source evidence"]
    C --> D["Meta diagnosis and patch proposal"]
    D --> E{"Patch Guard accepts edits?"}
    E -->|No| F["Report rejected proposal"]
    E -->|Yes| G["Apply patch to separate work copy"]
    G --> H["Create fresh execution sandbox"]
    H --> I["Run independent verification"]
    I --> J{"Verification passes?"}
    J -->|Yes| K["Report verified fix"]
    J -->|No| L["Report failed or unverified result"]
```

RepoRescue keeps the baseline and verification results distinct.

It does not commit changes to GitHub or silently modify the original repository. Reports describe the pinned repository snapshot and the evidence collected during that investigation.

---

## Security and execution isolation

Repository code is untrusted. RepoRescue is designed to keep repository inspection, investigation orchestration, and code execution within separate boundaries.

### Authentication and ownership

- Protected API routes verify Supabase access tokens.
- Project, snapshot, job, and investigation access is scoped to the authenticated owner.
- Snapshot retrieval and job operations enforce ownership checks.

### Repository ingestion

- Supports public HTTPS GitHub repository URLs.
- Git operations use validated arguments, timeouts, and a scrubbed environment.
- Repository inspection applies bounded context and file-selection rules.

### Snapshot preparation

Before execution, the worker stages a separate, bounded repository snapshot.

The staging process excludes:

- Git metadata.
- Environment and secret files.
- Local dependency environments.
- Symbolic links.
- Generated directories and excluded caches.

Snapshot size, individual file size, and file count are capped.

Inspection snapshots persisted in PostgreSQL contain repository metadata and bounded context, not execution archives. The worker reconstructs the pinned commit and prepares the filtered execution snapshot.

### Modal execution

- Baseline and verification use fresh isolated sandboxes.
- Dependency setup occurs in the execution image build.
- Network access is blocked during test commands.
- Execution has bounded resource and time limits.
- Cancellation and cleanup are handled by the execution backend.
- Missing Modal credentials must not silently cause an unsafe execution fallback.

For hosted deployment, explicitly configure:

```env
REPORESCUE_EXECUTION_BACKEND=modal
```

Keep `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` exclusively in the trusted worker environment.

Never use `unsafe-local` for untrusted repositories.

See [docs/MODAL.md](./docs/MODAL.md) for execution configuration and backend details.

---

## Technology stack

| Layer | Technologies |
|---|---|
| Frontend | React, TypeScript, Vite |
| Authentication | Supabase Auth, JWT verification |
| API | Python, FastAPI, Pydantic |
| Persistence | Supabase PostgreSQL, SQLAlchemy |
| Schema management | Alembic |
| Job processing | Separate Python worker, PostgreSQL-backed queue |
| Repository ingestion | Git, public GitHub HTTPS |
| AI | Meta Model API |
| Isolated execution | Modal |
| Local execution backend | Docker |

---

## Application workflow and API

The primary investigation workflow is asynchronous:

| Endpoint | Purpose |
|---|---|
| `POST /api/repos/ingest` | Inspect a repository and persist its inspection snapshot. |
| `POST /api/jobs` | Create a persistent investigation job. |
| `GET /api/jobs/{job_id}` | Retrieve job progress and results. |
| `GET /api/jobs/active` | Restore an active job after refresh. |
| `POST /api/jobs/{job_id}/cancel` | Request job cancellation. |
| `/api/projects` | Manage projects. |
| `GET /api/health` | Check API process liveness. |

The API also retains the synchronous `POST /api/investigations` endpoint and the fixture-only `POST /api/demo/investigate` endpoint.

Interactive API documentation is available at `/docs` when the API is running.

**Progress and persistence:** Jobs, progress, and results are stored durably in PostgreSQL. The frontend polls the API and can restore an active investigation after a browser refresh.

---

## Run locally

### Prerequisites

- Python 3.10 or newer.
- Node.js 20.19+, 22.12+, or 24+.
- npm.
- A Supabase project with Auth and PostgreSQL configured.
- A Meta API key for live queued investigation modes.
- Modal credentials if using Modal for execution.

### 1. Clone the repository

```bash
git clone https://github.com/Kunle777/reporescue.git
cd reporescue
```

### 2. Configure the backend

From the repository root:

```powershell
cd reporescue\backend

py -m venv .venv
.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure `backend/.env`:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-supabase-anon-key
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE

META_API_KEY=your-meta-api-key
```

For Modal execution, also configure:

```env
REPORESCUE_EXECUTION_BACKEND=modal
MODAL_TOKEN_ID=your-modal-token-id
MODAL_TOKEN_SECRET=your-modal-token-secret
```

Keep credentials private. Never commit `.env` or expose backend secrets through frontend `VITE_` variables.

See [backend/.env.example](./backend/.env.example) for supported configuration.

### 3. Apply migrations and start the API

Apply the current schema against your intended Supabase PostgreSQL database:

```powershell
alembic upgrade head
```

Start FastAPI:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API health endpoint:

`http://127.0.0.1:8000/api/health`

Interactive API documentation:

`http://127.0.0.1:8000/docs`

The health endpoint is a process-liveness check, not a database-readiness check.

### 4. Start the worker

Open a second terminal:

```powershell
cd reporescue\backend

.venv\Scripts\Activate.ps1
python -m app.worker
```

The API and worker must use the same `DATABASE_URL`.

The worker requires `META_API_KEY` for live AI-backed investigation modes. For Modal execution, configure the Modal credentials and set `REPORESCUE_EXECUTION_BACKEND=modal`.

### 5. Configure the frontend

Open another terminal:

```powershell
cd reporescue\frontend

npm install
Copy-Item .env.example .env.local
```

Configure `.env.local`:

```env
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your-supabase-anon-key
```

For local development, `VITE_API_BASE_URL` can remain empty because Vite proxies `/api` to the local FastAPI server.

Start the frontend:

```powershell
npm run dev
```

Open the local URL printed by Vite, normally:

`http://localhost:5173`

Configure the required sign-in methods and local redirect URLs in Supabase Auth.

---

## Deployment architecture

The hosted application uses three application services and one shared database:

| Component | Platform | Responsibility |
|---|---|---|
| Frontend | Vercel | Static React application |
| API | Railway | FastAPI control plane |
| Worker | Railway | Persistent background job processing |
| Database and Auth | Supabase | PostgreSQL, queue, identity and durable records |
| Execution | Modal | Isolated repository execution |

### Frontend — Vercel

Configure:

- Root directory: `reporescue/frontend`
- Build command: `npm run build`
- Output directory: `dist`

Frontend environment variables:

```env
VITE_SUPABASE_URL=your-supabase-project-url
VITE_SUPABASE_ANON_KEY=your-supabase-anon-key
VITE_API_BASE_URL=https://your-railway-api-domain
```

`VITE_API_BASE_URL` is the Railway API origin without an `/api` suffix.

### Backend — Railway

Create two separate services from the same GitHub repository.

Both services use:

```text
Root directory: /reporescue/backend
Build command: pip install -r requirements.txt
```

**API service start command:**

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Configure the API healthcheck path:

```text
/api/health
```

**Worker service start command:**

```bash
python -m app.worker
```

The worker is a background process and does not need public HTTP networking or an API healthcheck.

### Environment separation

Both backend services require the same database connection:

```env
DATABASE_URL=your-supabase-postgresql-connection-string
```

The API requires:

```env
SUPABASE_URL=your-supabase-project-url
SUPABASE_ANON_KEY=your-supabase-anon-key
REPORESCUE_CORS_ORIGINS=https://your-vercel-domain
```

The trusted worker requires:

```env
META_API_KEY=your-meta-api-key
REPORESCUE_EXECUTION_BACKEND=modal
MODAL_TOKEN_ID=your-modal-token-id
MODAL_TOKEN_SECRET=your-modal-token-secret
```

The queued investigation pipeline uses the Meta client directly. `REPORESCUE_AI_MODE=meta` is not required to enable Meta for queued jobs; that setting controls the legacy fixture endpoint.

### Database migrations

Run:

```bash
alembic upgrade head
```

as a one-time release migration against the intended Supabase PostgreSQL database.

Do not run migrations concurrently from multiple API replicas or independently from both backend services.

### Supabase authentication

Configure the production Vercel domain as the Supabase Auth Site URL and add the deployed frontend origin to the allowed redirect URLs.

If GitHub OAuth is enabled, configure the provider credentials and Supabase callback URL in the OAuth provider settings.

The frontend authentication callback is handled at `/`; path-based SPA rewrites are not required.

**Deployment note:** These instructions describe the intended hosting configuration. They are not a claim that all production services have been deployed or that production acceptance testing has been completed.

---

## Current scope and limitations

RepoRescue is an in-progress hackathon MVP, not a general-purpose CI platform.

Current limitations:

- Repository ingestion supports public GitHub HTTPS URLs only.
- Python is the supported runnable investigation path, with recognized `pytest` and `unittest` frameworks.
- JavaScript detection and Modal execution components exist, but JavaScript is not yet wired into the complete investigation pipeline.
- Other languages do not currently have runnable investigation support.
- Explain and Health are static assessments; they do not execute repository code.
- General routing is signal-based and may require a clearer objective. Its fallback is explicitly low-confidence.
- The conversation records are persisted, but a follow-up conversation API or agent is not yet implemented.
- One worker process is the supported deployment topology until higher-concurrency production operation has been load-tested.
- Docker end-to-end execution is not part of the hosted Modal execution path.

These boundaries are intentional disclosures. RepoRescue distinguishes what it has inspected, what it has executed, and what it has actually verified.

---

## Meta API behavior

The legacy fixture endpoint, `POST /api/demo/investigate`, uses `REPORESCUE_AI_MODE` and defaults to `stub`. Its deterministic demo patch does not call Meta by default.

The current queued investigation pipeline calls the Meta client directly for Debug diagnosis and Explain/Health summaries. A valid `META_API_KEY` is required for those live AI-backed operations.

Setting `REPORESCUE_AI_MODE=meta` opts the legacy fixture endpoint into live Meta calls; it is not required for the queued investigation pipeline.

---

## Project structure

```text
reporescue/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── worker.py
│   │   ├── general.py
│   │   ├── orchestrate.py
│   │   ├── jobqueue.py
│   │   ├── patch_guard.py
│   │   └── ...
│   ├── alembic/
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
│
├── frontend/
│   ├── src/
│   ├── package.json
│   └── .env.example
│
├── docs/
│   ├── MODAL.md
│   └── LIMITATIONS.md
│
└── README.md
```

---

## Development and implementation references

- [Backend API](./backend/app/main.py)
- [Background worker](./backend/app/worker.py)
- [Job queue](./backend/app/jobqueue.py)
- [Database migrations](./backend/alembic/versions)
- [Modal execution documentation](./docs/MODAL.md)
- [Known limitations](./docs/LIMITATIONS.md)
- [Frontend](./frontend)

---

## Project

**RepoRescue — Investigate repositories with evidence. Verify proposed fixes with execution.**

Built for developers who need more than plausible answers: they need to know what was inspected, what was executed, and whether the proposed solution actually worked.

[GitHub Repository](https://github.com/Kunle777/reporescue)