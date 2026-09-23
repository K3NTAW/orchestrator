"""Deterministic minimal skill selection for shadow routing telemetry."""
from __future__ import annotations

from . import attribution, skills_registry


MANDATORY = {
    "execute": ("executor/implement-spec",),
    "codex_execute": ("executor/implement-spec",),
    "review": ("review/adversarial-review",),
    "spec_review": ("review/adversarial-review",),
    "challenge": ("review/challenge",),
    "planner": ("planner/memory", "planner/orchestrate", "planner/resume", "planner/write-spec"),
}


def _skills_cfg(cfg):
    if not isinstance(cfg, dict):
        return {}
    value = cfg.get("skills", cfg)
    return value if isinstance(value, dict) else {}


def _strength(matches):
    return sum(len(str(trigger).removeprefix("scope:").split()) for trigger in matches)


def select(task, role, cfg=None, history=None, jev=True):
    """Return the smallest deterministic set; history is reserved for Stage 4."""
    del history
    records = skills_registry.load().get("skills", {})
    registry_role = {"codex_execute": "execute", "security_review": "review"}.get(role, role)
    eligible = {
        skill_id: record for skill_id, record in records.items()
        if record.get("state") == "active" and registry_role in (record.get("roles") or [])
    }
    matched_rows = skills_registry.candidates(task, registry_role, cfg=cfg)
    matched = {row["id"]: list(row.get("matched") or []) for row in matched_rows}
    mandatory = list(MANDATORY.get(role, MANDATORY.get(registry_role, ())))
    candidates = sorted(set(eligible) | set(mandatory))
    ambiguous = sorted((skill_id for skill_id, triggers in matched.items()
                        if skill_id not in mandatory and _strength(triggers) == 1),
                       key=lambda skill_id: (-_strength(matched[skill_id]), skill_id))
    ranked = sorted(
        (skill_id for skill_id in matched if skill_id not in mandatory and skill_id not in ambiguous),
        key=lambda skill_id: (-_strength(matched[skill_id]), skill_id),
    )
    cap = max(0, int(_skills_cfg(cfg).get("max_selected", 3)))
    ranked_kept = ranked[:cap]
    selected = list(mandatory) + ranked_kept
    jev_result = None
    skills_cfg = _skills_cfg(cfg)
    jev_mode = skills_cfg.get("jev_mode", "shadow")
    if jev and ambiguous and jev_mode in ("shadow", "active"):
        # Lazy imports avoid skill_router -> jev_skills -> jev -> spawn -> skill_router.
        from . import jev_skills
        jev_result = jev_skills.classify(task, role, ambiguous, cfg=cfg)
        if jev_mode == "active":
            headroom = max(0, cap - len(ranked_kept))
            approved = [skill_id for skill_id in ambiguous
                        if (jev_result.get("decisions", {}).get(skill_id) or {}).get("select")]
            added = approved[:headroom]
            selected.extend(added)
            for skill_id in approved[headroom:]:
                jev_result["decisions"][skill_id]["status"] = "jev_selected_not_added"
    rejected = []
    for skill_id in candidates:
        if skill_id in selected:
            continue
        if skill_id in ambiguous:
            reason = "ambiguous weak trigger"
        elif skill_id in ranked[cap:]:
            reason = "non-mandatory cap"
        else:
            reason = "no trigger match"
        rejected.append({"id": skill_id, "reason": reason})
    exposed = [record for record in records.values()
               if record.get("state") == "active" and record.get("provenance") == "builtin"]
    tokens_l0 = lambda ids: sum(int(records.get(skill_id, {}).get("est_tokens_l0") or 0) for skill_id in ids)
    tokens_l2 = lambda ids: sum(int(records.get(skill_id, {}).get("est_tokens_l2") or 0) for skill_id in ids)
    return {
        "candidates": candidates,
        "selected": selected,
        "rejected": rejected,
        "reason": "mandatory skills plus firm trigger matches",
        "tokens_exposed_l0": sum(int(record.get("est_tokens_l0") or 0) for record in exposed),
        "tokens_selected_l0": tokens_l0(selected),
        "tokens_selected_l2": tokens_l2(selected),
        "ambiguous": ambiguous,
        "triggers": {skill_id: matched.get(skill_id, []) for skill_id in candidates},
        "task_class": attribution.task_class(task),
        "mandatory": mandatory,
        "jev": jev_result,
    }
