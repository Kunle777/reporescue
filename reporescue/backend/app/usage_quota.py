"""Persistent, atomic daily usage quotas for AI-backed operations.

Counters live in the application database so API restarts and multiple
workers share the same limits. Dates follow Africa/Lagos calendar days.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

LAGOS = ZoneInfo("Africa/Lagos")
AI_DAILY_USER_LIMIT = 10
DEBUG_DAILY_USER_LIMIT = 3
def _global_limit() -> int:
    raw = os.getenv("REPORESCUE_GLOBAL_AI_DAILY_LIMIT", "100")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("REPORESCUE_GLOBAL_AI_DAILY_LIMIT must be a positive integer.") from exc
    if value < 1:
        raise ValueError("REPORESCUE_GLOBAL_AI_DAILY_LIMIT must be a positive integer.")
    return value


GLOBAL_AI_DAILY_LIMIT = _global_limit()


class UsageLimitExceeded(Exception):
    def __init__(self, code: str, limit: int, reset_at: datetime):
        self.code = code
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(code)


def _today_and_reset() -> tuple[date, datetime]:
    now = datetime.now(LAGOS)
    tomorrow = now.date() + timedelta(days=1)
    return now.date(), datetime.combine(tomorrow, time.min, tzinfo=LAGOS)


def _reserve_counter(
    db: Session, *, scope: str, day: date, kind: str, limit: int,
    code: str, reset_at: datetime,
) -> None:
    # INSERT .. ON CONFLICT and the conditional UPDATE are atomic on both
    # PostgreSQL and SQLite. No read-then-write race can exceed the limit.
    db.execute(text("""
        INSERT INTO daily_usage_counters (scope_key, usage_date, quota_type, used)
        VALUES (:scope, :day, :kind, 0)
        ON CONFLICT (scope_key, usage_date, quota_type) DO NOTHING
    """), {"scope": scope, "day": day, "kind": kind})
    result = db.execute(text("""
        UPDATE daily_usage_counters
        SET used = used + 1
        WHERE scope_key = :scope AND usage_date = :day
          AND quota_type = :kind AND used < :limit
        RETURNING used
    """), {"scope": scope, "day": day, "kind": kind, "limit": limit})
    if result.first() is None:
        raise UsageLimitExceeded(code, limit, reset_at)


def _release_counter(
    db: Session, *, scope: str, day: date, kind: str,
) -> None:
    # Refunds are best-effort and idempotent: a missing row or a
    # zero counter simply has nothing to return. Callers guard
    # double refunds with charged/refunded flags; the floor keeps
    # concurrent refunds from driving the counter negative.
    db.execute(text("""
        UPDATE daily_usage_counters
        SET used = used - 1
        WHERE scope_key = :scope AND usage_date = :day
          AND quota_type = :kind AND used > 0
    """), {"scope": scope, "day": day, "kind": kind})


def reserve_global_call(db: Session, *, commit: bool = True) -> None:
    """Reserve one shared application-wide slot for a Meta classification call."""
    day, reset_at = _today_and_reset()
    try:
        _reserve_counter(
            db, scope="__global__", day=day, kind="ai",
            limit=GLOBAL_AI_DAILY_LIMIT, code="GLOBAL_AI_DAILY_LIMIT_REACHED",
            reset_at=reset_at,
        )
        if commit:
            db.commit()
    except UsageLimitExceeded:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def reserve_operation(
    db: Session, *, user_id: str, include_debug: bool = False, commit: bool = True
) -> None:
    """Reserve one user AI operation, global AI slot, and optional Debug slot.

    A single investigation is one operation even if its internal workflow
    makes bounded provider calls/retries. Failed reservations roll back all
    counters so partial quota consumption cannot occur.
    """
    day, reset_at = _today_and_reset()
    try:
        _reserve_counter(
            db, scope=f"user:{user_id}", day=day, kind="ai",
            limit=AI_DAILY_USER_LIMIT, code="AI_DAILY_USER_LIMIT_REACHED",
            reset_at=reset_at,
        )
        _reserve_counter(
            db, scope="__global__", day=day, kind="ai",
            limit=GLOBAL_AI_DAILY_LIMIT, code="GLOBAL_AI_DAILY_LIMIT_REACHED",
            reset_at=reset_at,
        )
        if include_debug:
            _reserve_counter(
                db, scope=f"user:{user_id}", day=day, kind="debug",
                limit=DEBUG_DAILY_USER_LIMIT, code="BETA_DAILY_DEBUG_LIMIT_REACHED",
                reset_at=reset_at,
            )
        if commit:
            db.commit()
    except UsageLimitExceeded:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def release_operation(
    db: Session, *, user_id: str, include_debug: bool = False, commit: bool = True
) -> None:
    """Return one user AI operation, global AI slot, and optional Debug slot.

    Mirror of :func:`reserve_operation` for operations that failed
    after reserving: provider errors, timeouts, late validation
    failures, and worker-side terminal failures. Refunds target the
    current Lagos day; a failure landing after midnight simply frees
    a slot on the new day, which always favors the user.
    """
    day, _ = _today_and_reset()
    try:
        _release_counter(db, scope=f"user:{user_id}", day=day, kind="ai")
        _release_counter(db, scope="__global__", day=day, kind="ai")
        if include_debug:
            _release_counter(db, scope=f"user:{user_id}", day=day, kind="debug")
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise


def usage_status(db: Session, *, user_id: str) -> dict:
    day, reset_at = _today_and_reset()

    def used(scope: str, kind: str) -> int:
        value = db.execute(text("""
            SELECT used FROM daily_usage_counters
            WHERE scope_key = :scope AND usage_date = :day AND quota_type = :kind
        """), {"scope": scope, "day": day, "kind": kind}).scalar_one_or_none()
        return int(value or 0)

    ai_used = used(f"user:{user_id}", "ai")
    debug_used = used(f"user:{user_id}", "debug")
    return {
        "timezone": "Africa/Lagos",
        "resets_at": reset_at.isoformat(),
        "ai": {
            "used": ai_used,
            "limit": AI_DAILY_USER_LIMIT,
            "remaining": max(0, AI_DAILY_USER_LIMIT - ai_used),
        },
        "debug": {
            "used": debug_used,
            "limit": DEBUG_DAILY_USER_LIMIT,
            "remaining": max(0, DEBUG_DAILY_USER_LIMIT - debug_used),
        },
    }
