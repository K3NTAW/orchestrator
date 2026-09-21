"""Purpose-first taxonomy for Planner decisions.

The decision type describes why Planner is invoked, not how complex the work is:
a complexity 8 ``closable_goal`` is routine, while a complexity 6
``ambiguous_requirement`` is not.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import attribution


DECISION_TYPES = (
    "initial_goal_plan",
    "scout_results",
    "held_task",
    "architectural_replan",
    "fix_strategy",
    "closable_goal",
    "retrospective",
    "ambiguous_requirement",
    "other",
)

_CTX_FIELDS = (
    "scout_count",
    "signature_repeated",
    "spec_review_request_changes",
    "auto_fix_rounds_used",
    "auto_fix_rounds",
    "failing_ids",
    "in_scope_review_comments",
    "ambiguous",
    "touches_interfaces",
    "touches_migrations",
    "touches_auth",
    "touches_data_deletion",
    "security_trigger",
    "semantic_trigger",
)
_AMBIGUITY_WORDS = (
    "or",
    "either",
    "unclear",
    "tbd",
    "conflict",
    "maybe",
    "alternatively",
)


def _safe_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any, default: int = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _attribution(function: Any, value: Any, default: str) -> str:
    try:
        return function(value)
    except Exception:
        return default


def _ambiguity_signals(goal: Mapping[str, Any]) -> list[str]:
    text = f"{goal.get('title', '')} {goal.get('spec', '')}"
    lowered = text.lower()
    signals = [
        f"ambiguity:{word}"
        for word in _AMBIGUITY_WORDS
        if re.search(rf"\b{re.escape(word)}\b", lowered)
    ]
    signals.extend("ambiguity:?" for _ in range(text.count("?")))
    return signals


def classify(
    point: Mapping[str, Any],
    ctx: Mapping[str, Any],
    *,
    goal: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a Planner invocation without mutating or reading external state."""
    point_map = _safe_mapping(point)
    ctx_map = _safe_mapping(ctx)
    goal_map = _safe_mapping(goal)
    task_map = _safe_mapping(task)

    signals = [f"unknown:{field}" for field in _CTX_FIELDS if field not in ctx_map]

    def value(field: str, default: Any) -> Any:
        return ctx_map.get(field, default)

    kind = point_map.get("kind")
    if kind == "retrospective":
        decision_type = "retrospective"
    elif kind == "scouts_done":
        decision_type = "initial_goal_plan" if _number(value("scout_count", 0)) == 0 else "scout_results"
    elif kind == "held":
        architectural_replan = (
            bool(value("signature_repeated", False))
            or _number(value("spec_review_request_changes", 0)) >= 2
            or _number(value("auto_fix_rounds_used", 0))
            >= _number(value("auto_fix_rounds", 0))
        )
        if architectural_replan:
            decision_type = "architectural_replan"
        elif value("failing_ids", ()) or value("in_scope_review_comments", ()):
            decision_type = "fix_strategy"
        else:
            decision_type = "held_task"
    elif kind == "closable":
        decision_type = "closable_goal"
    else:
        decision_type = "other"

    ambiguity = _ambiguity_signals(goal_map)
    explicitly_ambiguous = value("ambiguous", False) is True
    ambiguous = explicitly_ambiguous or len(ambiguity) >= 2
    if explicitly_ambiguous:
        ambiguity.append("ambiguous:true")
    if ambiguous:
        signals.extend(ambiguity)
        if decision_type in ("initial_goal_plan", "scout_results"):
            decision_type = "ambiguous_requirement"

    entity = task_map if kind == "held" and task_map else goal_map
    complexity = _number(entity.get("complexity", 0))
    task_class = _attribution(attribution.task_class, entity or None, "unknown")
    architectural = (
        task_class in ("architectural", "security")
        or complexity >= 7
        or bool(value("touches_interfaces", False))
        or bool(value("touches_migrations", False))
        or decision_type == "architectural_replan"
    )

    if (
        bool(value("touches_auth", False))
        or bool(value("touches_data_deletion", False))
        or bool(value("security_trigger", False))
        or task_class == "security"
    ):
        risk_class = "high"
    elif (
        bool(value("touches_interfaces", False))
        or bool(value("touches_migrations", False))
        or bool(value("semantic_trigger", False))
    ):
        risk_class = "low"
    else:
        risk_class = "none"

    return {
        "decision_type": decision_type,
        "architectural": architectural,
        "ambiguous": ambiguous,
        "risk_class": risk_class,
        "signals": signals,
        "band": _attribution(attribution.band, complexity, "unknown"),
        "task_class": task_class,
    }


def explain(classification: Mapping[str, Any]) -> str:
    """Return a stable one-line summary of a classification."""
    signals = classification.get("signals", ())
    return (
        f"type={classification.get('decision_type')} "
        f"arch={classification.get('architectural')} "
        f"risk={classification.get('risk_class')} "
        f"signals={','.join(str(signal) for signal in signals)}"
    )
