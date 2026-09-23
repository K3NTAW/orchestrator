"""Structured, bounded evidence for orchestrator decisions.

Generic mappings: model_routing~routing, scheduling~wave/jev_sched,
workflow_strategy~strategy, and planner_routing~planner_route. Legacy call sites
keep their kinds; new context-program code uses the generic kinds. skill_selection
has no legacy synonym. retrieval rows are memory retrievals: candidates carry ids,
tiers and scores; extra carries legacy_ids, tokens_legacy, tokens_tiered and hot_fresh.
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
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
    "steering",
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
    if kind == "steering":
        validate_steering(row)
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


STEERING_KEYS = {"trigger", "severity", "critical", "action", "evidence_hash", "message_chars"}


def validate_steering(row):
    """Steering metadata lives in extra, following other decision kinds."""
    def has_message(value):
        if isinstance(value, dict):
            return "message" in value or any(has_message(v) for v in value.values())
        return isinstance(value, (tuple, list)) and any(has_message(v) for v in value)
    required = {"candidates", "hard_constraints", "deterministic", "reason", "selected", "mode"}
    if not required <= row.keys() or not STEERING_KEYS <= (row.get("extra") or {}).keys():
        raise ValueError("missing steering metadata")
    if has_message(row):
        raise ValueError("steering rows must omit message text")


GROUPED_SCAN_LIMIT = 2000


def recent(root=None, limit=500, *, kind=None, subject=None, subjects=None):
    """Read newest matching rows, returning each result in chronological order.

    Grouped reads retain at most limit rows per subject and inspect at most
    GROUPED_SCAN_LIMIT lines total, including malformed and unrelated lines.
    """
    grouped = {subject: [] for subject in subjects} if subjects is not None else None
    if limit <= 0 or grouped == {}:
        return grouped if grouped is not None else []
    completed = set()
    rows, scanned = [], 0
    def result():
        return {tid: own[::-1] for tid, own in grouped.items()} if grouped is not None else rows[::-1]
    path = _directory(root) / "decisions.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            pos, pending = stream.tell(), b""
            while pos:
                size = min(pos, 8192)
                pos -= size
                stream.seek(pos)
                lines = (stream.read(size) + pending).split(b"\n")
                pending = lines.pop(0) if pos else b""
                for line in reversed(lines):
                    if grouped is not None and scanned >= GROUPED_SCAN_LIMIT:
                        return result()
                    scanned += 1
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if (isinstance(row, dict) and (kind is None or row.get("kind") == kind)
                            and (subject is None or row.get("subject") == subject)):
                        if grouped is not None:
                            tid = row.get("subject")
                            if tid not in grouped or tid in completed:
                                continue
                            own = grouped[tid]
                            own.append(row)
                            if len(own) >= limit:
                                completed.add(tid)
                            if len(completed) == len(grouped):
                                return result()
                            continue
                        rows.append(row)
                        if len(rows) >= limit:
                            return result()
    except FileNotFoundError:
        pass
    return result()


def _tail_lines(path, limit=200, max_bytes=2 * 1024 * 1024):
    """Read a bounded suffix, never loading an entire decision archive."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            position = stream.tell()
            chunks, size, newlines = [], 0, 0
            while position and size < max_bytes and newlines <= limit:
                count = min(position, 8192, max_bytes - size)
                position -= count
                stream.seek(position)
                chunk = stream.read(count)
                chunks.append(chunk)
                size += len(chunk)
                newlines += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
            if position:
                data = data.partition(b"\n")[2]
            return data.splitlines()[-limit:]
    except FileNotFoundError:
        return []


def last_row(kind, *, role, exclude_subject, require_key):
    """Latest qualifying row in a 200-line, two-local-day bounded tail.

    The current schedlog uses one decisions.jsonl archive; date filtering keeps
    that layout compatible with daily decision files without changing writers.
    """
    today = datetime.now(ZoneInfo("Europe/Zurich")).date()
    dates = (today, today - timedelta(days=1))
    paths = [_directory() / "decisions.jsonl"]
    paths.extend(_directory() / f"decisions-{day.isoformat()}.jsonl" for day in dates)
    rows = []
    remaining = 200
    for path in paths:
        if not remaining:
            break
        lines = _tail_lines(path, limit=remaining)
        remaining -= len(lines)
        for line in lines:
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                day = datetime.fromtimestamp(float(row["ts"]), ZoneInfo("Europe/Zurich")).date()
                if day in dates:
                    rows.append(row)
            except (ValueError, KeyError, TypeError, OSError, OverflowError):
                continue
    for row in sorted(rows, key=lambda row: row["ts"], reverse=True)[:200]:
        if (row.get("kind") != kind or row.get("subject") == exclude_subject
                or row.get("reason") == "hidden_tool_requested"
                or (row.get("deterministic") or {}).get("role") != role):
            continue
        value = row
        for key in require_key.split("."):
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            return row
    return None
