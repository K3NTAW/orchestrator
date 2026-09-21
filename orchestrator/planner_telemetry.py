"""Per-invocation Planner telemetry and goal-level usage attribution.

The ledgers intentionally contain only routing metadata and token counts.  They
never store prompts, transcripts, or chain-of-thought.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import STATE, bus, planner_runs, pool, scorecard


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _bounded(value):
    """Cap free text defensively while preserving structured row values."""
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, list):
        return [_bounded(item) for item in value]
    if isinstance(value, dict):
        return {key: _bounded(item) for key, item in value.items()}
    return value


def _append(name, row, root=None):
    root = Path(root) if root is not None else STATE
    path = root / "runs" / "sched" / f"{name}.jsonl"
    record = _bounded(dict(row))
    record.setdefault("ts", time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(path):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    return record


def _read(name, root=None):
    root = Path(root) if root is not None else STATE
    path = root / "runs" / "sched" / f"{name}.jsonl"
    rows, malformed = [], 0
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return rows, malformed
    with stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                malformed += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                malformed += 1
    return rows, malformed


def record_launch(*, launch_id, goal_id, event, decision_type, kind, payload_keys,
                  model, tier, account, complexity, band, task_class,
                  architectural, route, route_reason, mode, state_version,
                  packet_chars, started_at, reescalation=False, root=None):
    root = Path(root) if root is not None else STATE
    return _append("planner_invocations", {
        "phase": "launch", "launch_id": launch_id, "goal_id": goal_id,
        "event": event, "decision_type": decision_type, "kind": kind,
        "payload_keys": payload_keys, "model": model, "tier": tier,
        "account": account, "complexity": complexity, "band": band,
        "task_class": task_class, "architectural": architectural,
        "route": route, "route_reason": route_reason, "mode": mode,
        "state_version": state_version, "packet_chars": packet_chars,
        "started_at": started_at, "reescalation": bool(reescalation),
    }, root)


def record_usage(launch_id, *, input_tokens=0, output_tokens=0,
                 cache_read_tokens=0, cache_write_tokens=0, usd=None,
                 latency_s=None, outcome, session_id=None, root=None):
    root = Path(root) if root is not None else STATE
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    cache_read_tokens = int(cache_read_tokens or 0)
    cache_write_tokens = int(cache_write_tokens or 0)
    return _append("planner_invocations", {
        "phase": "usage", "launch_id": launch_id,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": input_tokens + output_tokens + cache_read_tokens // 10,
        "usd": usd, "latency_s": latency_s, "outcome": outcome,
        "session_id": session_id,
    }, root)


def goal_plan_section(goal_id, root=None):
    root = Path(root) if root is not None else STATE
    try:
        lines = (root / "plan.md").read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return None
    token = re.compile(r"GOAL\s+" + re.escape(goal_id) + r"(?![\w-])")
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and token.search(line):
            start = index
            break
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "".join(lines[start:end])


def snapshot(goal_id, root=None):
    root = Path(root) if root is not None else STATE
    tasks = {}
    tasks_dir = root / "tasks"
    for path in sorted(tasks_dir.glob("*.json")) if tasks_dir.exists() else []:
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = task.get("id", path.stem)
        if task_id != goal_id and task.get("parent") != goal_id:
            continue
        constraints = task.get("constraints") or {}
        result = task.get("result")
        result_escalate = bool(result.get("escalate")) if isinstance(result, dict) else False
        tasks[task_id] = {
            "status": task.get("status"),
            "depends_on": task.get("depends_on"),
            "hold_reason": task.get("hold_reason"),
            "fix_round_for": constraints.get("fix_round_for") if isinstance(constraints, dict) else None,
            # Reserved forward-compatible hook; no producer sets result.escalate today.
            "result_escalate": result_escalate,
            "parent": task.get("parent"),
            "created_at": task.get("created_at"),
        }
    section = goal_plan_section(goal_id, root)
    return {"tasks": tasks,
            "plan_hash": hashlib.sha256(section.encode()).hexdigest() if section is not None else None}


def materiality(before, after):
    before_tasks = before.get("tasks") or {}
    after_tasks = after.get("tasks") or {}
    new_tasks = sorted(set(after_tasks) - set(before_tasks))
    dependencies_changed = sorted(
        task_id for task_id in set(before_tasks) & set(after_tasks)
        if before_tasks[task_id].get("depends_on") != after_tasks[task_id].get("depends_on"))

    gained_fixes = set()
    for child_id, child in after_tasks.items():
        target = child.get("fix_round_for")
        if target and (child_id not in before_tasks or before_tasks[child_id].get("fix_round_for") != target):
            gained_fixes.add(target)
    fix_strategy_for = sorted(gained_fixes)

    escalated = False
    for task_id, task in after_tasks.items():
        old = before_tasks.get(task_id, {})
        old_reason = str(old.get("hold_reason") or "").lower()
        new_reason = str(task.get("hold_reason") or "").lower()
        if "escalat" in new_reason and "escalat" not in old_reason:
            escalated = True
        if task.get("result_escalate") and not old.get("result_escalate"):
            escalated = True
        if not task.get("parent") and task.get("status") in {"held", "failed"} and old.get("status") != task.get("status"):
            escalated = True
    before_hash, after_hash = before.get("plan_hash"), after.get("plan_hash")
    plan_changed = before_hash is not None and after_hash is not None and before_hash != after_hash
    material = bool(new_tasks or dependencies_changed or fix_strategy_for or escalated or plan_changed)
    return {"material": material, "new_tasks": new_tasks,
            "dependencies_changed": dependencies_changed,
            "fix_strategy_for": fix_strategy_for, "escalated": escalated,
            "plan_changed": plan_changed}


def record_materiality(launch_id, before, after, root=None):
    root = Path(root) if root is not None else STATE
    return _append("planner_invocations",
                   {"phase": "materiality", "launch_id": launch_id,
                    **materiality(before, after)}, root)


def record_skip(*, goal_id, event, reason, kind=None, payload_key=None, root=None):
    root = Path(root) if root is not None else STATE
    return _append("planner_skips", {"goal_id": goal_id, "event": event,
                                      "reason": reason, "kind": kind,
                                      "payload_key": payload_key}, root)


def read_invocations_with_malformed(root=None):
    root = Path(root) if root is not None else STATE
    phases, malformed = _read("planner_invocations", root)
    merged, order = {}, []
    for phase in phases:
        launch_id = phase.get("launch_id")
        if launch_id is None:
            continue
        if launch_id not in merged:
            merged[launch_id] = {}
            order.append(launch_id)
        merged[launch_id].update(phase)
    return [merged[launch_id] for launch_id in order], malformed


def read_invocations(root=None):
    root = Path(root) if root is not None else STATE
    return read_invocations_with_malformed(root)[0]


def per_goal(goal_id, root=None):
    root = Path(root) if root is not None else STATE
    rows = [row for row in read_invocations(root) if row.get("goal_id") == goal_id]
    tokens = lambda row: int(row.get("total_tokens") or 0)
    planner_tokens = sum(tokens(row) for row in rows)
    fable = [row for row in rows if row.get("tier") == "fable"]
    opus = [row for row in rows if row.get("tier") == "opus"]
    material_count = sum(bool(row.get("material")) for row in rows)
    skip_rows, _ = _read("planner_skips", root)
    return {
        "planner_tokens": planner_tokens,
        "fable_tokens": sum(tokens(row) for row in fable),
        "opus_tokens": sum(tokens(row) for row in opus),
        "other_tokens": sum(tokens(row) for row in rows if row.get("tier") not in {"fable", "opus"}),
        "calls": len(rows), "fable_calls": len(fable), "opus_calls": len(opus),
        "escalations": sum(row.get("route") == "escalate" for row in fable),
        "reescalations": sum(bool(row.get("reescalation")) for row in rows),
        "material_decisions": material_count,
        "tokens_per_material_decision": planner_tokens / material_count if material_count else None,
        "skips": sum(row.get("goal_id") == goal_id for row in skip_rows),
        "usd": sum(float(row.get("usd") or 0) for row in rows),
    }


def accepted_goal_summary(root=None):
    root = Path(root) if root is not None else STATE
    goal_ids = scorecard.accepted_goals(root)
    n_goals = len(goal_ids)
    if not n_goals:
        return {"n_goals": 0, "planner_tokens_per_accepted_goal": None,
                "fable_tokens_per_accepted_goal": None,
                "fable_calls_per_accepted_goal": None, "fable_share": None,
                "planner_tokens_per_material_decision": None,
                "planner_usd_per_accepted_goal": None}
    summaries = [per_goal(goal_id, root) for goal_id in goal_ids]
    planner_tokens = sum(item["planner_tokens"] for item in summaries)
    fable_tokens = sum(item["fable_tokens"] for item in summaries)
    material_count = sum(item["material_decisions"] for item in summaries)
    return {
        "n_goals": n_goals,
        "planner_tokens_per_accepted_goal": planner_tokens / n_goals,
        "fable_tokens_per_accepted_goal": fable_tokens / n_goals,
        "fable_calls_per_accepted_goal": sum(item["fable_calls"] for item in summaries) / n_goals,
        "fable_share": fable_tokens / planner_tokens if planner_tokens else None,
        "planner_tokens_per_material_decision": planner_tokens / material_count if material_count else None,
        "planner_usd_per_accepted_goal": sum(item["usd"] for item in summaries) / n_goals,
    }


def goal_closed_at(goal):
    if goal.get("status") != "done":
        return None
    done = [event.get("ts") for event in (goal.get("events") or [])
            if isinstance(event, dict) and event.get("status") == "done" and event.get("ts") is not None]
    return done[-1] if done else None


def _load_goals(root):
    goals = []
    tasks_dir = root / "tasks"
    for path in sorted(tasks_dir.glob("*.json")) if tasks_dir.exists() else []:
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if task.get("role") in ("triage", "goal") and not task.get("parent"):
            goals.append(task)
    return goals


def interactive_by_goal(root=None, cfg=None, now=None, days=30):
    """Approximately attribute interactive tokens using per-day open-time overlap.

    Transcript usage has day granularity here, so each day's tokens are divided in
    proportion to the seconds each goal was open during that Zurich calendar day.
    """
    root = Path(root) if root is not None else STATE
    cfg = cfg if cfg is not None else bus.pool_config()
    now = time.time() if now is None else now
    summary = planner_runs._interactive_summary(root, cfg, now - days * 86400, now, [])
    goals = _load_goals(root)
    model = (cfg.get("models") or {}).get("planner")
    allocations = {}
    for day_row in summary.get("day_totals", []):
        try:
            day = date.fromisoformat(day_row["day"])
        except (KeyError, TypeError, ValueError):
            continue
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=pool.TZ).timestamp()
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=pool.TZ).timestamp()
        day_end = min(day_end, now)
        overlaps = []
        for goal in goals:
            try:
                opened = float(goal.get("created_at"))
            except (TypeError, ValueError):
                continue
            closed = goal_closed_at(goal)
            closed = now if closed is None else min(float(closed), now)
            seconds = max(0.0, min(day_end, closed) - max(day_start, opened))
            if seconds:
                overlaps.append((goal.get("id"), seconds))
        total_tokens = (int(day_row.get("input_tokens") or 0)
                        + int(day_row.get("output_tokens") or 0)
                        + int(day_row.get("cache_read_tokens") or 0) // 10)
        denominator = sum(seconds for _, seconds in overlaps)
        shares = [(None, 1.0)] if not denominator else [
            (goal_id, seconds / denominator) for goal_id, seconds in overlaps]
        for goal_id, share in shares:
            key = (goal_id, model)
            target = allocations.setdefault(key, {"goal_id": goal_id, "days": [], "tokens": 0.0,
                                                   "model": model,
                                                   "model_source": "assumed_models_planner",
                                                   "method": "day_overlap_approximate"})
            target["days"].append(day.isoformat())
            target["tokens"] += total_tokens * share
    return list(allocations.values())
