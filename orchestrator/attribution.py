"""Shared run attribution, independent of scorecard aggregation."""
import fnmatch
import json
from . import bus


_SECURITY_REASONS = {"security_paths", "diff_unavailable", "security_paths_empty"}


def review_facts(review_task):
    """Return stable telemetry for new and historical review tasks, tolerating partial old rows."""
    try:
        task = review_task or {}
        constraints = task.get("constraints") or {}
        pipeline = task.get("pipeline") or {}
        result = task.get("result") or {}
        comments = result.get("comments") if isinstance(result.get("comments"), list) else []
        severities = {"high": 0, "med": 0, "low": 0, "other": 0}
        aliases = {"critical": "high", "high": "high", "medium": "med", "med": "med",
                   "low": "low", "info": "low", "nit": "low"}
        for comment in comments:
            raw = comment.get("severity") if isinstance(comment, dict) else None
            severities[aliases.get(str(raw).lower(), "other")] += 1

        packet_version = result.get("packet_version")
        if packet_version is None:
            for path in sorted(bus.RUNS.glob("*"), reverse=True):
                try:
                    row = json.loads(path.read_text())
                except (OSError, ValueError, TypeError, IsADirectoryError):
                    continue
                if row.get("task") == task.get("id"):
                    packet_version = (row.get("packet_meta") or {}).get("version")
                    if packet_version is not None:
                        break

        inputs = task.get("inputs") or []
        reviewed_id = inputs[0] if inputs and isinstance(inputs[0], str) else None
        siblings = []
        if reviewed_id is not None:
            try:
                siblings = [row for row in bus.read(role="review")
                            if (row.get("inputs") or [None])[0] == reviewed_id]
            except Exception:
                siblings = []
        siblings.sort(key=lambda row: (row.get("created_at") or "", row.get("id") or ""))
        pass_index = next((i for i, row in enumerate(siblings, 1) if row.get("id") == task.get("id")), None)
        reviews_expected = None
        if reviewed_id is not None:
            try:
                reviews_expected = (bus.get(reviewed_id).get("pipeline") or {}).get("reviews_expected")
            except Exception:
                pass
        return {
            "verdict": result.get("verdict"),
            "findings_count": len(comments),
            "findings_by_severity": severities,
            "reviewer_role": constraints.get("reviewer_role") or "general",
            "checklist_used": pipeline.get("review_reason") in _SECURITY_REASONS or (task.get("complexity") or 0) >= 7,
            "reviewed_sha": constraints.get("reviewed_sha") or pipeline.get("reviewed_sha"),
            "packet_version": packet_version,
            "review_pass_index": pass_index,
            "reviews_expected": reviews_expected,
        }
    except Exception:
        return {"verdict": None, "findings_count": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0, "other": 0},
                "reviewer_role": "general", "checklist_used": False, "reviewed_sha": None,
                "packet_version": None, "review_pass_index": None, "reviews_expected": None}


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
