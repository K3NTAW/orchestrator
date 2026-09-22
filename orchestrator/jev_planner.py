"""Jev shadow signal for Planner tier routing decisions."""

from __future__ import annotations

from collections import OrderedDict, Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from orchestrator import jev, schedlog

jev.declare_boundary('planner', fields=('decision_type', 'band', 'task_class', 'architectural', 'ambiguous', 'risk_class', 'signals', 'subject_id', 'subject_title', 'spec_excerpt', 'goal_title', 'scope_count', 'scout_count', 'failing_test_count', 'spec_review_request_changes', 'auto_fix_rounds_used', 'signature_repeated'),
                     max_chars=4000, raw_source_allowed=False,
                     notes='Titles 200; spec 300; up to 10 signals of 100 chars.')


QUESTIONS = OrderedDict(
    [
        ("routine_localized", {"type": "noul", "instructions": "Is this a routine, localized change with a clear implementation path?"}),
        ("architectural_reasoning", {"type": "noul", "instructions": "Does this decision require architectural reasoning across components?"}),
        ("requirements_ambiguous", {"type": "noul", "instructions": "Are the requirements materially ambiguous or underspecified?"}),
        ("long_horizon", {"type": "noul", "instructions": "Does success require reasoning over a long implementation horizon?"}),
        ("fable_improves_outcome", {"type": "noul", "instructions": "Would the Fable tier materially improve the likely outcome?"}),
    ]
)


def subject(goal: dict[str, Any], task: dict[str, Any] | None) -> dict[str, Any]:
    return task if task is not None else goal


def _redacted(value: Any, limit: int) -> str:
    return jev.redact(str(value or ""))[:limit]


def state(
    classification: dict[str, Any],
    ctx: dict[str, Any],
    goal: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = subject(goal, task)
    result = {
        "decision_type": classification.get("decision_type"),
        "band": classification.get("band"),
        "task_class": classification.get("task_class"),
        "architectural": classification.get("architectural"),
        "ambiguous": classification.get("ambiguous"),
        "risk_class": classification.get("risk_class"),
        "signals": [_redacted(signal, 100) for signal in (classification.get("signals") or [])[:10]],
        "subject_id": item.get("id"),
        "subject_title": _redacted(item.get("title"), 200),
        "spec_excerpt": _redacted(str(item.get("spec") or "")[:300], 300),
        "goal_title": _redacted(goal.get("title"), 200),
        "scope_count": len(item.get("scope") or []),
        "scout_count": ctx.get("scout_count"),
        "failing_test_count": len(ctx.get("failing_ids") or []),
        "spec_review_request_changes": ctx.get("spec_review_request_changes"),
        "auto_fix_rounds_used": ctx.get("auto_fix_rounds_used"),
        "signature_repeated": ctx.get("signature_repeated"),
    }
    while len(json.dumps(result)) > 4000 and result["spec_excerpt"]:
        result["spec_excerpt"] = result["spec_excerpt"][:-100]
    while len(json.dumps(result)) > 4000 and result["signals"]:
        result["signals"].pop()
    return result


def fingerprint(classification: dict[str, Any]) -> str:
    payload = {
        key: classification.get(key)
        for key in ("decision_type", "risk_class", "architectural", "ambiguous", "band", "task_class")
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def should_ask(
    router_mode: str,
    jev_mode: str,
    hard_reasons: list[str],
    availability: dict[str, Any],
    route: str,
) -> tuple[bool, str]:
    if jev_mode == "off":
        return False, "jev_off"
    if hard_reasons:
        return False, "hard_decision"
    available = sum(
        1 for value in availability.values() if isinstance(value, dict) and value.get("available") is True
    )
    if available < 2:
        return False, "single_tier"
    if route != "escalate":
        return False, "deterministic"
    if router_mode == "off":
        return False, "cannot_affect"
    return True, "eligible"


def _answer(answer: Any) -> tuple[float | None, float | None]:
    if not isinstance(answer, dict):
        return None, None
    try:
        probability = float(answer["noul"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not 0.0 <= probability <= 1.0:
        return None, None
    confidence = answer.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None
    return probability, confidence


def _sched_call(function, name: str, root, *args):
    if root is None:
        return function(name, *args)
    previous = schedlog.SCHED_DIR
    try:
        schedlog.SCHED_DIR = Path(root) / "runs" / "sched"
        return function(name, *args)
    finally:
        schedlog.SCHED_DIR = previous


def ask(
    classification: dict[str, Any],
    ctx: dict[str, Any],
    goal: dict[str, Any],
    *,
    cfg: dict[str, Any],
    router_mode: str,
    hard_reasons: list[str],
    availability: dict[str, Any],
    route: str,
    state_version: Any,
    task: dict[str, Any] | None = None,
    ask_fn=jev.ask,
    cache: dict[Any, Any] | None = None,
    root=None,
) -> dict[str, Any]:
    mode = cfg.get("jev_mode", "shadow")
    item = subject(goal, task)
    subject_id = item.get("id")
    fp = fingerprint(classification)
    cache_key = (subject_id, state_version, fp)
    allowed, reason = should_ask(router_mode, mode, hard_reasons, availability, route)
    base = {
        "asked": False,
        "reason": reason,
        "mode": mode,
        "signals": {key: None for key in QUESTIONS},
        "soft_adjust": 0.0,
        "confidence": None,
        "cached": False,
    }

    if allowed and cache is not None and cache_key in cache:
        result = dict(cache[cache_key])
        result["cached"] = True
    elif allowed:
        result = dict(base)
        result["asked"] = True
        try:
            raw = ask_fn(state(classification, ctx, goal, task), QUESTIONS, site="planner", task=subject_id)
        except Exception:
            raw = None
        if raw is None:
            result["reason"] = "jev_unavailable"
        elif not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
            result["reason"] = "jev_malformed"
        else:
            parsed = {key: _answer(raw["answers"].get(key)) for key in QUESTIONS}
            probabilities = {key: value[0] for key, value in parsed.items()}
            if any(value is None for value in probabilities.values()):
                result["reason"] = "jev_malformed"
            else:
                result["reason"] = "eligible"
                result["signals"] = probabilities
                positive = sum(probabilities[key] for key in tuple(QUESTIONS)[1:]) / 4.0
                adjust = 0.2 * positive - 0.2 * probabilities["routine_localized"]
                result["soft_adjust"] = max(-0.2, min(0.2, adjust))
                confidences = [value[1] for value in parsed.values()]
                if all(value is not None for value in confidences):
                    result["confidence"] = sum(confidences) / len(confidences)
        if cache is not None:
            cache[cache_key] = dict(result)
    else:
        result = base

    row = {
        "goal_id": goal.get("id"),
        "subject_id": subject_id,
        "decision_type": classification.get("decision_type"),
        "state_version": state_version,
        "fingerprint": fp,
        **result,
    }
    _sched_call(schedlog.append, "planner_jev", root, row)
    return result


def summary(root=None) -> dict[str, Any]:
    rows, _malformed = _sched_call(schedlog.read_with_malformed, "planner_jev", root)
    reasons = Counter(row.get("reason") for row in rows if not row.get("asked"))
    adjustments = [float(row.get("soft_adjust", 0.0)) for row in rows]
    return {
        "n": len(rows),
        "asked": sum(bool(row.get("asked")) for row in rows),
        "cached": sum(bool(row.get("cached")) for row in rows),
        "skip_reasons": dict(reasons),
        "mean_soft_adjust": sum(adjustments) / len(adjustments) if adjustments else 0.0,
    }
