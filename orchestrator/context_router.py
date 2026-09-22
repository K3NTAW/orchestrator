"""Pure, deterministic routing of canonical evidence into context levels."""
from dataclasses import dataclass
import fnmatch

from . import evidence
from .failures import _test_ids as test_ids


LEVELS = ("HIDE", "SHORT", "LONG", "FULL")


@dataclass(frozen=True)
class Routed:
    evidence_id: str
    level: str
    reason: str
    tokens_full: int
    tokens_at_level: int


@dataclass(frozen=True)
class RoutedPacket:
    items: list
    candidate_tokens: int
    routed_tokens: int
    reduction_ratio: float
    ambiguous_ids: list
    profile: str
    rules_version: str = "v1"


def _path(ev):
    return ev.location.split(":", 1)[0]


def _text(ev, level):
    if level == "HIDE":
        return ""
    if level == "SHORT":
        return f"- {ev.location} — {ev.summary_short}"
    if level == "LONG":
        return f"- {ev.location}\n  {ev.summary_long}"
    return f"- {ev.location}\n```\n{ev.content}\n```"


def _security_match(path, cfg):
    review = (cfg or {}).get("review") or {}
    return any(fnmatch.fnmatch(path, pattern) for pattern in review.get("security_paths", []))


def _choice(task, ev, role, head_sha, cfg, failing_ids):
    relevance = evidence.relevance_for(ev, task)
    scope = task.get("scope") or task.get("write_scope") or []
    in_scope = _path(ev) in scope

    if ev.source_type == "source_chunk" and in_scope and role in ("execute", "scout"):
        return "FULL", "in_scope_file"
    if ev.source_type == "source_chunk" and in_scope and role in ("review", "security_review"):
        return "LONG", "in_scope_file"
    if (role == "security_review" and ev.source_type == "source_chunk"
            and _security_match(_path(ev), cfg)):
        return "FULL", "security_path"
    if role == "planner" and ev.source_type == "source_chunk":
        return "SHORT", "read_scope"
    if ev.source_type == "test_result" and role in ("review", "security_review"):
        return "FULL", "failing_output"
    if ev.source_type == "test_result" and (
            any(test_id in ev.location for test_id in failing_ids)
            or "FAIL" in ev.content or "ERROR" in ev.content):
        return "FULL", "failing_output"
    task_id = task.get("id", "")
    if (ev.source_type == "architecture_note"
            and ev.location in (f"task:{task_id}:spec", f"task:{task_id}:acceptance")):
        return "FULL", "acceptance"
    if role == "planner" and ev.source_type in ("decision", "architecture_note"):
        return "LONG", "dependency"
    if (not relevance["scope_match"] and relevance["title_terms"] == 0
            and relevance["path_terms"] == 0
            and ev.source_type in ("memory_entry", "decision", "previous_result", "review_finding")):
        return "HIDE", "unrelated_memory"
    if (head_sha is not None and not evidence.fresh(ev, head_sha)
            and ev.source_type in ("source_chunk", "test_result")):
        return "HIDE", "stale"
    if ev.source_type == "source_chunk":
        return "SHORT", "read_scope"
    if role in ("review", "security_review") and ev.source_type in ("review_finding", "previous_result"):
        return "SHORT", "dependency"
    if ev.source_type in ("previous_result", "scout_finding", "decision", "memory_entry") \
            and (relevance["scope_match"] or relevance["title_terms"]):
        return "LONG", "dependency"
    return "LONG", "ambiguous"


def route(task, candidates, *, role, head_sha=None, cfg=None):
    profile = role if role in ("execute", "review", "security_review", "planner", "scout") else "execute"
    failing_ids = test_ids((task.get("resume_hint") or {}).get("failures")) or []
    items, ambiguous = [], []
    for ev in candidates:
        level, reason = _choice(task, ev, profile, head_sha, cfg, failing_ids)
        full_tokens = len(_text(ev, "FULL")) // 4
        routed_tokens = len(_text(ev, level)) // 4
        items.append(Routed(ev.id, level, reason, full_tokens, routed_tokens))
        if reason == "ambiguous":
            ambiguous.append(ev.id)
    candidate_tokens = sum(item.tokens_full for item in items)
    routed_tokens = sum(item.tokens_at_level for item in items)
    return RoutedPacket(items, candidate_tokens, routed_tokens,
                        routed_tokens / candidate_tokens if candidate_tokens else 1.0,
                        ambiguous, profile)


def _lookup(source, evidence_id):
    if hasattr(source, "get"):
        return source.get(evidence_id)
    if callable(source):
        return source(evidence_id)
    return source[evidence_id]


def render(routed_packet, evidence_pool_or_lookup):
    visible = [item for item in routed_packet.items if item.level != "HIDE"]
    visible.sort(key=lambda item: ({"FULL": 0, "LONG": 1, "SHORT": 2}[item.level],
                                  _lookup(evidence_pool_or_lookup, item.evidence_id).location))
    lines = ["## evidence (routed v1)"]
    lines.extend(_text(_lookup(evidence_pool_or_lookup, item.evidence_id), item.level)
                 for item in visible)
    lines.append(f"hidden: {sum(item.level == 'HIDE' for item in routed_packet.items)}")
    return "\n".join(lines)


def decision_row(task, routed_packet, *, mode):
    counts = {level: sum(item.level == level for item in routed_packet.items) for level in LEVELS}
    return {
        "kind": "context_selection",
        "subject": task["id"],
        "candidates": [f"{item.evidence_id}:{item.level}" for item in routed_packet.items],
        "hard_constraints": [item.reason for item in routed_packet.items if item.level == "FULL"],
        "deterministic": {**counts, "candidate_tokens": routed_packet.candidate_tokens,
                          "routed_tokens": routed_packet.routed_tokens,
                          "reduction_ratio": routed_packet.reduction_ratio,
                          "ambiguous": len(routed_packet.ambiguous_ids)},
        "selected": "routed v1",
        "reason": f"{routed_packet.profile} profile rules v1",
        "mode": mode,
        "extra": None,
    }

