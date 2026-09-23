"""Outcome scorecard for tiered-memory retrieval decisions."""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import ROOT, decision_log, gitutil, memory_store


def _state(root):
    root = Path(root)
    return root / ".orchestrator" if (root / ".orchestrator").is_dir() else root


def _tasks(state):
    rows = {}
    for path in sorted((state / "tasks").glob("*.json")) if (state / "tasks").is_dir() else ():
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows[row.get("id", path.stem)] = row
    return rows


def _candidate_id(value):
    return str(value.get("id", "")) if isinstance(value, dict) else str(value)


def _presented(row):
    if row.get("mode") == "active":
        values = row.get("selected") or []
    else:
        values = (row.get("extra") or {}).get("legacy_ids") or []
    if isinstance(values, (str, dict)):
        values = [values]
    return [_candidate_id(value) for value in values if _candidate_id(value)]


def _paths(task):
    worktree = task.get("worktree")
    if worktree and Path(worktree).is_dir():
        changed = gitutil.changed_paths(task)
        if changed is not None:
            return list(changed), "worktree"
    if "changed_files" in task and task.get("changed_files") is not None:
        return list(task.get("changed_files") or []), "changed_files"
    return list(task.get("scope") or task.get("write_scope") or []), "scope"


def _record(record_id, candidates, repo_root):
    try:
        found = memory_store.get(record_id, repo_root)
    except Exception:
        found = None
    if found:
        return found
    for candidate in candidates:
        if _candidate_id(candidate) == record_id and isinstance(candidate, dict):
            return candidate
    return {"id": record_id, "title": record_id, "files": [], "components": []}


def _used(record, paths, summary):
    normalized = [str(path).lower().strip("/") for path in paths]
    for filename in record.get("files") or []:
        name = str(filename).lower().strip("/")
        if any(path == name or path.endswith("/" + name) or name.endswith("/" + path)
               for path in normalized if path):
            return True
    path_parts = {part for path in normalized for part in Path(path).parts}
    path_stems = {Path(path).stem for path in normalized}
    if any(str(component).lower() in path_parts | path_stems
           for component in record.get("components") or []):
        return True
    haystack = str(summary or "").lower()
    if record.get("id") and str(record["id"]).lower() in haystack:
        return True
    stop = {"and", "for", "from", "into", "the", "this", "that", "with"}
    words = {word for word in re.findall(r"[a-z0-9_]+", str(record.get("title", "")).lower())
             if len(word) >= 3 and word not in stop}
    return bool(words and words.intersection(re.findall(r"[a-z0-9_]+", haystack)))


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _timestamp(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0


def build(root=None, days=7, now=None):
    """Build per-task rows and mode aggregates from retrieval evidence."""
    state = _state(root or (ROOT / ".orchestrator"))
    repo_root = state.parent
    cutoff = (time.time() if now is None else float(now)) - max(0, int(days)) * 86400
    decisions = [row for row in decision_log.read_all(root=state, since_ts=cutoff)
                 if row.get("kind") == "retrieval"]
    decisions.sort(key=lambda row: (row.get("ts", 0), row.get("subject", "")))
    tasks = _tasks(state)
    fix_counts = defaultdict(int)
    for task in tasks.values():
        target = (task.get("constraints") or {}).get("fix_round_for")
        if target:
            fix_counts[target] += 1

    seen_by_lineage = defaultdict(set)
    grouped = {}
    for decision in decisions:
        task_id = str(decision.get("subject", ""))
        mode = decision.get("mode") or "unknown"
        task = tasks.get(task_id, {})
        lineage = task.get("parent") or (task.get("constraints") or {}).get("fix_round_for") or task_id
        key = (task_id, mode)
        item = grouped.setdefault(key, {"task": task_id, "mode": mode, "candidates": [],
                                        "presented": [], "tokens": 0, "repeated": 0,
                                        "ts": decision.get("ts", 0)})
        item["candidates"].extend(decision.get("candidates") or [])
        presented = _presented(decision)
        item["presented"].extend(presented)
        token_key = "tokens_tiered" if mode == "active" else "tokens_legacy"
        item["tokens"] += int((decision.get("extra") or {}).get(token_key) or 0)
        for record_id in presented:
            marker = (lineage, record_id)
            if marker in seen_by_lineage:
                item["repeated"] += 1
            seen_by_lineage[marker].add(task_id)

    rows = []
    for (task_id, mode), item in sorted(grouped.items()):
        task = tasks.get(task_id, {})
        paths, source = _paths(task)
        summary = (task.get("result") or {}).get("summary", "") if isinstance(task.get("result"), dict) else ""
        presented = list(dict.fromkeys(item["presented"]))
        candidate_ids = list(dict.fromkeys(_candidate_id(value) for value in item["candidates"] if _candidate_id(value)))
        used = [record_id for record_id in presented
                if _used(_record(record_id, item["candidates"], repo_root), paths, summary)]
        fixes = fix_counts.get(task_id, 0)
        merged = bool(task.get("merged_into") or task.get("merged_at") or task.get("sha"))
        rows.append({"task": task_id, "mode": mode, "records_retrieved": len(candidate_ids),
                     "records_presented": len(presented), "memory_tokens": item["tokens"],
                     "used": len(used), "used_ids": used, "downstream_use": _rate(len(used), len(presented)),
                     "first_pass": bool(merged and not fixes), "fix_rounds": fixes,
                     "repeated_retrieval": item["repeated"], "irrelevant": len(presented) - len(used),
                     "changed_paths": paths, "changed_path_source": source})

    by_mode = {}
    for mode in sorted({row["mode"] for row in rows}):
        selected = [row for row in rows if row["mode"] == mode]
        presented = sum(row["records_presented"] for row in selected)
        used = sum(row["used"] for row in selected)
        tokens = sum(row["memory_tokens"] for row in selected)
        by_mode[mode] = {"mode": mode, "tasks": len(selected), "records_retrieved": sum(r["records_retrieved"] for r in selected),
                         "records_presented": presented, "used": used, "irrelevant": sum(r["irrelevant"] for r in selected),
                         "repeated_retrieval": sum(r["repeated_retrieval"] for r in selected),
                         "memory_tokens": tokens, "precision": _rate(used, presented),
                         "utility_per_token": round(used / tokens * 1000, 4) if tokens else None,
                         "first_pass_rate": _rate(sum(r["first_pass"] for r in selected), len(selected))}

    memory_tasks = {row["task"] for row in rows if row["records_presented"]}
    retrieval_tasks = {row["task"] for row in rows}
    eligible = [task for task in tasks.values()
                if (task.get("id") in retrieval_tasks
                    or _timestamp(task.get("merged_at") or task.get("created_at")) >= cutoff)
                and (task.get("merged_into") or task.get("merged_at") or task.get("sha"))]
    with_memory = [task for task in eligible if task.get("id") in memory_tasks]
    without_memory = [task for task in eligible if task.get("id") not in memory_tasks]
    def first_pass_rate(values):
        return _rate(sum(not fix_counts.get(task.get("id"), 0) for task in values), len(values))
    return {"days": int(days), "rows": rows, "by_mode": by_mode,
            "first_pass": {"with_memory": {"tasks": len(with_memory), "rate": first_pass_rate(with_memory)},
                           "without_memory": {"tasks": len(without_memory), "rate": first_pass_rate(without_memory)}}}


report = build


def format_report(card):
    lines = ["mode\ttasks\tprecision\tutility_per_1k_tokens\tfirst_pass"]
    for mode, row in sorted(card["by_mode"].items()):
        lines.append(f"{mode}\t{row['tasks']}\t{row['precision']}\t{row['utility_per_token']}\t{row['first_pass_rate']}")
    first = card["first_pass"]
    lines.append(f"first_pass_with_memory={first['with_memory']['rate']} without_memory={first['without_memory']['rate']}")
    return "\n".join(lines)
