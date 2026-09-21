"""Experimental, fail-open Jev decision points.

The deterministic policy remains authoritative.  Shadow mode only records Jev's
suggestion; active mode may apply suggestions which add work or context, never
ones which reduce deterministic safeguards.
"""
from . import STATE, decision_log, jev
from . import pool as P


POINTS = (
    "scout_necessity",
    "context_escalation",
    "review_escalation",
    "planner_relaunch",
)

OPTIONS = {
    "scout_necessity": ("launch", "skip"),
    "context_escalation": ("expand", "keep"),
    "review_escalation": ("add_review", "keep"),
    "planner_relaunch": ("relaunch", "wait"),
}

QUESTIONS = {
    point: {
        option: {
            "type": "noul",
            "instructions": f"Should the orchestrator {option.replace('_', ' ')} for {point.replace('_', ' ')}?",
        }
        for option in options
    }
    for point, options in OPTIONS.items()
}

KINDS = {
    "scout_necessity": "scout",
    "context_escalation": "strategy",
    "review_escalation": "review_plan",
    "planner_relaunch": "strategy",
}

ESCALATIONS = {
    "context_escalation": "expand",
    "review_escalation": "add_review",
    "planner_relaunch": "relaunch",
}


def _mode(point, cfg):
    if cfg is None:
        try:
            cfg = P.config()
        except (FileNotFoundError, OSError, ValueError):
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = getattr(cfg, "cfg", {})
    jev_cfg = cfg.get("jev") or {}
    points = jev_cfg.get("points") or cfg.get("points") or {}
    value = points.get(point, "shadow") if isinstance(points, dict) else "shadow"
    if isinstance(value, dict):
        value = value.get("mode", "shadow")
    return value if value in ("off", "shadow", "active") else "shadow"


def _memory_titles(task):
    values = task.get("memory_titles") or task.get("memories") or []
    titles = []
    for value in values[:10]:
        title = value.get("title") if isinstance(value, dict) else value
        if isinstance(title, str):
            titles.append(jev.redact(title)[:200])
    return titles


def _state(task, evidence):
    constraints = task.get("constraints") or {}
    task_class = task.get("task_class") or (constraints.get("task_class") if isinstance(constraints, dict) else None)
    return {
        "title": jev.redact(str(task.get("title") or ""))[:300],
        "spec": jev.redact(str(task.get("spec") or "")[:600]),
        "acceptance_count": len(task.get("acceptance") or []),
        "scope": [jev.redact(str(path))[:300] for path in (task.get("scope") or [])[:100]],
        "complexity": task.get("complexity"),
        "task_class": task_class,
        "memory_titles": _memory_titles(task),
        "deterministic_evidence": _redacted_evidence(evidence),
    }


def _redacted_evidence(value, depth=0):
    """Copy bounded evidence without ever reading paths or accepting content fields."""
    if depth >= 5:
        return "[bounded]"
    if isinstance(value, str):
        return jev.redact(value)[:600]
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:100]:
            clean_key = jev.redact(str(key))[:100]
            if clean_key.lower() in {"content", "contents", "file_content", "file_contents", "source"}:
                continue
            out[clean_key] = _redacted_evidence(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redacted_evidence(item, depth + 1) for item in value[:100]]
    return value if value is None or isinstance(value, (bool, int, float)) else jev.redact(str(value))[:600]


def _answer(result, point):
    answers = result["answers"]
    signals = {}
    confidences = []
    for option in OPTIONS[point]:
        answer = answers[option]
        signals[option] = float(answer["noul"])
        if not 0 <= signals[option] <= 1:
            raise ValueError("probability outside [0, 1]")
        confidences.append(answer.get("confidence"))
    suggestion = max(OPTIONS[point], key=lambda option: (signals[option], -OPTIONS[point].index(option)))
    confidence = None if any(value is None for value in confidences) else sum(confidences) / len(confidences)
    return signals, confidence, suggestion


def _scout_escalates(evidence):
    for key in ("scout_needed", "needs_scout", "necessary"):
        if key in evidence:
            return bool(evidence[key])
    reuse = evidence.get("reuse_check")
    if isinstance(reuse, dict):
        for key in ("scout_needed", "needs_scout", "necessary"):
            if key in reuse:
                return bool(reuse[key])
        reuse = reuse.get("result") or reuse.get("decision")
    if isinstance(reuse, str):
        return reuse.lower() not in {"unnecessary", "reuse", "skip", "not_needed", "not needed"}
    return True


def _applied(point, mode, suggestion, evidence):
    if mode != "active":
        return False
    if point == "scout_necessity":
        return suggestion == "launch" and _scout_escalates(evidence)
    return suggestion == ESCALATIONS.get(point)


def ask(point, task, evidence, *, cfg=None, ask_fn=jev.ask):
    """Ask one named point and record its evidence; failures never affect policy."""
    if point not in POINTS:
        raise ValueError(f"unknown Jev point: {point}")
    evidence = dict(evidence or {})
    mode = _mode(point, cfg)
    signals, confidence, suggestion, error = {}, None, None, None
    if mode != "off":
        try:
            response = ask_fn(_state(task, evidence), QUESTIONS[point], task=task.get("id"))
            if response is None:
                raise ValueError("Jev returned no answer")
            signals, confidence, suggestion = _answer(response, point)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    applied = _applied(point, mode, suggestion, evidence) if suggestion is not None else False
    hard_constraints = (["security_review_deterministic", "required_reviews_floor"]
                        if point == "review_escalation" else [])
    decision_log.record(
        KINDS[point], task.get("id") or task.get("title") or "unknown",
        candidates=list(OPTIONS[point]), hard_constraints=hard_constraints,
        deterministic=evidence, jev=signals, selected=suggestion,
        reason=error or ("applied escalation" if applied else f"{mode} suggestion"),
        confidence=confidence, mode=mode, extra={"point": point},
    )
    return {"point": point, "mode": mode, "signals": signals, "confidence": confidence,
            "suggestion": suggestion, "applied": applied, "error": error}


def _deterministic_choice(evidence, options):
    for key in ("selected", "suggestion", "decision", "baseline", "recommended"):
        value = evidence.get(key) if isinstance(evidence, dict) else None
        if value in options:
            return value
    return None


def summary(root=STATE):
    """Aggregate recorded point suggestions for later shadow evaluation."""
    result = {point: {"n": 0, "agreement": None, "mean_confidence": None} for point in POINTS}
    confidence_values = {point: [] for point in POINTS}
    agreements = {point: [] for point in POINTS}
    for row in decision_log.read_all(root=root):
        point = (row.get("extra") or {}).get("point")
        if point not in result:
            continue
        result[point]["n"] += 1
        if isinstance(row.get("confidence"), (int, float)):
            confidence_values[point].append(row["confidence"])
        deterministic = _deterministic_choice(row.get("deterministic"), OPTIONS[point])
        if deterministic is not None and row.get("selected") in OPTIONS[point]:
            agreements[point].append(row["selected"] == deterministic)
    for point in POINTS:
        if confidence_values[point]:
            result[point]["mean_confidence"] = sum(confidence_values[point]) / len(confidence_values[point])
        if agreements[point]:
            result[point]["agreement"] = sum(agreements[point]) / len(agreements[point])
    return result
