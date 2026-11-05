"""General-mode routing: classify the objective, delegate to a strategy.

General is a routing capability, not a fourth pipeline. The route stage
scores the user's objective against transparent per-strategy signals,
persists the decision (requested mode ``general`` stays on the
investigation; the selected execution mode goes to ``selected_mode``),
then executes the selected strategy through its EXISTING plan by
nested :func:`app.orchestrate.execute`. Sub-stage progress, findings,
evidence, and reports flow through unchanged; the final report carries
a ``routing`` block plus a summary sentence explaining the choice.

Signal matching is word-based, never substring-based: ``bug`` does not
match ``debug``, and prefix signals (marked with a trailing ``*``) only
match at the start of a word (``fail*`` matches ``failing`` but not
``unfailing``). Multi-word signals match on word boundaries.

Routing rules:

- Empty objective: no route (``needs_objective`` error). Never invent
  a problem statement.
- No signals, or only a single generic intent word (``debug``, ``fix``,
  ``explain``, ``review``, ...): no route (``needs_clarification``
  with a targeted follow-up question). Never invent an objective and
  never launch an investigation that cannot answer the request.
- Highest signal score wins; ties break debug > explain > health and
  are recorded as low confidence with all matched signals.
- Capabilities never override the objective: if the selected strategy
  is inapplicable (e.g. Debug on an unsupported repo), the run fails
  with that strategy's honest reason instead of silently switching.
- A pre-classified route (from ``POST /api/general/classify``) is
  honored when it names a valid strategy; otherwise the objective is
  routed deterministically as above.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .recorder import JobCancelled

# A trailing "*" marks a word-prefix signal: it matches words that START
# with the stem ("fail*" matches "failing"). Plain signals match whole
# words or whole phrases only ("bug" never matches "debug").
DEBUG_SIGNALS = (
    "fail*",
    "error*",
    "bug*",
    "broken",
    "crash*",
    "exception*",
    "traceback*",
    "stack trace",
    "wrong",
    "incorrect",
    "unexpected",
    "not working",
    "doesnt work",
    "does not work",
    "fix*",
    "repair*",
    "regression*",
    "debug*",
    "test*",
)

EXPLAIN_SIGNALS = (
    "explain*",
    "how",
    "how does",
    "how do",
    "how is",
    "how can",
    "how to",
    "how should",
    "what does",
    "what is",
    "what are",
    "why does",
    "why is",
    "why do",
    "understand*",
    "describe*",
    "structure*",
    "architecture*",
    "walkthrough",
    "walk through",
    "where is",
    "where are",
    "where do",
    "documentation",
    "document*",
    "guide",
    "tutorial*",
    "example*",
)

HEALTH_SIGNALS = (
    "health*",
    "review*",
    "audit*",
    "quality",
    "securit*",
    "vulnerab*",
    "coverage",
    "maintain*",
    "dependenc*",
    "assess*",
    "checkup",
    "check up",
    "improve*",
    "best practice",
    "code smell",
    "smell",
    "debt",
)

# Generic intent words: matching ONLY one of these (and nothing else)
# means the user stated a direction but no actionable objective, so
# General asks a clarifying question instead of routing.
_GENERIC_SIGNALS = {
    "debug": frozenset({"debug*", "fix*", "repair*", "test*"}),
    "explain": frozenset({"explain*", "describe*", "document*", "guide", "tutorial*"}),
    "health": frozenset({"health*", "review*", "audit*", "assess*", "improve*"}),
}

_SIGNAL_TABLE = (
    ("debug", DEBUG_SIGNALS),
    ("explain", EXPLAIN_SIGNALS),
    ("health", HEALTH_SIGNALS),
)

_TIE_PRIORITY = ("debug", "explain", "health")

_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

CLARIFY_GENERAL_QUESTION = (
    "Your objective is unclear, so nothing was started. Do you want to "
    "(1) investigate a specific failure — describe the error, failing test, "
    "or broken behavior; (2) understand how part of the repository works — "
    "name the area or question; or (3) get a broad health and security "
    "review — say what it should focus on?"
)

CLARIFY_DEBUG_QUESTION = (
    "You mentioned debugging, but no specific failure was described, so "
    "nothing was started. What should be investigated — for example a "
    "failing test, an error message, or broken behavior — or should the "
    "repository's test suite be checked for failures?"
)

CLARIFY_EXPLAIN_QUESTION = (
    "You asked for an explanation, but not what to explain, so nothing was "
    "started. Which part of the repository, behavior, or workflow should be "
    "explained?"
)

CLARIFY_HEALTH_QUESTION = (
    "You asked for a review, but not what to focus on, so nothing was "
    "started. Should this be a broad health and security assessment, or "
    "should it focus on something specific such as security, "
    "maintainability, or dependencies?"
)

_CLARIFY_QUESTIONS = {
    "debug": CLARIFY_DEBUG_QUESTION,
    "explain": CLARIFY_EXPLAIN_QUESTION,
    "health": CLARIFY_HEALTH_QUESTION,
}


def _normalize(text: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to one space."""
    return _NON_WORD_RE.sub(" ", text.lower()).strip()


def _signal_matches(text: str, signals: tuple[str, ...]) -> list[str]:
    """Return the signals matching ``text`` on word boundaries.

    Plain signals match whole words/phrases; ``*``-suffixed signals
    match words starting with the stem. The returned names keep their
    declared spelling (including any ``*``).
    """
    normalized = _normalize(text)
    if not normalized:
        return []
    haystack = f" {normalized} "
    words = normalized.split(" ")
    matched: list[str] = []
    for signal in signals:
        if signal.endswith("*"):
            stem = _normalize(signal[:-1])
            if stem and any(word.startswith(stem) for word in words):
                matched.append(signal)
        else:
            needle = _normalize(signal)
            if needle and f" {needle} " in haystack:
                matched.append(signal)
    return matched


def _all_signal_matches(text: str) -> dict[str, list[str]]:
    return {
        mode: _signal_matches(text, table) for mode, table in _SIGNAL_TABLE
    }


def _clarification(
    signals: dict[str, list[str]], suggested_mode: str | None
) -> dict[str, Any]:
    """Build a needs_clarification routing outcome (never invents a route)."""
    if suggested_mode is not None:
        question = _CLARIFY_QUESTIONS[suggested_mode]
        rationale = (
            f"Objective only states a generic {suggested_mode} intent "
            f"({', '.join(signals[suggested_mode])}) without an actionable "
            "objective, so General asked for clarification instead of routing."
        )
    else:
        question = CLARIFY_GENERAL_QUESTION
        rationale = (
            "Objective matched no debug, explain, or health signals, so "
            "General asked for clarification instead of assuming an objective."
        )
    return {
        "selected_mode": None,
        "reason": "needs_clarification",
        "question": question,
        "rationale": rationale,
        "confidence": "none",
        "signals": signals,
        "suggested_mode": suggested_mode,
    }


def route_objective(
    objective: str | None, capabilities: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Classify an objective into a strategy.

    Returns ``{"selected_mode", "rationale", "confidence", "signals"}``,
    ``{"selected_mode": None, "reason": "needs_objective", ...}`` when
    there is nothing to route, or ``{"selected_mode": None, "reason":
    "needs_clarification", "question": ..., "suggested_mode": ...}``
    when the objective is too vague to act on. ``capabilities`` is
    accepted for future capability-aware routing and recorded for
    provenance; it never overrides the objective today.
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
    signals = _all_signal_matches(text)
    scores = {mode: len(matched) for mode, matched in signals.items()}
    best = max(scores.values())
    if best == 0:
        return _clarification(signals, None)
    total_matches = sum(scores.values())
    if total_matches == 1:
        (only_mode,) = [mode for mode in _TIE_PRIORITY if scores[mode] == 1]
        (only_signal,) = signals[only_mode]
        if only_signal in _GENERIC_SIGNALS[only_mode]:
            return _clarification(signals, only_mode)
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


def _preselected_routing(ctx: Any) -> dict[str, Any] | None:
    """Return the honored pre-classification, or None when absent/invalid.

    Pre-classification comes from ``POST /api/general/classify`` (model
    or deterministic, chosen before the job was queued). Only a valid
    strategy mode is honored; anything else falls through to
    deterministic routing of the objective.
    """
    selected = getattr(ctx, "preselected_mode", None)
    if selected not in ("debug", "explain", "health"):
        return None
    rationale = getattr(ctx, "preselect_rationale", None) or (
        f"Pre-classified as {selected} before the investigation was queued."
    )
    confidence = getattr(ctx, "preselect_confidence", None) or "medium"
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    source = getattr(ctx, "preselect_source", None) or "preclassified"
    return {
        "selected_mode": selected,
        "rationale": str(rationale),
        "confidence": confidence,
        "signals": _all_signal_matches(ctx.message or ""),
        "source": str(source),
    }


def stage_general_route(ctx: Any, progress: Any, deps: Any, scope: Any) -> Any:
    """Route a General investigation, then run the selected plan.

    The selected strategy executes via nested ``execute()`` so stage
    progress, evidence recording, cancellation, and reports behave
    exactly as a direct run of that strategy. The route decision is
    persisted (see ``record_routing``) and attached to the report.
    A pre-classified route is honored; otherwise the objective is
    routed deterministically, and a vague objective fails with
    ``needs_clarification`` (carrying the follow-up question) instead
    of launching a meaningless investigation.
    """
    from .orchestrate import (
        StageResult,
        UnknownPlanError,
        UnsupportedProjectError,
        execute,
        legacy_error_of,
        plan_for_mode,
    )

    routing = _preselected_routing(ctx) or route_objective(ctx.message, ctx.capabilities)
    selected = routing.get("selected_mode")
    if not selected:
        reason = str(routing.get("reason") or "needs_objective")
        message = str(
            routing.get("question") or routing.get("rationale") or ""
        )
        legacy = {
            "status": "error",
            "reason": reason,
            "message": message,
            "profile": dict(ctx.profile or {}),
        }
        if reason == "needs_clarification":
            legacy["question"] = message
            legacy["suggested_mode"] = routing.get("suggested_mode")
        return StageResult(
            stage="route", status="failed", detail=message[:2000],
            reason=reason, output={"legacy_error": legacy},
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
