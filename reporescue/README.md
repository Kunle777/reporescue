# RepoRescue — first vertical slice

> The initial vertical-slice instructions below describe the original local
> demo. The current canonical frontend and persistent investigation backend
> are documented in **Current deployment** below.

## Current deployment

The canonical app is `frontend/` (Vite) and `backend/` (FastAPI). Configure
Vercel with root directory `reporescue/frontend`, build command `npm run build`,
and output directory `dist`. Set the public browser variables
`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, and `VITE_API_BASE_URL` to the
Supabase project URL, its publishable/anon key, and the FastAPI origin. No
`VITE_` variable may contain a private credential.

Railway should run two services from root directory `reporescue/backend`, both
using `pip install -r requirements.txt` and the same backend environment. Run
`alembic upgrade head` once as the release/pre-deploy migration command, then
start the API with `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Start the
separate worker service with `python -m app.worker`. The API health check is
`/api/health`.

The current canonical backend does **not** use Celery or Redis: jobs are
persisted in PostgreSQL and claimed by the polling worker in `app.worker`.
There is no broker/result-backend configuration in this codebase, so do not
configure an Upstash URL as though it were active. Adopting Celery/Upstash
would require a separate architecture change and is outside this deployment
configuration.

The API and worker need the same `DATABASE_URL`; the API also needs
`SUPABASE_URL` and `SUPABASE_ANON_KEY`. Set `REPORESCUE_CORS_ORIGINS` on the API
to a comma-separated list of exact browser origins, including the production
Vercel origin; wildcards and URL paths are rejected. The worker needs
`META_API_KEY` and `REPORESCUE_AI_MODE=meta` for live Meta calls. Keep
`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` in the trusted Railway worker
environment only. For production, set
`REPORESCUE_EXECUTION_BACKEND=modal` so missing Modal configuration fails
closed instead of selecting a development backend.

In Supabase Auth, configure the production app URL and redirect allowlist for
the Vercel origin. Configure GitHub OAuth credentials in Supabase; the GitHub
provider callback is the Supabase Auth callback shown by the Supabase provider
settings, while the app redirect returns to the Vercel origin. Configure the
desired hosted email-confirmation and recovery policy in Supabase; frontend
code does not change it. The frontend currently handles the callback at `/`;
there are no path-based SPA routes requiring a Vercel rewrite.

Before deployment, apply migrations with `alembic upgrade head` against the
intended Supabase PostgreSQL database and verify both Railway services use the
same `DATABASE_URL`, Supabase settings, and Modal configuration where needed.
Do not run migrations concurrently from multiple API replicas.

A free, local-first prototype for the Meta Global AI Developer Hackathon.

This first slice proves the loop:
1. React UI starts an investigation.
2. FastAPI runs a controlled, prepared Python bug scenario.
3. The backend captures a failing baseline.
4. A deterministic demo patch is applied in a temporary copy.
5. The backend reruns tests and returns evidence to the UI.

**Important:** this is not connected to Meta's API yet. It makes no paid API calls. The patch is a clearly labelled local demo patch, not an AI-generated patch. The backend only runs the bundled fixture; it does not accept arbitrary repositories or execute user-supplied code.

## Prerequisites
- Python 3.10+ (Python 3.11 recommended)
- Node.js 20+ and npm
- VS Code

## 1. Start the backend
Open a terminal in VS Code:

```bash
cd backend
python -m venv .venv
```

Activate the environment:

Windows PowerShell:
```powershell
.\.venv\Scripts\Activate.ps1
```

Git Bash:
```bash
source .venv/Scripts/activate
```

Install and run:
```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Backend health check: http://127.0.0.1:8000/api/health
Interactive API docs: http://127.0.0.1:8000/docs

Keep this terminal running.

## 2. Start the frontend
Open a second VS Code terminal from the project root:

```bash
cd frontend
npm install
npm run dev
```

Open the local URL Vite prints, usually http://localhost:5173.

## 3. Run the demo
Click **Run investigation**. You should see:
- the baseline test fail,
- the demo patch applied,
- regression tests pass after the patch,
- the changed file and final verdict.

The first run may take a little longer while Python starts pytest.

## Troubleshooting
- If `python` is not found on Windows, try `py` instead.
- If port 8000 is busy, run Uvicorn with `--port 8001` and update `frontend/vite.config.ts`.
- If `npm` is not found, install Node.js LTS and reopen VS Code.
- If the API request fails, confirm the backend terminal is still running.

## Next milestone
Replace the deterministic demo patch with a Meta Model API-generated proposal **only after promotional credits are visible and billing safety is confirmed**. Then preserve this independent test runner as the verifier.
