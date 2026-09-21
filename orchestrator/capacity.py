"""Read-only executor capacity snapshots and deterministic task admission."""

from datetime import datetime
import time

from . import pool as pool_module


def snapshot(pool, running_claude=0, inflight_claude=0, now=None):
    """Return capacity at *now* without invoking state-rolling pool methods."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).astimezone().date().isoformat()
    executors = {}
    for executor_id, executor in pool.executors.items():
        effective_day_tasks = executor.day_tasks if executor.day == today else 0
        budget = executor.daily_budget_tasks
        executors[executor_id] = {
            "free": max(0, executor.max_parallel - executor.running),
            "cooling_s": max(0, executor.cooldown_until - now),
            "enabled": executor.enabled,
            "roles": list(executor.roles),
            "complexity": [executor.complexity_min, executor.complexity_max],
            "day_tasks_left": None if budget == 0 else budget - effective_day_tasks,
            "weight": executor.weight,
        }

    cap = pool.cfg.get("window_cap_tokens", getattr(pool, "cap", 2_000_000))
    accounts = {}
    for account in pool.accounts:
        utilization = 0.0
        if now - account.window_started <= pool_module.WINDOW_S:
            utilization = (account.window_tokens + account.planner_window_tokens) / cap
        accounts[account.id] = {
            "utilization": utilization,
            "cooling_s": max(0, account.cooldown_until - now),
        }

    max_workers = pool.cfg.get("limits", {}).get("max_parallel_claude_workers", 4)
    enabled_execute = [row for row in executors.values()
                       if row["enabled"] and "execute" in row["roles"]]
    return {
        "executors": executors,
        "claude_workers_free": max(0, max_workers - running_claude - inflight_claude),
        "accounts": accounts,
        "fallback": bool(enabled_execute) and all(row["cooling_s"] > 0 for row in enabled_execute),
        "ts": now,
    }


def _complexity(task):
    return int(task.get("complexity", 1))


def eligible_executors_from_snapshot(task, snapshot):
    """Return executor ids satisfying every capacity constraint in a snapshot."""
    complexity = _complexity(task)
    if snapshot.get("fallback"):
        return ["claude"] if pool_module.fallback_tier(complexity) is not None else []
    return [executor_id for executor_id, row in snapshot.get("executors", {}).items()
            if row["enabled"]
            and "execute" in row["roles"]
            and row["cooling_s"] == 0
            and row["complexity"][0] <= complexity <= row["complexity"][1]
            and row["free"] > 0
            and (row["day_tasks_left"] is None or row["day_tasks_left"] > 0)]


def _priority(task_id, priorities):
    value = priorities.get(task_id, {})
    if isinstance(value, dict):
        return value.get("critical_path_s", 0)
    return value or 0


def _reason(task, snapshot, free):
    complexity = _complexity(task)
    if snapshot.get("fallback"):
        if pool_module.fallback_tier(complexity) is None:
            matching = [row for row in snapshot.get("executors", {}).values()
                        if row["enabled"] and "execute" in row["roles"]
                        and row["complexity"][0] <= complexity <= row["complexity"][1]]
            return "cooldown" if matching and all(row["cooling_s"] > 0 for row in matching) \
                else "no_eligible_executor"
        return "account_capacity" if free.get("claude", 0) <= 0 else "no_eligible_executor"

    rows = [row for row in snapshot.get("executors", {}).values()
            if row["enabled"] and "execute" in row["roles"]
            and row["complexity"][0] <= complexity <= row["complexity"][1]]
    if not rows:
        return "no_eligible_executor"
    non_cooling = [row for row in rows if row["cooling_s"] == 0]
    if not non_cooling:
        return "cooldown"
    within_budget = [row for row in non_cooling
                     if row["day_tasks_left"] is None or row["day_tasks_left"] > 0]
    if not within_budget:
        return "budget"
    return "executor_capacity"


def admit(ranked_ids, tasks, snapshot, priorities, imminent=None, cfg=None):
    """Admit ranked tasks using only local copies of snapshot capacity."""
    cfg = cfg or {}
    scheduler_cfg = cfg.get("scheduler", cfg)
    reserve = scheduler_cfg.get("reserve_imminent", True)
    free = {executor_id: row["free"] for executor_id, row in snapshot.get("executors", {}).items()}
    if snapshot.get("fallback"):
        free["claude"] = snapshot.get("claude_workers_free", 0)

    imminent_sets = {}
    if reserve:
        for task_id in imminent or []:
            task = tasks.get(task_id)
            if task is not None:
                imminent_sets[task_id] = eligible_executors_from_snapshot(task, snapshot)

    admitted = []
    deferred = {}
    assignments = {}
    for task_id in ranked_ids:
        task = tasks.get(task_id)
        if task is None:
            deferred[task_id] = "no_eligible_executor"
            continue
        eligible = eligible_executors_from_snapshot(task, snapshot)
        available = [executor_id for executor_id in eligible if free.get(executor_id, 0) > 0]
        if not available:
            deferred[task_id] = _reason(task, snapshot, free)
            continue

        def choice_key(executor_id):
            weight = 0 if executor_id == "claude" else snapshot["executors"][executor_id]["weight"]
            return (-free[executor_id], weight, executor_id)

        available.sort(key=choice_key)
        chosen = None
        reservation = None
        for executor_id in available:
            blocker = next((imminent_id for imminent_id, imminent_eligible in imminent_sets.items()
                            if _priority(imminent_id, priorities) > _priority(task_id, priorities)
                            and imminent_eligible == [executor_id]
                            and free[executor_id] == 1), None)
            if blocker is None:
                chosen = executor_id
                break
            if reservation is None:
                reservation = blocker
        if chosen is None:
            deferred[task_id] = f"reserved_for_critical:{reservation}"
            continue
        admitted.append(task_id)
        assignments[task_id] = chosen
        free[chosen] -= 1

    return {"admit": admitted, "deferred": deferred, "assignments": assignments,
            "reasons": dict(deferred)}
