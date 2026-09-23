"""Explicit worker cancellation and bounded, observable partial results."""
import json
import math
import os
from pathlib import Path
import signal
import time

from . import bus, evidence, pool, worker_registry
from .gitutil import _git_in, _resolve_base


def redact(text):
    # Worker launch modules are also imported while Jev initializes.
    from .jev import redact as redact_text
    return redact_text(text)


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


def _partial_lines(partial):
    lines = []
    for path in partial.get("files_changed", []):
        lines.append(f"file: {path}")
    if partial.get("diff_stat"):
        lines.extend(f"diff: {line}" for line in partial["diff_stat"].splitlines() if line.strip())
    for commit in partial.get("commits", []):
        lines.append(f"commit: {commit.get('sha', '')} {commit.get('subject', '')}".rstrip())
    for name in ("tests_run", "errors", "files_inspected"):
        label = {"tests_run": "test", "errors": "error", "files_inspected": "inspected"}[name]
        lines.extend(f"{label}: {line}" for line in partial.get(name, []))
    return "\n".join(lines)


def preserve_partial(task):
    """Persist observable worktree facts once for a replacement worker."""
    current = bus.get(task["id"])
    pipeline = dict(current.get("pipeline") or {})
    if pipeline.get("partial_preserved_at"):
        return None
    worktree = current.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        return None
    head = _git_in(worktree, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        return None
    sha = head.stdout.strip()
    partial = ((current.get("result") or {}).get("partial")
               or partial_result(current))
    content = _partial_lines(partial)
    limit = int((pool.Pool().cfg.get("context_router") or {}).get("partial_max_tokens", 800)) * 4
    ev = evidence.make("worker_partial", f"{current['id']}:{sha}", content[:max(0, limit)],
                       commit=sha, provenance="worker_partial", scope=current.get("scope") or [])
    saved = evidence.EvidencePool(current.get("parent") or current["id"]).add(ev)
    pipeline["partial_preserved_at"] = time.time()
    bus.update(current["id"], pipeline=pipeline)
    return saved


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
    _terminate(pid, grace_s=grace_s, sleep=sleep, alive=alive, signal_fn=signal_fn)
    partial = partial_result(task)
    result = _bounded_result(partial, reason, source)
    with bus.locked():
        bus.update(task_id, status="held", hold_reason="cancelled", pid=None, result=result)
        pool.Pool().release(task_id, {})
        worker_registry.finish(task_id, "cancelled")
    preserve_partial(bus.get(task_id))
    return partial


def _terminate(pid, *, grace_s, sleep, alive, signal_fn):
    """Interrupt a process and escalate after the bounded grace period."""
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


def launch_epoch(task_id):
    epoch = (worker_registry.get(task_id) or {}).get("epoch", 1)
    return epoch if type(epoch) is int else 1


def is_current(task_id, epoch):
    """Call under the bus lock when a mutation follows this check."""
    try:
        task = bus.get(task_id)
    except KeyError:
        return epoch == launch_epoch(task_id)
    return epoch >= (task.get("pipeline") or {}).get("steer_epoch", 1)


def release_if_current(task_id, epoch, pool, usage=None):
    with bus.locked():
        if epoch == launch_epoch(task_id):
            pool.release(task_id, usage or {})
            return True
    return False


def write_if_current(task_id, epoch, method, *args, **fields):
    with bus.locked():
        if is_current(task_id, epoch):
            return method(*args, **fields)


def steer(task_id, message, *, reason, source="planner", grace_s=20,
          sleep=time.sleep, alive=None, signal_fn=os.kill):
    """Record steering separately from the immutable contract, interrupt, and resume."""
    from . import executor, spawn
    if not isinstance(message, str) or not message.strip():
        raise ValueError("steering message must be nonempty text")
    if not isinstance(reason, str):
        raise ValueError("steering reason must be text")
    message, reason = redact(message), redact(reason)
    if isinstance(source, str):
        source = redact(source)
    worker_registry._validate({"cancel_reason": reason, "source": source})
    if source is None or not isinstance(grace_s, (int, float)) or not math.isfinite(grace_s) or grace_s < 0:
        raise ValueError("invalid steering source or grace")
    alive = alive or _alive
    with bus.locked():
        task = bus.get(task_id)
        previous_status = task["status"]
        worker = worker_registry.get(task_id)
        if not worker or worker["status"] not in ("running", "waiting"):
            raise ValueError("worker is not running")
        provider = worker.get("provider")
        thread = worker.get("thread") or task.get("codex_thread")
        session = (task.get("packet_meta") or {}).get("session_id")
        if provider == "codex" and not thread:
            raise ValueError("cannot steer: no recorded Codex thread")
        if provider == "claude" and not session:
            raise ValueError("cannot steer: no recorded Claude session")
        if provider not in ("codex", "claude"):
            raise ValueError("cannot steer: unsupported provider")
        if provider == "claude" and (worker.get("tools") is None or not worker.get("account")):
            raise ValueError("cannot steer: no recorded Claude tools or account")
        pid = worker.get("pid")
        if pid is None:
            raise ValueError("cannot steer: no recorded worker pid")
        if pid <= 1 or pid == os.getpid():
            raise ValueError("refusing unsafe worker pid")
        if not alive(pid):
            raise ValueError("cannot steer: recorded worker pid is not alive")
        path = worker_registry._path(task_id).with_suffix(".steering.jsonl")
        row = {"ts": time.time(), "task": task_id, "reason": reason, "message": message, "source": source}
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        epoch = worker.get("epoch", 1) + 1
        worker_registry.event(task_id, "steer", status="steering", epoch=epoch,
                              message_chars=len(message), source=source)
        meta = dict(task.get("packet_meta") or {})
        meta["steering_count"] = meta.get("steering_count", 0) + 1
        pipeline = dict(task.get("pipeline") or {})
        pipeline["steer_epoch"] = epoch
        # bus.update appends its own event, including these payload-free counters.
        task = bus.update(task_id, packet_meta=meta, pipeline=pipeline)
    _terminate(pid, grace_s=grace_s, sleep=sleep, alive=alive, signal_fn=signal_fn)
    prompt = f"Steering from {source} ({reason}):\n{message}\nThe original task contract is unchanged."
    task = {**task, "_launch_epoch": epoch, "_steering": True,
            "worktree": task.get("worktree") or worker.get("worktree")}
    try:
        if provider == "codex":
            result = executor.steer_resume(task, thread, prompt)
        else:
            result = spawn.resume_worker(task, prompt, session)
    except Exception as exc:
        with bus.locked():
            if launch_epoch(task_id) == epoch and is_current(task_id, epoch):
                worker_registry.finish(task_id, "failed", "steer_failed", epoch=epoch)
                pipeline = dict(bus.get(task_id).get("pipeline") or {})
                pipeline["steer_failed"] = redact(f"{type(exc).__name__}: {exc}")[:512]
                bus.update(task_id, status=previous_status, pid=None, pipeline=pipeline)
            release_if_current(task_id, epoch, pool.Pool())
        raise
    return {"status": "steered", "task": task_id, "epoch": epoch, "delivery": result}
