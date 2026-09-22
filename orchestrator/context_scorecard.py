"""Context-economy reporting from run-log packet telemetry."""
import json
from collections import defaultdict
from pathlib import Path

from . import STATE


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


def build(root=STATE):
    entries = list(_entries(root))
    roles = {}
    for role in sorted({row.get("role") or "?" for row in entries}):
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
        disclosed = [row["context"].get("tool_tokens_disclosed") for row in measured
                     if row["context"].get("tool_tokens_disclosed") is not None]
        minimal = [row["context"].get("tool_tokens_minimal") for row in measured
                   if row["context"].get("tool_tokens_minimal") is not None]
        sections = defaultdict(list)
        for row in measured:
            for name, meta in (row["context"].get("sections") or {}).items():
                sections[name].append(meta.get("est_tokens", 0))
        roles[role] = {"runs": len(rows), "measured": len(measured),
                       "unmeasured": len(rows) - len(measured), "avg_presented": _avg(presented),
                       "avg_candidate": _avg(candidate),
                       "reduction_ratio": round(1 - sum(candidate_presented) / sum(candidate), 3)
                       if candidate and sum(candidate) else None,
                       "avg_instruction_tokens": _avg(instructions),
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
    return {"roles": roles, "goals": goals}


def format_report(card):
    lines = ["Per role", "role\truns\tmeasured\tunmeasured\tavg presented\tavg candidate\treduction\tavg instructions\ttool disclosed\ttool minimal\ttool reduction\tshadow routed\tshadow reduction\tshadow hidden\tshadow ambiguous\tshadow unmeasured\ttop sections"]
    for role, row in card["roles"].items():
        lines.append("\t".join(map(str, (role, row["runs"], row["measured"], row["unmeasured"],
                                          row["avg_presented"], row["avg_candidate"], row["reduction_ratio"],
                                          row["avg_instruction_tokens"], row["tool_tokens_disclosed"],
                                          row["tool_tokens_minimal"], row["tool_reduction"],
                                          row["shadow_routed_tokens"],
                                          row["shadow_reduction"], row["shadow_hidden"],
                                          row["shadow_ambiguous_rate"], row["shadow_unmeasured"],
                                          row["top_sections"]))))
    lines += ["", "Per goal", "goal\truns\tmeasured\tamplification\tshadow routed\tshadow reduction\tshadow hidden\tshadow ambiguous\tshadow unmeasured\tby section\trepeated\tlineage roots"]
    for goal, row in card["goals"].items():
        lines.append("\t".join(map(str, (goal, row["runs"], row["measured"], row["amplification"],
                                          row["shadow_routed_tokens"], row["shadow_reduction"],
                                          row["shadow_hidden"], row["shadow_ambiguous_rate"],
                                          row["shadow_unmeasured"],
                                          row["amplification_by_section"], row["repeated_sections"], row["lineage_roots"]))))
    return "\n".join(lines)
