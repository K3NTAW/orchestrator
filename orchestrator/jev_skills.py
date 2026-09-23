"""Bounded Jev classification for only the ambiguous skill-routing bucket."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import STATE, attribution, jev, skills_registry


BOUNDARY_FIELDS = ("title", "spec", "scope", "task_class", "role", "skills")
jev.declare_boundary("skills", fields=BOUNDARY_FIELDS, max_chars=6000,
                     raw_source_allowed=False,
                     notes="Redacted task digest and Level 1 skill renderings only.")

QUESTION_NAMES = ("relevant", "reduces_rework", "duplicates", "worth_cost", "requires_specialist")
QUESTION_TEXT = {
    "relevant": "Is this skill relevant to completing the task?",
    "reduces_rework": "Would this skill materially reduce likely rework?",
    "duplicates": "Does this skill duplicate guidance already selected for the role?",
    "worth_cost": "Is loading this skill worth its context and execution cost?",
    "requires_specialist": "Does correct use of this skill require a specialist role?",
}


def _settings(cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    skills = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else cfg
    jev_cfg = cfg.get("jev") if isinstance(cfg.get("jev"), dict) else {}
    cache = jev_cfg.get("skills") if isinstance(jev_cfg.get("skills"), dict) else {}
    return {
        "max_batch": max(0, int(skills.get("jev_max_batch", 8))),
        "cache_ttl_s": max(0, cache.get("cache_ttl_s", 86400)),
        "cache_max_entries": max(0, int(cache.get("cache_max_entries", 500))),
    }


def _repo_head():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _scope(task):
    value = task.get("scope") or task.get("write_scope") or []
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else []


def _cache_key(task, records, skill_ids):
    content = {"title": task.get("title"), "spec": task.get("spec"), "scope": _scope(task)}
    content_hash = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    versions = sorted((skill_id, str(records.get(skill_id, {}).get("version", "unknown")))
                      for skill_id in skill_ids)
    raw = [str(task.get("id") or ""), content_hash, versions, _repo_head()]
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]


def _cache_path():
    return Path(STATE) / "runs" / "jev" / "skills_cache.json"


def _with_cache(fn):
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            return fn(path)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_cache(key, ttl):
    def read(path):
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        row = rows.get(key) if isinstance(rows, dict) else None
        age = time.time() - row.get("saved_at", 0) if isinstance(row, dict) else -1
        return row.get("result") if row is not None and 0 <= age <= ttl else None
    return _with_cache(read)


def _write_cache(key, result, ttl, maximum):
    if maximum <= 0:
        return
    def write(path):
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            rows = {}
        if not isinstance(rows, dict):
            rows = {}
        now = time.time()
        rows = {item: row for item, row in rows.items() if isinstance(row, dict)
                and 0 <= now - row.get("saved_at", 0) <= ttl}
        rows[key] = {"saved_at": now, "result": result}
        rows = dict(sorted(rows.items(), key=lambda item: item[1]["saved_at"])[-maximum:])
        fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=".skills_cache.")
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
    _with_cache(write)


def _probability(answer):
    value = float(answer["noul"] if isinstance(answer, dict) else answer)
    if not 0 <= value <= 1:
        raise ValueError("probability outside [0,1]")
    return value


def classify(task, role, ambiguous_ids, cfg=None, ask=None):
    """Classify a bounded ambiguous batch; every failure excludes extra skills."""
    ask = ask or jev.ask
    ids = list(dict.fromkeys(ambiguous_ids or []))
    empty = {"decisions": {}, "source": "none", "latency_ms": 0, "usage": None,
             "batch_size": 0}
    if not ids:
        return empty
    settings = _settings(cfg)
    sent, omitted = ids[:settings["max_batch"]], ids[settings["max_batch"]:]
    records = skills_registry.load().get("skills", {})
    key = _cache_key(task, records, sent)
    cached = _read_cache(key, settings["cache_ttl_s"]) if settings["cache_max_entries"] else None
    if isinstance(cached, dict):
        return {**cached, "source": "cache", "latency_ms": 0}
    decisions = {skill_id: {"status": "unavailable", "select": False} for skill_id in sent}
    decisions.update({skill_id: {"status": "not_sent", "select": False} for skill_id in omitted})
    if not sent:
        return {**empty, "decisions": decisions}
    started = time.monotonic()
    try:
        state = {
            "title": jev.redact(str(task.get("title") or "")),
            "spec": jev.redact(str(task.get("spec") or "")[:800]),
            "scope": [jev.redact(item) for item in _scope(task)],
            "task_class": attribution.task_class(task),
            "role": str(role),
            "skills": [{"id": skill_id,
                        "level1": jev.redact(skills_registry.render(skill_id, 1)[:400])}
                       for skill_id in sent],
        }
        questions = {f"{skill_id}:{name}": {"type": "noul", "instructions": text}
                     for skill_id in sent for name, text in QUESTION_TEXT.items()}
        result = ask(state, questions, site="skills", task=task.get("id"))
        if result is None:
            return {**empty, "decisions": decisions, "batch_size": len(sent),
                    "latency_ms": (time.monotonic() - started) * 1000}
        answers = result["answers"]
        for skill_id in sent:
            row = {f"{name}_p": _probability(answers[f"{skill_id}:{name}"])
                   for name in QUESTION_NAMES}
            row["select"] = (row["relevant_p"] >= .6 and row["worth_cost_p"] >= .5
                             and row["duplicates_p"] < .5)
            decisions[skill_id] = row
    except Exception:
        return {**empty, "decisions": decisions, "batch_size": len(sent),
                "latency_ms": (time.monotonic() - started) * 1000}
    output = {"decisions": decisions, "source": "jev",
              "latency_ms": (time.monotonic() - started) * 1000,
              "usage": result.get("usage"), "batch_size": len(sent)}
    _write_cache(key, output, settings["cache_ttl_s"], settings["cache_max_entries"])
    return output
