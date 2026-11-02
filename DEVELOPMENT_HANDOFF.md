# DEVELOPMENT_HANDOFF — RepoRescue

## 1. Project
RepoRescue.

## 2. Repository URL
Current: `https://github.com/Kunle777/reporescue`
Target (rename pending, manual step): `https://github.com/Kunle777/RepoRescue`
GitHub auto-redirects the old URL after a rename.

## 3. Branch and commit
- Branch: `main`
- Task 3 completion checkpoint: `a042635d40cf2546076fef6050a33889df4407ab`
- This handoff file is committed on top of that checkpoint.
- Canonical source tree: `reporescue/` (backend, frontend, docs).

## 4. Completed milestone
Phase 5B Task 3 — complete and checkpointed.

## 5. Technology stacks
- Backend (`reporescue/backend`, see `requirements.txt`): FastAPI, Uvicorn,
  SQLAlchemy, Alembic (migrations `0001`–`0004`), `psycopg[binary]`, PyJWT,
  `cryptography`, `python-dotenv`, `packaging`; tests with pytest (27 files
  under `tests/`). No Dockerfile or docker-compose file in the repo.
- Frontend (`reporescue/frontend`, see `package.json`): React, Vite,
  TypeScript, `@supabase/supabase-js`, `lucide-react`; tests with Vitest
  (`npm run test`), Testing Library, jsdom.

## 6. Required environment variables (names only, never values)
Recreate these locally tomorrow; values were never committed.
- Backend `reporescue/backend/.env` (template: `.env.example`):
  `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `DATABASE_URL`, `META_API_KEY`,
  `REPORESCUE_AI_MODE`; optional: `SUPABASE_JWT_AUDIENCE`, `DB_POOL_SIZE`,
  `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE`,
  `REPORESCUE_ALLOW_UNSAFE_LOCAL_EXECUTION`.
- Frontend `reporescue/frontend/.env.local` (template: `.env.example`):
  `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`.

## 7. Python and Node versions
Required per `reporescue/README.md`: Python 3.10+ (3.11 recommended),
Node.js 20+ with npm. Tonight's PC ran Python 3.12.10 and Node v24.21.0
(npm 11.19.0).

## 8. Dependency installation
```powershell
git clone https://github.com/Kunle777/reporescue.git
cd reporescue/reporescue/backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cd ../frontend
npm install
```
Note: tonight's PC used a venv at `reporescue/venv`; the README uses
`backend/.venv`. Either location works; do not commit the venv.

## 9. Backend tests
```powershell
cd reporescue/reporescue/backend
python -m pytest
```
Recorded result (Task 3 closeout): 661 passed, 3 skipped, 0 failed.
`test_meta_connection.py` is a manual live-API check needing `META_API_KEY`.

## 10. Docker prerequisites and E2E acceptance
Prerequisites: Docker Desktop installed, running, and reachable from the
shell running the commands. Acceptance (from `reporescue/backend`):
```powershell
python verify_docker_phase3.py
python -m pytest tests/test_real_investigation.py::test_end_to_end_flat_layout_pyproject_docker_no_editable
```
`verify_docker_phase3.py` exits 0 on pass, 1 on failure, 3 when the daemon
is unreachable.

## 11. Known Docker / sandbox limitation
The Docker daemon is unreachable from sandboxed shells (named-pipe
permission errors). There the Docker E2E test SKIPS with
`docker unavailable: ...` — a skip is environmental and is never a pass.
Run item 10 on a PC where Docker Desktop is reachable.

## 12. Remaining Task 3 limitations
Authoritative register: `reporescue/docs/LIMITATIONS.md` (L1–L12):
adapters only for Python/JavaScript (L1–L3), Docker E2E environmental
(L4), verification covers executed cycles only (L5), container hardening
outstanding (L6), demo report has no confidence label (L7), enqueue needs
a live ingestion record (L8), cancellation at stage boundaries (L9),
multi-worker tested to a 2-thread race (L10), conversation sessions are
persistence-only (L11), heavy-repo install budget unchanged (L12).

## 13. Next milestone
Phase 5B Task 4 — General / Automatic mode.

## 14. Do not start Task 4
Do NOT start Task 4 until its architecture and implementation prompt have
been reviewed.

## 15. Working-tree notes for tomorrow
- `reporescue-starter/` deletions are intentionally unstaged; removing that
  legacy tree is a pending owner decision (`git rm -r` only after review).
- `live-savings/` is a separate nested repo (`savings-vault-ledger`),
  untouched and never to be committed here.
- If the push below did not complete, run `git push origin main` from a
  network with GitHub access, then verify with `git status`.
