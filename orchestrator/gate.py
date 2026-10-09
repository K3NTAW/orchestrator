"""Bounded execution for repository gates."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path

from . import STATE
from . import notify
from . import pool


DEFAULT_TIMEOUT_S = 2700
KILL_GRACE_S = 15
_ON_START = ContextVar("gate_on_start", default=None)
# Every process group _run() started in this process and has not finished yet: terminate_own() kills them on
# daemon shutdown so no gate outlives the daemon that would apply its result.
_PROCS = {}
_ABORTED = set()
_PROCS_LOCK = threading.Lock()
_OWNER = {}


class Aborted(RuntimeError):
    """The gate's process group was killed by terminate_own(): the result belongs to nobody, never red or timeout."""


def _owner():
    """This process's identity for gate registry entries: pid plus start time, so a reused pid is not mistaken
    for the daemon that started the gate."""
    if _OWNER.get("pid") != os.getpid():
        from . import machine
        _OWNER.clear()
        _OWNER.update(pid=os.getpid(), start=machine.process_start(os.getpid()))
    return _OWNER


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
    post_cmd = gate_cfg.get("post_cmd")
    if not isinstance(post_cmd, str) or not post_cmd:
        post_cmd = None
    max_parallel = gate_cfg.get("max_parallel", 1)
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        max_parallel = 1
    return {"timeout_s": timeout_s, "cleanup_cmd": cleanup_cmd,
            "cleanup_timeout_s": cleanup_timeout_s, "post_cmd": post_cmd,
            "max_parallel": max_parallel}


def _run(argv, *, cwd, timeout_s, input, kill_grace_s, env=None):
    started = time.monotonic()
    deadline = time.time() + timeout_s
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, env=env)
    with _PROCS_LOCK:
        _PROCS[proc.pid] = proc
    try:
        return _wait(proc, started, deadline, input, kill_grace_s)
    finally:
        with _PROCS_LOCK:
            _PROCS.pop(proc.pid, None)
            aborted = proc.pid in _ABORTED
            _ABORTED.discard(proc.pid)
        if aborted:
            raise Aborted(f"gate process group {proc.pid} terminated on daemon shutdown")


def _wait(proc, started, deadline, input, kill_grace_s):
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
    except (ProcessLookupError, PermissionError):
        pass
    try:
        stdout, stderr = proc.communicate(timeout=kill_grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()
    else:
        # The direct child may exit on SIGTERM while a descendant remains in
        # the process group without holding our pipes open.
        # A reaped child's group ID may also have been reused by another user.
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return {"returncode": None, "stdout": stdout or "", "stderr": stderr or "",
            "timed_out": True, "duration_s": time.monotonic() - started}


def run_bounded(argv, *, cwd, timeout_s, input=None, kill_grace_s=KILL_GRACE_S, env=None):
    """Run an argv in its own process group, killing the whole group on timeout. env None inherits."""
    return _run(argv, cwd=cwd, timeout_s=timeout_s, input=input, kill_grace_s=kill_grace_s, env=env)


def _gate_key(worktree, task_id):
    raw = task_id or Path(worktree).resolve().name
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw)).strip("-.") or "gate"


def _post(opts, worktree, task_id):
    """Run [gate].post_cmd after an attempt; failures are logged and never change the gate result."""
    if not opts["post_cmd"]:
        return
    try:
        result = run_bounded(["bash", "-c", opts["post_cmd"]], cwd=worktree,
                             timeout_s=opts["cleanup_timeout_s"])
    except Exception as e:
        print(f"[gate] post_cmd failed: task_id={task_id}: {e!r}", file=sys.stderr)
        return
    if result["timed_out"] or result["returncode"] != 0:
        print(f"[gate] post_cmd failed: task_id={task_id} returncode={result['returncode']} "
              f"timed_out={result['timed_out']}", file=sys.stderr)


def run_gate(worktree, *, script, task_id=None, cfg=None, env=None):
    """Run a repository gate, cleaning up and retrying once after a timeout."""
    opts = settings(cfg)
    gates_dir = STATE / "gates"
    registry = gates_dir / f"{_gate_key(worktree, task_id)}.json"
    timeouts = 0
    for attempt in (1, 2):
        def register(pid):
            from . import machine
            owner = _owner()
            gates_dir.mkdir(parents=True, exist_ok=True)
            registry.write_text(json.dumps({"task_id": task_id, "worktree": str(worktree),
                                            "pid": pid, "pgid": pid, "proc_start": machine.process_start(pid),
                                            "owner_pid": owner["pid"], "owner_start": owner["start"],
                                            "started_at": time.time(), "timeout_s": opts["timeout_s"]}))

        token = _ON_START.set(register)
        try:
            result = run_bounded([str(script), str(worktree)], cwd=worktree,
                                 timeout_s=opts["timeout_s"], input="{}", env=env)
        finally:
            _ON_START.reset(token)
            try:
                registry.unlink()
            except FileNotFoundError:
                pass
        if not result["timed_out"]:
            _post(opts, worktree, task_id)
            return {**{k: result[k] for k in ("returncode", "stdout", "stderr", "timed_out")},
                    "attempts": attempt, "timeouts": timeouts}
        timeouts += 1
        if opts["cleanup_cmd"]:
            run_bounded(["bash", "-c", opts["cleanup_cmd"]], cwd=worktree,
                        timeout_s=opts["cleanup_timeout_s"])
        _post(opts, worktree, task_id)
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


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _stop_group(pgid, grace_s, exited):
    """SIGTERM the group, wait up to grace_s for exited(), then SIGKILL whatever is left of the group."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and not exited():
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def terminate_own(grace_s=5):
    """Daemon shutdown: kill every gate process group this process started and drop the registry entries it
    owns. The killed runs raise Aborted in their threads, so no caller records a red or timed-out gate. Returns
    the killed process group ids."""
    with _PROCS_LOCK:
        procs = list(_PROCS.values())
        _ABORTED.update(proc.pid for proc in procs)
    killed = [proc.pid for proc in procs
              if _stop_group(proc.pid, grace_s, lambda proc=proc: proc.poll() is not None)]
    for path in (STATE / "gates").glob("*.json"):
        try:
            if json.loads(path.read_text()).get("owner_pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError, AttributeError):
            continue
    return killed


def _ppid(pid):
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10).stdout
        return int(out.strip())
    except Exception:
        return None


def reap_orphans(grace_s=KILL_GRACE_S):
    """Daemon start: kill gates whose owner is gone, since their result has nowhere to go. An entry is orphaned
    when its owner pid is dead or reused (start time differs), or, for entries written before owner_pid
    existed, when the gate was reparented to init. A gate pid now used by another process (start time differs)
    is forgotten, not killed. Gates of a live owner (a CLI merge, another process) are left alone. Returns the
    killed entries."""
    from . import machine
    killed = []
    for path in (STATE / "gates").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            pid = int(entry["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        owner = entry.get("owner_pid")
        if owner == os.getpid():
            continue
        if owner:
            owner_start = machine.process_start(owner)
            if owner_start and owner_start == entry.get("owner_start", owner_start):
                continue
        elif _ppid(pid) not in (1, None):
            continue
        pgid = int(entry.get("pgid") or pid)
        start = machine.process_start(pid)
        same = start is not None and entry.get("proc_start") in (None, start)
        if same and _group_alive(pgid) and _stop_group(pgid, grace_s, lambda: not _group_alive(pgid)):
            killed.append(entry)
            print(f"[gate] killed orphaned gate pid {pid} task_id={entry.get('task_id')}", file=sys.stderr)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return killed
