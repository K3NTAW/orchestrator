"""Pure, compact packets for Planner decisions and Opus-to-Fable escalation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .planner_taxonomy import DECISION_TYPES


# Kept local so this pure module does not import planner_runs and its bus/file dependencies.
_SCOUTS_DONE_OPTIONS = {
    "synthesize_now": "enough scouts have reported to synthesize the plan now",
    "wait_for_more": "wait for more scouts to finish before synthesizing",
    "drop_low_confidence": "drop the low-confidence scout findings and synthesize with what's left",
}
_NEXT_ACTION_OPTIONS = {
    "fix_round": "write a fix-round task from the review comments",
    "respec": "the spec itself is wrong; recreate the task",
    "escalate": "a human must decide: auth, billing, data deletion, or three failed rounds",
    "noop": "the hold is stale or already superseded; nothing to do",
}

_EXPECTED = {
    "initial_goal_plan": "write plan.md and atomic specs",
    "scout_results": "write plan.md and atomic specs",
    "fix_strategy": "write one fix-round spec with depends_on=[held]",
    "held_task": "write one fix-round spec with depends_on=[held]",
    "architectural_replan": "respec or split; state why",
    "closable_goal": "open PR goal/<id> -> main or say why not",
    "ambiguous_requirement": "state the ambiguity under Needs the human",
    "retrospective": "record.sh add",
    "other": "decide",
}
_KIND_FALLBACK = {
    "scouts_done": "scout_results",
    "held": "held_task",
    "closable": "closable_goal",
    "retrospective": "retrospective",
}


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _clean(value, limit=None):
    value = str(value or "")
    if limit is not None:
        value = value[:limit]
    return re.sub(r"`{3,}", "[backticks elided]", value)


def _fenced(label, value, limit=None):
    """Render untrusted content as data (copied locally from planner_runs)."""
    return [f"{label}:", "```data", _clean(value, limit), "```"]


def _decision_type(section):
    classification = _mapping(_mapping(section).get("classification"))
    value = classification.get("decision_type")
    if value not in DECISION_TYPES:
        value = _KIND_FALLBACK.get(_mapping(_mapping(section).get("point")).get("kind"), "other")
    return value if value in DECISION_TYPES else "other"


def _route_value(route, key, default=None):
    if isinstance(route, Mapping):
        return route.get(key, default)
    return getattr(route, key, default)


def _section_info(sections):
    result = []
    for number, raw in enumerate(sections or (), 1):
        section = _mapping(raw)
        point = _mapping(section.get("point"))
        decision_type = _decision_type(section)
        identifier = point.get("task_id") or point.get("goal_id") or "?"
        result.append((number, section, decision_type, _clean(identifier, 120)))
    return result


def _header_lines(info):
    return [
        f"Section {number} ({identifier}): {decision_type} — expected output: {_EXPECTED[decision_type]}"
        for number, _section, decision_type, identifier in info
    ]


def _goal_lines(goal):
    goal = _mapping(goal)
    lines = ["2 Goal", f"id: {_clean(goal.get('id') or goal.get('goal_id'), 120)}"]
    lines += _fenced("title", goal.get("title"), 200)
    lines.append(f"complexity: {_clean(goal.get('complexity'), 40)}")
    lines += _fenced("spec", goal.get("spec"), 600)
    return lines


def _changes_lines(previous, changes):
    lines = ["3 Changes since previous decision"]
    if previous is None:
        return lines + ["first Planner decision for this goal"]
    if changes:
        for change in list(changes)[:20]:
            change = _mapping(change)
            lines.append(
                f"- {_clean(change.get('task'), 120)} {_clean(change.get('field'), 80)}: "
                f"{_clean(change.get('from'), 160)} -> {_clean(change.get('to'), 160)} "
                f"({_clean(change.get('ts'), 80)})"
            )
    else:
        lines.append("state changed since previous decision, no field-level diff available")
    lines.append(f"previous state_version: {_mapping(previous).get('state_version')}")
    return lines


def _relevant_lines(info, include_failures=True):
    lines = ["4 Relevant task state"]
    for number, section, _decision_type_value, identifier in info:
        task = _mapping(section.get("task"))
        lines.append(f"Section {number} ({identifier})")
        lines += _fenced("title", task.get("title"), 200)
        lines += _fenced("status", task.get("status"), 40)
        lines += _fenced("hold_reason", task.get("hold_reason"), 300)
        if include_failures and section.get("failures"):
            lines += _fenced("failures", section.get("failures"), 1500)
        reviews = []
        for review in list(section.get("reviews") or ())[:10]:
            review = _mapping(review)
            reviews.append(
                f"{_clean(review.get('path'), 200)}:{_clean(review.get('line'), 40)} "
                f"{_clean(review.get('issue'), 200)}"
            )
        if reviews:
            lines += _fenced("review comments", "\n".join(reviews))
        statuses = []
        for dependency in section.get("depends_on_statuses") or ():
            dependency = _mapping(dependency)
            statuses.append(f"{_clean(dependency.get('id'), 120)}: {_clean(dependency.get('status'), 40)}")
        if statuses:
            lines += _fenced("depends_on statuses", "\n".join(statuses))
    return lines


def _scout_lines(scout_findings):
    lines = ["5 Scout findings"]
    findings = []
    for item in list(scout_findings or ())[:10]:
        item = _mapping(item)
        findings.append(
            f"{_clean(item.get('finding'), 200)} "
            f"({_clean(item.get('source'), 160)}, {_clean(item.get('confidence'), 40)})"
        )
    return lines + _fenced("findings", "\n".join(findings))


def _memory_lines(memory_hits):
    titles = [_clean(_mapping(hit).get("title"), 200) for hit in list(memory_hits or ())[:10]]
    return ["6 Memory"] + _fenced("titles", "\n".join(titles))


def _alternatives_lines(info):
    lines = ["7 Unresolved alternatives"]
    for number, section, decision_type, _identifier in info:
        lines.append(f"Section {number}")
        evidence = [_clean(item, 300) for item in list(_route_value(section.get("route"), "evidence", ()) or ())[:20]]
        lines += _fenced("route evidence", "\n".join(evidence))
        if decision_type in ("initial_goal_plan", "scout_results"):
            options = _SCOUTS_DONE_OPTIONS
        elif decision_type in ("held_task", "fix_strategy", "architectural_replan"):
            options = _NEXT_ACTION_OPTIONS
        else:
            options = {}
        if options:
            lines.append("option names: " + ", ".join(options))
    return lines


def _required_lines(info):
    lines = ["8 Required output"]
    for number, _section, decision_type, _identifier in info:
        lines.append(f"Section {number}: {_EXPECTED[decision_type]}")
    lines.append("Do not restate this packet")
    return lines


def _render(parts, order):
    return "\n".join(line for name in order if name in parts for line in parts[name])


def _finish(parts, order, cap_chars, *, delta, section_names, removable):
    try:
        cap = max(0, int(cap_chars))
    except (TypeError, ValueError):
        cap = 6000
    truncated = []
    text = _render(parts, order)
    for name in removable:
        if len(text) <= cap:
            break
        if name == "Relevant task state failures":
            if parts.get("Relevant task state") != parts.get("Relevant task state without failures"):
                parts["Relevant task state"] = parts["Relevant task state without failures"]
                truncated.append(name)
        elif name in parts:
            del parts[name]
            truncated.append(name)
        text = _render(parts, order)
    if len(text) > cap:
        text = text[:cap]
        truncated.append("hard_cap")
    details = meta(text)
    return {
        "text": text,
        **details,
        "sections": [name for name in section_names if name in parts],
        "truncated": truncated,
        "delta": delta,
    }


def build(sections, *, goal, previous=None, changes=None, scout_findings=None,
          memory_hits=None, cap_chars=6000):
    """Build a bounded decision packet exclusively from caller-supplied data."""
    info = _section_info(sections)
    include_scouts = any(item[2] in ("initial_goal_plan", "scout_results") for item in info)
    parts = {
        "Decision header": ["1 Decision header", *_header_lines(info)],
        "Goal": _goal_lines(goal),
        "Changes": _changes_lines(previous, changes),
        "Relevant task state": _relevant_lines(info),
        "Relevant task state without failures": _relevant_lines(info, False),
        "Unresolved alternatives": _alternatives_lines(info),
        "Required output": _required_lines(info),
    }
    if include_scouts:
        parts["Scout findings"] = _scout_lines(scout_findings)
    if memory_hits:
        parts["Memory"] = _memory_lines(memory_hits)
    order = ["Decision header", "Goal", "Changes", "Relevant task state", "Scout findings",
             "Memory", "Unresolved alternatives", "Required output"]
    section_names = order[:]
    return _finish(
        parts, order, cap_chars, delta=previous is not None, section_names=section_names,
        removable=["Memory", "Scout findings", "Unresolved alternatives", "Changes",
                   "Relevant task state failures", "Relevant task state"],
    )


def _opus_lines(opus_decision):
    source = _mapping(opus_decision)
    tasks = []
    for raw in list(source.get("tasks_proposed") or ())[:10]:
        task = _mapping(raw)
        tasks.append({
            "title": _clean(task.get("title"), 120),
            "scope": [_clean(item, 200) for item in list(task.get("scope") or ())[:10]],
        })
    whitelisted = {
        "proposed_action": _clean(source.get("proposed_action"), 400),
        "summary": _clean(source.get("summary"), 800),
        "tasks_proposed": tasks,
        "confidence": source.get("confidence"),
        "needs_fable": source.get("needs_fable"),
        "unresolved": [_clean(item, 200) for item in list(source.get("unresolved") or ())[:10]],
    }
    return ["Opus proposed decision (data)", *_fenced("decision", json.dumps(whitelisted, sort_keys=True))]


def escalation_packet(*, goal, original_sections, opus_decision, unresolved,
                      conflicting_evidence=None, scout_findings=None, memory_hits=None,
                      reason, cap_chars=6000):
    """Build a compact packet asking Fable to solve only Opus's unresolved work."""
    info = _section_info(original_sections)
    include_scouts = any(item[2] in ("initial_goal_plan", "scout_results") for item in info)
    header = [
        f"Escalation reason: {_clean(reason, 300)}. Solve only the unresolved items.",
        *_header_lines(info),
    ]
    parts = {
        "Decision header": header,
        "Goal": _goal_lines(goal),
        "Opus proposed decision": _opus_lines(opus_decision),
        "Unresolved": ["Unresolved", *_fenced("items", "\n".join(_clean(x, 200) for x in list(unresolved or ())[:10]))],
        "Relevant task state": _relevant_lines(info),
        "Relevant task state without failures": _relevant_lines(info, False),
        "Unresolved alternatives": _alternatives_lines(info),
        "Required output": _required_lines(info),
    }
    if conflicting_evidence:
        parts["Conflicting evidence"] = [
            "Conflicting evidence",
            *_fenced("items", "\n".join(_clean(x, 200) for x in list(conflicting_evidence)[:10])),
        ]
    if include_scouts:
        parts["Scout findings"] = _scout_lines(scout_findings)
    if memory_hits:
        parts["Memory"] = _memory_lines(memory_hits)
    order = ["Decision header", "Goal", "Opus proposed decision", "Unresolved", "Conflicting evidence",
             "Relevant task state", "Scout findings", "Memory", "Unresolved alternatives", "Required output"]
    return _finish(
        parts, order, cap_chars, delta=False, section_names=order,
        removable=["Memory", "Scout findings", "Unresolved alternatives",
                   "Relevant task state failures", "Relevant task state"],
    )


def invalidated(previous_state_version, current_state_version):
    """Return true when cached state is missing or no longer current."""
    return (previous_state_version is None or current_state_version is None
            or previous_state_version != current_state_version)


def meta(text):
    """Return length, rough token count, and stable hash for exact packet text."""
    text = str(text)
    chars = len(text)
    return {
        "chars": chars,
        "est_tokens": chars // 4,
        "hash": hashlib.sha256(text.encode()).hexdigest()[:12],
    }
