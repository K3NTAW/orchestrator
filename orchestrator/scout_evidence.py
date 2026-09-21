"""Evidence selection and telemetry helpers for read-only scout work.

This module deliberately depends only on the bus, git evidence, and the memory
recall script.  It is safe to use before a worker (or daemon) is started.
"""
import datetime as _datetime
import importlib.util
from pathlib import Path

from . import STATE, bus, gitutil


OBJECTIVES = (
    "implementation-map", "dependency-map", "architecture", "test-surface",
    "failure-history", "security-context", "migration-impact", "API-consumers",
)

_RECALL = None


def normalize_objective(text):
    """Return the canonical objective name, accepting case and dash variants."""
    if not isinstance(text, str):
        return None
    key = "-".join(text.strip().lower().replace("_", " ").replace("-", " ").split())
    return next((item for item in OBJECTIVES if item.lower() == key), None)


def memory_recall(question, **kwargs):
    """Lazily load the shared recall skill, mirroring ``spawn.memory_recall``."""
    global _RECALL
    if _RECALL is None:
        path = Path(__file__).resolve().parents[1] / ".claude/skills/memory/scripts/recall.py"
        spec = importlib.util.spec_from_file_location("orchestrator_scout_recall", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RECALL = module.recall
    return _RECALL(question, **kwargs)


def _age_days(value):
    try:
        return max(0, (_datetime.date.today() - _datetime.date.fromisoformat(str(value)[:10])).days)
    except (TypeError, ValueError):
        return None


def _project_root(root):
    root = Path(root)
    return root.parent if root.name == ".orchestrator" else root


def _bus_fresh(hit, *, root, head_sha, git):
    """A bus hit is reusable only when its completed task's scoped files stayed put."""
    try:
        task = bus.get(hit.get("id"))
    except (KeyError, OSError, TypeError):
        return False, "task result unavailable"
    if not task.get("result"):
        return False, "task result unavailable"
    packet = task.get("packet_meta") or {}
    base = packet.get("base")
    if not base:
        return True, "packet base unknown"
    try:
        moved = gitutil.moved_paths(base, head_sha or "HEAD", cwd=_project_root(root), git=git)
    except Exception:
        return False, "scope movement unknown"
    scope = set(task.get("scope") or [])
    changed = sorted(scope.intersection(moved))
    return not changed, "scope moved: " + ", ".join(changed) if changed else "scope unchanged"


def reuse_check(question, *, objective=None, root=STATE, max_age_days=14, min_hits=2,
                head_sha=None, git=None):
    """Report whether recall has enough current evidence to avoid a new scout."""
    canonical = normalize_objective(objective) if objective is not None else None
    project_root = _project_root(root)
    try:
        recalled = memory_recall(question, root=project_root, layers=("notes", "bus", "graph"))
        raw_hits = recalled.get("hits", []) if isinstance(recalled, dict) else []
    except Exception:
        raw_hits = []
    hits = []
    for raw in raw_hits:
        if not isinstance(raw, dict):
            continue
        age = _age_days(raw.get("date"))
        layer = raw.get("layer") or ""
        fresh = age is not None and age <= max_age_days
        reason = "within age limit" if fresh else "missing or stale date"
        if layer == "bus":
            current, bus_reason = _bus_fresh(raw, root=project_root, head_sha=head_sha, git=git)
            fresh = fresh and current
            reason = bus_reason if current else bus_reason
        hits.append({"id": raw.get("id"), "title": raw.get("title", ""), "date": raw.get("date"),
                     "layer": layer, "age_days": age, "fresh": fresh, "reason": reason})
    current = [hit for hit in hits if hit["fresh"]]
    sufficient = len(current) >= min_hits
    if sufficient:
        reason = "fresh evidence: " + ", ".join(str(hit["id"] or hit["title"]) for hit in current[:min_hits])
    elif current:
        reason = f"only {len(current)} fresh evidence hit(s)"
    else:
        reason = "no fresh reusable evidence"
    return {"sufficient": sufficient, "hits": hits, "reason": reason, "objective": canonical}


def _source(item):
    value = item.get("source")
    if isinstance(value, str) and value:
        return value
    for key in ("evidence", "provenance"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            found = next((part for part in value if isinstance(part, str) and part), None)
            if found:
                return found
    return None


def normalize_findings(result):
    """Convert either scout JSON shape to entries consumers can compare safely."""
    if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
        return []
    open_questions = result.get("open_questions", [])
    unresolved_default = bool(open_questions) if isinstance(open_questions, list) else False
    entries = []
    for item in result["findings"]:
        if not isinstance(item, dict):
            continue
        source = _source(item)
        finding = item.get("finding", item.get("claim"))
        if not source or not isinstance(finding, str) or not finding:
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        relevance = item.get("relevance", 1.0)
        unresolved = bool(item.get("unresolved", unresolved_default))
        entries.append({"finding": finding, "source": source, "confidence": confidence,
                        "relevance": relevance, "unresolved": unresolved,
                        "needs_challenge": confidence < 0.7})
    return entries


def _path(source):
    if not isinstance(source, str):
        return ""
    # Source locations conventionally end in :line (and optionally :column).
    bits = source.rsplit(":", 2)
    while len(bits) > 1 and bits[-1].isdigit():
        bits.pop()
    return ":".join(bits)


def _as_findings(value):
    return normalize_findings(value) if isinstance(value, dict) else normalize_findings({"findings": value})


def overlap(findings_a, findings_b):
    a = {_path(item["source"]) for item in _as_findings(findings_a) if _path(item["source"])}
    b = {_path(item["source"]) for item in _as_findings(findings_b) if _path(item["source"])}
    shared = a & b
    return {"jaccard_sources": len(shared) / len(a | b) if a | b else 0.0,
            "duplicate_count": len(shared)}


def orthogonal(objectives):
    seen, duplicates = set(), []
    for objective in objectives or []:
        canonical = normalize_objective(objective) or objective
        if canonical in seen and canonical not in duplicates:
            duplicates.append(canonical)
        seen.add(canonical)
    return {"ok": not duplicates, "duplicates": duplicates}


def record_decision(*, question, objective, considered, launched, skipped_reason, hits, task_id=None):
    return bus.log_run(task=task_id, role="scout_decision", outcome="recorded", question=question,
                       objective=normalize_objective(objective) or objective, considered=considered,
                       launched=launched, skipped_reason=skipped_reason, n_hits=len(hits or []))


def record_use(task_id, used_by):
    return bus.log_run(task=task_id, role="scout_use", outcome="recorded", used_by=used_by)
