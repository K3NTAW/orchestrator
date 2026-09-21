"""Fail-open Jev classification and bounded executor-routing adjustment.

The hypothetical policy is intentionally small: strong reasoning signals choose the
highest-success candidate; a very simple, low-risk task chooses the cheapest; all
other cases retain the baseline. Active routing defaults to ``max_adjustment=0.25``
and ``evidence_floor_n=10``; it can never introduce an ineligible executor.
"""
import fcntl, hashlib, json, os, tempfile, time
from pathlib import Path

from . import STATE, bus, jev, scorecard

QUESTIONS = {
    "localized_simple": {"type": "noul", "instructions": "Is this a localized, mechanically simple change?"},
    "substantial_reasoning": {"type": "noul", "instructions": "Does this require substantial reasoning?"},
    "large_context": {"type": "noul", "instructions": "Does this require a large context window?"},
    "debugging_reproduction": {"type": "noul", "instructions": "Does this require debugging or reproduction?"},
    "architectural": {"type": "noul", "instructions": "Is this an architectural change?"},
    "elevated_risk": {"type": "noul", "instructions": "Does this have elevated implementation risk?"},
    "stronger_executor_helps": {"type": "noul", "instructions": "Would a stronger executor materially help?"},
}


def _routing_cfg(pool):
    return (pool.cfg.get("jev") or {}).get("routing") or {}


def _key(task):
    payload = {k: task.get(k) for k in ("spec", "acceptance", "scope", "complexity")}
    payload["task_class"] = scorecard.task_class(task)
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cache_paths(root):
    directory = Path(root) / "runs" / "jev"
    return directory / "route_cache.json", directory / "route_cache.lock"


def _cache_read(root, key, ttl):
    path, lock = _cache_paths(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            try:
                rows = json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return None
            row = rows.get(key) if isinstance(rows, dict) else None
            age = time.time() - row.get("saved_at", 0) if isinstance(row, dict) else -1
            if row is None or not 0 <= age <= ttl:
                return None
            return {**row, "key": key, "cache": "hit"}
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _cache_write(root, key, row, ttl, max_entries):
    max_entries = max(0, max_entries)
    if max_entries == 0:
        return
    path, lock = _cache_paths(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            try:
                rows = json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                rows = {}
            if not isinstance(rows, dict):
                rows = {}
            now = time.time()
            rows = {cached_key: cached for cached_key, cached in rows.items()
                    if isinstance(cached, dict) and 0 <= now - cached.get("saved_at", 0) <= ttl}
            rows[key] = {"signals": row["signals"], "confidence": row["confidence"],
                         "saved_at": now, "task": row["task"]}
            rows = dict(sorted(rows.items(), key=lambda item: item[1]["saved_at"])[-max_entries:])
            fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=".route_cache.")
            with os.fdopen(fd, "w") as out:
                json.dump(rows, out)
            os.replace(name, path)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _memory_titles(task):
    values = task.get("memory_titles") or task.get("memories") or []
    titles = []
    for value in values[:10]:
        title = value.get("title") if isinstance(value, dict) else value
        if isinstance(title, str):
            titles.append(jev.redact(title))
    return titles


def classify(task, pool, eligible, root=STATE):
    """Return a cached/fresh classification, or None on every skip and failure path."""
    routing = _routing_cfg(pool)
    if routing.get("mode", "off") == "off" or len(eligible) < 2:
        return None
    cfg = jev._cfg()
    if not cfg["enabled"] or jev._day_tokens_used() >= cfg["daily_budget_tokens"]:
        return None
    key = _key(task)
    max_entries = max(0, routing.get("cache_max_entries", 500))
    if max_entries:
        cached = _cache_read(root, key, routing.get("cache_ttl_s", 86400))
        if cached is not None:
            return cached
    state = {
        "spec": jev.redact((task.get("spec") or "")[:1500]),
        "acceptance": [jev.redact(str(v)) for v in task.get("acceptance") or []],
        "scope": [jev.redact(str(v)) for v in task.get("scope") or []],
        "complexity": task.get("complexity"), "task_class": scorecard.task_class(task),
        "memory_titles": _memory_titles(task),
    }
    started = time.monotonic()
    try:
        result = jev.ask(state, QUESTIONS, model=cfg["model"], timeout_s=cfg["timeout_s"], task=task.get("id"))
        answers = result["answers"]
        signals = {key: float(answers[key]["noul"]) for key in QUESTIONS}
        if any(not 0 <= value <= 1 for value in signals.values()):
            return None
        confidences = [answers[key].get("confidence") for key in QUESTIONS]
        confidence = None if any(v is None for v in confidences) else sum(confidences) / len(confidences)
        row = {"signals": signals, "confidence": confidence, "task": task.get("id"),
               "latency_ms": (time.monotonic() - started) * 1000,
               "usage": result.get("usage"), "cache": "miss", "key": key}
    except Exception:
        return None
    _cache_write(root, key, row, routing.get("cache_ttl_s", 86400), max_entries)
    return row


def evidence_for(eligible, task, root=STATE):
    task_class = scorecard.task_class(task)
    try:
        economics = scorecard.executor_economics(root=root)
    except Exception:
        economics = {}
    return {ex.id: {"class_success": scorecard.class_success(ex.id, task_class, root=root),
                    "n": scorecard.class_sample_size(ex.id, task_class, root=root),
                    "expected_cost": scorecard.expected_cost(ex.id, task_class, root=root),
                    "economics": economics.get(ex.id)} for ex in eligible}


def _branch_and_favoured(signals, evidence, ids):
    reasoning = max(signals[k] for k in
                    ("substantial_reasoning", "architectural", "stronger_executor_helps"))
    if reasoning >= .6:
        measured = [(evidence.get(eid, {}).get("class_success"), eid) for eid in ids]
        measured = [(value, eid) for value, eid in measured if value is not None]
        return (reasoning, max(measured, default=(None, None), key=lambda row: (row[0], row[1]))[1])
    if signals["localized_simple"] >= .7 and signals["elevated_risk"] < .3:
        measured = [(evidence.get(eid, {}).get("expected_cost"), eid) for eid in ids]
        measured = [(value, eid) for value, eid in measured if value is not None]
        return (signals["localized_simple"],
                min(measured, default=(None, None), key=lambda row: (row[0], row[1]))[1])
    return None, None


def adjustment(signals, evidence, eligible_ids, cfg):
    """Return one bounded, sample-shrunk suitability boost, or neutral multipliers."""
    ids = list(eligible_ids)
    neutral = {eid: 1.0 for eid in ids}
    if not ids or not isinstance(signals, dict) or not isinstance(evidence, dict):
        return neutral
    values = [signals.get(key) for key in QUESTIONS]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
           for value in values):
        return neutral
    signal, favoured = _branch_and_favoured(signals, evidence, ids)
    if favoured is None:
        return neutral
    row = evidence.get(favoured, {})
    n = row.get("n") if isinstance(row, dict) else None
    if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
        return neutral
    confidence = cfg.get("confidence")
    if confidence is None:
        confidence = .5
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return neutral
    maximum = cfg.get("max_adjustment", .25)
    floor = cfg.get("evidence_floor_n", 10)
    if (isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum < 0 or
            isinstance(floor, bool) or not isinstance(floor, (int, float)) or floor <= 0):
        return neutral
    magnitude = maximum * signal * confidence * floor / (n + floor)
    neutral[favoured] = min(1 + maximum, max(1 - maximum, 1 + magnitude))
    return neutral


def hypothetical(eligible, signals, evidence, baseline=None):
    """Apply the documented shadow heuristic, always selecting from ``eligible``."""
    if not eligible:
        return None
    ids = [ex.id for ex in eligible]
    fallback = baseline if baseline in ids else ids[0]
    complete = {key: signals.get(key, 0) for key in QUESTIONS}
    _, favoured = _branch_and_favoured(complete, evidence, ids)
    return favoured or fallback


def active_scores(task, pool, eligible, classification, evidence, base_scores=None):
    """Apply Jev suitability only in active mode, failing open to baseline scores."""
    ids = [ex.id for ex in eligible]
    baseline = dict(base_scores) if base_scores is not None else {eid: 1.0 for eid in ids}
    if _routing_cfg(pool).get("mode", "off") != "active" or not ids or classification is None:
        return baseline
    signals = classification.get("signals") if isinstance(classification, dict) else None
    cfg = {**_routing_cfg(pool), "confidence": classification.get("confidence")}
    multipliers = adjustment(signals, evidence, ids, cfg)
    return {eid: baseline.get(eid, 1.0) * multipliers[eid] for eid in ids}


def shadow_context(task, pool):
    mode = _routing_cfg(pool).get("mode", "off")
    if mode not in ("shadow", "active"):
        return None
    if mode == "active" and pool.notification_transition("jev_routing_active", True):
        from .daemon import notify
        notify("jev routing active requested; active ranking lands in P5")
    eligible = pool.eligible_executors("execute", task["complexity"], task)
    classification = classify(task, pool, eligible)
    evidence = evidence_for(eligible, task)
    signals = (classification or {}).get("signals") or {}
    cfg = {**_routing_cfg(pool), "confidence": (classification or {}).get("confidence")}
    return {"mode": mode, "eligible": eligible, "classification": classification,
            "evidence": evidence, "adjustment": adjustment(signals, evidence,
                                                             [ex.id for ex in eligible], cfg),
            "active": mode == "active"}


def record_shadow(task_id, context):
    task = bus.get(task_id)
    baseline = task.get("executor") or task.get("tier")
    classification = context["classification"]
    signals = (classification or {}).get("signals") or {}
    hyp = hypothetical(context["eligible"], signals, context["evidence"], baseline)
    key = (classification or {}).get("key") or _key(task)
    pipeline = dict(task.get("pipeline") or {})
    route_adjustment = context.get("adjustment", {ex.id: 1.0 for ex in context["eligible"]})
    active = context.get("active", context["mode"] == "active")
    pipeline["jev_route"] = {"baseline": baseline, "hypothetical": hyp, "key": key,
                             "adjustment": route_adjustment, "mode": context["mode"]}
    bus.update(task_id, pipeline=pipeline)
    bus.log_run(role="jev_route", task=task_id, mode=context["mode"],
                eligible=[ex.id for ex in context["eligible"]], baseline=baseline, hypothetical=hyp,
                agrees=baseline == hyp, signals=signals, confidence=(classification or {}).get("confidence"),
                evidence=context["evidence"], latency_ms=(classification or {}).get("latency_ms"),
                adjustment=route_adjustment, active=active,
                usage=(classification or {}).get("usage"), cache=(classification or {}).get("cache"),
                error=None if classification else "classification_failed")
