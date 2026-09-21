"""Measure merge-queue pressure and selectively defer interfering work."""

from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from . import STATE, bus, interference, schedlog


DEFAULTS = {
    "merge_pressure_mode": "observe",
    "merge_queue_elevated": 3,
    "merge_queue_saturated": 5,
    "merge_conflicts_saturated": 2,
}


def _scheduler_cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    if cfg is None:
        try:
            source = bus.pool_config()
        except (ImportError, OSError, ValueError):
            source = {}
    else:
        source = cfg
    scheduler = source.get("scheduler", source) if isinstance(source, Mapping) else {}
    return {**DEFAULTS, **scheduler}


def _tasks(root: Path | str) -> list[dict[str, Any]]:
    result = []
    for path in sorted((Path(root) / "tasks").glob("*.json")):
        try:
            task = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(task, dict):
            result.append(task)
    return result


def _recent(ts: Any, cutoff: float) -> bool:
    try:
        return float(ts) >= cutoff
    except (TypeError, ValueError):
        return False


def _conflict_task(task: Mapping[str, Any], cutoff: float) -> bool:
    if task.get("role") != "execute":
        return False
    conflict = task.get("reason") == "rebase_conflict" or str(task.get("hold_reason", "")).lower().startswith(
        "merge conflict"
    )
    if not conflict:
        return False
    timestamps = [task.get("updated_at"), task.get("created_at")]
    timestamps.extend(event.get("ts") for event in task.get("events", []) if isinstance(event, Mapping))
    known = [stamp for stamp in timestamps if stamp is not None]
    return not known or any(_recent(stamp, cutoff) for stamp in known)


def _approval_voided(task: Mapping[str, Any]) -> bool:
    events = [event for event in task.get("events", []) if isinstance(event, Mapping)]
    if any("approval void" in json.dumps(event).lower() for event in events):
        return True
    expected = []
    for event in events:
        pipeline = event.get("pipeline")
        if isinstance(pipeline, Mapping) and "reviews_expected" in pipeline:
            expected.append(pipeline.get("reviews_expected"))
    return len(expected) > 1 and any(current != previous for previous, current in zip(expected, expected[1:]))


def assess(root: Path | str = STATE, now: float | None = None, window_s: float = 86400,
           cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a deterministic snapshot of merge queue depth and recent contention."""
    current = time.time() if now is None else float(now)
    cutoff = current - float(window_s)
    tasks = _tasks(root)
    reviewed = {
        str(task.get("inputs", [""])[0])
        for task in tasks
        if task.get("role") == "review" and task.get("inputs")
    }
    waiting_tasks = [
        task for task in tasks
        if task.get("role") == "execute"
        and task.get("status") == "done"
        and not task.get("merged_into")
        and ((task.get("pipeline") or {}).get("gated_at") or str(task.get("id")) in reviewed)
    ]
    waiting_tasks.sort(key=lambda task: str(task.get("id", "")))
    waiting = [str(task.get("id")) for task in waiting_tasks]
    waits = []
    for task in waiting_tasks:
        first_green = (task.get("pipeline") or {}).get("first_green_at")
        try:
            waits.append(max(0.0, current - float(first_green)))
        except (TypeError, ValueError):
            pass

    # schedlog.read is deliberately tolerant of malformed JSONL rows. Its state
    # directory is normally the same root supplied here and can be patched by callers.
    stale_rows = schedlog.read("stale")
    recent_stale = [row for row in stale_rows if _recent(row.get("ts"), cutoff)]
    task_conflicts = sum(_conflict_task(task, cutoff) for task in tasks)
    recent_conflicts = task_conflicts + sum(row.get("action") == "rebase_conflict" for row in recent_stale)
    recent_rebases = sum(row.get("action") == "rebased" for row in recent_stale)
    stale_events = sum(row.get("risk") == "high" for row in recent_stale)
    invalidated = sum(_approval_voided(task) for task in tasks)

    settings = _scheduler_cfg(cfg)
    depth = len(waiting)
    elevated = int(settings["merge_queue_elevated"])
    saturated = int(settings["merge_queue_saturated"])
    conflict_limit = int(settings["merge_conflicts_saturated"])
    if depth <= 1:
        pressure = "none"
        reason = f"queue depth {depth}; at most one task is waiting"
    elif depth >= saturated:
        pressure = "saturated"
        reason = f"queue depth {depth} >= saturated threshold {saturated}"
    elif depth >= elevated and recent_conflicts >= conflict_limit:
        pressure = "saturated"
        reason = (f"queue depth {depth} >= elevated threshold {elevated} and "
                  f"{recent_conflicts} recent conflicts >= threshold {conflict_limit}")
    elif depth >= elevated:
        pressure = "elevated"
        reason = f"queue depth {depth} >= elevated threshold {elevated}"
    else:
        pressure = "none"
        reason = f"queue depth {depth} below elevated threshold {elevated}"
    return {
        "waiting": waiting,
        "queue_depth": depth,
        "median_wait_s": median(waits) if waits else None,
        "recent_conflicts": recent_conflicts,
        "recent_rebases": recent_rebases,
        "stale_events": stale_events,
        "invalidated": invalidated,
        "pressure": pressure,
        "reason": reason,
    }


def throttle(candidate_ids: Iterable[str], tasks: Any, waiting_ids: Iterable[str],
             assessment: Mapping[str, Any], pairwise_rows: Iterable[Mapping[str, Any]] | None = None,
             cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Defer only candidates coupled to waiting merge work when throttling is active."""
    candidates = list(dict.fromkeys(str(task_id) for task_id in candidate_ids))
    waiting = list(dict.fromkeys(str(task_id) for task_id in waiting_ids))
    lookup = ({str(key): value for key, value in tasks.items()} if isinstance(tasks, Mapping)
              else {str(task.get("id")): task for task in (tasks or [])})
    rows = list(pairwise_rows or [])

    def coupled(candidate: str, waiting_id: str) -> bool:
        for row in rows:
            endpoints = {str(row.get("a")), str(row.get("b"))}
            if endpoints == {candidate, waiting_id}:
                return row.get("level") in {"hard", "soft"}
        finding = interference.classify(
            lookup.get(candidate, {"id": candidate}),
            lookup.get(waiting_id, {"id": waiting_id}),
            lookup,
        )
        return finding.get("level") in {"hard", "soft"}

    would_defer: dict[str, str] = {}
    if assessment.get("pressure") == "saturated":
        for candidate in candidates:
            blocker = next((waiting_id for waiting_id in waiting if coupled(candidate, waiting_id)), None)
            if blocker is not None:
                would_defer[candidate] = f"merge_pressure:{blocker}"

    applied = assessment.get("pressure") == "saturated" and _scheduler_cfg(cfg)["merge_pressure_mode"] == "throttle"
    deferred = dict(would_defer) if applied else {}
    keep = [candidate for candidate in candidates if candidate not in deferred]
    return {"keep": keep, "deferred": deferred, "would_defer": would_defer, "applied": applied}
