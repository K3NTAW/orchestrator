"""Bounded execution for repository gates."""
import json
import os
import re
import signal
import subprocess
import time
from contextvars import ContextVar
from pathlib import Path

from . import STATE
from . import notify
from . import pool


DEFAULT_TIMEOUT_S = 2700
KILL_GRACE_S = 15
_ON_START = ContextVar("gate_on_start", default=None)


def settings(cfg=None):
    """Return normalized gate settings from an explicit or repository config."""
    cfg = pool.config() if cfg is None else cfg
    gate_cfg = cfg.get("gate", {})
    timeout_s = gate_cfg.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0:
        timeout_s = DEFAULT_TIMEOUT_S
    cleanup_timeout_s = gate_cfg.get("cleanup_timeout_s", 300)
    if not isinstance(cleanup_timeout_s, int) or isinstance(cleanup_timeout_s, bool) or cleanup_timeout_s <= 0:
        cleanup_timeout_s = 300
    cleanup_cmd = gate_cfg.get("cleanup_cmd")
    if not isinstance(cleanup_cmd, str) or not cleanup_cmd:
        cleanup_cmd = None
    return {"timeout_s": timeout_s, "cleanup_cmd": cleanup_cmd,
            "cleanup_timeout_s": cleanup_timeout_s}


def _run(argv, *, cwd, timeout_s, input, kill_grace_s):
    started = time.monotonic()
    deadline = time.time() + timeout_s
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    on_start = _ON_START.get()
    if on_start is not None:
        on_start(proc.pid)
    pending_input = input
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        timeout = min(30, max(0.1, remaining))
        try:
            stdout, stderr = proc.communicate(input=pending_input, timeout=timeout)
        except subprocess.TimeoutExpired:
            pending_input = None
            continue
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr,
                "timed_out": False, "duration_s": time.monotonic() - started}

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        stdout, stderr = proc.communicate(timeout=kill_grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
    else:
        # The direct child may exit on SIGTERM while a descendant remains in
        # the process group without holding our pipes open.
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return {"returncode": None, "stdout": stdout or "", "stderr": stderr or "",
            "timed_out": True, "duration_s": time.monotonic() - started}


def run_bounded(argv, *, cwd, timeout_s, input=None, kill_grace_s=KILL_GRACE_S):
    """Run an argv in its own process group, killing the whole group on timeout."""
    return _run(argv, cwd=cwd, timeout_s=timeout_s, input=input, kill_grace_s=kill_grace_s)


def _gate_key(worktree, task_id):
    raw = task_id or Path(worktree).resolve().name
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw)).strip("-.") or "gate"


def run_gate(worktree, *, script, task_id=None, cfg=None):
    """Run a repository gate, cleaning up and retrying once after a timeout."""
    opts = settings(cfg)
    gates_dir = STATE / "gates"
    registry = gates_dir / f"{_gate_key(worktree, task_id)}.json"
    timeouts = 0
    for attempt in (1, 2):
        def register(pid):
            gates_dir.mkdir(parents=True, exist_ok=True)
            registry.write_text(json.dumps({"task_id": task_id, "worktree": str(worktree),
                                            "pid": pid, "started_at": time.time(),
                                            "timeout_s": opts["timeout_s"]}))

        token = _ON_START.set(register)
        try:
            result = run_bounded([str(script), str(worktree)], cwd=worktree,
                                 timeout_s=opts["timeout_s"], input="{}")
        finally:
            _ON_START.reset(token)
            try:
                registry.unlink()
            except FileNotFoundError:
                pass
        if not result["timed_out"]:
            return {**{k: result[k] for k in ("returncode", "stdout", "stderr", "timed_out")},
                    "attempts": attempt, "timeouts": timeouts}
        timeouts += 1
        if opts["cleanup_cmd"]:
            run_bounded(["bash", "-c", opts["cleanup_cmd"]], cwd=worktree,
                        timeout_s=opts["cleanup_timeout_s"])
        notify.notify(f"gate timeout: task_id={task_id} attempt={attempt} timeout_s={opts['timeout_s']}")
    return {"returncode": None, "stdout": result["stdout"], "stderr": result["stderr"],
            "timed_out": True, "attempts": 2, "timeouts": timeouts}


def running_gates(now=None, threshold=0.8):
    """Return readable gate registry entries nearing their timeout."""
    now = time.time() if now is None else now
    entries = []
    for path in (STATE / "gates").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            elapsed = now - entry["started_at"]
            if elapsed > threshold * entry["timeout_s"]:
                entries.append({**entry, "elapsed_s": elapsed})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return entries
