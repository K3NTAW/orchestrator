"""Empirical execution-duration estimates for scheduler task ranking.

The evidence ladder is ``(task_class, executor, band)``, then
``(task_class, band)``, then ``band``; the first level meeting the configured
minimum sample count wins, otherwise the deterministic band prior is used.
"""
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from . import STATE, attribution, bus, critical_path


def _stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def actual_s(task, rows=None):
    """Return gated-minus-claimed seconds, or summed execute-run seconds."""
    if not isinstance(task, dict):
        return None
    pipeline = task.get("pipeline") if isinstance(task.get("pipeline"), dict) else {}
    claimed = _stamp(pipeline.get("claimed_at", task.get("claimed_at")))
    gated = _stamp(pipeline.get("gated_at", task.get("gated_at")))
    if claimed is not None and gated is not None and gated >= claimed:
        return gated - claimed

    supplied = rows if rows is not None else task.get("runs")
    if not isinstance(supplied, (list, tuple)):
        return None
    durations = []
    for row in supplied:
        if not isinstance(row, dict) or row.get("role") != "execute":
            continue
        value = row.get("duration_s")
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            durations.append(float(value))
    return sum(durations) if durations else None


def _run_rows(root):
    rows = []
    run_dir = Path(root) / "runs"
    if not run_dir.exists():
        return rows
    for path in sorted(run_dir.glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def history(root=STATE):
    """Return known durations for merged execute tasks, ignoring bad files."""
    root = Path(root)
    grouped = {}
    for row in _run_rows(root):
        grouped.setdefault(row.get("task"), []).append(row)
    result = []
    task_dir = root / "tasks"
    if not task_dir.exists():
        return result
    for path in sorted(task_dir.glob("*.json")):
        try:
            task = json.loads(path.read_text())
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if not isinstance(task, dict) or task.get("role") != "execute" or not task.get("merged_into"):
            continue
        task_id = task.get("id", path.stem)
        duration = actual_s(task, grouped.get(task_id))
        if duration is None:
            continue
        result.append({
            "task": task_id,
            "task_class": attribution.task_class(task),
            "executor": task.get("executor") or task.get("tier"),
            "band": attribution.band(task.get("complexity", 1)),
            "actual_s": duration,
        })
    return result


def _settings(cfg):
    if cfg is None:
        try:
            cfg = bus.pool_config()
        except (OSError, ValueError, TypeError):
            cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    scheduler = cfg.get("scheduler", cfg)
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    mode = scheduler.get("duration_mode", "empirical")
    try:
        minimum = max(1, int(scheduler.get("duration_min_samples", 5)))
    except (TypeError, ValueError):
        minimum = 5
    try:
        trim = float(scheduler.get("duration_trim", 0.1))
    except (TypeError, ValueError):
        trim = 0.1
    if not math.isfinite(trim):
        trim = 0.1
    return (mode if mode == "cold_start" else "empirical", minimum,
            min(0.499999, max(0.0, trim)))


def _statistic(values, trim):
    ordered = sorted(values)
    if len(ordered) >= 10:
        dropped = int(len(ordered) * trim)
        if dropped:
            ordered = ordered[dropped:-dropped]
    return float(statistics.median(ordered))


def estimate(task, root=STATE, cfg=None):
    """Estimate using class+executor+band, class+band, band, then prior."""
    task = task if isinstance(task, dict) else {}
    band = attribution.band(task.get("complexity", 1))
    task_class = attribution.task_class(task)
    executor = task.get("executor") or task.get("tier")
    base = {"band": band, "task_class": task_class, "executor": executor}
    mode, minimum, trim = _settings(cfg)
    if mode != "cold_start":
        samples = history(root)
        levels = (
            ("class_executor", lambda row: row["task_class"] == task_class
             and row["executor"] == executor and row["band"] == band),
            ("band_class", lambda row: row["task_class"] == task_class and row["band"] == band),
            ("band", lambda row: row["band"] == band),
        )
        for source, matches in levels:
            values = [row["actual_s"] for row in samples if matches(row)]
            if len(values) >= minimum:
                return {"est_s": _statistic(values, trim), "source": source,
                        "n": len(values), **base}
    return {"est_s": float(critical_path.COLD_START_S[band]), "source": "cold_start",
            "n": 0, **base}


def explain_for(tasks, root=STATE, cfg=None):
    """Return the estimate explanation for every task, keyed by task id."""
    iterable = tasks.values() if isinstance(tasks, dict) else tasks
    return {task["id"]: estimate(task, root=root, cfg=cfg) for task in iterable}


def durations_for(tasks, root=STATE, cfg=None):
    """Return task-id duration estimates suitable for critical_path helpers."""
    return {task_id: explanation["est_s"]
            for task_id, explanation in explain_for(tasks, root=root, cfg=cfg).items()}


def prediction_error(predicted_s, actual_s):
    """Return absolute error and predicted/actual ratio when both are usable."""
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in (predicted_s, actual_s)):
        return None
    if predicted_s < 0 or actual_s <= 0:
        return None
    return {"abs_s": abs(float(predicted_s) - float(actual_s)),
            "ratio": float(predicted_s) / float(actual_s)}
