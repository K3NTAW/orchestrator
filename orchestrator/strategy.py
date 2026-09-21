"""Observe workflow strategies without changing the deterministic task policy.

The dependency-map signal intentionally applies only to scout tasks created with
the Planner convention ``constraints.objective``; titles and result prose are
not interpreted as strategy metadata.
"""
import json
import statistics
import tomllib
from datetime import datetime
from pathlib import Path

from . import STATE, attribution, schedlog, scorecard as scorecard_api


STRATEGIES = (
    "direct_execute",
    "scout_execute",
    "dependency_scout_execute",
    "architecture_impact_scouts_then_decompose",
    "execute_specialist_review",
    "security_context_strong_execute_security_review",
    "parallel_wave",
)
REQUIRED = ["security_review_if_security_paths", "tests_green", "spec_review_from_6"]
_STEP_ORDER = ("scout", "spec_review", "execute", "review", "security_review", "fix_round")


def _constraints(task):
    value = task.get("constraints") or {}
    return value if isinstance(value, dict) else {}


def _security_review(task):
    reason = ((task.get("pipeline") or {}).get("review_reason") or "")
    return (reason in ("diff_unavailable", "security_paths_empty")
            or reason.startswith("security_paths:") or reason.startswith("semantic_"))


def _root_id(task, tasks):
    """Use the public helper, completing old/offline lineages from the supplied task map."""
    public = attribution.lineage(task).get("root")
    current, seen = task, {task.get("id")}
    while (target := _constraints(current).get("fix_round_for")) and target not in seen:
        seen.add(target)
        public = target
        current = tasks.get(target, {})
    return public


def derive(task, tasks, waves_rows=None):
    """Return the stable strategy id and observed workflow steps for one execute root."""
    values = list(tasks.values()) if isinstance(tasks, dict) else list(tasks)
    by_id = {row.get("id"): row for row in values if row.get("id")}
    root_id = task.get("id")
    parent = task.get("parent")
    created = task.get("created_at")
    scouts = [row for row in values if row.get("role") == "scout" and row.get("parent") == parent
              and (created is None or row.get("created_at") is not None
                   and row.get("created_at") < created)]
    objectives = [_constraints(row).get("objective") for row in scouts]
    children = [row for row in values if (row.get("inputs") or [None])[0] == root_id]
    spec_reviews = [row for row in values if row.get("role") == "spec_review"
                    and ((row.get("inputs") or [None])[0] == root_id
                         or _constraints(row).get("spec_review_for") == root_id)]
    reviews = [row for row in children if row.get("role") == "review"]
    reviewer_roles = {_constraints(row).get("reviewer_role") for row in reviews}
    fixes = [row for row in values if row.get("role") == "execute"
             and _constraints(row).get("fix_round_for") and _root_id(row, by_id) == root_id]

    matched = []
    if "dependency-map" in objectives:
        matched.append("dependency_scout_execute")
    elif sum(value in ("architecture", "migration-impact", "API-consumers")
             for value in objectives) >= 2:
        matched.append("architecture_impact_scouts_then_decompose")
    elif scouts:
        matched.append("scout_execute")
    if {"acceptance", "adversarial"} <= reviewer_roles:
        matched.append("execute_specialist_review")
    if _security_review(task):
        matched.append("security_context_strong_execute_security_review")
    if waves_rows is None:
        waves_rows = schedlog.read("waves")
    if any(root_id in (row.get("wave") or []) and len(row.get("wave") or []) >= 2
           for row in waves_rows):
        matched.append("parallel_wave")
    if not matched:
        matched = ["direct_execute"]
    matched.sort(key=STRATEGIES.index)

    observed = {"execute"}
    if scouts:
        observed.add("scout")
    if spec_reviews:
        observed.add("spec_review")
    if reviews:
        observed.add("review")
    if _security_review(task):
        observed.add("security_review")
    if fixes:
        observed.add("fix_round")
    return {"strategy": "+".join(matched),
            "steps": [step for step in _STEP_ORDER if step in observed],
            "components": matched}


def _tasks(root):
    rows = {}
    directory = Path(root) / "tasks"
    for path in sorted(directory.glob("T-*.json")) if directory.exists() else []:
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows[row.get("id", path.stem)] = row
    return rows


def _run_rows(root):
    rows = []
    directory = Path(root) / "runs"
    for path in sorted(directory.glob("*.jsonl")) if directory.exists() else []:
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _waves(root):
    if Path(root) == Path(STATE):
        return schedlog.read("waves")
    rows = []
    path = Path(root) / "runs" / "sched" / "waves.jsonl"
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _seconds(start, end):
    if start is None or end is None:
        return None
    try:
        return float(end) - float(start)
    except (TypeError, ValueError):
        try:
            a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            return (b - a).total_seconds()
        except (TypeError, ValueError):
            return None


def _tokens(row):
    if row.get("total_tokens") is not None:
        return row.get("total_tokens") or 0
    return sum(row.get(key) or 0 for key in
               ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def observations(root=STATE):
    """Build one outcome row per resolved initial execute lineage."""
    root = Path(root)
    tasks = _tasks(root)
    runs = _run_rows(root)
    waves = _waves(root)
    efficient = scorecard_api.efficiency(root).get("tasks", {})
    roots = {tid: task for tid, task in tasks.items()
             if task.get("role") == "execute" and not _constraints(task).get("fix_round_for")
             and (task.get("merged_into") or task.get("status") == "failed")}
    output = []
    for tid, task in sorted(roots.items()):
        accepted = bool(task.get("merged_into"))
        if accepted and tid not in efficient:
            continue
        members = {mid for mid, member in tasks.items() if _root_id(member, tasks) == tid}
        members.add(tid)
        lineage = [tasks[mid] for mid in members if mid in tasks]
        reviews = [row for row in tasks.values() if row.get("role") == "review"
                   and (row.get("inputs") or [None])[0] in members]
        metric = efficient.get(tid, {}) if accepted else {}
        related_runs = [row for row in runs if row.get("task") in members]
        tokens = metric.get("tokens") if accepted else sum(_tokens(row) for row in related_runs)
        usd = metric.get("usd") if accepted else sum(row.get("usd") or 0 for row in related_runs)
        if not accepted and not any(row.get("usd") is not None for row in related_runs):
            usd = None
        if not accepted and not related_runs:
            tokens = None
        models = {task.get("executor")}
        models.update(row.get("tier") for row in reviews)
        derived = derive(task, tasks, waves)
        merged_at = (task.get("pipeline") or {}).get("merged_at") or task.get("merged_at")
        fixes = [row for row in lineage if _constraints(row).get("fix_round_for")]
        output.append({
            "task": tid, "goal": metric.get("goal_id") or task.get("parent"),
            "repo": root.parent.name, "strategy": derived["strategy"], "steps": derived["steps"],
            "task_class": attribution.task_class(task), "band": attribution.band(task.get("complexity")),
            "complexity": task.get("complexity"), "models": sorted(value for value in models if value),
            "tokens": tokens, "usd": usd, "time_s": _seconds(task.get("created_at"), merged_at),
            "first_pass": metric.get("first_pass", not fixes),
            "fix_rounds": metric.get("fix_rounds", len(fixes)),
            "gate_reds": sum((row.get("pipeline") or {}).get("gate_reds") or 0 for row in lineage),
            "review_request_changes": sum((row.get("result") or {}).get("verdict") == "request_changes"
                                          for row in reviews),
            "accepted": accepted,
        })
    return output


def scorecard(root=STATE, by=("task_class", "band")):
    grouped = {}
    for row in observations(root):
        key = (row["strategy"],) + tuple(row.get(name) for name in by)
        grouped.setdefault(key, []).append(row)
    card = {}
    for key, rows in grouped.items():
        accepted = [row for row in rows if row["accepted"]]
        first_pass = [row["first_pass"] for row in accepted if row["first_pass"] is not None]
        usd = [row["usd"] for row in rows if row["usd"] is not None]
        times = [row["time_s"] for row in rows if row["time_s"] is not None]
        card[key] = {
            "n": len(rows),
            "accepted_rate": sum(row["accepted"] for row in rows) / len(rows),
            "first_pass_rate": sum(first_pass) / len(first_pass) if first_pass else None,
            "first_pass_defined_count": len(first_pass),
            "median_usd": statistics.median(usd) if usd else None,
            "median_time_s": statistics.median(times) if times else None,
            "avg_fix_rounds": sum(row["fix_rounds"] or 0 for row in rows) / len(rows),
        }
    return card


def format(card):
    lines = ["strategy | task_class | band | n | accepted | first_pass | median_usd | median_time_s | avg_fix_rounds"]
    for key, row in sorted(card.items()):
        first_pass = row["first_pass_rate"]
        values = list(key) + [row["n"], f'{row["accepted_rate"]:.3f}',
                              f'{first_pass:.3f}' if first_pass is not None else None,
                              row["median_usd"], row["median_time_s"], f'{row["avg_fix_rounds"]:.2f}']
        lines.append(" | ".join("-" if value is None else str(value) for value in values))
    return "\n".join(lines)


def _config(root, cfg):
    if cfg is not None:
        return cfg.get("strategy", cfg) if isinstance(cfg, dict) else {}
    try:
        data = tomllib.loads((Path(root) / "pool.toml").read_text())
        return data.get("strategy", {})
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return {}


def recommend(task_class, band, *, repo=None, root=STATE, cfg=None):
    settings = _config(root, cfg)
    mode = settings.get("mode", "shadow")
    if mode not in ("off", "shadow", "active"):
        mode = "shadow"
    if mode == "off":
        return {"mode": mode, "strategy": None, "n": 0, "evidence": {}, "reason": "mode_off",
                "required": list(REQUIRED)}
    minimum = int(settings.get("min_samples", 10))
    rows = [row for row in observations(root) if row["task_class"] == task_class and row["band"] == band
            and (repo is None or row["repo"] == repo)]
    groups = {}
    for row in rows:
        groups.setdefault(row["strategy"], []).append(row)
    evidence = {}
    for name, samples in groups.items():
        accepted = [row for row in samples if row["accepted"]]
        costs = [row["usd"] for row in samples if row["usd"] is not None]
        evidence[name] = {"n": len(samples),
                          "accepted_rate": len(accepted) / len(samples),
                          "first_pass_rate": sum(bool(row["first_pass"]) for row in accepted) / len(accepted)
                          if accepted else 0,
                          "median_usd": statistics.median(costs) if costs else None}
    eligible = {name: value for name, value in evidence.items() if value["n"] >= minimum}
    if not eligible:
        return {"mode": mode, "strategy": None, "n": len(rows), "evidence": evidence,
                "reason": "insufficient_evidence", "required": list(REQUIRED)}
    def rank(item):
        name, value = item
        cost = value["median_usd"]
        return (value["accepted_rate"], value["first_pass_rate"],
                -(cost if cost is not None else float("inf")), -STRATEGIES.index(name.split("+")[0]))
    name, best = max(eligible.items(), key=rank)
    return {"mode": mode, "strategy": name, "n": best["n"], "evidence": evidence,
            "reason": "recommended", "required": list(REQUIRED)}
