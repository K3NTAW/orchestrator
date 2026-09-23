"""Explicit worker cancellation and bounded, observable partial results."""
import json
import math
import os
from pathlib import Path
import signal
import time

from . import bus, pool, worker_registry
from .gitutil import _git_in, _resolve_base
from .jev import redact


def _tail(path):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            return [redact(line)[:512] for line in stream.read().decode('utf-8', 'replace').splitlines()[-20:]]
    except (OSError, ValueError):
        return []


def partial_result(task):
    """Read Git and explicit diagnostic artifacts, never worker conversations."""
    partial = {"files_changed": [], "diff_stat": "", "commits": [], "tests_run": [],
               "errors": [], "files_inspected": [], "unresolved": []}
    worker = worker_registry.get(task["id"]) or {}
    worktree = task.get("worktree") or worker.get("worktree")
    if worktree and Path(worktree).is_dir():
        status = _git_in(worktree, "status", "--porcelain", "-z")
        entries = iter(status.stdout.split('\0')) if status.returncode == 0 else iter(())
        for entry in entries:
            if entry:
                partial["files_changed"].append(redact(entry[3:]))
                if 'R' in entry[:2] or 'C' in entry[:2]:
                    old = next(entries, '')
                    if old:
                        partial["files_changed"].append(redact(old))
        base = _resolve_base(worktree, task.get("parent"))
        if base:
            diff = _git_in(worktree, "diff", "--stat", base)
            if diff.returncode == 0:
                partial["diff_stat"] = redact(diff.stdout)
            commits = _git_in(worktree, "log", "--format=%H%x00%s", f"{base}..HEAD")
            if commits.returncode == 0:
                for line in commits.stdout.splitlines():
                    sha, sep, subject = line.partition('\0')
                    if sep:
                        partial["commits"].append({"sha": sha, "subject": redact(subject)})
        root = Path(worktree)
        logs = list((root / '.orchestrator/runs/tests').glob(f'{task["id"]}-*.log'))
        logs += [root / name for name in ('tests-green.log', 'failures_only.log')
                 if (root / name).is_file()]
        if logs:
            partial["tests_run"] = _tail(max(logs, key=lambda path: path.stat().st_mtime))
    for event in worker_registry.events(task["id"], limit=200):
        data = event.get("data") or {}
        if event.get("kind") == "exit" and isinstance(data.get("stderr_path"), str):
            partial["errors"] = _tail(data["stderr_path"])
        if event.get("kind") == "tool":
            paths = data.get("files_inspected", [])
            if isinstance(paths, list):
                partial["files_inspected"].extend(redact(path) for path in paths if isinstance(path, str))
    return partial


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _bounded_result(partial, reason, source):
    result = {"partial": partial, "cancel_reason": reason, "cancelled_by": source,
              "confidence": 0.0, "provenance": ["worker_partial"]}
    while len(json.dumps(result)) > bus.MAX_RESULT_CHARS:
        key = max(partial, key=lambda name: len(json.dumps(partial[name])))
        value = partial[key]
        if isinstance(value, list) and value:
            value.pop()
        elif isinstance(value, str) and value:
            partial[key] = value[:len(value) // 2]
        else:
            raise ValueError("cancellation metadata exceeds result cap")
    return result


def cancel(task_id, reason, *, source="planner", grace_s=20, sleep=time.sleep,
           alive=None, signal_fn=os.kill):
    """Stop the registered process, preserve its worktree, and hold it for the Planner."""
    if not isinstance(reason, str):
        raise ValueError("cancellation reason must be text")
    reason = redact(reason)
    worker_registry._validate({"cancel_reason": reason, "source": source})
    if source is None or not isinstance(grace_s, (int, float)) or not math.isfinite(grace_s) or grace_s < 0:
        raise ValueError("invalid cancellation source or grace")
    alive = alive or _alive
    with bus.locked():
        task = bus.get(task_id)
        worker = worker_registry.get(task_id)
        if not worker:
            raise ValueError("worker not found")
        if worker["status"] == "cancelled":
            stored = (task.get("result") or {}).get("partial")
            return stored if stored is not None else partial_result(task)
        if worker["status"] in worker_registry.TERMINAL:
            raise ValueError("worker is already finished")
        pid = worker.get("pid")
        if pid is not None and (pid <= 1 or pid == os.getpid()):
            raise ValueError("refusing unsafe worker pid")
        worker_registry.event(task_id, "cancel_requested", status="cancelling",
                              cancel_reason=reason, source=source)
    if pid and alive(pid):
        try:
            signal_fn(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            remaining = grace_s
            while alive(pid) and remaining > 0:
                step = min(0.1, remaining)
                sleep(step)
                remaining = max(0, remaining - step)
            if alive(pid):
                try:
                    signal_fn(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    partial = partial_result(task)
    result = _bounded_result(partial, reason, source)
    with bus.locked():
        bus.update(task_id, status="held", hold_reason="cancelled", pid=None, result=result)
        pool.Pool().release(task_id, {})
        worker_registry.finish(task_id, "cancelled")
    return partial
