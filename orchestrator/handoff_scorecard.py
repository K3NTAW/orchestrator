"""Read-only economics for executor and Planner handoffs."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import STATE, attribution, planner_telemetry, scorecard


def _tasks(root):
    result = {}
    directory = Path(root) / "tasks"
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            result[row.get("id", path.stem)] = row
    return result


def _runs(root):
    rows = []
    directory = Path(root) / "runs"
    for path in sorted(directory.glob("*.jsonl")) if directory.exists() else ():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("role") == "execute" and row.get("task"):
                rows.append(row)
    return rows


def _lineage(task, tasks):
    """Root/index equivalent to attribution.lineage, for an arbitrary state root."""
    root, index, current = task.get("id"), 0, task
    seen = {root}
    while True:
        constraints = current.get("constraints") or {}
        target = constraints.get("fix_round_for") if isinstance(constraints, dict) else None
        if not target or target in seen:
            break
        seen.add(target)
        root, index = target, index + 1
        current = tasks.get(target, {})
    return {"root": root, "round_index": index}


def _tokens(run):
    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    return sum(int(run.get(key, usage.get(key, 0)) or 0)
               for key in ("input_tokens", "output_tokens"))


def _usd(run):
    return float(run.get("usd", run.get("total_cost_usd", 0)) or 0)


def _reason(task):
    raw = str(task.get("hold_reason") or "")
    lowered = raw.lower()
    if lowered == "gate_red":
        return "gate_red"
    if lowered.startswith("review request_changes"):
        return "review"
    if "conflict" in lowered:
        return "conflict"
    return {"kind": "other", "hold_reason": raw}


def lineage_rows(root=STATE):
    """Return one ordered aggregate for every execute lineage with a run."""
    root = Path(root)
    tasks = _tasks(root)
    grouped = defaultdict(lambda: defaultdict(list))
    round_tasks = {}
    for run in _runs(root):
        task = tasks.get(run["task"], {"id": run["task"], "constraints": {}})
        info = _lineage(task, tasks)
        grouped[info["root"]][info["round_index"]].append(run)
        round_tasks[(info["root"], info["round_index"])] = task

    output = []
    for lineage_root, indexed in sorted(grouped.items()):
        ordered = sorted(indexed)
        executors = []
        tokens = []
        costs = []
        resume = fresh = reconstruction = 0
        estimated = False
        pairs = []
        for position, index in enumerate(ordered):
            runs = indexed[index]
            task = round_tasks[(lineage_root, index)]
            executor = next((r.get("executor") or r.get("tier") for r in runs
                             if r.get("executor") or r.get("tier")), None)
            executors.append(executor)
            tokens.append(sum(_tokens(run) for run in runs))
            costs.append(sum(_usd(run) for run in runs))
            if position:
                mode = next((run.get("resume_mode") for run in runs if run.get("resume_mode")), None)
                mode = mode or (task.get("pipeline") or {}).get("resume", {}).get("mode")
                if mode == "resume":
                    resume += 1
                else:
                    fresh += 1
                    presented = [((run.get("context") or {}).get("presented_tokens")) for run in runs]
                    measured = [value for value in presented if isinstance(value, (int, float))]
                    if measured:
                        reconstruction += sum(measured)
                    else:
                        reconstruction += sum(max(0, int(run.get("input_tokens") or 0) -
                                                   int(run.get("cache_read_input_tokens") or 0)) for run in runs)
                        estimated = True
                pairs.append((executors[position - 1], executor, _reason(task)))
        first = tasks.get(lineage_root, {})
        accepted = bool(first.get("merged_into"))
        output.append({
            "lineage": lineage_root, "task_class": attribution.task_class(first),
            "first_executor": executors[0], "executors_by_round": executors,
            "rounds": len(ordered), "resume_rounds": resume, "fresh_rounds": fresh,
            "tokens_by_round": tokens, "usd_by_round": costs,
            "first_pass": len(ordered) == 1 and accepted, "accepted": accepted,
            "accepted_tokens": sum(tokens) if accepted else 0,
            "accepted_usd": sum(costs) if accepted else 0,
            "reconstruction_tokens": reconstruction, "estimated": estimated,
            "handoff_pairs": pairs,
        })
    return output


def by_start(root=STATE):
    grouped = defaultdict(list)
    for row in lineage_rows(root):
        grouped[(row["first_executor"], row["task_class"])].append(row)
    result = {}
    for key, rows in grouped.items():
        n = len(rows)
        result[key] = {
            "n": n,
            "first_pass_rate": sum(row["first_pass"] for row in rows) / n,
            "avg_rounds": sum(row["rounds"] for row in rows) / n,
            "avg_accepted_tokens": sum(row["accepted_tokens"] for row in rows) / n,
            "avg_accepted_usd": sum(row["accepted_usd"] for row in rows) / n,
            "avg_reconstruction_tokens": sum(row["reconstruction_tokens"] for row in rows) / n,
        }
    return result


def expected_route_cost(task_class, executor, root=STATE, cfg=None):
    rows = [row for row in lineage_rows(root)
            if row["task_class"] == task_class and row["first_executor"] == executor]
    minimum = ((cfg or {}).get("promotion") or {}).get("min_samples", 20)
    if len(rows) < minimum:
        return {"insufficient": True, "n": len(rows)}
    first = [row["tokens_by_round"][0] for row in rows]
    fixes = [sum(row["tokens_by_round"][1:]) for row in rows if row["rounds"] > 1]
    handed = [sum(cost for cost, ex in zip(row["tokens_by_round"][1:], row["executors_by_round"][1:])
                    if ex != executor) for row in rows]
    fix_rows = [row for row in rows if row["rounds"] > 1]
    handoff_rows = [row for row in rows if any(ex != executor for ex in row["executors_by_round"][1:])]
    first_term = sum(first) / len(first)
    fix_term = ((len(fix_rows) / len(rows)) *
                ((sum(fixes) / len(fixes) if fixes else 0) +
                 (sum(row["reconstruction_tokens"] for row in fix_rows) / len(fix_rows) if fix_rows else 0)))
    destination_term = ((len(handoff_rows) / len(rows)) *
                        (sum(handed) / len(handoff_rows) if handoff_rows else 0))
    return {"insufficient": False, "n": len(rows), "expected_route_cost": first_term + fix_term + destination_term,
            "terms": {"first_round": {"value": first_term, "n": len(first)},
                      "fix": {"value": fix_term, "n": len(fix_rows)},
                      "handoff_destination": {"value": destination_term, "n": len(handoff_rows)}}}


def recommendation(task_class, root=STATE, cfg=None):
    rows = [row for row in lineage_rows(root) if row["task_class"] == task_class]
    executors = sorted({row["first_executor"] for row in rows if row["first_executor"]})
    costs = {executor: expected_route_cost(task_class, executor, root, cfg) for executor in executors}
    sufficient = {executor: value for executor, value in costs.items() if not value.get("insufficient")}
    selected = min(sufficient, key=lambda key: sufficient[key]["expected_route_cost"]) if sufficient else None
    first_cost = {}
    for executor in executors:
        values = [row["tokens_by_round"][0] for row in rows if row["first_executor"] == executor]
        if values:
            first_cost[executor] = sum(values) / len(values)
    cheapest = min(first_cost, key=first_cost.get) if first_cost else None
    return {"task_class": task_class, "executor": selected, "cheapest_first": cheapest,
            "start_strong": selected is not None and cheapest != selected, "costs": costs}


def _shadow_rows(root):
    path = Path(root) / "runs/sched/planner_shadow.jsonl"
    rows = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def planner_handoffs(root=STATE):
    invocations = planner_telemetry.read_invocations(root)
    shadow_by_goal = defaultdict(list)
    for row in _shadow_rows(root):
        shadow_by_goal[row.get("goal_id")].append(row)
    accepted = scorecard.accepted_goals(Path(root))
    rows = []
    for launch in invocations:
        if launch.get("route") != "escalate":
            continue
        shadow = shadow_by_goal.get(launch.get("goal_id"), [])
        before = None
        if shadow:
            usage = shadow[-1].get("usage") or {}
            before = usage.get("total_tokens")
        after = launch.get("total_tokens")
        packet_chars = launch.get("packet_chars")
        packet_tokens = round(packet_chars / 4) if isinstance(packet_chars, (int, float)) else None
        rows.append({"launch_id": launch.get("launch_id"), "goal_id": launch.get("goal_id"),
                     "before_tokens": before if before is not None else "unmeasured",
                     "escalation_packet_tokens": packet_tokens if packet_tokens is not None else "unmeasured",
                     "escalation_packet_estimated": packet_tokens is not None,
                     "after_tokens": after if after is not None else "unmeasured",
                     "accepted": launch.get("goal_id") in accepted})
    numeric = lambda key: [row[key] for row in rows if isinstance(row[key], (int, float))]
    return {"rows": rows, "n": len(rows),
            "accepted": sum(row["accepted"] for row in rows),
            "averages": {key: (sum(values) / len(values) if values else "unmeasured")
                         for key in ("before_tokens", "escalation_packet_tokens", "after_tokens")
                         for values in [numeric(key)]}}


def build(root=STATE, cfg=None):
    starts = by_start(root)
    classes = sorted({key[1] for key in starts})
    return {"by_start": {"/".join(key): value for key, value in starts.items()},
            "recommendations": {name: recommendation(name, root, cfg) for name in classes},
            "planner_handoffs": planner_handoffs(root)}


def format_report(card):
    return json.dumps(card, indent=1)
