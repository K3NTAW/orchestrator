"""Pure adaptive-concurrency selection for scheduler-ready tasks.

``interface`` and ``migration`` reason kinds are reserved for a future
interference extension.  Current interference rules already classify shared
globs (``same_glob`` and ``glob_covers``) as hard conflicts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from orchestrator.critical_path import est_duration_s


def reason_kind(reason: str) -> str:
    """Return the stable kind prefix of an interference reason."""
    return reason.split(":", 1)[0]


def compatible_groups(ready_ids: Iterable[str], pairwise_rows: Iterable[Mapping[str, Any]]) -> list[list[str]]:
    """Return deterministic hard/soft connected components of ready tasks."""
    members = sorted(set(ready_ids))
    ready = set(members)
    neighbors = {task_id: set() for task_id in members}
    for row in pairwise_rows:
        left, right = row.get("a"), row.get("b")
        if row.get("level") in {"hard", "soft"} and left in ready and right in ready and left != right:
            neighbors[left].add(right)
            neighbors[right].add(left)

    groups = []
    unseen = set(members)
    while unseen:
        first = min(unseen)
        pending = [first]
        component = []
        unseen.remove(first)
        while pending:
            current = pending.pop()
            component.append(current)
            for neighbor in sorted(neighbors[current], reverse=True):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    pending.append(neighbor)
        groups.append(sorted(component))
    return groups


def group_contribution(group: Iterable[str], pairwise_rows: Iterable[Mapping[str, Any]]) -> int:
    """Return the safe concurrency contribution of one compatible group."""
    members = sorted(set(group))
    if len(members) <= 1:
        return len(members)

    member_set = set(members)
    pair_levels: dict[tuple[str, str], set[str]] = {}
    for row in pairwise_rows:
        left, right = row.get("a"), row.get("b")
        if left in member_set and right in member_set and left != right and row.get("level") in {"hard", "soft"}:
            pair = tuple(sorted((left, right)))
            pair_levels.setdefault(pair, set()).add(str(row["level"]))

    pair_count = len(members) * (len(members) - 1) // 2
    every_pair_hard = len(pair_levels) == pair_count and all(levels == {"hard"} for levels in pair_levels.values())
    if every_pair_hard or (len(members) >= 3 and any("hard" in levels for levels in pair_levels.values())):
        return 1
    return 2


def _fixed_mode(cfg: Mapping[str, Any] | None) -> bool:
    scheduler = (cfg or {}).get("scheduler", {})
    return isinstance(scheduler, Mapping) and scheduler.get("concurrency_mode", "adaptive") == "fixed"


def _capacity(snapshot: Mapping[str, Any] | None) -> int | None:
    if snapshot is None:
        return None
    if snapshot.get("fallback"):
        return max(0, int(snapshot.get("claude_workers_free", 0)))
    executors = snapshot.get("executors", {})
    return sum(max(0, int(executor.get("free", 0))) for executor in executors.values())


def select_limit(
    hard_max: int,
    ready_ids: Iterable[str],
    tasks: Mapping[str, Mapping[str, Any]],
    pairwise_rows: Iterable[Mapping[str, Any]],
    priorities: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None = None,
    pressure: Mapping[str, Any] | None = None,
    durations: Mapping[str, float] | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select an evidence-based concurrency limit without side effects."""
    cap = max(0, int(hard_max))
    ready = list(ready_ids)
    if _fixed_mode(cfg):
        return _result(cap, cap, ["fixed"], ready, tasks, priorities, durations, [])
    if not ready:
        return _result(cap, 0, ["no_ready"], ready, tasks, priorities, durations, [])

    rows = list(pairwise_rows)
    raw_groups = compatible_groups(ready, rows)
    groups = [{"members": group, "contribution": group_contribution(group, rows)} for group in raw_groups]
    limit = cap
    reasons = [f"hard_max={cap}"]

    structural = sum(group["contribution"] for group in groups)
    if structural < limit:
        limit = structural
        reasons.append(f"groups={len(groups)} contributions={structural}")
    for group in groups:
        if len(group["members"]) > 1 and group["contribution"] == 1:
            reasons.append("coupled_group:" + ",".join(group["members"]))

    capacity = _capacity(snapshot)
    if capacity is not None and capacity < limit:
        limit = capacity
        reasons.append(f"capacity={capacity}")

    pressure_level = (pressure or {}).get("pressure", "none")
    if pressure_level == "saturated":
        pressure_cap = max(1, sum(len(group["members"]) == 1 for group in groups))
        if pressure_cap < limit:
            limit = pressure_cap
            reasons.append("merge_pressure=saturated")

    if capacity is None or capacity > 0:
        limit = max(1, limit)
    limit = min(cap, limit)
    return _result(cap, limit, reasons, ready, tasks, priorities, durations, groups)


def _result(hard_max, limit, reasons, ready, tasks, priorities, durations, groups):
    ranked = sorted(ready, key=lambda task_id: (-priorities.get(task_id, {}).get("priority", 0), task_id))
    selected = ranked[:limit]
    estimates = [
        float(durations[task_id]) if durations and task_id in durations else float(est_duration_s(tasks[task_id]))
        for task_id in selected
        if task_id in tasks
    ]
    benefit = sum(estimates) - max(estimates) if estimates else 0.0
    return {
        "hard_max": hard_max,
        "limit": limit,
        "reasons": reasons,
        "tasks_considered": ready,
        "expected_benefit_s": benefit,
        "groups": groups,
    }


def log_row(result: Mapping[str, Any], wave_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Build the payload for ``schedlog.append('concurrency', ...)``."""
    return {
        "hard_max": result["hard_max"],
        "limit": result["limit"],
        "reasons": result["reasons"],
        "tasks_considered": result["tasks_considered"],
        "expected_benefit_s": result["expected_benefit_s"],
        "selected": list(wave_ids or []),
        "observed_outcome": None,
    }
