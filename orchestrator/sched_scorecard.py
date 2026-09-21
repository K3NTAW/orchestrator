"""Join scheduler predictions to task outcomes for heuristic validation."""

from __future__ import annotations

import json
import math
import statistics
import threading
from datetime import datetime
from pathlib import Path

from . import STATE, duration, schedlog


_READ_LOCK = threading.Lock()


def _stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def _sched_rows(root, name):
    """Use schedlog's tolerant reader while allowing an explicit state root."""
    directory = Path(root) / "runs" / "sched"
    with _READ_LOCK:
        previous = schedlog.SCHED_DIR
        try:
            schedlog.SCHED_DIR = directory
            return schedlog.read_with_malformed(name)
        finally:
            schedlog.SCHED_DIR = previous


def _tasks(root):
    result = {}
    directory = Path(root) / "tasks"
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            task = json.loads(path.read_text())
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if isinstance(task, dict):
            result[str(task.get("id", path.stem))] = task
    return result


def _pipeline_value(task, key):
    pipeline = task.get("pipeline") if isinstance(task.get("pipeline"), dict) else {}
    return pipeline.get(key, task.get(key))


def _ids(value):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            task_id = item.get("task", item.get("id"))
            if task_id is not None:
                result.append(str(task_id))
    return result


def _deferred_pair(item):
    if not isinstance(item, dict):
        return None
    a, b = item.get("a"), item.get("b")
    reason = item.get("reason", item.get("reasons", ""))
    if a is None and item.get("task") is not None:
        a, b = item.get("task"), item.get("with", item.get("blocked_by"))
    text = " ".join(str(part) for part in reason) if isinstance(reason, list) else str(reason)
    if a is None or b is None or not text.startswith(("hard:", "soft:")):
        return None
    return tuple(sorted((str(a), str(b))))


def _failed_conflict(task):
    values = [task.get("reason"), task.get("hold_reason"), task.get("failure_reason")]
    pipeline = task.get("pipeline") if isinstance(task.get("pipeline"), dict) else {}
    values.extend((pipeline.get("reason"), pipeline.get("failure_reason")))
    return any(value == "rebase_conflict" or "merge conflict" in str(value).lower()
               for value in values if value is not None)


def pair_outcomes(root=STATE):
    tasks = _tasks(root)
    waves, _ = _sched_rows(root, "waves")
    stale, _ = _sched_rows(root, "stale")
    stale_high = {str(row.get("task", row.get("id"))) for row in stale
                  if row.get("risk") == "high"}
    stale_conflict = {str(row.get("task", row.get("id"))) for row in stale
                      if row.get("action") == "rebase_conflict"}
    children = {str((task.get("constraints") or {}).get("fix_round_for"))
                for task in tasks.values()
                if isinstance(task.get("constraints"), dict)
                and (task.get("constraints") or {}).get("fix_round_for") is not None}

    pairs = {}
    wave_dispatch = {}
    serialized = set()
    rank = {"none": 0, "soft": 1, "hard": 2}
    for wave in waves:
        at = _stamp(wave.get("ts"))
        for task_id in _ids(wave.get("wave")):
            if at is not None:
                wave_dispatch[task_id] = min(at, wave_dispatch.get(task_id, at))
        for item in wave.get("deferred", []) if isinstance(wave.get("deferred"), list) else []:
            pair = _deferred_pair(item)
            if pair:
                serialized.add(pair)
        for prediction in wave.get("predicted", []) if isinstance(wave.get("predicted"), list) else []:
            if not isinstance(prediction, dict) or prediction.get("a") is None or prediction.get("b") is None:
                continue
            a, b = sorted((str(prediction["a"]), str(prediction["b"])))
            level = str(prediction.get("level", "none"))
            reasons = prediction.get("reasons", [])
            reasons = list(reasons) if isinstance(reasons, (list, tuple)) else [str(reasons)]
            current = pairs.get((a, b))
            if current is None:
                pairs[(a, b)] = {"a": a, "b": b, "level": level,
                                 "predicted_reasons": reasons}
            else:
                if rank.get(level, 0) > rank.get(current["level"], 0):
                    current["level"] = level
                current["predicted_reasons"] = sorted(set(current["predicted_reasons"]) | set(reasons))

    result = []
    for key, row in sorted(pairs.items()):
        a, b = key
        ta, tb = tasks.get(a, {}), tasks.get(b, {})
        da = _stamp(_pipeline_value(ta, "dispatched_at")) or wave_dispatch.get(a)
        db = _stamp(_pipeline_value(tb, "dispatched_at")) or wave_dispatch.get(b)
        gates = [stamp for stamp in (_stamp(_pipeline_value(ta, "gated_at")),
                                     _stamp(_pipeline_value(tb, "gated_at"))) if stamp is not None]
        concurrent = da is not None and db is not None and (not gates or max(da, db) < min(gates))
        row.update({
            "both_ran_concurrently": concurrent,
            "actual_conflict": (_failed_conflict(ta) or _failed_conflict(tb)
                                or a in stale_conflict or b in stale_conflict),
            "stale_event": a in stale_high or b in stale_high,
            "fix_round": a in children or b in children,
            "serialized": key in serialized,
        })
        result.append(row)
    return result


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


def _number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else 0.0)


def task_outcomes(root=STATE):
    tasks = _tasks(root)
    waves, _ = _sched_rows(root, "waves")
    estimates, first_ready = {}, {}
    for wave in waves:
        stamp = _stamp(wave.get("ts"))
        if stamp is not None:
            for task_id in _ids(wave.get("ready")):
                first_ready[task_id] = min(stamp, first_ready.get(task_id, stamp))
        priority = wave.get("priority")
        if isinstance(priority, dict):
            for task_id, explanation in priority.items():
                if isinstance(explanation, dict):
                    estimates[str(task_id)] = explanation

    children = {}
    for task_id, task in tasks.items():
        parent = (task.get("constraints") or {}).get("fix_round_for") if isinstance(task.get("constraints"), dict) else None
        if parent is not None:
            children.setdefault(str(parent), []).append(task_id)
    runs = _run_rows(root)

    def lineage(task_id):
        found, pending = {task_id}, list(children.get(task_id, []))
        while pending:
            child = pending.pop()
            if child not in found:
                found.add(child)
                pending.extend(children.get(child, []))
        return found

    outcomes = []
    for task_id, task in sorted(tasks.items()):
        if task.get("role") != "execute":
            continue
        prediction = estimates.get(task_id, {})
        predicted = prediction.get("est_duration_s", prediction.get("est_s"))
        actual = duration.actual_s(task, [row for row in runs if str(row.get("task")) == task_id])
        error = duration.prediction_error(predicted, actual)
        dispatched = _stamp(_pipeline_value(task, "dispatched_at"))
        origin = first_ready.get(task_id, _stamp(task.get("created_at", task.get("created"))))
        related = lineage(task_id)
        task_runs = [row for row in runs if str(row.get("task")) in related]
        tokens = sum(_number(row.get("total_tokens", row.get("tokens"))) for row in task_runs)
        usd = sum(_number(row.get("usd", row.get("cost_usd"))) for row in task_runs)
        outcomes.append({
            "task": task_id,
            "queue_wait_s": dispatched - origin if dispatched is not None and origin is not None and dispatched >= origin else None,
            "predicted_duration_s": predicted,
            "actual_duration_s": actual,
            "error": error,
            "critical_path_s": prediction.get("critical_path_s", prediction.get("priority")),
            "accepted": bool(task.get("merged_into")),
            "fix_rounds": len(related) - 1,
            "total_tokens": tokens if task_runs else None,
            "total_usd": usd if task_runs else None,
        })
    return outcomes


def _literal_files(task):
    changed = task.get("changed_files")
    entries = changed if isinstance(changed, list) else task.get("scope", [])
    return {str(item).replace("\\", "/") for item in entries
            if isinstance(item, str) and item and not item.endswith(("/", "**"))
            and not any(char in item for char in "*?[")}


def build(root=STATE):
    root = Path(root)
    pairs = pair_outcomes(root)
    tasks = _tasks(root)
    task_rows = task_outcomes(root)
    telemetry = {name: _sched_rows(root, name) for name in ("waves", "stale", "dispatch")}
    malformed = sum(value[1] for value in telemetry.values())

    hard = [row for row in pairs if row["level"] == "hard" and row["both_ran_concurrently"]]
    soft = [row for row in pairs if row["level"] == "soft" and row["both_ran_concurrently"]]
    none = [row for row in pairs if row["level"] == "none" and row["both_ran_concurrently"]]
    concurrent = [row for row in pairs if row["both_ran_concurrently"]]
    serialized = [row for row in pairs if row["serialized"]]
    determined = []
    for row in serialized:
        files_a = _literal_files(tasks.get(row["a"], {}))
        files_b = _literal_files(tasks.get(row["b"], {}))
        if files_a and files_b:
            determined.append(files_a.isdisjoint(files_b))
    errors = [row["error"]["ratio"] for row in task_rows if row["error"] is not None]
    waits = [row["queue_wait_s"] for row in task_rows if row["queue_wait_s"] is not None]

    goals = {}
    for task in tasks.values():
        goal = task.get("parent")
        created = _stamp(task.get("created_at", task.get("created")))
        merged = _stamp(_pipeline_value(task, "merged_at"))
        if goal is not None and created is not None:
            bucket = goals.setdefault(str(goal), {"created": [], "merged": []})
            bucket["created"].append(created)
            if merged is not None:
                bucket["merged"].append(merged)
    latencies = {goal: max(times["merged"]) - min(times["created"])
                 for goal, times in goals.items() if times["merged"]}

    def rate(rows, key):
        return sum(bool(row[key]) for row in rows) / len(rows) if rows else None

    card = {
        "hard_conflict_precision": rate(hard, "actual_conflict"),
        "hard_conflict_precision_n": len(hard),
        "soft_conflict_usefulness": (rate(soft, "actual_conflict") - rate(none, "actual_conflict")
                                     if soft and none else None),
        "soft_conflict_usefulness_n": len(soft) + len(none) if soft and none else 0,
        "unnecessary_serialization": sum(determined) / len(determined) if determined else None,
        "unnecessary_serialization_n": len(determined),
        "serialization_undetermined": len(serialized) - len(determined),
        "conflict_rate": rate(concurrent, "actual_conflict"),
        "conflict_rate_n": len(concurrent),
        "stale_work_rate": rate(pairs, "stale_event"),
        "stale_work_rate_n": len(pairs),
        "duration_mape": statistics.fmean(abs(value - 1.0) for value in errors) if errors else None,
        "duration_mape_n": len(errors),
        "median_queue_wait_s": statistics.median(waits) if waits else None,
        "median_queue_wait_s_n": len(waits),
        "goal_latency_s": latencies or None,
        "goal_latency_s_n": len(latencies),
        "accepted_cost_usd": sum(row["total_usd"] for row in task_rows
                                 if row["accepted"] and row["total_usd"] is not None),
        "accepted_cost_usd_n": sum(1 for row in task_rows
                                   if row["accepted"] and row["total_usd"] is not None),
        "n_waves": len(telemetry["waves"][0]),
        "n_pairs": len(pairs),
        "malformed": malformed,
    }
    if card["accepted_cost_usd_n"] == 0:
        card["accepted_cost_usd"] = None
    return card


def format(card):
    """Render a compact text table with a sample count on every metric row."""
    labels = (
        "hard_conflict_precision", "soft_conflict_usefulness", "unnecessary_serialization",
        "conflict_rate", "stale_work_rate", "duration_mape", "median_queue_wait_s",
        "goal_latency_s", "accepted_cost_usd",
    )
    lines = ["metric                         value  n", "-----------------------------  -----  -"]
    for name in labels:
        value = card.get(name)
        rendered = "—" if value is None else json.dumps(value, sort_keys=True)
        lines.append(f"{name:<29}  {rendered}  {card.get(name + '_n', 0)}")
    lines.extend((f"serialization_undetermined     {card.get('serialization_undetermined', 0)}  {card.get('serialization_undetermined', 0)}",
                  f"n_waves                       {card.get('n_waves', 0)}  {card.get('n_waves', 0)}",
                  f"n_pairs                       {card.get('n_pairs', 0)}  {card.get('n_pairs', 0)}",
                  f"malformed                     {card.get('malformed', 0)}  {card.get('malformed', 0)}"))
    return "\n".join(lines)
