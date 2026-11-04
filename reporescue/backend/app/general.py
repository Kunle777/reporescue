"""General-mode routing: classify the objective, delegate to a strategy.

General is a routing capability, not a fourth pipeline. The route stage
scores the user's objective against transparent per-strategy signals,
persists the decision (requested mode ``general`` stays on the
investigation; the selected execution mode goes to ``selected_mode``),
then executes the selected strategy through its EXISTING plan by
nested :func:`app.orchestrate.execute`. Sub-stage progress, findings,
evidence, and reports flow through unchanged; the final report carries
a ``routing`` block plus a summary sentence explaining the choice.

Routing rules:

- Empty objective: no route (``needs_objective`` error). Never invent
  a problem statement.
- Highest signal score wins; ties break debug > explain > health and
  are recorded as low confidence with all matched signals.
- No signals at all: health (broad assessment) with an explicit
  low-confidence rationale stating the fallback.
- Capabilities never override the objective: if the selected strategy
  is inapplicable (e.g. Debug on an unsupported repo), the run fails
  with that strategy's honest reason instead of silently switching.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .recorder import JobCancelled

DEBUG_SIGNALS = (
    "failing",
    "failure",
    "failed",
    "fail",
    "error",
    "bug",
    "broken",
    "crash",
    "exception",
    "traceback",
    "stack trace",
    "wrong",
    "incorrect",
    "unexpected",
    "not working",
    "doesn't work",
    "doesnt work",
    "fix",
    "repair",
    "regression",
)

EXPLAIN_SIGNALS = (
    "explain",
    "how does",
    "how do",
    "how is",
    "what does",
    "what is",
    "why does",
    "why is",
    "understand",
    "describe",
    "structure",
    "architecture",
    "walkthrough",
    "walk through",
    "where is",
    "where are",
    "documentation",
    "document",
)

HEALTH_SIGNALS = (
    "health",
    "review",
    "audit",
    "quality",
    "security",
    "vulnerab",
    "coverage",
    "maintain",
    "assess",
    "checkup",
    "check-up",
    "improve",
    "best practice",
    "code smell",
    "smell",
)

_SIGNAL_TABLE = (
    ("debug", DEBUG_SIGNALS),
    ("explain", EXPLAIN_SIGNALS),
    ("health", HEALTH_SIGNALS),
)

_TIE_PRIORITY = ("debug", "explain", "health")


def _matched_signals(text: str, signals: tuple[str, ...]) -> list[str]:
    lowered = f" {text.lower()} "
    matched: list[str] = []
    for signal in signals:
        if signal in lowered and signal not in matched:
            matched.append(signal)
    return matched


def route_objective(
    objective: str | None, capabilities: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Classify an objective into a strategy.

    Returns ``{"selected_mode", "rationale", "confidence", "signals"}``,
    or ``{"selected_mode": None, "reason": "needs_objective", ...}``
    when there is nothing to route. ``capabilities`` is accepted for
    future capability-aware routing and recorded for provenance; it
    never overrides the objective today.
    """
    _ = capabilities
    text = (objective or "").strip()
    if not text:
        return {
            "selected_mode": None,
            "reason": "needs_objective",
            "rationale": (
                "General needs an objective to route on. Describe the bug, "
                "the question, or the assessment you want."
            ),
            "confidence": "none",
            "signals": {"debug": [], "explain": [], "health": []},
        }
    signals = {
        mode: _matched_signals(text, table) for mode, table in _SIGNAL_TABLE
    }
    scores = {mode: len(matched) for mode, matched in signals.items()}
    best = max(scores.values())
    if best == 0:
        return {
            "selected_mode": "health",
            "rationale": (
                "No specific bug or explanation request detected, so General "
                "selected a broad repository health assessment. Rephrase "
                "with 'fix', 'explain', or 'review' terms to steer routing."
            ),
            "confidence": "low",
            "signals": signals,
        }
    winners = [mode for mode in _TIE_PRIORITY if scores[mode] == best]
    selected = winners[0]
    runner_up = max(
        (scores[mode] for mode in _TIE_PRIORITY if mode != selected),
        default=0,
    )
    margin = best - runner_up
    if len(winners) > 1:
        confidence = "low"
        rationale = (
            f"Ambiguous objective matched {', '.join(sorted(set(sum((signals[m] for m in winners), []))))}; "
            f"General selected {selected} by tie-break "
            f"({' > '.join(_TIE_PRIORITY)})."
        )
    else:
        confidence = "high" if margin >= 2 else "medium"
        rationale = (
            f"Objective matched {selected} signals "
            f"({', '.join(signals[selected])}); General selected {selected}."
        )
    return {
        "selected_mode": selected,
        "rationale": rationale,
        "confidence": confidence,
        "signals": signals,
    }


def _unroutable(ctx: Any, routing: dict[str, Any], reason: str, message: str) -> Any:
    """Build the failure result when the selected strategy has no plan.

    Routing succeeded but the strategy is inapplicable; report that
    honestly instead of silently switching to another strategy.
    """
    from .orchestrate import StageResult

    legacy = {
        "status": "error",
        "reason": reason,
        "message": message,
        "profile": dict(ctx.profile or {}),
        "routing": routing,
    }
    return StageResult(
        stage="route", status="failed", detail=message[:2000],
        reason=reason, output={"legacy_error": legacy, "routing": routing},
    )


def stage_general_route(ctx: Any, progress: Any, deps: Any, scope: Any) -> Any:
    """Route a General investigation, then run the selected plan.

    The selected strategy executes via nested ``execute()`` so stage
    progress, evidence recording, cancellation, and reports behave
    exactly as a direct run of that strategy. The route decision is
    persisted (see ``record_routing``) and attached to the report.
    """
    from .orchestrate import (
        StageResult,
        UnknownPlanError,
        UnsupportedProjectError,
        execute,
        legacy_error_of,
        plan_for_mode,
    )

    routing = route_objective(ctx.message, ctx.capabilities)
    selected = routing.get("selected_mode")
    if not selected:
        legacy = {
            "status": "error",
            "reason": "needs_objective",
            "message": str(routing.get("rationale", "")),
            "profile": dict(ctx.profile or {}),
        }
        return StageResult(
            stage="route", status="failed", detail=str(routing.get("rationale", "")),
            reason="needs_objective", output={"legacy_error": legacy},
        )
    record_routing = getattr(progress, "record_routing", None)
    if callable(record_routing):
        try:
            record_routing(selected, routing)
        except JobCancelled:
            raise
        except Exception:
            pass
    try:
        sub_plan = plan_for_mode(selected, ctx.capabilities or {})
    except UnsupportedProjectError as exc:
        reason = "unsupported_project"
        return _unroutable(ctx, routing, reason, str(exc))
    except UnknownPlanError as exc:
        reason = "bad_plan"
        return _unroutable(ctx, routing, reason, str(exc))
    sub_ctx = replace(
        ctx, mode=selected, plan_id=sub_plan.plan_id, routing=routing
    )
    sub_result = execute(sub_ctx, progress=progress, deps=deps)
    if sub_result.status == "cancelled":
        raise JobCancelled("General investigation was cancelled.")
    if sub_result.status != "complete":
        legacy = legacy_error_of(sub_result) or dict(sub_result.error or {})
        legacy.setdefault("status", "error")
        detail = str(legacy.get("message") or legacy.get("reason") or "strategy failed.")
        return StageResult(
            stage="route", status="failed", detail=detail[:2000],
            reason=str(legacy.get("reason") or "strategy_failed"),
            output={"legacy_error": legacy, "routing": routing},
        )
    report = dict(sub_result.report or {})
    report["routing"] = routing
    report["requested_mode"] = "general"
    report["selected_mode"] = selected
    summary = str(report.get("summary", "") or "")
    report["summary"] = (
        f"General routed this investigation to {selected}: "
        f"{routing.get('rationale', '')} {summary}".strip()
    )
    return StageResult(stage="route", status="ok", detail="Routed.", output=report)
