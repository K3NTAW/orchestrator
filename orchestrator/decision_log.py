"""Structured, bounded evidence for orchestrator decisions.

Generic mappings: model_routing~routing, scheduling~wave/jev_sched,
workflow_strategy~strategy, and planner_routing~planner_route. Legacy call sites
keep their kinds; new context-program code uses the generic kinds. skill_selection
has no legacy synonym. retrieval rows are memory retrievals: candidates carry ids,
tiers and scores; extra carries legacy_ids, tokens_legacy, tokens_tiered and hot_fresh.
"""

import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import schedlog


CONTEXT_KINDS = (
    "context_selection",
    "evidence_reuse",
    "retrieval",
    "tool_disclosure",
    "instruction_loading",
    "model_routing",
    "planner_routing",
    "scheduling",
    "action_gate",
    "workflow_strategy",
    "handoff",
    "skill_selection",
)

KINDS = (
    "output_contract",
    "harness_depth",
    "routing",
    "wave",
    "capacity",
    "concurrency",
    "merge_pressure",
    "stale",
    "jev_sched",
    "allocation",
    "strategy",
    "speculation",
    "scout",
    "review_plan",
    "planner_route",
) + CONTEXT_KINDS

_MAX_TEXT = 2000
_TRUNCATION_MARKER = "...[truncated]"


class ExplainRows(list):
    """A list of explanations carrying the count of malformed input lines."""

    def __init__(self, rows=(), malformed_count=0):
        super().__init__(rows)
        self.malformed_count = malformed_count

    @property
    def malformed(self):
        return self.malformed_count


def _bounded(value):
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            return value[: _MAX_TEXT - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return value
    if isinstance(value, list):
        return [_bounded(item) for item in value]
    if isinstance(value, tuple):
        return [_bounded(item) for item in value]
    if isinstance(value, dict):
        return {_bounded(key): _bounded(item) for key, item in value.items()}
    return value


def _directory(root=None):
    if root is None:
        return schedlog.SCHED_DIR
    root = Path(root)
    return root / "runs" / "sched"


@contextmanager
def _using_root(root):
    if root is None:
        yield
        return
    original = schedlog.SCHED_DIR
    schedlog.SCHED_DIR = _directory(root)
    try:
        yield
    finally:
        schedlog.SCHED_DIR = original


def _append(row, root=None):
    row = dict(row)
    row.setdefault("ts", time.time())
    with _using_root(root):
        schedlog.append("decisions", row)
    return row


def record(
    kind,
    subject,
    *,
    candidates,
    hard_constraints,
    deterministic,
    historical=None,
    jev=None,
    selected,
    rejected=None,
    reason,
    confidence=None,
    n=None,
    mode=None,
    role=None,
    extra=None,
    root=None,
):
    """Append one bounded decision evidence row and return it."""
    if kind not in KINDS:
        raise ValueError(f"invalid decision kind: {kind}")

    if rejected is None:
        selected_values = selected if isinstance(selected, (list, tuple, set)) else [selected]
        rejected = [candidate for candidate in candidates if candidate not in selected_values]

    row = {
        "kind": kind,
        "subject": subject,
        "candidates": candidates,
        "hard_constraints": hard_constraints,
        "deterministic": deterministic,
        "historical": historical,
        "jev": jev,
        "selected": selected,
        "rejected": rejected,
        "reason": reason,
        "confidence": confidence,
        "n": n,
        "mode": mode,
        "extra": extra,
    }
    if role is not None:
        row["role"] = role
    return _append(_bounded(row), root=root)


def outcome(subject, kind, root=None, **fields):
    """Append an outcome linked to a prior decision kind."""
    row = {
        "kind": "outcome",
        "subject": subject,
        "decision_kind": kind,
        **fields,
    }
    return _append(_bounded(row), root=root)


def _read(root=None):
    with _using_root(root):
        return schedlog.read_with_malformed("decisions")


def read_all(root=None, since_ts=None):
    rows, _malformed = _read(root)
    if since_ts is None:
        return rows
    return [row for row in rows if row.get("ts", 0) >= since_ts]


def explain(subject, kinds=None, root=None):
    """Return decisions newest first, assigning outcomes to the latest predecessor."""
    rows, malformed = _read(root)
    wanted = set(kinds) if kinds is not None else None
    decisions = []
    latest = {}
    # Stable ordering preserves append order when timestamps tie.
    for row in sorted(rows, key=lambda row: row.get("ts", 0)):
        if row.get("subject") != subject:
            continue
        if row.get("kind") == "outcome":
            decision = latest.get(row.get("decision_kind"))
            if decision is not None:
                decision["outcomes"].append(dict(row))
            continue
        if wanted is not None and row.get("kind") not in wanted:
            continue
        decision = dict(row)
        decision["outcomes"] = []
        latest[row.get("kind")] = decision
        decisions.append(decision)
    decisions.sort(key=lambda row: row.get("ts", 0), reverse=True)
    return ExplainRows(decisions, malformed)


def _timestamp(value):
    try:
        return datetime.fromtimestamp(float(value), ZoneInfo("Europe/Zurich")).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _rejected_summary(rejected):
    if isinstance(rejected, dict):
        return ", ".join(f"{item}: {why}" for item, why in rejected.items())
    if isinstance(rejected, (list, tuple, set)):
        return ", ".join(map(str, rejected))
    return str(rejected) if rejected is not None else "none"


def format_explain(rows):
    """Format explanation rows as concise, human-readable evidence lines."""
    lines = []
    for row in rows:
        lines.append(f"{_timestamp(row.get('ts'))} {row.get('kind')} subject={row.get('subject')}")
        lines.append(f"selected: {row.get('selected')}")
        lines.append(f"reason: {row.get('reason')}")
        lines.append(f"rejected: {_rejected_summary(row.get('rejected'))}")
        confidence = row.get("confidence")
        n = row.get("n")
        if confidence is not None or n is not None:
            lines.append(f"confidence/n: {confidence}/{n}")
        outcomes = row.get("outcomes", ())
        if outcomes:
            summary = "; ".join(
                ", ".join(f"{key}={value}" for key, value in outcome.items() if key not in {"ts", "kind", "subject", "decision_kind"})
                for outcome in outcomes
            )
            lines.append(f"outcome: {summary}")
    return "\n".join(lines)
