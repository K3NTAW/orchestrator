"""Deterministic critical-path priority for ready execute tasks."""

COLD_START_S = {"1-3": 600, "4-6": 1200, "7-10": 2400}


def _band(complexity):
    complexity = int(complexity)
    if complexity <= 3:
        return "1-3"
    if complexity <= 6:
        return "4-6"
    return "7-10"


def est_duration_s(task, durations=None):
    """Return a task-specific, band-specific, or cold-start duration."""
    durations = durations or {}
    task_id = task["id"]
    band = _band(task.get("complexity", 1))
    if task_id in durations:
        return durations[task_id]
    if band in durations:
        return durations[band]
    return COLD_START_S[band]


def _unresolved(task):
    return not task.get("merged_into") and task.get("status") != "failed"


def _children(tasks):
    children = {task_id: [] for task_id in tasks}
    for child_id, task in tasks.items():
        if not _unresolved(task):
            continue
        for parent_id in task.get("depends_on") or []:
            if parent_id in children:
                children[parent_id].append(child_id)
    for child_ids in children.values():
        child_ids.sort()
    return children


def _path_metrics(task_id, tasks, durations):
    children = _children(tasks)
    memo = {}
    visiting = set()

    def walk(current):
        if current in memo:
            return memo[current]
        visiting.add(current)
        best_depth = 0
        best_tail_s = 0
        for child_id in children.get(current, ()):
            if child_id in visiting:  # A cycle's back edge contributes no path.
                continue
            child_depth, child_path_s = walk(child_id)
            best_depth = max(best_depth, 1 + child_depth)
            best_tail_s = max(best_tail_s, child_path_s)
        visiting.remove(current)
        result = (best_depth, est_duration_s(tasks[current], durations) + best_tail_s)
        memo[current] = result
        return result

    return walk(task_id)


def _blocked_descendants(task_id, tasks):
    children = _children(tasks)
    seen = {task_id}
    pending = list(children.get(task_id, ()))
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(children.get(current, ()))
    return len(seen) - 1


def critical_path_s(task_id, tasks, durations=None):
    """Return the longest unresolved duration path beginning at task_id."""
    return _path_metrics(task_id, tasks, durations)[1]


def explain(task_id, tasks, durations=None):
    """Explain the priority inputs for one task."""
    downstream_depth, path_s = _path_metrics(task_id, tasks, durations)
    estimate = est_duration_s(tasks[task_id], durations)
    return {
        "downstream_depth": downstream_depth,
        "blocked_descendants": _blocked_descendants(task_id, tasks),
        "est_duration_s": estimate,
        "critical_path_s": path_s,
        "priority": path_s,
    }


def rank(ready_ids, tasks, durations=None):
    """Order known ready ids by critical path; retain unknown ids at the end."""
    known = [task_id for task_id in ready_ids if task_id in tasks]
    unknown = [task_id for task_id in ready_ids if task_id not in tasks]
    explanations = {task_id: explain(task_id, tasks, durations) for task_id in known}
    known.sort(key=lambda task_id: (-explanations[task_id]["priority"],
                                    -explanations[task_id]["blocked_descendants"], task_id))
    return known + unknown
