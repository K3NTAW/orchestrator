"""Read-only, version-windowed lifecycle recommendations. The CLI applies them."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import STATE, decision_log, skill_scorecard, skills_registry as registry


def version_window(record, entry, now=None):
    now = time.time() if now is None else now
    versions = list(entry.get("versions") or record.get("versions") or [])
    versions += [{"content_hash": row.get("version"), "at": row.get("at")}
                 for row in entry.get("history", []) if row.get("version")]
    # Older registry history recorded the hash in the reason rather than a field.
    versions += [{"content_hash": row["reason"].split("→")[-1], "at": row.get("at")}
                 for row in entry.get("history", [])
                 if str(row.get("reason", "")).startswith("content changed:") and "→" in row["reason"]]
    starts = [skill_scorecard.timestamp(v.get("at")) for v in versions
              if v.get("content_hash") == record["version"]]
    starts = [stamp for stamp in starts if stamp is not None]
    start = max(starts) if starts else skill_scorecard.timestamp(record.get("updated_at"))
    # Missing provenance cannot borrow old evidence.
    return (now if start is None else start), now


def suite_ready(root, now=None):
    now = time.time() if now is None else now
    report = registry._read_json(Path(root) / "skill_eval.json", {})
    stamp = skill_scorecard.timestamp(report.get("ran_at"))
    return bool(report.get("suite_passed") is True and stamp is not None and 0 <= now - stamp <= 7 * 86400)


def _inspection(record, root):
    report = registry._read_json(Path(root) / "skills/quarantine" / record["id"] / "findings.json", {})
    if record.get("provenance") != "builtin":
        if not report or report.get("content_hash") != record.get("content_hash"):
            return False, False
        from .skill_discovery import _hash
        directory = Path(root) / "skills/quarantine" / record["id"]
        files = {}
        for path in directory.rglob("*"):
            if path.is_symlink():
                return False, False
            if path.is_file() and path != directory / "findings.json":
                files[path.relative_to(directory).as_posix()] = path.read_bytes()
        if _hash(files) != record.get("content_hash"):
            return False, False
    return report.get("max_severity") != "block", not bool(report.get("findings"))


def _irrelevant(skill_id, root, since, until, usage):
    logs = [r for r in decision_log.read_all(root=root) if skill_scorecard.in_window(r, since, until)]
    outcomes = {r.get("subject"): r for r in logs
                if r.get("kind") == "outcome" and r.get("decision_kind") == "skill_selection"}
    used_by_task = {r.get("task"): r["skills_used"] for r in usage}
    selected = unused = 0
    for row in logs:
        if row.get("kind") != "skill_selection" or skill_id not in (row.get("selected") or []):
            continue
        used = outcomes.get(row.get("subject"), {}).get("skills_used", used_by_task.get(row.get("subject")))
        if used is None:  # Pending spawns are not evidence of non-use.
            continue
        selected += 1
        unused += skill_id not in used
    return {"selected": selected, "unused": unused,
            "rate": unused / selected if selected else None}


def _newer_conflict(skill_id, records, root, since, until):
    current = registry.state(skill_id, root)
    for row in decision_log.read_all(root=root):
        if row.get("kind") != "skill_selection" or not skill_scorecard.in_window(row, since, until):
            continue
        specialist = (row.get("extra") or {}).get("specialist") or (row.get("deterministic") or {}).get("specialist") or {}
        for conflict in specialist.get("conflicts", []):
            pair = {conflict.get("kept"), conflict.get("dropped")}
            if skill_id not in pair:
                continue
            for other in pair - {skill_id}:
                if records.get(other, {}).get("state") == "active":
                    other_entry = registry.state(other, root)
                    if (skill_scorecard.timestamp(other_entry.get("since")) or 0) > (skill_scorecard.timestamp(current.get("since")) or 0):
                        return conflict
    return None


def evaluate_skill(skill_id, root=STATE, cfg=None):
    """Recommend one transition without mutating state or running validation tests."""
    root = Path(root)
    cfg = cfg or {}
    minimum = max(20, int(cfg.get("promotion", {}).get("min_samples", 20)))
    records = registry.load(root)["skills"]
    record, entry = records[skill_id], registry.state(skill_id, root)
    since, until = version_window(record, entry)
    marginal = skill_scorecard.marginal(root, skill_id, min_samples=minimum, since_s=since, until_s=until)
    usage = skill_scorecard.usage_rows(root, since_s=since, until_s=until)
    irrelevant = _irrelevant(skill_id, root, since, until, usage)
    evidence = {"version": record["version"], "since_s": since, "until_s": until,
                "min_samples": minimum, "marginal": marginal, "irrelevant": irrelevant}
    status = entry["state"]
    target = None
    why = "insufficient evidence"
    inspected, secure = _inspection(record, root)
    validation = record.get("validation") or {}
    tested = validation.get("status") == "tested" and validation.get("version", record["version"]) == record["version"]
    sufficient = [r for r in marginal if not r.get("insufficient") and
                  min(r.get("n_with", 0), r.get("n_without", 0)) >= minimum]
    bad = [r for r in sufficient if r.get("verdict") in ("harmful", "costly")]
    efficient = any(r.get("verdict") in ("valuable", "neutral") and
                    any(r.get(key) is not None and r[key] < 0 for key in
                        ("accepted_tokens_delta", "accepted_usd_delta", "fix_rounds_delta")) for r in sufficient)
    if status == "active":
        stale_since = skill_scorecard.timestamp(record.get("stale_since"))
        stale_due = record.get("stale") and (validation.get("status") == "failed" or
                    (stale_since is not None and until - stale_since > 86400))
        conflict = _newer_conflict(skill_id, records, root, since, until)
        if stale_due:
            target, why = "shadow", "stale dependencies: validation failed or overdue"
        elif bad or (irrelevant["selected"] >= 20 and irrelevant["rate"] >= .7) or conflict:
            target, why = "demoted", "harmful/costly evidence, repeated irrelevant activation, or newer active conflict"
            evidence["conflict"] = conflict
    elif status == "testing" and tested and inspected and not record.get("stale"):
        target, why = "shadow", "validation passed and inspection below block"
    elif status == "shadow":
        if tested and inspected and secure and efficient and not bad and not record.get("stale"):
            if suite_ready(root, until):
                target, why = "active", "sufficient version evidence with measurable efficiency gain and recent passing suite"
            else:
                why = "active promotion requires a passing skill-eval within seven days"
    elif status == "demoted":
        demotions = [r for r in entry.get("history", []) if r.get("to") == "demoted" and r.get("from") != "demoted"]
        last = skill_scorecard.timestamp(demotions[-1].get("at")) if demotions else None
        if last is not None and since > last and tested and inspected and secure and efficient and not bad and not record.get("stale"):
            target, why = "shadow", "later version evidence clears demotion"
    return {"id": skill_id, "from": status, "to": target, "evidence": evidence,
            "reason": why + ": " + json.dumps(evidence, sort_keys=True)}


def recommendations(root=STATE, cfg=None):
    return [evaluate_skill(skill_id, root, cfg) for skill_id in sorted(registry.load(root)["skills"])]
