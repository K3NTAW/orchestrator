"""Deterministic, side-effect-free interference checks for orchestrator tasks."""

from __future__ import annotations

from fnmatch import fnmatchcase
from itertools import chain
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


DEFAULT_RULES = {
    "hard": ("dependency", "same_file", "glob_covers", "same_glob"),
    "soft": ("dir_contains", "same_dir", "test_adjacent", "sibling_globs"),
    "soft_conflict_policy": "defer",
}


def _task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("id", ""))


def _scope(task: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({str(entry).replace("\\", "/") for entry in task.get("scope", [])}))


def _is_dir(entry: str) -> bool:
    return entry.endswith("/") or entry.endswith("**")


def _is_glob(entry: str) -> bool:
    return not _is_dir(entry) and ("*" in entry or "?" in entry)


def _is_literal(entry: str) -> bool:
    return not _is_dir(entry) and not _is_glob(entry)


def _dir_prefix(entry: str) -> str:
    return entry[:-2].rstrip("/") + "/" if entry.endswith("**") else entry


def _rules(rules: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_RULES)
    if rules:
        merged.update(rules)
    # Accept the more explicit spellings as aliases without expanding the API.
    if "hard_reasons" in merged:
        merged["hard"] = merged["hard_reasons"]
    if "soft_reasons" in merged:
        merged["soft"] = merged["soft_reasons"]
    return merged


def _reason_kind(reason: str) -> str:
    return reason.split(":", 1)[0]


def _task_map(tasks: Any) -> dict[str, Mapping[str, Any]]:
    if tasks is None:
        return {}
    if isinstance(tasks, Mapping):
        return {str(key): value for key, value in tasks.items()}
    return {_task_id(task): task for task in tasks}


def _depends_on(start: str, target: str, tasks: Mapping[str, Mapping[str, Any]]) -> bool:
    pending = list(tasks.get(start, {}).get("depends_on", []) or [])
    seen: set[str] = set()
    while pending:
        current = str(pending.pop())
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(tasks.get(current, {}).get("depends_on", []) or [])
    return False


def graph_signal(t1: Mapping[str, Any], t2: Mapping[str, Any], graph: Any) -> list[str]:
    """Return graph-derived reasons (reserved for a later implementation)."""
    return []


def classify(
    t1: Mapping[str, Any],
    t2: Mapping[str, Any],
    tasks: Any = None,
    graph: Any = None,
    rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the interference between two tasks."""
    if t1 is t2 or (_task_id(t1) and _task_id(t1) == _task_id(t2)):
        return {"level": "none", "reasons": [], "score": 0.0}

    first, second = sorted((t1, t2), key=lambda task: (_task_id(task), _scope(task)))
    first_scope, second_scope = _scope(first), _scope(second)
    reasons: set[str] = set()

    task_lookup = _task_map(tasks)
    for task in (first, second):
        task_lookup.setdefault(_task_id(task), task)
    first_id, second_id = _task_id(first), _task_id(second)
    if first_id and second_id:
        if _depends_on(first_id, second_id, task_lookup):
            reasons.add(f"dependency:{second_id}")
        if _depends_on(second_id, first_id, task_lookup):
            reasons.add(f"dependency:{first_id}")

    literals_a = [entry for entry in first_scope if _is_literal(entry)]
    literals_b = [entry for entry in second_scope if _is_literal(entry)]
    globs_a = [entry for entry in first_scope if _is_glob(entry)]
    globs_b = [entry for entry in second_scope if _is_glob(entry)]
    dirs_a = [entry for entry in first_scope if _is_dir(entry)]
    dirs_b = [entry for entry in second_scope if _is_dir(entry)]

    for path in sorted(set(literals_a) & set(literals_b)):
        reasons.add(f"same_file:{path}")
    for glob, path in chain(
        ((glob, path) for glob in globs_a for path in literals_b),
        ((glob, path) for glob in globs_b for path in literals_a),
    ):
        if fnmatchcase(path, glob):
            reasons.add(f"glob_covers:{glob}->{path}")
    for glob in sorted(set(globs_a) & set(globs_b)):
        reasons.add(f"same_glob:{glob}")

    for prefix, entries in chain(
        ((entry, second_scope) for entry in dirs_a),
        ((entry, first_scope) for entry in dirs_b),
    ):
        normalized = _dir_prefix(prefix)
        for entry in entries:
            if entry == prefix or entry.startswith(normalized):
                reasons.add(f"dir_contains:{prefix}->{entry}")
    for left in literals_a:
        for right in literals_b:
            left_dir = str(PurePosixPath(left).parent)
            if left_dir == str(PurePosixPath(right).parent):
                reasons.add(f"same_dir:{left_dir}")

    modules = [entry for entry in first_scope + second_scope
               if _is_literal(entry) and entry.startswith("orchestrator/") and entry.endswith(".py")]
    all_a, all_b = first_scope, second_scope
    for module in modules:
        name = PurePosixPath(module).stem
        test_path = f"tests/test_{name}.py"
        module_in_a = module in all_a
        other_scope = all_b if module_in_a else all_a
        if any(entry == test_path or (_is_glob(entry) and fnmatchcase(test_path, entry)) for entry in other_scope):
            reasons.add(f"test_adjacent:{name}")

    for left in globs_a:
        for right in globs_b:
            if left != right and PurePosixPath(left).parent == PurePosixPath(right).parent:
                reasons.add(f"sibling_globs:{PurePosixPath(left).parent}")

    if graph is not None:
        reasons.update(graph_signal(first, second, graph))

    configured = _rules(rules)
    hard = set(configured["hard"])
    soft = set(configured["soft"])
    ordered = sorted(reasons)
    if any(_reason_kind(reason) in hard for reason in ordered):
        return {"level": "hard", "reasons": ordered, "score": 1.0}
    soft_count = sum(_reason_kind(reason) in soft for reason in ordered)
    if soft_count:
        return {"level": "soft", "reasons": ordered, "score": min(0.9, 0.25 * soft_count)}
    return {"level": "none", "reasons": ordered, "score": 0.0}


def pairwise(tasks_list: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    tasks = list(tasks_list)
    result = []
    for index, left in enumerate(tasks):
        for right in tasks[index + 1:]:
            finding = classify(left, right, **kwargs)
            if finding["level"] != "none":
                result.append({"a": _task_id(left), "b": _task_id(right),
                               "level": finding["level"], "reasons": finding["reasons"]})
    return result


def select_wave(
    ready: Iterable[str],
    running: Iterable[str],
    tasks: Any,
    capacity: int,
    order: Iterable[str] | None = None,
    graph: Any = None,
    rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select ready tasks, postponing soft conflicts behind clean candidates."""
    ready_ids = list(dict.fromkeys(str(item) for item in ready))
    ready_set = set(ready_ids)
    requested = ready_ids if order is None else [str(item) for item in order]
    candidates = list(dict.fromkeys(item for item in requested if item in ready_set))
    candidates.extend(item for item in ready_ids if item not in candidates)
    lookup = _task_map(tasks)
    running_ids = [str(item) for item in running]
    configured = _rules(rules)
    limit = max(0, int(capacity))
    wave: list[str] = []
    deferred: list[dict[str, str]] = []
    soft_queue: list[tuple[str, str]] = []

    def conflict(candidate: str, others: Iterable[str]) -> tuple[str | None, str | None]:
        for other in others:
            finding = classify(lookup.get(candidate, {"id": candidate}),
                               lookup.get(other, {"id": other}), lookup, graph, configured)
            if finding["level"] == "hard":
                return "hard", other
            if finding["level"] == "soft":
                return "soft", other
        return None, None

    for candidate in candidates:
        level, other = conflict(candidate, running_ids + wave)
        if level == "hard":
            deferred.append({"task": candidate, "reason": f"hard:{other}"})
        elif level == "soft" and configured["soft_conflict_policy"] == "defer":
            soft_queue.append((candidate, str(other)))
        elif len(wave) < limit:
            wave.append(candidate)
        else:
            deferred.append({"task": candidate, "reason": "capacity"})

    for candidate, soft_other in soft_queue:
        level, other = conflict(candidate, running_ids + wave)
        if level == "hard":
            deferred.append({"task": candidate, "reason": f"hard:{other}"})
        elif len(wave) < limit:
            wave.append(candidate)
        else:
            deferred.append({"task": candidate, "reason": f"soft:{soft_other}"})
    return {"wave": wave, "deferred": deferred}
