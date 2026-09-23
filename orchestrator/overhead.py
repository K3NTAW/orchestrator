"""Orchestration overhead for accepted goal lineages."""
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import STATE, bus, scorecard


ORCHESTRATION_ROLES = (
    "planner", "planner_decision", "planner_shadow", "scout", "review", "spec_review", "challenge",
    "jev_route", "jev_gate", "jev_sched", "jev_points", "jev_rank", "jev_skills", "memory", "routing",
    "env_policy",
)
EXECUTION_ROLES = ("execute", "fix")


def _state(root):
    root = Path(root)
    return root / ".orchestrator" if (root / ".orchestrator").is_dir() else root


def _rows(root):
    state = _state(root)
    rows = []
    runs = state / "runs"
    for path in sorted(runs.glob("*.jsonl")) if runs.exists() else []:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(row, dict) and row.get("role"):
                rows.append(row)
    return rows


def _tokens(row):
    usage = row.get("usage")
    if isinstance(usage, dict):
        return int(bus.normalize_usage(row.get("provider") or "claude", usage)["total_tokens"])
    return int(row.get("est_tokens") or 0)


def _planner_tokens(goal_id, state, rows):
    try:
        planner_runs = scorecard._planner_runs_for_goal(state, goal_id)
    except (AttributeError, TypeError):
        planner_runs = []
    explicit = [run for run in planner_runs if run.get("tokens") is not None]
    if explicit:
        return sum(int(run.get("tokens") or 0) for run in explicit)
    usage = scorecard._planner_usage_totals(state)
    if usage is None:
        return 0
    today = datetime.now().date().isoformat()
    today_rows = [row for row in rows if row.get("goal_id") and
                  datetime.fromtimestamp(float(row.get("ts") or 0)).date().isoformat() == today]
    own = sum(row.get("goal_id") == goal_id for row in today_rows)
    return round(usage[0] * own / len(today_rows)) if today_rows else 0


def for_goal(goal_id, root=STATE):
    """Measure token, cost and latency overhead for one goal-tagged lineage."""
    state = _state(root)
    all_rows = _rows(state)
    rows = [row for row in all_rows if row.get("goal_id") == goal_id]
    roles = defaultdict(lambda: {"tokens": 0, "runs": 0})
    orchestration_tokens = execution_tokens = 0
    orchestration_usd = goal_usd = orchestration_s = 0.0
    timestamps = []
    for row in rows:
        role = row.get("role")
        tokens = _tokens(row)
        roles[role]["tokens"] += tokens
        roles[role]["runs"] += 1
        usd = float(row.get("usd") or 0)
        duration = float(row.get("duration_s") or 0)
        goal_usd += usd
        if role in ORCHESTRATION_ROLES:
            orchestration_tokens += tokens
            orchestration_usd += usd
            orchestration_s += duration
        elif role in EXECUTION_ROLES:
            execution_tokens += tokens
        if row.get("ts") is not None:
            timestamps.append(float(row["ts"]))

    planner_tokens = _planner_tokens(goal_id, state, all_rows)
    if planner_tokens:
        roles["planner"]["tokens"] += planner_tokens
        try:
            roles["planner"]["runs"] += len(scorecard._planner_runs_for_goal(state, goal_id))
        except (AttributeError, TypeError):
            pass
        orchestration_tokens += planner_tokens

    goal_s = max(timestamps) - min(timestamps) if timestamps else 0.0
    unattributed_tokens = sum(_tokens(row) for row in all_rows if not row.get("goal_id"))
    return {
        "goal_id": goal_id,
        "orchestration_tokens": orchestration_tokens,
        "execution_tokens": execution_tokens,
        "amplification": orchestration_tokens / execution_tokens if execution_tokens else None,
        "orchestration_usd": orchestration_usd,
        "goal_usd": goal_usd,
        "cost_share": orchestration_usd / goal_usd if goal_usd else None,
        "orchestration_s": orchestration_s,
        "goal_s": goal_s,
        "latency_share": orchestration_s / goal_s if goal_s else None,
        "roles": dict(sorted(roles.items())),
        "unattributed_tokens": unattributed_tokens,
    }


def report(root=STATE, days=None):
    """Return accepted-goal rows followed by aggregate totals and medians."""
    state = _state(root)
    goals = scorecard.accepted_goals(state)
    if days is not None:
        cutoff = time.time() - float(days) * 86400
        recent = {row.get("goal_id") for row in _rows(state) if float(row.get("ts") or 0) >= cutoff}
        goals = [goal for goal in goals if goal in recent]
    rows = [for_goal(goal, state) for goal in goals]
    ratio_fields = ("amplification", "cost_share", "latency_share")
    additive = ("orchestration_tokens", "execution_tokens", "orchestration_usd", "goal_usd",
                "orchestration_s", "goal_s")
    total = {"goal_id": "total", **{field: sum(row[field] for row in rows) for field in additive}}
    total["amplification"] = (total["orchestration_tokens"] / total["execution_tokens"]
                              if total["execution_tokens"] else None)
    total["cost_share"] = total["orchestration_usd"] / total["goal_usd"] if total["goal_usd"] else None
    total["latency_share"] = total["orchestration_s"] / total["goal_s"] if total["goal_s"] else None
    total["unattributed_tokens"] = rows[0]["unattributed_tokens"] if rows else sum(
        _tokens(row) for row in _rows(state) if not row.get("goal_id"))
    total_roles = defaultdict(lambda: {"tokens": 0, "runs": 0})
    for row in rows:
        for role, values in row["roles"].items():
            total_roles[role]["tokens"] += values["tokens"]
            total_roles[role]["runs"] += values["runs"]
    total["roles"] = dict(sorted(total_roles.items()))
    median = {"goal_id": "median"}
    for field in (*additive, *ratio_fields, "unattributed_tokens"):
        values = [row[field] for row in rows if row.get(field) is not None]
        median[field] = statistics.median(values) if values else None
    median["roles"] = {}
    return [*rows, total, median]


def format_report(rows):
    def cell(value, digits=3):
        return "-" if value is None else str(round(value, digits))
    lines = ["goal\torch_tokens\texec_tokens\tamplification\torch_usd\tgoal_usd\tcost_share\torch_s\tgoal_s\tlatency_share"]
    for row in rows:
        lines.append("\t".join((row["goal_id"], str(round(row["orchestration_tokens"])),
                                str(round(row["execution_tokens"])), cell(row["amplification"]),
                                cell(row["orchestration_usd"], 2), cell(row["goal_usd"], 2),
                                cell(row["cost_share"]), cell(row["orchestration_s"], 1),
                                cell(row["goal_s"], 1), cell(row["latency_share"]))))
    return "\n".join(lines)
