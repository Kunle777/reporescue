"""Minimal in-memory fixed-window rate limiter for expensive routes.

Buckets are keyed by (route, user id) with generous per-minute budgets
that normal use never approaches. This blunts accidental abuse and
credential-stuffing-adjacent retry storms without adding infrastructure.

Limitation: buckets live in process memory, so a multi-worker deploy
enforces per-process budgets. A shared store (Redis) would be needed
for exact global limits; the per-process bound still caps total load
by worker count.
"""

from __future__ import annotations

import time

from fastapi import HTTPException

from .auth import AuthenticatedUser

# bucket -> (max calls, window seconds)
LIMITS: dict[str, tuple[int, int]] = {
    "ingest": (20, 60),
    "jobs": (30, 60),
    "classify": (60, 60),
    "conversation": (20, 60),
    "demo": (10, 60),
}

_buckets: dict[tuple[str, str], list[float]] = {}


def check_rate_limit(bucket: str, user: AuthenticatedUser) -> None:
    """Consume one call from ``bucket`` or raise HTTP 429."""
    max_calls, window = LIMITS[bucket]
    now = time.monotonic()
    key = (bucket, user.id)
    hits = [tick for tick in _buckets.get(key, []) if now - tick < window]
    if len(hits) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this operation; retry shortly.",
        )
    hits.append(now)
    _buckets[key] = hits


def reset() -> None:
    """Clear all buckets (tests only)."""
    _buckets.clear()
