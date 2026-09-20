"""Shared run attribution, independent of scorecard aggregation."""
import fnmatch
from . import bus


def bucket_of(role, task=None):
    constraints = (task or {}).get("constraints") or {}
    if role in ("planner_decision", "planner"):
        return "planner"
    if role == "execute":
        return "fix_round" if constraints.get("fix_round_for") or constraints.get("auto_round") else "execute"
    if role in ("scout", "triage"):
        return "scout"
    if role == "jev_route":
        return "jev"
    return role if role in ("spec_review", "review", "challenge", "jev", "memory") else "other"


def lineage(task):
    root, index = task.get("id"), 0
    seen = {root}
    while target := (task.get("constraints") or {}).get("fix_round_for"):
        if target in seen:
            break
        seen.add(target)
        root, index = target, index + 1
        try:
            task = bus.get(target)
        except (KeyError, OSError, ValueError):
            break
    return {"root": root, "round_index": index}


def model_of(executor_id, tier, pool_cfg):
    for row in pool_cfg.get("executors", []):
        if row.get("id") == executor_id:
            return row.get("model")
    return pool_cfg.get("models", {}).get(tier)


def band(complexity):
    if complexity is None:
        return None
    if complexity <= 3:
        return "1-3"
    if complexity <= 6:
        return "4-6"
    return "7-10"


def task_class(task):
    """Return the routing class explicitly requested by a spec, or infer its durable fallback class."""
    constraints = task.get("constraints") or {}
    explicit = constraints.get("task_class") if isinstance(constraints, dict) else None
    if explicit:
        return explicit
    try:
        security_paths = bus.pool_config().get("review", {}).get("security_paths", [])
    except Exception:
        security_paths = []
    scope = task.get("scope") or []
    if any(fnmatch.fnmatch(path, pattern) for path in scope for pattern in security_paths):
        return "security"
    if (task.get("complexity") or 0) >= 7:
        return "architectural"
    title = (task.get("title") or "").lower()
    if title.startswith("fix") or (isinstance(constraints, dict) and constraints.get("fix_round_for")):
        return "debugging"
    if (task.get("complexity") or 0) <= 3:
        return "mechanical"
    return "unfamiliar"
