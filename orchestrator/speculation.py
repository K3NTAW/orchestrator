"""Policy and shadow evidence for selective speculative execution.

This module only decides and measures.  A selected speculative result must still
pass the normal gate, review, and serial merge path; nothing here dispatches or
merges work.
"""
from __future__ import annotations

import fnmatch
import json
from datetime import datetime
from pathlib import Path

from . import STATE, bus, decision_log, scorecard

DEFAULTS = {
    "mode": "off",
    "min_fix_round_p": 0.5,
    "min_samples": 5,
    "min_retry_cost_usd": 1.0,
}


def _config(cfg):
    if cfg is None:
        try:
            cfg = bus.pool_config()
        except Exception:
            cfg = {}
    section = (cfg.get("speculation") or {}) if isinstance(cfg, dict) else {}
    values = {**DEFAULTS, **section}
    review = cfg.get("review", {}) if isinstance(cfg, dict) else {}
    values["security_paths"] = section.get("security_paths", review.get("security_paths", []))
    if values["mode"] not in ("off", "shadow", "active"):
        values["mode"] = "off"
    return values


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _priority(value):
    return _number(value.get("critical_path_s")) if isinstance(value, dict) else _number(value)


def _retry_cost(measured):
    if "fix_round_cost_usd" in measured:
        return _number(measured.get("fix_round_cost_usd"))
    cost = measured.get("cost_to_accepted_usd", measured.get("cost_to_accepted"))
    fix_p = measured.get("fix_round_p", measured.get("fix_round_probability"))
    return _number(cost) * _number(fix_p)


def _unsafe(task, patterns):
    constraints = task.get("constraints") or {}
    if constraints.get("task_class") == "security" or task.get("task_class") == "security":
        return True
    for path in task.get("scope") or task.get("write_scope") or []:
        lowered = str(path).lower()
        if "migration" in lowered or "config" in lowered or lowered.endswith("pool.toml"):
            return True
        if any(fnmatch.fnmatch(str(path), pattern) for pattern in patterns):
            return True
    return False


def eligible(task, *, priority, ready_priorities, evidence, snapshot, budget_left_usd, cfg=None):
    """Return all failed eligibility conditions and, on success, two executors.

    Expected retry cost uses explicit fix_round_cost_usd when present. Otherwise
    it is cost_to_accepted_usd * fix_round_p: the expected rework share of accepted
    cost. Scorecard's cost_to_accepted and fix_round_probability are aliases.
    The shadow estimator uses this same proxy.
    """
    settings = _config(cfg)
    reasons = []
    ready = [_priority(value) for value in (ready_priorities or {}).values()]
    if ready and _priority(priority) < max(ready):
        reasons.append("not_critical")

    baseline = task.get("executor") or task.get("tier")
    measured = (evidence or {}).get(baseline, {})
    fix_p = measured.get("fix_round_p", measured.get("fix_round_probability"))
    if _number(fix_p) < _number(settings["min_fix_round_p"]):
        reasons.append("low_fix_round_p")
    if _number(measured.get("n", measured.get("n_tasks", 0))) < _number(settings["min_samples"]):
        reasons.append("insufficient_samples")
    if _retry_cost(measured) < _number(settings["min_retry_cost_usd"]):
        reasons.append("low_retry_cost")

    # A snapshot is already the capacity authority.  Keep this policy usable with
    # both full capacity.snapshot rows and the deliberately small public shape.
    available = []
    for executor_id, row in (snapshot or {}).get("executors", {}).items():
        if not isinstance(row, dict) or _number(row.get("free")) <= 0:
            continue
        if row.get("enabled") is False or row.get("cooldown") or _number(row.get("cooldown_until")) > 0:
            continue
        roles = row.get("roles")
        complexity = _number(task.get("complexity"), 1)
        if roles is not None and "execute" not in roles:
            continue
        if complexity < _number(row.get("complexity_min"), 1):
            continue
        if row.get("complexity_max") is not None and complexity > _number(row.get("complexity_max")):
            continue
        if row.get("day_tasks_left") == 0:
            continue
        available.append(executor_id)
    available = sorted(set(available))
    if len(available) < 2:
        reasons.append("no_spare_capacity")

    initial_cost = _number(measured.get("cost_to_accepted_usd", measured.get("cost_to_accepted")))
    if _number(budget_left_usd) < 2 * initial_cost:
        reasons.append("insufficient_budget")
    if _unsafe(task, settings["security_paths"]):
        reasons.append("unsafe_to_duplicate")
    return {"eligible": not reasons, "reasons": reasons,
            "executors": available[:2] if not reasons else []}


def plan(task, **kwargs):
    settings = _config(kwargs.get("cfg"))
    eligibility = eligible(task, **kwargs)
    mode = settings["mode"]
    if mode == "off":
        return {"speculate": False, "mode": mode, "reason": "mode_off", "eligibility": eligibility}
    if mode == "shadow":
        reason = "would_speculate" if eligibility["eligible"] else "ineligible"
        return {"speculate": False, "mode": mode, "reason": reason, "eligibility": eligibility}
    return {"speculate": eligibility["eligible"], "mode": mode,
            "reason": "eligible" if eligibility["eligible"] else "ineligible", "eligibility": eligibility}


def select_result(candidates):
    """Select at most one green result using stable, economic tie breakers."""
    green = [row for row in candidates if row.get("tests_green")]
    if not green:
        return {"selected": None, "discarded": list(candidates), "reason": "no_green"}
    key = lambda row: (_number(row.get("gate_reds")), _number(row.get("usd")),
                       _number(row.get("duration_s")), str(row.get("executor", "")))
    selected = min(green, key=key)
    discarded = [row for row in candidates if row is not selected]
    return {"selected": selected, "discarded": discarded, "reason": "best_green"}


def _stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _tasks(root):
    rows = {}
    for path in sorted((Path(root) / "tasks").glob("*.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("id"):
            rows[row["id"]] = row
    return rows


def _run_costs(root):
    costs = {}
    for path in sorted((Path(root) / "runs").glob("*")):
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("role") == "execute" and row.get("task"):
                costs[row["task"]] = costs.get(row["task"], 0.0) + _number(row.get("usd"))
    return costs


def shadow_estimate(root=STATE, evidence=None):
    """Estimate lineage economics using eligible()'s expected retry-cost proxy."""
    root = Path(root)
    tasks, costs = _tasks(root), _run_costs(root)
    if evidence is None:
        try:
            evidence = scorecard.executor_economics(root=root)
        except Exception:
            evidence = {}
    children = {}
    for row in tasks.values():
        parent = (row.get("constraints") or {}).get("fix_round_for")
        if parent:
            children.setdefault(parent, []).append(row)
    result = []
    for task_id, initial in sorted(tasks.items()):
        fixes = children.get(task_id, [])
        if not fixes or (initial.get("constraints") or {}).get("fix_round_for"):
            continue
        executor = initial.get("executor") or initial.get("tier")
        economics = (evidence or {}).get(executor, {})
        p_other = economics.get("first_pass_p", economics.get("first_pass_green_rate", 0))
        initial_usd = costs.get(task_id, 0.0)
        fix_usd = _retry_cost(economics)
        expected = fix_usd * _number(p_other) - initial_usd
        start = _stamp((initial.get("pipeline") or {}).get("first_green_at")
                       or (initial.get("pipeline") or {}).get("gated_at")
                       or initial.get("updated_at"))
        ends = [_stamp((row.get("pipeline") or {}).get("first_green_at")
                       or (row.get("pipeline") or {}).get("gated_at")
                       or row.get("updated_at")) for row in fixes]
        ends = [value for value in ends if value is not None]
        result.append({"task": task_id, "initial_usd": initial_usd, "fix_round_usd": fix_usd,
                       "dup_cost_usd": 2 * initial_usd, "expected_saved_usd": expected,
                       "latency_saved_s": max(0.0, max(ends) - start) if start is not None and ends else 0.0,
                       "would_have_paid": expected > 0})
    return result


def summary(rows):
    rows = list(rows)
    return {"n": len(rows), "paid_rate": (sum(bool(row.get("would_have_paid")) for row in rows) / len(rows)
                                             if rows else 0),
            "net_usd": sum(_number(row.get("expected_saved_usd")) for row in rows)}


def record(task_id, plan):
    """Persist the speculation decision without changing task state."""
    eligibility = plan.get("eligibility") or {}
    return decision_log.record(
        "speculation", task_id, candidates=eligibility.get("executors", []),
        hard_constraints=eligibility.get("reasons", []), deterministic=True,
        selected=eligibility.get("executors") if plan.get("speculate") else None,
        rejected=[] if plan.get("speculate") else eligibility.get("executors", []),
        reason=plan.get("reason"), mode=plan.get("mode"), extra={"speculate": bool(plan.get("speculate"))})
