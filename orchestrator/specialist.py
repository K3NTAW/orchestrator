"""Ephemeral, deterministic role/skill composition; never writes a role or policy."""
from dataclasses import dataclass
from pathlib import Path
import re

from . import bus, evidence, skill_router, skills_registry, tool_catalog


WORKER_ROLES = {"execute", "codex_execute", "review", "security_review", "scout",
                "challenge", "spec_review"}
TOOL_ALIASES = {
    "Bash": (),  # Expanded against the role's legacy allowlist.
    "Search": ("Grep", "Glob"),
    **{name: (name,) for name in ("Read", "Grep", "Glob", "Edit", "Write")},
    **{name.removeprefix("mcp__bus__"): (name,)
       for name in tool_catalog.CATALOG if name.startswith("mcp__bus__")},
    **{name: (name,) for name in (
        "spawn_scout", "spawn_review", "spawn_challenge", "spawn_spec_review", "codex",
        "codex_reply", "merge", "status", "executor_fallback", "hold_account", "resume_account")},
}


@dataclass
class Specialist:
    role: str
    skills: list[dict]
    context_requirements: list[str]
    tools: list[str]
    model: str | None
    name: str
    conflicts: list[dict]
    decision: dict
    reviewed_id: str | None = None

    def filter_evidence(self, candidates):
        prefix = f"task:{self.reviewed_id}:review:"
        return [item for item in candidates
                if not (self.role in ("review", "security_review") and self.reviewed_id
                        and item.source_type == "review_finding" and item.location.startswith(prefix))]

    def shared_evidence(self, task):
        goal = task.get("parent") or task.get("id")
        if not goal:
            return []
        pool = evidence.EvidencePool(goal)
        return sorted(self.filter_evidence([
            item for kind in evidence.SOURCE_TYPES for item in pool.by_type(kind)
        ]), key=lambda item: item.id)


def normalize_tool(tool, offered):
    """Return catalog ids, or None for an unknown declaration."""
    if tool == "Bash":
        return [item for item in offered if item.startswith("Bash(")]
    if tool.startswith("script:"):
        return ["Bash(bash skills/*)"]
    if tool in TOOL_ALIASES:
        return list(TOOL_ALIASES[tool])
    if tool in tool_catalog.CATALOG:
        return [tool]
    return None


def _contracts(record):
    meta, body = {}, ""
    if record.get("source"):
        try:
            meta, body = skills_registry._frontmatter(
                (skills_registry.REPO / record["source"]).read_text())
        except OSError:
            pass
    output = str(meta.get("output") or record.get("output_contract") or "").strip().lower()
    # Supporting artifacts do not own the role's final response. Explicit final
    # result declarations and the existing worker result schemas do.
    final = bool(re.search(r"final (?:result|output|response)|verdict|findings.*json|"
                           r"json|markdown table|committed task branch|finish with", output))
    shape = re.sub(r"\s+", " ", output) if final else None
    if final and "json" in output:
        container = "array" if re.search(r"\b(?:array|list)\b", output) else "object"
        result_kind = "verdict" if "verdict" in output else "findings" if "findings" in output else ""
        shape = f"json:{container}:{result_kind}"
    elif final and re.search(r"\b(?:markdown )?table\b", output):
        shape = "table"
    def script_path(value):
        path = Path(value)
        if value.startswith("skills/"):
            path = skills_registry.REPO / path
        elif value.startswith("scripts/") and record.get("source"):
            path = (skills_registry.REPO / record["source"]).parent / path
        return str(path)
    scripts = {}
    for declaration in record.get("tools", []):
        if declaration.startswith("script:"):
            parts = declaration[7:].split(maxsplit=1)
            scripts[script_path(parts[0])] = parts[1] if len(parts) > 1 else ""
    for match in re.finditer(r"\bbash\s+([^\s`]+\.sh)([^`\n]*)", body):
        scripts[script_path(match[1])] = " ".join(match[2].split())
    # Explicit frontmatter contracts take precedence over procedure examples.
    for entry in skills_registry._list(meta.get("scripts")):
        parts = entry.split(maxsplit=1)
        if parts:
            scripts[script_path(parts[0])] = parts[1] if len(parts) > 1 else ""
    stop = skills_registry._sections(body).get("Stop conditions", "").lower()
    negative = bool(re.search(
        r"(?:never|do not|don't|must not|no)\s+(?:(?:git|create|a)\s+)*commit|\buncommitted\b", stop))
    positive = bool(re.search(r"\bcommit(?:ted)?\b", stop)) and not negative
    return shape, scripts, -1 if negative else 1 if positive else 0


def _conflict(left, right, records, contracts, role):
    if (right in records.get(left, {}).get("conflicts_with", [])
            or left in records.get(right, {}).get("conflicts_with", [])):
        return "conflicts_with"
    a, b = contracts[left], contracts[right]
    if a[0] and b[0] and a[0] != b[0]:
        return "output_contract"
    if any(a[1][path] != b[1][path] for path in a[1].keys() & b[1].keys()):
        return "script_arguments"
    if role in ("execute", "codex_execute") and a[2] * b[2] == -1:
        return "commit_rule"
    return None


def _model(task, role, cfg):
    if task.get("executor"):
        return task["executor"]
    if task.get("tier"):
        return cfg.get("models", {}).get(task["tier"], task["tier"])
    if role in ("execute", "codex_execute"):
        from .pool import Pool
        chosen = Pool().pick_executor("execute", task.get("complexity", 1), task=task)
        return chosen.id if chosen else None
    return None


def compose(task, role, cfg=None) -> Specialist:
    """Compose once for this spawn; selection and model routing remain owned upstream."""
    if cfg is None:
        from .pool import config
        cfg = config()
    choice = skill_router.select(task, role, cfg=cfg)
    records = skills_registry.load().get("skills", {})
    selected = list(choice["selected"])
    unavailable, unknown, conflicts = [], [], []
    declared = {}
    offered, minimal = [], {"keep": [], "mandatory": []}
    if role == "planner":
        selected = list(choice["mandatory"])
        choice["reason"] = "planner tools not catalogued"
    elif role in WORKER_ROLES:
        catalog_role = "review" if role == "security_review" else role
        offered = tool_catalog.disclosed(catalog_role)
        minimal = tool_catalog.minimal_set(task, catalog_role)
        for skill_id in list(selected):
            declared[skill_id] = set()
            missing = []
            for tool in records.get(skill_id, {}).get("tools", []):
                normalized = normalize_tool(tool, offered)
                if normalized is None:
                    unknown.append({"id": skill_id, "tool": tool, "reason": "unknown_tool"})
                    continue
                if not normalized or any(item not in offered for item in normalized):
                    missing.append(tool)
                    unavailable.append({"id": skill_id, "tool": tool, "reason": "unavailable_tool"})
                else:
                    declared[skill_id].update(normalized)
            if missing:
                selected.remove(skill_id)
                choice["rejected"].append({"id": skill_id, "reason": "tool_unavailable", "tools": missing})
    if role != "planner":
        mandatory = set(choice["mandatory"])
        strength = lambda item: skill_router._strength(choice["triggers"].get(item, []))
        ordered = sorted(selected, key=lambda item: (-strength(item), item not in mandatory, item))
        contracts = {item: _contracts(records.get(item, {})) for item in selected}
        kept = []
        for item in ordered:
            winner = next((other for other in kept if _conflict(other, item, records, contracts, role)), None)
            if winner is None:
                kept.append(item)
                continue
            rule = ("trigger_strength" if strength(winner) != strength(item) else
                    "mandatory" if (winner in mandatory) != (item in mandatory) else "lexical")
            conflicts.append({"kept": winner, "dropped": item, "rule": rule,
                              "cause": _conflict(winner, item, records, contracts, role)})
            choice["rejected"].append({"id": item, "reason": "conflict"})
        selected = kept
    selected = sorted(selected)
    tools = sorted(set(minimal["keep"]) | set(minimal["mandatory"]) |
                   {tool for item in selected for tool in declared.get(item, ())})
    requirements = sorted({kind for item in selected
                           for kind in records.get(item, {}).get("context_requirements", [])})
    name = role + ("+" + "+".join(item.rsplit("/", 1)[-1] for item in selected) if selected else "")
    reviewed_id, exclusion = None, "not a review role"
    if role in ("review", "security_review"):
        inputs = task.get("inputs") or []
        exclusion = "skipped: inputs empty"
        if inputs:
            exclusion = "skipped: reviewed input is not an execute task"
            try:
                reviewed = bus.get(inputs[0]) if isinstance(inputs[0], str) else None
            except KeyError:
                reviewed = None
            if reviewed and reviewed.get("role") == "execute":
                reviewed_id = reviewed["id"]
                exclusion = f"excluded task:{reviewed_id}:review:"
    choice["selected"] = selected
    for level in (0, 2):
        choice[f"tokens_selected_l{level}"] = sum(
            int(records.get(item, {}).get(f"est_tokens_l{level}") or 0) for item in selected)
    choice["specialist"] = {
        "name": name, "tools_added": sorted(set(tools) - set(minimal["keep"])),
        "tools_unavailable": unavailable, "tools_unknown": unknown, "tools": tools,
        "context_requirements": requirements, "conflicts": conflicts,
        "review_evidence": exclusion,
    }
    return Specialist(role, [{"id": item, "version": records.get(item, {}).get("version", "unknown")}
                             for item in selected], requirements, tools, _model(task, role, cfg),
                      name, conflicts, choice, reviewed_id)
