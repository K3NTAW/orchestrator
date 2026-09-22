"""Skill exposure, use, and token-overhead reporting from worker run rows."""
import json
from collections import defaultdict
from pathlib import Path

from . import STATE


def _avg(values):
    return round(sum(values) / len(values), 3) if values else None


def build(root=STATE):
    root = Path(root)
    rows = []
    for path in sorted((root / "runs").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row.get("context"), dict):
                rows.append(row)
    buckets = defaultdict(list)
    for row in rows:
        context = row["context"]
        for skill_id in context.get("skills_exposed") or []:
            buckets[(row.get("role") or "unknown", skill_id)].append(row)
    by_role_skill = {}
    for (role, skill_id), selected in sorted(buckets.items()):
        uses = sum(skill_id in (row["context"].get("skills_used") or []) for row in selected)
        overhead = []
        for row in selected:
            tokens = row.get("input_tokens") or (row.get("usage") or {}).get("input_tokens")
            if tokens:
                overhead.append(((row["context"].get("skill_tokens_l0") or 0) +
                                 (row["context"].get("skill_tokens_l2") or 0)) / tokens)
        by_role_skill[f"{role}/{skill_id}"] = {
            "role": role, "skill_id": skill_id, "exposures": len(selected), "uses": uses,
            "use_rate": round(uses / len(selected), 3),
            "skill_tokens_l0": _avg([row["context"].get("skill_tokens_l0", 0) for row in selected]),
            "skill_tokens_l2": _avg([row["context"].get("skill_tokens_l2", 0) for row in selected]),
            "skill_overhead_ratio": _avg(overhead),
        }
    accepted = {}
    for row in rows:
        task = row.get("task")
        try:
            record = json.loads((root / "tasks" / f"{task}.json").read_text())
        except (OSError, ValueError, TypeError):
            continue
        if not record.get("merged_into"):
            continue
        lineage = row.get("lineage_root") or row.get("goal_id") or task
        accepted.setdefault(lineage, 0)
        accepted[lineage] += (row["context"].get("skill_tokens_l0") or 0) + (row["context"].get("skill_tokens_l2") or 0)
    return {"by_role_skill": by_role_skill, "skill_tokens_per_accepted_task": accepted}


def format_report(card):
    lines = ["role/skill\texposures\tuses\tuse rate\tl0 avg\tl2 avg\toverhead ratio"]
    for key, row in card["by_role_skill"].items():
        lines.append(f"{key}\t{row['exposures']}\t{row['uses']}\t{row['use_rate']}\t{row['skill_tokens_l0']}\t{row['skill_tokens_l2']}\t{row['skill_overhead_ratio']}")
    lines += ["", "accepted lineage\tskill tokens", *
              (f"{key}\t{value}" for key, value in card["skill_tokens_per_accepted_task"].items())]
    return "\n".join(lines)
