# RepoRescue — first vertical slice

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
