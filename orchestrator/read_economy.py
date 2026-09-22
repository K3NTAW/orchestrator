"""Deterministic read/search economy classification and shadow scorecard."""
import json
import os
from pathlib import Path


READ_TOOLS = frozenset({"Read"})
SEARCH_TOOLS = frozenset({"Grep", "Glob"})
EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def _input(call):
    return call.get("input") or call.get("tool_input") or {}


def _name(call):
    return call.get("name") or call.get("tool") or call.get("tool_name") or ""


def _path(call):
    data = _input(call)
    return str(data.get("file_path") or data.get("path") or data.get("notebook_path") or
               call.get("path") or call.get("tool_target") or "")


def _stat(call):
    if call.get("mtime") is not None or call.get("size") is not None:
        return call.get("mtime"), call.get("size")
    try:
        value = os.stat(_path(call))
        return value.st_mtime_ns, value.st_size
    except (OSError, TypeError):
        return None, None


def _search_signature(call):
    data = _input(call)
    ignored = {"result_size", "output_size", "result", "output"}
    return tuple(sorted((key, json.dumps(value, sort_keys=True))
                        for key, value in data.items() if key not in ignored))


def _result_size(call):
    for key in ("result_size", "output_size", "tokens_estimate"):
        value = call.get(key)
        if value is None:
            value = _input(call).get(key)
        if isinstance(value, int):
            return value
    result = call.get("result") or call.get("output")
    return len(result) // 4 if isinstance(result, str) else None


def _read_tokens(call, size):
    if size is None:
        return None
    data = _input(call)
    offset = max(0, int(data.get("offset") or 0))
    remaining = max(0, int(size) - offset)
    if data.get("limit") is not None:
        remaining = min(remaining, max(0, int(data["limit"])))
    return remaining // 4


def _has_evidence(evidence, path, head_sha):
    if evidence is None or head_sha is None or not path:
        return False
    try:
        items = evidence.by_type("source_chunk")
    except (AttributeError, TypeError):
        items = [item for item in evidence if getattr(item, "source_type", None) == "source_chunk"]
    normalized = os.path.normpath(path)
    for item in items:
        location = str(item.location).split(":", 1)[0]
        if (os.path.normpath(location) == normalized or normalized.endswith(os.sep + location)) \
                and evidence.fresh(item, head_sha):
            return True
    return False


def classify(call, history, *, evidence=None, head_sha=None):
    """Classify *call* against earlier calls without consulting Jev."""
    name, path = _name(call), _path(call)
    prior_path = [old for old in history if _path(old) == path]
    kind = "other"
    tokens = None

    if name in READ_TOOLS:
        _mtime, size = _stat(call)
        tokens = _read_tokens(call, size)
        prior_reads = [old for old in prior_path if _name(old) in READ_TOOLS]
        if _has_evidence(evidence, path, head_sha):
            kind = "evidence_available"
        elif not prior_reads:
            kind = "first_read"
        else:
            old_mtime, old_size = _stat(prior_reads[-1])
            kind = ("repeated_read_unchanged" if (old_mtime, old_size) == (_mtime, size)
                    else "repeated_read_changed")
    elif name in SEARCH_TOOLS:
        prior_searches = [old for old in prior_path if _name(old) == name]
        signature = _search_signature(call)
        exact = next((old for old in reversed(prior_searches)
                      if _search_signature(old) == signature), None)
        if exact is not None:
            kind, tokens = "repeated_search", _result_size(exact)
        else:
            pattern = str(_input(call).get("pattern") or "")
            narrower = next((old for old in reversed(prior_searches)
                             if str(_input(old).get("pattern") or "") in pattern), None)
            if narrower is not None:
                kind, tokens = "narrower_search", _result_size(narrower)

    return {"kind": kind, "tokens_estimate": tokens,
            "would_suppress": kind in ("repeated_read_unchanged", "repeated_search")}


def _empty():
    return {"reads": 0, "repeated_read_unchanged": 0, "repeated_search": 0,
            "evidence_available": 0, "tokens_avoidable": 0, "jev_calls_avoided": 0,
            "false_suppression_proxy": 0}


def _later_transcript_edit(row):
    path = row.get("transcript_path")
    if not path or row.get("call_index") is None:
        return False
    calls = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            content = (json.loads(line).get("message") or {}).get("content") or []
        except (ValueError, TypeError):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append({"name": block.get("name"), "input": block.get("input") or {}})
    start = int(row["call_index"]) + 1
    target = os.path.normpath(str(row.get("tool_target") or ""))
    return any(_name(call) in EDIT_TOOLS and os.path.normpath(_path(call)) == target
               for call in calls[start:start + 5])


def summary(root):
    """Aggregate gate shadow rows per role and task class."""
    root = Path(root)
    log = root / "runs" / "jev" / "gate.jsonl"
    rows = []
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    tasks = {}
    for row in rows:
        task_id = row.get("task")
        if task_id not in tasks:
            try:
                tasks[task_id] = json.loads((root / "tasks" / f"{task_id}.json").read_text())
            except (OSError, ValueError, TypeError):
                tasks[task_id] = {}

    out = {"by_role": {}, "by_task_class": {}}
    for index, row in enumerate(rows):
        kind = row.get("read_kind")
        if not kind:
            continue
        task = tasks.get(row.get("task"), {})
        role = row.get("role") or task.get("role") or "unknown"
        constraints = task.get("constraints") or {}
        task_class = row.get("task_class") or task.get("task_class") or constraints.get("task_class") or "unknown"
        target = row.get("tool_target")
        later = rows[index + 1:index + 6]
        false_proxy = bool(row.get("would_suppress") and (any(
            candidate.get("session") == row.get("session")
            and candidate.get("tool") in EDIT_TOOLS
            and candidate.get("tool_target") == target for candidate in later)
            or _later_transcript_edit(row)))
        for group, key in (("by_role", role), ("by_task_class", task_class)):
            item = out[group].setdefault(key, _empty())
            item["reads"] += row.get("tool") == "Read"
            if kind in ("repeated_read_unchanged", "repeated_search", "evidence_available"):
                item[kind] += 1
            item["tokens_avoidable"] += int(row.get("tokens_estimate") or 0) if row.get("would_suppress") else 0
            item["jev_calls_avoided"] += kind == "first_read" and not row.get("sampled")
            item["false_suppression_proxy"] += false_proxy
    return out


def format_summary(card):
    lines = []
    fields = tuple(_empty())
    for group in ("by_role", "by_task_class"):
        lines.append(group)
        lines.append("name\t" + "\t".join(fields))
        for name, values in sorted(card[group].items()):
            lines.append(str(name) + "\t" + "\t".join(str(values[field]) for field in fields))
    return "\n".join(lines)
