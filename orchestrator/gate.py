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
# Gate threads queued for the machine heavy-gate slot: (str(STATE), key) -> Event, guarded by _PROCS_LOCK.
# terminate_own() sets the Events of its STATE; the waiter raises Aborted at its next check.
_SLOT_WAITERS = {}
# One [gate].max_parallel count per repo across every path that starts a gate (daemon background gate, the merge
# gate, ship): slot key -> repo state dir, reserved by a thread of this process before its gate starts. Gates of
# other processes count through their live registry entries. _HELD names the slot the calling context already
# reserved, so run_gate does not wait on the slot its own caller holds.
_SLOTS = {}
_SLOTS_LOCK = threading.Lock()
_HELD = ContextVar("gate_slot_held", default=None)
SLOT_POLL_S = 5
# Registry entry state of a gate queued for the machine heavy-gate slot (docs/gate-contract.md): no gate process
# exists yet, so the entry carries only its owner, never pid, pgid, proc_start, started_at or timeout_s.
WAITING = "waiting"


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


def _write_registry(path, entry):
    """Write a registry entry atomically: a reader sees the old entry or the new one, never a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry))
    os.replace(tmp, path)
    return path


def write_waiting_entry(key, *, task_id, worktree, mode, base):
    """Register a gate that waits for the machine heavy-gate slot. Returns the registry path."""
    owner = _owner()
    return _write_registry(STATE / "gates" / f"{key}.json",
                           {"state": WAITING, "task_id": task_id, "worktree": str(worktree),
                            "owner_pid": owner["pid"], "owner_start": owner["start"],
                            "waiting_for": "machine_slot", "waiting_since": time.time(),
                            "mode": mode, "base": base})


def is_waiting(entry):
    return isinstance(entry, dict) and entry.get("state") == WAITING


def _owner_alive(entry):
    """The process that wrote entry still runs: pid alive and, when recorded, the same start time."""
    from . import machine
    pid = entry.get("owner_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not machine._pid_alive(pid):
        return False
    return entry.get("owner_start") is None or machine.process_start(pid) in (None, entry["owner_start"])


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
    scoped = gate_cfg.get("scoped_task_gates", True)

    def machine_int(key, default, minimum):
        value = gate_cfg.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            print(f"[gate] invalid [gate].{key}={value!r}, using default {default}", file=sys.stderr)
            return default
        return value

    heavy = gate_cfg.get("heavy", False)
    if not isinstance(heavy, bool):
        print(f"[gate] invalid [gate].heavy={heavy!r}, using default False", file=sys.stderr)
        heavy = False
    return {"timeout_s": timeout_s, "cleanup_cmd": cleanup_cmd,
            "cleanup_timeout_s": cleanup_timeout_s, "post_cmd": post_cmd,
            "max_parallel": max_parallel, "scoped_task_gates": scoped is not False,
            "machine_max_parallel": machine_int("machine_max_parallel", 1, 0), "heavy": heavy,
            # One queued gate outlives one worst-case holder: two attempts, each followed by cleanup_cmd and
            # post_cmd bounded by cleanup_timeout_s, plus kill grace.
            "machine_wait_max_s": machine_int("machine_wait_max_s", 2 * (timeout_s + 2 * cleanup_timeout_s) + 60, 1),
            "machine_wait_log_s": machine_int("machine_wait_log_s", 300, 1),
            "machine_ticket_reissue_max": machine_int("machine_ticket_reissue_max", 3, 0)}


def _heavy(mode, opts):
    """A heavy gate takes the machine-wide heavy-gate lease: full or unscoped gates, or any gate of a repo
    whose [gate].heavy is true."""
    return mode in (None, "full") or opts["heavy"]


def task_base(worktree, target):
    """The base a task-mode gate diffs against: merge-base of target and HEAD in worktree, target itself when
    merge-base fails but target resolves, None when target does not resolve. Never raises."""
    def git(*args):
        try:
            r = subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    if not target:
        return None
    sha = git("merge-base", str(target), "HEAD")
    if sha:
        return sha
    return str(target) if git("rev-parse", "--verify", "--quiet", f"{target}^{{commit}}") else None


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


def slot_key(worktree, task_id=None):
    """The gate's slot and registry key: one gate per key at a time."""
    return _gate_key(worktree, task_id)


def _live_registry_keys():
    """Registry keys whose gate process group still runs (a reused pid with another start time does not). A
    waiting entry counts while its owner lives: a gate queued for the machine slot keeps its repo slot."""
    from . import machine
    keys = set()
    for path in (STATE / "gates").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            if is_waiting(entry):
                if _owner_alive(entry):
                    keys.add(path.stem)
                continue
            pid = int(entry["pid"])
            pgid = int(entry.get("pgid") or pid)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not _group_alive(pgid):
            continue
        if entry.get("proc_start") and machine.process_start(pid) not in (None, entry["proc_start"]):
            continue
        keys.add(path.stem)
    return keys


def live_slots():
    """Gates running or reserved in this repo: this process's reservations plus other live registry entries."""
    repo = str(STATE)
    with _SLOTS_LOCK:
        mine = {key for key, owner in _SLOTS.items() if owner == repo}
    return len(mine | _live_registry_keys())


def try_acquire_slot(key, cfg=None):
    """Reserve a gate slot for key when fewer than [gate].max_parallel gates run in this repo. False when the cap
    is full or key already holds a slot; the caller retries later and never starts the gate."""
    cap = settings(cfg)["max_parallel"]
    repo = str(STATE)
    with _SLOTS_LOCK:
        if key in _SLOTS:
            return False
        mine = {k for k, owner in _SLOTS.items() if owner == repo}
        live = _live_registry_keys()
        if key in live or len(mine | live) >= cap:
            return False
        _SLOTS[key] = repo
        return True


def release_slot(key):
    with _SLOTS_LOCK:
        _SLOTS.pop(key, None)


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


def run_gate(worktree, *, script, task_id=None, cfg=None, env=None, mode=None, base=None):
    """Run a repository gate, cleaning up and retrying once after a timeout. Takes a [gate].max_parallel slot
    first, waiting until one frees, unless the calling context already holds the slot for this gate.
    mode (task | full) and base reach the gate as ORCH_GATE_MODE / ORCH_GATE_BASE when not None
    (docs/gate-contract.md); env None inherits os.environ.

    A heavy gate (_heavy) then queues FIFO for one of [gate].machine_max_parallel machine-wide heavy-gate leases
    (0 = off); queue time never counts toward [gate].timeout_s. The wait is bounded by [gate].machine_wait_max_s.
    terminate_own aborts a queued waiter of this STATE: it raises Aborted with its repo slot released.
    No re-entrancy guard: nothing calls run_gate from inside a gate (daemon._start_gate's thread, merge.merge and
    ship._gate are all top level). Every result carries machine_wait_s and machine_wait_exceeded."""
    key = _gate_key(worktree, task_id)
    held = _HELD.get() == key
    if not held:
        while not try_acquire_slot(key, cfg):
            time.sleep(SLOT_POLL_S)
    try:
        return _run_gate(worktree, script=script, task_id=task_id, cfg=cfg, env=env, key=key, mode=mode, base=base)
    finally:
        if not held:
            release_slot(key)


def _machine_failure(mode, base, wait_s, *, error=None, stderr=None):
    """Result of a gate that never ran: returncode 1 when the lease could not be read or written, else the
    wait-exceeded timeout shape (returncode None, timeouts 0, so no caller files a gate timeout retry)."""
    if error is not None:
        return {"returncode": 1, "stdout": "", "stderr": error, "timed_out": False, "error": error,
                "attempts": 0, "timeouts": 0, "machine_wait_s": wait_s, "machine_wait_exceeded": False,
                "mode": mode, "base": base}
    return {"returncode": None, "stdout": "", "stderr": stderr, "timed_out": True, "attempts": 0,
            "timeouts": 0, "machine_wait_exceeded": True, "machine_wait_s": wait_s, "mode": mode, "base": base}


def _unlink_waiting(key):
    """Remove this process's waiting registry entry for key; a running entry or another owner's is kept."""
    path = STATE / "gates" / f"{key}.json"
    try:
        entry = json.loads(path.read_text())
        if is_waiting(entry) and entry.get("owner_pid") == os.getpid():
            path.unlink()
    except (FileNotFoundError, ValueError):
        pass


def _wait_machine_slot(key, task_id, worktree, mode, base, opts):
    """Queue FIFO for a machine heavy-gate lease. Returns (lease_id, wait_s) or a result dict for run_gate."""
    from . import machine
    owner = _owner()
    holder = {"repo": str(STATE.parent), "task": task_id, "pid": owner["pid"]}
    if owner["start"] is not None:
        holder["proc_start"] = owner["start"]
    cap = opts["machine_max_parallel"]
    ticket, ok, start, reissues, logged = None, False, None, 0, None
    mark, event = (str(STATE), key), threading.Event()
    with _PROCS_LOCK:
        _SLOT_WAITERS[mark] = event

    def check():
        with _PROCS_LOCK:
            if event.is_set():
                raise Aborted(f"gate {key} queued for the machine slot aborted on daemon shutdown")

    try:
        try:
            write_waiting_entry(key, task_id=task_id, worktree=worktree, mode=mode, base=base)
            ticket = machine.wait_ticket("heavy_gate", holder)
            start = time.monotonic()
            while True:
                check()
                lease = machine.acquire("heavy_gate", cap, holder, ticket=ticket)
                waited = time.monotonic() - start
                if lease is not None:
                    with _PROCS_LOCK:
                        aborted = event.is_set()
                    if aborted:
                        machine.release(lease)
                        raise Aborted(f"gate {key} aborted on daemon shutdown after taking the machine slot")
                    ok = True
                    return lease, waited
                if waited > opts["machine_wait_max_s"]:
                    return _machine_failure(mode, base, waited, stderr=f"machine slot wait exceeded {int(waited)}s")
                if ticket not in {w.get("ticket") for w in machine._read_waiters("heavy_gate")}:
                    if reissues >= opts["machine_ticket_reissue_max"]:
                        return _machine_failure(mode, base, waited,
                                                stderr=f"machine slot ticket vanished {reissues + 1} times")
                    ticket = machine.wait_ticket("heavy_gate", holder)
                    reissues += 1
                    print(f"[gate] task_id={task_id} machine slot ticket vanished, re-queued at the tail "
                          f"({reissues}/{opts['machine_ticket_reissue_max']})", file=sys.stderr)
                if logged is None or time.monotonic() - logged >= opts["machine_wait_log_s"]:
                    logged = time.monotonic()
                    now = time.time()
                    held = [(h.get("repo"), h.get("task"), round(now - h.get("since", now)))
                            for h in machine.holders("heavy_gate")]
                    print(f"[gate] task_id={task_id} waiting for machine heavy-gate slot (cap={cap}, holders={held})",
                          file=sys.stderr)
                time.sleep(SLOT_POLL_S)
                check()
        except (OSError, ValueError) as e:
            waited = 0.0 if start is None else time.monotonic() - start
            return _machine_failure(mode, base, waited, error=f"machine slot: {e!r}")
    finally:
        with _PROCS_LOCK:
            if _SLOT_WAITERS.get(mark) is event:
                _SLOT_WAITERS.pop(mark)
        if ticket is not None:
            try:
                machine.drop_ticket("heavy_gate", ticket)
            except Exception as e:
                print(f"[gate] task_id={task_id} machine slot drop_ticket failed: {e!r}", file=sys.stderr)
        if not ok:
            _unlink_waiting(key)


def _run_gate(worktree, *, script, task_id, cfg, env, key, mode=None, base=None):
    opts = settings(cfg)
    lease, wait_s = None, 0.0
    if _heavy(mode, opts) and opts["machine_max_parallel"] > 0:
        got = _wait_machine_slot(key, task_id, worktree, mode, base, opts)
        if isinstance(got, dict):
            return got
        lease, wait_s = got
    try:
        result = _run_attempts(worktree, script=script, task_id=task_id, env=env, key=key, mode=mode, base=base,
                               opts=opts)
        return {**result, "machine_wait_s": wait_s, "machine_wait_exceeded": False}
    finally:
        if lease is not None:
            from . import machine
            try:
                machine.release(lease)
            except Exception as e:
                print(f"[gate] task_id={task_id} machine slot release failed: {e!r}", file=sys.stderr)


def _run_attempts(worktree, *, script, task_id, env, key, mode, base, opts):
    if mode is not None or base is not None:
        env = dict(env if env is not None else os.environ)
        if mode is not None:
            env["ORCH_GATE_MODE"] = str(mode)
        if base is not None:
            env["ORCH_GATE_BASE"] = str(base)
    gates_dir = STATE / "gates"
    registry = gates_dir / f"{key}.json"
    timeouts = 0
    for attempt in (1, 2):
        def register(pid):
            from . import machine
            owner = _owner()
            _write_registry(registry, {"task_id": task_id, "worktree": str(worktree),
                                            "pid": pid, "pgid": pid, "proc_start": machine.process_start(pid),
                                            "owner_pid": owner["pid"], "owner_start": owner["start"],
                                            "started_at": time.time(), "timeout_s": opts["timeout_s"],
                                            "mode": mode, "base": base})

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
                    "attempts": attempt, "timeouts": timeouts, "mode": mode, "base": base}
        timeouts += 1
        if opts["cleanup_cmd"]:
            run_bounded(["bash", "-c", opts["cleanup_cmd"]], cwd=worktree,
                        timeout_s=opts["cleanup_timeout_s"])
        _post(opts, worktree, task_id)
        notify.notify(f"gate timeout: task_id={task_id} attempt={attempt} timeout_s={opts['timeout_s']}")
    return {"returncode": None, "stdout": result["stdout"], "stderr": result["stderr"],
            "timed_out": True, "attempts": 2, "timeouts": timeouts, "mode": mode, "base": base}


def running_gates(now=None, threshold=0.8):
    """Return readable gate registry entries nearing their timeout."""
    now = time.time() if now is None else now
    entries = []
    for path in (STATE / "gates").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            if is_waiting(entry):
                continue
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
    the killed process group ids. Gate threads of this STATE queued for the machine slot raise Aborted too."""
    with _PROCS_LOCK:
        repo = str(STATE)
        for (state, _), event in _SLOT_WAITERS.items():
            if state == repo:
                event.set()
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
    killed entries. A waiting entry has no process to signal: it is unlinked when its owner is gone."""
    from . import machine
    killed = []
    for path in (STATE / "gates").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            if is_waiting(entry):
                if not _owner_alive(entry):
                    path.unlink(missing_ok=True)
                continue
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
