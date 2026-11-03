"""Deterministic routing for ``general``-mode investigations.

General mode inspects the user's message and delegates to one of the
concrete pipelines (debug / explain / health). Routing is a pure keyword
intent classifier — no model calls — and the decision is persisted as a
``RoutingInfo`` record so the workbench can explain why a mode was chosen.

Signal priority is debug > explain > health: a message that mentions
failures routes to the test-backed debug pipeline even when it also asks
for an explanation, since a failing repository needs repair before prose.
"""

from __future__ import annotations

DEBUG_SIGNALS: tuple[str, ...] = (
    "traceback",
    "exception",
    "failing",
    "failure",
    "regression",
    "broken",
    "crash",
    "error",
    "fail",
    "bug",
    "fix",
    "flaky",
)

EXPLAIN_SIGNALS: tuple[str, ...] = (
    "walk through",
    "how does",
    "how do",
    "what does",
    "why does",
    "explain",
    "understand",
    "overview",
    "architecture",
    "document",
)

HEALTH_SIGNALS: tuple[str, ...] = (
    "vulnerab",
    "security",
    "health",
    "quality",
    "audit",
    "review",
    "smell",
    "maintain",
    "coverage",
    "lint",
)


def _first_signal(text: str, signals: tuple[str, ...]) -> str | None:
    for signal in signals:
        if signal in text:
            return signal
    return None


def route_general(message: str | None) -> dict[str, object]:
    """Route a general-mode request to ``debug`` | ``explain`` | ``health``.

    Returns a ``RoutingInfo``-shaped dict (see frontend ``lib/types.ts``).
    """
    text = (message or "").strip().lower()
    reasons: list[str] = []
    if not text:
        resolved, intent, confidence = "debug", "unspecified", "low"
        reasons.append("no user message; defaulting to debug (test-backed)")
    else:
        hit = _first_signal(text, DEBUG_SIGNALS)
        if hit is not None:
            resolved, intent, confidence = "debug", hit, "high"
            reasons.append(f"message mentions {hit!r}; failures route to debug")
        elif (hit := _first_signal(text, EXPLAIN_SIGNALS)) is not None:
            resolved, intent, confidence = "explain", hit, "high"
            reasons.append(f"message asks {hit!r}; narrative routes to explain")
        elif (hit := _first_signal(text, HEALTH_SIGNALS)) is not None:
            resolved, intent, confidence = "health", hit, "high"
            reasons.append(f"message asks {hit!r}; audit routes to health")
        else:
            resolved, intent, confidence = "debug", "unspecified", "low"
            reasons.append("no mode signals; defaulting to debug (test-backed)")
    return {
        "requested_mode": "general",
        "intent": intent,
        "confidence": confidence,
        "reasons": reasons,
        "mode": resolved,
        "plan_id": None,
        "conservative": confidence == "low",
        "explanation": f"General investigation routed to {resolved}: {reasons[0]}.",
    }
