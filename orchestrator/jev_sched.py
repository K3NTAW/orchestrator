"""Bounded Jev shadow signals for ambiguous scheduler interference pairs."""

from functools import partial

import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from . import STATE, decision_log, jev
from .pool import config as pool_config

jev.declare_boundary('sched', fields=('pairs',),
                     max_chars=jev.DEFAULT_MAX_STATE_CHARS, raw_source_allowed=False,
                     notes='Pair a/b, titles, scope, spec_excerpts (400 each), reasons; default 8 pairs.')


PAIR_QUESTIONS = {
    "semantic_interference": {
        "type": "noul",
        "instructions": "Probability that these two tasks change the same behaviour or interface and would conflict if run concurrently",
    },
    "stale_assumption_risk": {
        "type": "noul",
        "instructions": "Probability that finishing one task first invalidates assumptions the other relies on",
    },
}

DEFAULTS = {
    "jev_mode": "shadow",
    "jev_max_pairs": 8,
    "jev_cache_ttl_s": 3600,
    "jev_cache_max_entries": 200,
}


def ambiguous_pairs(pairwise_rows):
    """Return only soft, non-dependency pairs, preserving deterministic row order."""
    result = []
    for row in pairwise_rows or ():
        if not isinstance(row, dict) or row.get("level") != "soft":
            continue
        reasons = row.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        if any("dependency:" in str(reason) for reason in reasons):
            continue
        if row.get("a") is not None and row.get("b") is not None:
            result.append(row)
    return result


def _cfg(cfg):
    if cfg is None:
        try:
            cfg = (pool_config().get("scheduler") or {})
        except (FileNotFoundError, OSError, ValueError, TypeError):
            cfg = {}
    elif isinstance(cfg, dict) and isinstance(cfg.get("scheduler"), dict):
        cfg = cfg["scheduler"]
    cfg = cfg if isinstance(cfg, dict) else {}
    values = {key: cfg.get(key, default) for key, default in DEFAULTS.items()}
    if values["jev_mode"] not in ("off", "shadow", "active"):
        values["jev_mode"] = "shadow"
    for key in ("jev_max_pairs", "jev_cache_max_entries"):
        value = values[key]
        values[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else DEFAULTS[key]
    ttl = values["jev_cache_ttl_s"]
    values["jev_cache_ttl_s"] = ttl if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl >= 0 else DEFAULTS["jev_cache_ttl_s"]
    return values


def _task(tasks, task_id):
    if isinstance(tasks, dict):
        value = tasks.get(task_id, {})
        return value if isinstance(value, dict) else {}
    for value in tasks or ():
        if isinstance(value, dict) and value.get("id") == task_id:
            return value
    return {}


def _scope(task):
    values = task.get("scope") or task.get("write_scope") or []
    return [str(value) for value in values] if isinstance(values, (list, tuple)) else []


def _key(rows, tasks, goal_head):
    pairs = []
    for row in rows:
        a, b = str(row["a"]), str(row["b"])
        pair_ids = sorted((a, b))
        pairs.append({"ids": pair_ids,
                      "scopes": [[item, _scope(_task(tasks, item))] for item in pair_ids]})
    payload = {"goal_head": goal_head, "pairs": sorted(pairs, key=lambda row: row["ids"])}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cache_paths(root):
    directory = Path(root) / "runs" / "jev"
    return directory / "sched_cache.json", directory / "sched_cache.lock"


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
            return row.get("signals") if row is not None and 0 <= age <= ttl else None
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _cache_write(root, key, signals, ttl, max_entries):
    if max_entries <= 0:
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
            rows = {cached_key: row for cached_key, row in rows.items()
                    if isinstance(row, dict) and 0 <= now - row.get("saved_at", 0) <= ttl}
            rows[key] = {"signals": signals, "saved_at": now}
            rows = dict(sorted(rows.items(), key=lambda item: item[1]["saved_at"])[-max_entries:])
            fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=".sched_cache.")
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(rows, out)
                os.replace(name, path)
            except Exception:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass
                raise
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _pair_state(row, tasks):
    a, b = str(row["a"]), str(row["b"])
    ta, tb = _task(tasks, a), _task(tasks, b)
    return {
        "a": a,
        "b": b,
        "titles": [jev.redact(str(ta.get("title") or "")), jev.redact(str(tb.get("title") or ""))],
        "scope": [_scope(ta), _scope(tb)],
        "spec_excerpts": [jev.redact(str(ta.get("spec") or "")[:400]),
                          jev.redact(str(tb.get("spec") or "")[:400])],
        "reasons": [jev.redact(str(value)) for value in (row.get("reasons") or [])],
    }


def _decode(result, rows):
    answers = result["answers"]
    signals = {}
    for row in rows:
        pair = f'{row["a"]}|{row["b"]}'
        values, confidences = {}, []
        for question in PAIR_QUESTIONS:
            answer = answers[f"{pair}:{question}"]
            value = float(answer["noul"])
            if not 0 <= value <= 1:
                raise ValueError("Jev probability outside [0,1]")
            confidence = answer.get("confidence")
            if confidence is not None:
                confidence = float(confidence)
                if not 0 <= confidence <= 1:
                    raise ValueError("Jev confidence outside [0,1]")
            values[question] = value
            confidences.append(confidence)
        values["confidence"] = None if None in confidences else sum(confidences) / len(confidences)
        signals[pair] = values
    return signals


def _copy_wave(wave_result):
    return list(wave_result.get("wave") or []), [dict(row) if isinstance(row, dict) else row
                                                  for row in (wave_result.get("deferred") or [])]


def annotate(wave_result, pairwise_rows, tasks, *, goal_head, cfg=None, root=STATE, ask=partial(jev.ask, site="sched")):
    """Annotate a deterministic wave with bounded Jev signals; fail open on every error."""
    settings = _cfg(cfg)
    mode = settings["jev_mode"]
    original_wave, original_deferred = _copy_wave(wave_result)
    result = {"mode": mode, "asked": 0, "cache": None, "signals": {}, "would_defer": [],
              "applied": [], "wave": original_wave, "deferred": original_deferred, "error": None}
    if mode == "off":
        return result

    rows = ambiguous_pairs(pairwise_rows)[:settings["jev_max_pairs"]]
    result["asked"] = len(rows)
    cache_key = _key(rows, tasks, goal_head) if rows else None
    if rows and settings["jev_cache_max_entries"]:
        result["signals"] = _cache_read(root, cache_key, settings["jev_cache_ttl_s"]) or {}
        if result["signals"]:
            result["cache"] = "hit"

    if rows and not result["signals"]:
        result["cache"] = "miss"
        questions = {f'{row["a"]}|{row["b"]}:{name}': question
                     for row in rows for name, question in PAIR_QUESTIONS.items()}
        state = {"pairs": [_pair_state(row, tasks) for row in rows]}
        try:
            answer = ask(state, questions)
            if answer is None:
                raise ValueError("Jev unavailable")
            result["signals"] = _decode(answer, rows)
            _cache_write(root, cache_key, result["signals"], settings["jev_cache_ttl_s"],
                         settings["jev_cache_max_entries"])
        except Exception as exc:
            result["signals"] = {}
            result["error"] = str(exc) or type(exc).__name__

    reason_for = {}
    if result["error"] is None:
        positions = {task_id: index for index, task_id in enumerate(original_wave)}
        for pair, signal in result["signals"].items():
            a, b = pair.split("|", 1)
            if signal["semantic_interference"] >= .7 and a in positions and b in positions:
                later = a if positions[a] > positions[b] else b
                if later not in result["would_defer"]:
                    result["would_defer"].append(later)
                    reason_for[later] = f"jev:{pair}"
        if mode == "active":
            result["applied"] = list(result["would_defer"])
            result["wave"] = [task_id for task_id in original_wave if task_id not in result["applied"]]
            result["deferred"] += [{"task": task_id, "reason": reason_for[task_id]}
                                   for task_id in result["applied"]]

    if result["asked"] > 0 or mode == "active":
        confidences = [signal.get("confidence") for signal in result["signals"].values()]
        confidence = None if not confidences or any(value is None for value in confidences) else sum(confidences) / len(confidences)
        first_id = str(rows[0]["a"]) if rows else (str(original_wave[0]) if original_wave else "unknown")
        first_task = _task(tasks, first_id)
        subject = first_task.get("goal_id") or first_task.get("parent") or first_id
        decision_log.record(
            kind="jev_sched", subject=subject,
            candidates=[f'{row["a"]}|{row["b"]}' for row in rows],
            hard_constraints=["dependency", "hard_conflicts", "capacity"],
            deterministic={"wave": original_wave, "deferred": original_deferred},
            jev=result["signals"], selected=result["applied"], rejected=result["would_defer"],
            reason=result["error"] or ("active Jev deferral" if result["applied"] else "shadow Jev signal"),
            confidence=confidence, n=result["asked"], mode=mode, root=root)
    return result
