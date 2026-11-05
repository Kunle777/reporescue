# Daily AI and Debug usage limits

RepoRescue enforces the following authenticated-user limits using the application
database (not process-local memory):

| Allowance | Default | Reset |
| --- | ---: | --- |
| AI-backed investigation operations | 10 per user | Midnight Africa/Lagos |
| Debug investigations | 3 per user | Midnight Africa/Lagos |
| Shared AI-backed operation budget | 100 across the application | Midnight Africa/Lagos |

A Debug investigation consumes one AI operation and one Debug allowance. Explain,
Health, and General investigations consume one AI operation. A General objective
that is unclear and cannot be routed does not consume a user investigation
allowance. A model-assisted General preflight is counted against the shared
application budget, but not charged as a second user operation; the investigation
it routes is the user's single operation.

The shared application limit can be lowered or raised with
`REPORESCUE_GLOBAL_AI_DAILY_LIMIT`. The default is 100. Set this on the backend
environment (Railway) before starting the service. The global limit is intentionally
not exposed in the user quota response.

The existing per-minute request limits remain in place as a separate abuse
protection layer. They are not substitutes for the daily quotas.

## Database migration

Apply Alembic migration `0008_daily_usage_counters` after `0007_preselect_and_run_command`
before deploying this change:

```bash
cd backend
alembic upgrade head
```

The migration creates only the `daily_usage_counters` table. It does not alter,
backfill, or delete existing investigation data. The quota counters are shared
across API workers and survive service restarts.

## Accounting

A newly accepted AI-backed investigation reserves one user AI allowance and one
shared application allowance before it is queued. Debug investigations reserve
the additional Debug allowance. Provider retries and internal model calls within
that investigation do not each consume another user operation. Classification
preflight reserves a shared application slot because it can call Meta before the
investigation is queued.

The quota endpoint is `GET /api/usage/quota`. It returns the authenticated user's
remaining AI and Debug allowances and the next reset time. Limit responses use
HTTP 429 with a stable `detail.code` and `Retry-After` header.
