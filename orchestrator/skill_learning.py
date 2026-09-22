"""Mine repeated successful work and store inert learned-skill proposals."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import STATE, attribution, skill_scorecard
from . import skills_registry as registry


def _rows(path):
    if not path.exists():
        return
    for line in path.read_text(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def _tasks(root):
    result = {}
    for path in sorted((Path(root) / "tasks").glob("*.json")):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            result[str(value.get("id") or path.stem)] = value
    return result


def _cutoff(since_s):
    if since_s in (None, ""):
        return None
    if isinstance(since_s, (int, float)):
        return datetime.now(timezone.utc) - timedelta(seconds=float(since_s))
    match = re.fullmatch(r"(\d+)([dhms])", str(since_s).strip().lower())
    if not match:
        raise ValueError("--since must be a duration such as 30d")
    seconds = int(match.group(1)) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[match.group(2)]
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _recent(row, cutoff):
    if cutoff is None:
        return True
    raw = row.get("ts") or row.get("at") or row.get("created_at")
    try:
        stamp = (datetime.fromtimestamp(float(raw), timezone.utc) if isinstance(raw, (int, float))
                 else datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except (ValueError, TypeError, OverflowError):
        return True
    return stamp >= cutoff


def _normal_target(tool, target):
    text = str(target or "").strip()
    script = re.search(r"(?:^|\s)((?:[\w.-]+/)+(?:[\w.-]+\.(?:sh|py|js|ts)))\b", text)
    if script:
        return script.group(1)
    if str(tool).lower() in ("bash", "shell", "exec", "exec_command"):
        words = re.findall(r"[^\s]+", text)
        return words[0].rsplit("/", 1)[-1] if words else ""
    path = re.search(r"(?:^|\s)([\w./-]+)", text)
    value = path.group(1) if path else text
    return Path(value).stem if "." in Path(value).name else value.rstrip("/").rsplit("/", 1)[-1]


def _sentences(text):
    for value in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")):
        sentence = re.sub(r"\s+", " ", value).strip(" -\t")
        if len(re.findall(r"\b\w+\b", sentence)) >= 8:
            yield sentence.casefold()


def _outcomes(root, tasks):
    result = {}
    for task_id, task in tasks.items():
        value = task.get("merged_into") or task.get("accepted")
        status = str(task.get("status") or (task.get("result") or {}).get("status") or "").lower()
        result[task_id] = bool(value or status in ("merged", "accepted"))
    for row in skill_scorecard.usage_rows(root):
        if row.get("accepted") is not None:
            result[str(row.get("task"))] = bool(row["accepted"])
            if row.get("lineage_root"):
                result[str(row["lineage_root"])] = bool(row["accepted"])
    # Learning must not depend on a task having selected a pre-existing skill.
    # Run logs predate skill telemetry and still carry useful terminal outcomes.
    for path in sorted((Path(root) / "runs").rglob("*.jsonl")):
        if path.name == "gate.jsonl":
            continue
        for row in _rows(path):
            task_id = row.get("task")
            outcome = str(row.get("outcome") or row.get("status") or "").casefold()
            if task_id and outcome in ("merged", "accepted"):
                result[str(task_id)] = True
    # Fix-round success belongs to the repaired lineage, not to whether the
    # auxiliary fix task itself was merged independently.
    for task_id, task in tasks.items():
        target = (task.get("constraints") or {}).get("fix_round_for")
        seen = set()
        while target in tasks and target not in seen:
            seen.add(target)
            parent = tasks[target]
            next_target = (parent.get("constraints") or {}).get("fix_round_for")
            if not next_target:
                break
            target = next_target
        if target in result:
            result[task_id] = result[target]
    return result


def _pattern(kind, signature, task_ids, evidence, tasks, outcomes, min_support):
    ids = sorted(set(task_ids))
    successes = sum(bool(outcomes.get(task)) for task in ids)
    support = len(ids)
    confidence = successes / support if support else 0.0
    roles = sorted({str(tasks.get(task, {}).get("role") or evidence.get(task, {}).get("role") or "unknown")
                    for task in ids})
    classes = sorted({attribution.task_class(tasks[task]) for task in ids if task in tasks})
    failures = [task for task in ids if not outcomes.get(task)]
    return {
        "source": kind, "procedure_signature": signature, "support_tasks": ids,
        "support": support, "successes": successes, "failures": failures,
        "roles": roles, "task_classes": classes, "confidence": round(confidence, 3),
        "evidence_strength": round(min(1.0, support / 10), 3),
        "evidence": [evidence.get(task, {}) for task in ids], "min_support": min_support,
    }


def patterns(root=STATE, since_s=None, min_support=3):
    """Return deterministic repeated patterns with at least 80 percent accepted outcomes."""
    root, minimum, cutoff = Path(root), int(min_support), _cutoff(since_s)
    tasks = _tasks(root)
    outcomes = _outcomes(root, tasks)
    found = []

    sequences = defaultdict(list)
    for row in _rows(root / "runs" / "jev" / "gate.jsonl"):
        if not _recent(row, cutoff) or not row.get("task") or not row.get("tool"):
            continue
        key = (str(row["task"]), str(row.get("role") or tasks.get(str(row["task"]), {}).get("role") or "unknown"))
        sequences[key].append((row.get("ts") or row.get("at") or len(sequences[key]),
                               f"{row['tool']}:{_normal_target(row['tool'], row.get('tool_target'))}"))
    groups, evidence = defaultdict(list), {}
    for (task, role), values in sequences.items():
        signature = " > ".join(item[1] for item in sorted(values, key=lambda item: str(item[0])))
        groups[(role, signature)].append(task)
        evidence[task] = {"role": role, "tools": [item[1] for item in sorted(values, key=lambda item: str(item[0]))]}
    for (_, signature), ids in groups.items():
        found.append(_pattern("tool_sequence", signature, ids, evidence, tasks, outcomes, minimum))

    instructions, instruction_evidence = defaultdict(set), defaultdict(dict)
    for task_id, task in tasks.items():
        if task.get("role") not in ("execute", "scout", "review", "spec_review") or not _recent(task, cutoff):
            continue
        for sentence in _sentences(task.get("spec")):
            instructions[sentence].add(task_id)
            instruction_evidence[sentence][task_id] = {"role": task.get("role"), "sentence": sentence}
    for sentence, ids in instructions.items():
        found.append(_pattern("instruction", "instruction:" + sentence, ids, instruction_evidence[sentence],
                              tasks, outcomes, minimum))

    fixes, fix_evidence = defaultdict(set), {}
    for task_id, task in tasks.items():
        constraints = task.get("constraints") or {}
        if not constraints.get("fix_round_for") or not _recent(task, cutoff):
            continue
        pipeline = task.get("pipeline") or {}
        strategy = (task.get("planner_note") or pipeline.get("planner_note") or
                    task.get("failure_kind") or pipeline.get("failure_kind") or
                    constraints.get("failure_kind") or task.get("note"))
        if not strategy:
            sentences = list(_sentences(task.get("spec")))
            strategy = sentences[0] if sentences else None
        if strategy:
            signature = "fix:" + re.sub(r"\s+", " ", str(strategy)).strip().casefold()
            fixes[signature].add(task_id)
            fix_evidence[task_id] = {"role": task.get("role"), "fix_strategy": str(strategy)}
    for signature, ids in fixes.items():
        found.append(_pattern("fix_strategy", signature, ids, fix_evidence, tasks, outcomes, minimum))

    return sorted((item for item in found
                   if item["support"] >= minimum and item["successes"] / item["support"] >= .8),
                  key=lambda item: (-item["evidence_strength"], item["procedure_signature"]))


def _slug(pattern):
    words = re.findall(r"[a-z0-9]+", pattern["procedure_signature"].lower())[:6]
    base = "-".join(words)[:48].strip("-") or "procedure"
    return base + "-" + hashlib.sha256(pattern["procedure_signature"].encode()).hexdigest()[:8]


def _store_learned(record, root=STATE):
    """Persist a learned record and seed lifecycle state before quarantine transition."""
    root = Path(root)
    path = root / "skills" / "discovered.json"
    document = registry._read_json(path, {"version": 1, "skills": {}})
    records = document.setdefault("skills", {})
    signature = record["provenance_info"]["procedure_signature"]
    previous = next((value for value in records.values()
                     if value.get("provenance") == "learned"
                     and (value.get("provenance_info") or {}).get("procedure_signature") == signature), None)
    if previous:
        record["id"] = previous["id"]
        record["created_at"] = previous["created_at"]
    records[record["id"]] = record
    states = registry._state_document(root)
    states.setdefault(record["id"], {"state": "discovered", "trust": "untrusted",
                                      "since": registry._now(), "history": [],
                                      "versions": [{"content_hash": record["content_hash"],
                                                    "at": record["updated_at"]}],
                                      "reason": "learned proposal"})
    registry._write_json(path, document)
    registry._write_json(registry._paths(root)[1], states)
    return previous


def propose(pattern, root=STATE):
    """Write or update one learned draft, refusing weak evidence."""
    root = Path(root)
    support = int(pattern.get("support", len(pattern.get("support_tasks") or [])))
    minimum = int(pattern.get("min_support", 3))
    confidence = float(pattern.get("confidence", 0))
    existing = registry._read_json(root / "skills" / "discovered.json", {"skills": {}}).get("skills", {})
    prior = next((r for r in existing.values()
                  if r.get("provenance") == "learned" and
                  (r.get("provenance_info") or {}).get("procedure_signature") == pattern.get("procedure_signature")), None)
    if support < minimum or confidence < .6:
        reason = "stale evidence" if prior else ("support below minimum" if support < minimum else "confidence below 0.6")
        if prior:
            states = registry._state_document(root)
            entry = states.setdefault(prior["id"], {"state": prior.get("state", "quarantined"),
                                                     "trust": "untrusted", "history": []})
            entry.setdefault("history", []).append({"at": registry._now(), "from": entry["state"],
                                                     "to": entry["state"], "reason": "stale evidence"})
            registry._write_json(registry._paths(root)[1], states)
        return {"refused": True, "reason": reason, "procedure_signature": pattern.get("procedure_signature")}

    tasks = _tasks(root)
    slug = _slug(pattern)
    skill_id = prior["id"] if prior else f"learned/{slug}"
    titles = " ".join(str(tasks.get(t, {}).get("title") or "") for t in pattern.get("support_tasks", []))
    keywords = [word for word, count in Counter(re.findall(r"[a-z][a-z0-9-]{3,}", titles.lower())).most_common(5)]
    sentence = next((e.get("sentence") for e in pattern.get("evidence", []) if e.get("sentence")),
                    "Apply the repeated procedure consistently and verify its result.")
    steps = pattern.get("procedure_signature", "").split(" > ") if pattern.get("source") == "tool_sequence" else []
    tools = list(dict.fromkeys(step.split(":", 1)[0] for step in steps))
    sections = Counter()
    for task in pattern.get("support_tasks", []):
        for section in ((next((r for r in skill_scorecard.usage_rows(root) if str(r.get("task")) == task), {})
                         .get("context") or {}).keys()):
            sections[section] += 1
    evidence_requirements = [name for name, _ in sections.most_common(5)] or ["task specification", "acceptance criteria"]
    failures = pattern.get("failures") or []
    fix_notes = [e.get("fix_strategy") for e in pattern.get("evidence", []) if e.get("fix_strategy")]
    now = registry._now()
    info = {"source_tasks": pattern.get("support_tasks", []), "successes": pattern.get("successes", 0),
            "failures": failures, "procedure_signature": pattern["procedure_signature"],
            "confidence": confidence, "evidence_strength": pattern.get("evidence_strength", min(1, support / 10)),
            "synthesised_at": now}
    body = "\n".join([
        "---", f"name: {slug}", "description: Learned procedure from repeated successful tasks.",
        "provenance: learned", f"roles: {json.dumps(pattern.get('roles') or [])}",
        f"task_classes: {json.dumps(pattern.get('task_classes') or [])}", f"triggers: {json.dumps(keywords)}",
        f"tools: {json.dumps(tools)}", "security: untrusted", "---", "## Trigger",
        "Use for " + (", ".join(pattern.get("task_classes") or ["matching tasks"])) +
        (" involving " + ", ".join(keywords) if keywords else "."), "", "## Objective", sentence, "",
        "## Procedure", *(f"{index}. {step}" for index, step in enumerate(steps or [sentence], 1)), "",
        "## Tools", ", ".join(tools) if tools else "No specific tool sequence was evidenced.", "",
        "## Evidence requirements", ", ".join(evidence_requirements), "", "## Output contract",
        "Return the role result with evidence and acceptance status.", "", "## Stop conditions",
        "Stop when the task acceptance criteria pass or the evidence is insufficient.", "", "## Failure/recovery",
        ("Use the observed recovery: " + "; ".join(fix_notes)) if fix_notes else
        ("Review counterexamples: " + ", ".join(failures) if failures else "Stop and report missing evidence."), "",
        "## Evidence", json.dumps(info, sort_keys=True), "",
    ])
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    directory = root / "skills" / "quarantine" / "learned" / skill_id.split("/", 1)[1]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body)
    record = {
        "id": skill_id, "version": digest, "content_hash": digest, "name": slug,
        "description": "Learned procedure from repeated successful tasks.", "provenance": "learned",
        "trust": "untrusted", "state": prior.get("state", "discovered") if prior else "discovered",
        "roles": pattern.get("roles") or [], "task_classes": pattern.get("task_classes") or [],
        "triggers": keywords, "tools": tools, "context_requirements": evidence_requirements,
        "output_contract": "Return the role result with evidence and acceptance status.",
        "est_tokens_l0": 12, "est_tokens_l1": 24, "est_tokens_l2": len(body) // 4,
        "security_class": "untrusted", "repo_scope": "orchestrator", "validation": {},
        "promotion_state": prior.get("state", "discovered") if prior else "discovered",
        "created_at": prior.get("created_at", now) if prior else now, "updated_at": now,
        "source": str(directory / "SKILL.md"), "provenance_info": info,
    }
    previous = _store_learned(record, root)
    if previous:
        states = registry._state_document(root)
        entry = states[record["id"]]
        entry.setdefault("history", []).append({"at": now, "from": entry["state"], "to": entry["state"],
                                                 "reason": f"learned proposal version {digest}"})
        entry.setdefault("versions", []).append({"content_hash": digest, "at": now})
        record["versions"] = entry["versions"]
        document = registry._read_json(root / "skills" / "discovered.json", {"skills": {}})
        document["skills"][record["id"]]["versions"] = entry["versions"]
        registry._write_json(root / "skills" / "discovered.json", document)
        registry._write_json(registry._paths(root)[1], states)
    else:
        registry.transition(record["id"], "quarantined", "learned proposal", root)
    return registry.load(root)["skills"][record["id"]]
