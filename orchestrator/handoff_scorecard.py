"""Read-only economics for executor and Planner handoffs."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median

from . import STATE, attribution, cache_telemetry, planner_telemetry, scorecard


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


def _cache_value(run, canonical, *legacy):
    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    for key in (canonical, *legacy):
        value = run.get(key, usage.get(key))
        if isinstance(value, (int, float)):
            return value
    return 0


def _packet_meta(row):
    value = row.get("packet_meta")
    return value if isinstance(value, dict) else {}


def _evidence_ids(row):
    values = _packet_meta(row).get("evidence_ids") or []
    return {str(value) for value in values} if isinstance(values, (list, tuple, set)) else set()


def _event_time(row, first):
    keys = (("started_at", "first_event_at", "ts") if first else
            ("ended_at", "finished_at", "completed_at", "ts"))
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            if first and key == "ts" and isinstance(row.get("duration_s"), (int, float)):
                return value - row["duration_s"]
            return value
    return None


def _handoff_metrics(predecessor, successor, cfg=None):
    retained = _cache_value(successor, "cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens")
    previous_read = _cache_value(predecessor, "cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens")
    uncached = _cache_value(successor, "input_uncached_tokens", "input_tokens")
    chars = _packet_meta(successor).get("chars")
    packet_tokens = chars / 4 if isinstance(chars, (int, float)) else None
    duplicated = sorted(_evidence_ids(predecessor) & _evidence_ids(successor))
    before_end, after_start = _event_time(predecessor, False), _event_time(successor, True)
    latency = max(0, after_start - before_end) if before_end is not None and after_start is not None else None
    provider = successor.get("provider") or "claude"
    ratios = cache_telemetry._cache_cfg(cfg)
    read_ratio = ratios.get(f"{provider}_read_ratio", ratios["claude_read_ratio"])
    lost = max(0, previous_read - retained)
    effective = None if packet_tokens is None else uncached + lost * read_ratio + packet_tokens
    return {
        "cached_context_retained": retained,
        "cache_lost": lost,
        "uncached_reconstruction": uncached,
        "handoff_packet_tokens": packet_tokens if packet_tokens is not None else "unmeasured",
        "duplicated_evidence": duplicated,
        "latency_s": latency if latency is not None else "unmeasured",
        "effective_handoff_cost": effective if effective is not None else "unmeasured",
    }


def handoff_rows(root=STATE, cfg=None):
    """Return measured predecessor/successor pairs without changing routing."""
    root = Path(root)
    tasks = _tasks(root)
    runs_by_task = defaultdict(list)
    for run in _runs(root):
        runs_by_task[run["task"]].append(run)
    rows = []
    for task_id, task in sorted(tasks.items()):
        constraints = task.get("constraints") or {}
        predecessor_id = next((constraints.get(key) for key in
                               ("fix_round_for", "replacement_for", "refile_for", "re_file_for")
                               if constraints.get(key)), None)
        if not predecessor_id or not runs_by_task.get(predecessor_id) or not runs_by_task.get(task_id):
            continue
        predecessor, successor = runs_by_task[predecessor_id][-1], runs_by_task[task_id][0]
        changed = (predecessor.get("executor") or predecessor.get("tier")) != (successor.get("executor") or successor.get("tier"))
        replacement = not constraints.get("fix_round_for")
        rows.append({"kind": "worker_replacement" if replacement else ("executor_change" if changed else "fix_round"),
                     "predecessor": predecessor_id, "successor": task_id,
                     "predecessor_executor": predecessor.get("executor") or predecessor.get("tier"),
                     "successor_executor": successor.get("executor") or successor.get("tier"),
                     **_handoff_metrics(predecessor, successor, cfg)})

    invocations = planner_telemetry.read_invocations(root)
    shadow_by_goal = defaultdict(list)
    for shadow in _shadow_rows(root):
        shadow_by_goal[shadow.get("goal_id")].append(shadow)
    for launch in invocations:
        if launch.get("route") != "escalate" or not shadow_by_goal.get(launch.get("goal_id")):
            continue
        predecessor = shadow_by_goal[launch["goal_id"]][-1]
        successor = {**launch, "packet_meta": launch.get("packet_meta") or {
            "chars": launch.get("packet_chars"), "evidence_ids": launch.get("evidence_ids") or []}}
        rows.append({"kind": "planner_escalation", "predecessor": predecessor.get("launch_id"),
                     "successor": launch.get("launch_id"), **_handoff_metrics(predecessor, successor, cfg)})
    return rows


def _handoff_medians(rows):
    fields = ("cached_context_retained", "cache_lost", "uncached_reconstruction",
              "handoff_packet_tokens", "latency_s", "effective_handoff_cost")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["kind"]].append(row)
    result = {}
    for kind, kind_rows in sorted(grouped.items()):
        result[kind] = {}
        for field in fields:
            values = [row[field] for row in kind_rows if isinstance(row.get(field), (int, float))]
            result[kind][field] = median(values) if values else "unmeasured"
    return result


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
        handoff = (first.get("pipeline") or {}).get("handoff")
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
            "handoff": handoff if isinstance(handoff, dict) else None,
        })
    return output


def by_start(root=STATE):
    lineage = lineage_rows(root)
    grouped = defaultdict(list)
    for row in lineage:
        grouped[(row["first_executor"], row["task_class"])].append(row)
    result = {}
    for key, rows in grouped.items():
        n = len(rows)
        stamped = [row for row in rows if row.get("handoff")]
        active_share = (sum(bool(row["handoff"].get("switched")) for row in stamped) / len(stamped)
                        if stamped else 0.0)
        result[key] = {
            "n": n,
            "first_pass_rate": sum(row["first_pass"] for row in rows) / n,
            "avg_rounds": sum(row["rounds"] for row in rows) / n,
            "avg_accepted_tokens": sum(row["accepted_tokens"] for row in rows) / n,
            "avg_accepted_usd": sum(row["accepted_usd"] for row in rows) / n,
            "avg_reconstruction_tokens": sum(row["reconstruction_tokens"] for row in rows) / n,
            "active_share": active_share,
        }
    return result


def expected_route_cost(task_class, executor, root=STATE, cfg=None):
    rows = [row for row in lineage_rows(root)
            if row["task_class"] == task_class and row["first_executor"] == executor]
    minimum = ((cfg or {}).get("promotion") or {}).get("min_samples", 20)
    if len(rows) < minimum:
        return {"insufficient": True, "n": len(rows), "effective_handoff_cost": "unmeasured"}
    first = [row["tokens_by_round"][0] for row in rows]
    fixes = [sum(row["tokens_by_round"][1:]) for row in rows if row["rounds"] > 1]
    handed = [sum(cost for cost, ex in zip(row["tokens_by_round"][1:], row["executors_by_round"][1:])
                    if ex != executor) for row in rows]
    fix_rows = [row for row in rows if row["rounds"] > 1]
    changed_lineages = [row for row in rows if any(ex != executor for ex in row["executors_by_round"][1:])]
    first_term = sum(first) / len(first)
    fix_term = ((len(fix_rows) / len(rows)) *
                ((sum(fixes) / len(fixes) if fixes else 0) +
                 (sum(row["reconstruction_tokens"] for row in fix_rows) / len(fix_rows) if fix_rows else 0)))
    destination_term = ((len(changed_lineages) / len(rows)) *
                        (sum(handed) / len(changed_lineages) if changed_lineages else 0))
    measured_handoffs = [row["effective_handoff_cost"] for row in handoff_rows(root, cfg)
                         if row["kind"] in ("executor_change", "worker_replacement") and
                         row.get("successor_executor") == executor and
                         isinstance(row["effective_handoff_cost"], (int, float))]
    effective_handoff_cost = median(measured_handoffs) if measured_handoffs else "unmeasured"
    return {"insufficient": False, "n": len(rows), "expected_route_cost": first_term + fix_term + destination_term,
            "effective_handoff_cost": effective_handoff_cost,
            "terms": {"first_round": {"value": first_term, "n": len(first)},
                      "fix": {"value": fix_term, "n": len(fix_rows)},
                      "handoff_destination": {"value": destination_term, "n": len(changed_lineages)}}}


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
    handoffs = handoff_rows(root, cfg)
    return {"by_start": {"/".join(key): value for key, value in starts.items()},
            "recommendations": {name: recommendation(name, root, cfg) for name in classes},
            "planner_handoffs": planner_handoffs(root),
            "handoffs": {"rows": handoffs, "n": len(handoffs),
                         "medians_by_kind": _handoff_medians(handoffs)}}


def format_report(card):
    handoffs = card.get("handoffs") or {}
    rows = handoffs.get("rows") or []
    columns = ("kind", "predecessor", "successor", "cached_context_retained", "cache_lost",
               "uncached_reconstruction", "handoff_packet_tokens", "duplicated_evidence",
               "latency_s", "effective_handoff_cost")
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(",".join(value) if isinstance(value, list) else str(value)
                               for value in (row.get(column, "unmeasured") for column in columns)))
    for kind, values in (handoffs.get("medians_by_kind") or {}).items():
        lines.append("median:" + kind + "\t" + "\t".join(
            f"{key}={value}" for key, value in values.items()))
    return "\n".join(lines)
