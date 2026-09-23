"""Durable, payload-free observations of worker processes; task state belongs to bus."""
import json
import math
import os
import re
import tempfile
import time
from collections import deque
from . import STATE, bus

STATUSES = ("starting", "running", "waiting", "steering", "cancelling", "cancelled",
            "done", "failed", "held")
TERMINAL = {"cancelled", "done", "failed", "held"}
KINDS = {"spawned", "claimed", "stage", "tool", "usage", "exit", "reconciled", "held", "thread"}
FIELDS = {"role", "model", "provider", "account", "pid", "thread", "worktree", "branch",
          "started_at", "last_event_at", "stage", "current_tool", "tokens", "usd",
          "parent", "status", "status_reason"}
NUMBERS = {"pid", "started_at", "last_event_at", "usd"}
TOKEN_KEYS = {"input_uncached", "cache_read", "output"}


def _identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[\w.:/@+~-]{1,512}", value))


def _path(task_id, events=False):
    if not isinstance(task_id, str) or not re.fullmatch(r"[\w-]+", task_id):
        raise ValueError("invalid task identifier")
    return STATE / "workers" / (task_id + (".events.jsonl" if events else ".json"))


def _validate(fields, *, tool_event=False):
    if set(fields) - FIELDS or ("current_tool" in fields and not tool_event):
        raise ValueError("unsupported registry field")
    for key, value in fields.items():
        if value is None:
            continue
        if key == "tokens":
            if not isinstance(value, dict) or set(value) - TOKEN_KEYS:
                raise ValueError("invalid token buckets")
            if any(type(v) is not int or v < 0 for v in value.values()):
                raise ValueError("invalid token count")
        elif key in NUMBERS:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid registry number")
            if key == "pid" and (type(value) is not int or value == 0):
                raise ValueError("invalid process identifier")
        elif key == "worktree":
            if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid worktree path")
        elif not _identifier(value):
            raise ValueError("registry values must be identifiers")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError("invalid worker status")


def _read(task_id):
    try:
        return json.loads(_path(task_id).read_text())
    except FileNotFoundError:
        return None


def _children(task_id):
    children = []
    for path in sorted(bus.TASKS.glob("*.json")):
        task = json.loads(path.read_text())
        if (task.get("constraints") or {}).get("fix_round_for") == task_id:
            children.append(task["id"])
    return children


def _document(task_id):
    now = time.time()
    return {"task": task_id, "role": None, "model": None, "provider": None,
            "account": None, "pid": None, "worktree": None, "branch": None,
            "started_at": now, "last_event_at": now, "stage": None, "tokens": {},
            "usd": None, "parent": None, "children": [], "status": "starting",
            "status_reason": None}


def _write(task_id, doc):
    path = _path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc["children"] = _children(task_id)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(doc, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def upsert(task_id, **fields):
    _validate(fields)
    with bus.locked():
        doc = _read(task_id) or _document(task_id)
        # A new launch replaces the previous attempt's snapshot, retaining its event history.
        if fields.get("status") == "starting":
            doc = _document(task_id)
        doc.update(fields)
        _write(task_id, doc)
        return get(task_id)


def get(task_id):
    doc = _read(task_id)
    if doc is None:
        return None
    end = doc["last_event_at"] if doc["status"] in TERMINAL else time.time()
    doc["elapsed_s"] = max(0, end - doc["started_at"])
    doc["children"] = _children(task_id)
    return doc


def listing(include_finished=False):
    now = time.time()
    docs = [get(path.stem) for path in sorted((STATE / "workers").glob("*.json"))]
    return [doc for doc in docs if doc["status"] not in TERMINAL or
            (include_finished and now - doc["last_event_at"] <= 86400)]


def active():
    return listing()


def events(task_id, limit=20):
    try:
        with _path(task_id, events=True).open() as stream:
            return [json.loads(line) for line in deque(stream, maxlen=limit)]
    except FileNotFoundError:
        return []


def event(task_id, kind, **data):
    if kind not in KINDS:
        raise ValueError("unsupported registry event")
    _validate(data, tool_event=kind == "tool")
    with bus.locked():
        doc = _read(task_id) or _document(task_id)
        ts = time.time()
        doc.update(data)
        doc["last_event_at"] = ts
        if kind == "spawned":
            doc["status"] = "running"
        elif kind == "held":
            doc["status"] = "held"
        path = _path(task_id, events=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps({"ts": ts, "kind": kind, "data": data}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _write(task_id, doc)
        return get(task_id)


def usage(task_id, provider, counts, usd=None):
    """Copy only known numeric counters from provider results."""
    keys = {"input_uncached": "input_tokens", "cache_read": "cached_input_tokens" if
            provider == "codex" else "cache_read_input_tokens", "output": "output_tokens"}
    tokens = {key: counts[source] for key, source in keys.items()
              if type(counts.get(source)) is int and counts[source] >= 0}
    if provider == "codex" and "input_uncached" in tokens:
        tokens["input_uncached"] = max(0, tokens["input_uncached"] - tokens.get("cache_read", 0))
    event(task_id, "usage", tokens=tokens, usd=usd)


def finish(task_id, status, reason=None):
    if status not in TERMINAL:
        raise ValueError("finish requires a terminal status")
    with bus.locked():
        if status == "held":
            event(task_id, "held", status_reason=reason)
        return event(task_id, "exit", status=status, status_reason=reason)


def reconcile(alive_fn):
    changed = []
    with bus.locked():
        for doc in active():
            if doc["status"] in ("running", "starting") and doc.get("pid") and not alive_fn(doc["pid"]):
                changed.append(event(doc["task"], "reconciled", status="failed", status_reason="process_dead"))
    return changed
