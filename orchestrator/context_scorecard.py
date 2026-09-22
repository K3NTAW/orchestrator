"""Context-economy reporting from run-log packet telemetry."""
import json
from collections import defaultdict
from pathlib import Path

from . import STATE, attribution, decision_log


def _entries(root):
    for path in sorted((Path(root) / "runs").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _avg(values):
    return round(sum(values) / len(values), 2) if values else None


def _shadow(rows):
    contexts = [row["context"] for row in rows if isinstance(row.get("context"), dict)]
    measured = [context for context in contexts if context.get("routed_tokens") is not None]
    candidates = [len(context.get("evidence_ids") or []) for context in measured]
    return {
        "shadow_routed_tokens": _avg([context["routed_tokens"] for context in measured]),
        "shadow_reduction": _avg([context["routed_reduction_ratio"] for context in measured
                                  if context.get("routed_reduction_ratio") is not None]),
        "shadow_hidden": _avg([context["routed_hidden"] for context in measured
                               if context.get("routed_hidden") is not None]),
        "shadow_ambiguous_rate": _avg([
            context.get("routed_ambiguous", 0) / count for context, count in zip(measured, candidates) if count]),
        "shadow_unmeasured": len(rows) - len(measured),
    }



def recovery(root=STATE):
    """P21: recovery Read events divided by HIDE items in active selections."""
    roles = {}
    task_roles = {}
    for row in decision_log.read_all(root=root):
        data = row.get("deterministic") or {}
        if row.get("kind") == "context_selection":
            task_roles[row.get("subject")] = data.get("role") or str(row.get("reason", "unknown")).split()[0]
        if row.get("mode") != "active":
            continue
        role = data.get("role") or task_roles.get(row.get("subject"), "unknown")
        if row.get("kind") == "context_selection":
            item = roles.setdefault(role, {"recovery_reads": 0, "hidden_items": 0})
            item["hidden_items"] += sum(value.endswith(":HIDE") for value in row.get("candidates", []))
        elif row.get("kind") == "evidence_reuse" and row.get("reason") == "recovery_read":
            item = roles.setdefault(role, {"recovery_reads": 0, "hidden_items": 0})
            item["recovery_reads"] += 1
    for item in roles.values():
        item["recovery_rate"] = item["recovery_reads"] / item["hidden_items"] if item["hidden_items"] else 0.0
    return roles

def build(root=STATE):
    entries = list(_entries(root))
    decisions = list(decision_log.read_all(root=root))
    tasks = {}
    for path in sorted((Path(root) / "tasks").glob("*.json")):
        try:
            task = json.loads(path.read_text())
            tasks[task.get("id", path.stem)] = task
        except (OSError, ValueError, TypeError):
            continue
    roles = {}
    recoveries = recovery(root)
    for role in sorted({row.get("role") or "?" for row in entries} | set(recoveries)):
        rows = [row for row in entries if (row.get("role") or "?") == role]
        measured = [row for row in rows if isinstance(row.get("context"), dict)]
        presented = [row["context"].get("presented_tokens") for row in measured
                     if row["context"].get("presented_tokens") is not None]
        candidate_rows = [row for row in measured if row["context"].get("candidate_known")
                          and row["context"].get("candidate_tokens") is not None]
        candidate = [row["context"]["candidate_tokens"] for row in candidate_rows]
        candidate_presented = [row["context"].get("presented_tokens", 0) for row in candidate_rows]
        instructions = [row["context"].get("instruction_tokens") for row in measured
                        if row["context"].get("instruction_tokens") is not None]
        modular_rows = [row for row in measured
                        if row["context"].get("instruction_tokens_modular") is not None]
        modular = [row["context"]["instruction_tokens_modular"] for row in modular_rows]
        modular_legacy = [row["context"].get("instruction_tokens") for row in modular_rows]
        disclosed = [row["context"].get("tool_tokens_disclosed") for row in measured
                     if row["context"].get("tool_tokens_disclosed") is not None]
        minimal = [row["context"].get("tool_tokens_minimal") for row in measured
                   if row["context"].get("tool_tokens_minimal") is not None]
        sections = defaultdict(list)
        for row in measured:
            for name, meta in (row["context"].get("sections") or {}).items():
                sections[name].append(meta.get("est_tokens", 0))
        roles[role] = {**recoveries.get(role, {"recovery_reads": 0, "hidden_items": 0, "recovery_rate": 0.0}), "runs": len(rows), "measured": len(measured),
                       "unmeasured": len(rows) - len(measured), "avg_presented": _avg(presented),
                       "avg_candidate": _avg(candidate),
                       "reduction_ratio": round(1 - sum(candidate_presented) / sum(candidate), 3)
                       if candidate and sum(candidate) else None,
                       "avg_instruction_tokens": _avg(instructions),
                       "instruction_tokens": _avg(instructions),
                       "instruction_tokens_modular": _avg(modular),
                       "instruction_reduction": round(sum(modular) / sum(modular_legacy), 3)
                       if modular_legacy and sum(modular_legacy) else None,
                       "instruction_unmeasured": len(rows) - len(modular_rows),
                       "tool_tokens_disclosed": _avg(disclosed), "tool_tokens_minimal": _avg(minimal),
                       "tool_reduction": round(sum(minimal) / sum(disclosed), 3) if disclosed and sum(disclosed) else None,
                       "top_sections": sorted(((name, _avg(values)) for name, values in sections.items()),
                                              key=lambda item: (-item[1], item[0]))[:5], **_shadow(rows)}
    goals = {}
    for goal in sorted({row.get("goal_id") for row in entries if row.get("goal_id")}):
        rows = [row for row in entries if row.get("goal_id") == goal]
        measured = [row for row in rows if isinstance(row.get("context"), dict)]
        total = unique = 0
        by_section = {}
        occurrences = defaultdict(list)
        for row in measured:
            for name, meta in (row["context"].get("sections") or {}).items():
                token_count = meta.get("est_tokens", 0)
                total += token_count
                occurrences[name].append((meta.get("sha256"), token_count, row.get("role")))
        for name, values in occurrences.items():
            unique_tokens = sum(next(tokens for digest2, tokens, _ in values if digest2 == digest)
                                for digest in {digest for digest, _, _ in values})
            by_section[name] = round(sum(tokens for _, tokens, _ in values) / unique_tokens, 3) if unique_tokens else 0.0
            unique += unique_tokens
        repeated = sorted(name for name, values in occurrences.items()
                          if any(len({role for digest2, _, role in values if digest2 == digest}) >= 2
                                 for digest in {digest for digest, _, _ in values}))
        goals[goal] = {"runs": len(rows), "measured": len(measured),
                       "amplification": round(total / unique, 3) if unique else None,
                       "amplification_by_section": by_section, "repeated_sections": repeated,
                       "lineage_roots": sorted({row.get("lineage_root") for row in rows if row.get("lineage_root")}),
                       **_shadow(rows)}
    by_role_class = {}
    disclosure = [row for row in decisions if row.get("kind") == "tool_disclosure" and row.get("mode") == "active"]
    disclosure_keys = {(row.get("deterministic") or {}).get("role", "unknown") for row in disclosure}
    for key in sorted({(row.get("role") or "unknown",
                        attribution.task_class(tasks[row.get("task")]) if row.get("task") in tasks else "unknown")
                       for row in entries} |
                      {(role, attribution.task_class(tasks[subject]) if subject in tasks else "unknown")
                       for role in disclosure_keys for subject in
                       {row.get("subject") for row in disclosure
                        if (row.get("deterministic") or {}).get("role", "unknown") == role}}):
        selected = [row for row in entries if (row.get("role") or "unknown") == key[0] and
                    (attribution.task_class(tasks[row.get("task")]) if row.get("task") in tasks else "unknown") == key[1]]
        contexts = [row["context"] for row in selected if isinstance(row.get("context"), dict)]
        disclosed = [c["tool_tokens_disclosed"] for c in contexts if c.get("tool_tokens_disclosed") is not None]
        minimal = [c["tool_tokens_minimal"] for c in contexts if c.get("tool_tokens_minimal") is not None]
        active = [row for row in disclosure
                  if (row.get("deterministic") or {}).get("role", "unknown") == key[0]
                  and (attribution.task_class(tasks[row.get("subject")])
                       if row.get("subject") in tasks else "unknown") == key[1]]
        spawns = sum(row.get("reason") != "hidden_tool_requested" for row in active)
        escalations = sum(row.get("reason") == "hidden_tool_requested" for row in active)
        by_role_class[key] = {**recoveries.get(key[0], {"recovery_reads": 0, "hidden_items": 0, "recovery_rate": 0.0}),
                              "runs": len(selected), "measured": len(contexts),
                              "hidden_tool_recovery_rate": escalations / spawns if spawns else 0.0,
                              "tool_tokens_disclosed": _avg(disclosed), "tool_tokens_minimal": _avg(minimal),
                              "tool_reduction": round(sum(minimal) / sum(disclosed), 3) if disclosed and sum(disclosed) else None,
                              **_shadow(selected)}
    return {"roles": roles, "goals": goals, "by_role_task_class": by_role_class}


def format_report(card):
    lines = ["Per role", "role\truns\tmeasured\tunmeasured\tavg presented\tavg candidate\treduction\tavg instructions\tmodular instructions\tinstruction reduction\ttool disclosed\ttool minimal\ttool reduction\tshadow routed\tshadow reduction\tshadow hidden\tshadow ambiguous\tshadow unmeasured\trecovery_rate\ttop sections"]
    for role, row in card["roles"].items():
        lines.append("\t".join(map(str, (role, row["runs"], row["measured"], row["unmeasured"],
                                          row["avg_presented"], row["avg_candidate"], row["reduction_ratio"],
                                          row["avg_instruction_tokens"], row["instruction_tokens_modular"],
                                          row["instruction_reduction"], row["tool_tokens_disclosed"],
                                          row["tool_tokens_minimal"], row["tool_reduction"],
                                          row["shadow_routed_tokens"],
                                          row["shadow_reduction"], row["shadow_hidden"],
                                          row["shadow_ambiguous_rate"], row["shadow_unmeasured"],
                                          row["recovery_rate"], row["top_sections"]))))
    lines += ["", "Per goal", "goal\truns\tmeasured\tamplification\tshadow routed\tshadow reduction\tshadow hidden\tshadow ambiguous\tshadow unmeasured\tby section\trepeated\tlineage roots"]
    for goal, row in card["goals"].items():
        lines.append("\t".join(map(str, (goal, row["runs"], row["measured"], row["amplification"],
                                          row["shadow_routed_tokens"], row["shadow_reduction"],
                                          row["shadow_hidden"], row["shadow_ambiguous_rate"],
                                          row["shadow_unmeasured"],
                                          row["amplification_by_section"], row["repeated_sections"], row["lineage_roots"]))))
    lines += ["", "Per role/task class", "role\ttask class\truns\thidden_tool_recovery_rate"]
    for (role, task_class), row in card["by_role_task_class"].items():
        lines.append("\t".join(map(str, (role, task_class, row["runs"], row["hidden_tool_recovery_rate"]))))
    return "\n".join(lines)
