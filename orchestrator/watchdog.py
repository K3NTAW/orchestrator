"""Machine-level watchdog: restart dead daemons, report spawn failures and stalled queues across every known repo.

Read-only on each repo's .orchestrator state (it never writes any repo's bus); its own state lives in the machine
dir. Each condition is notified once and again only after it cleared and came back."""
import fcntl, json, os, re, subprocess, sys, time, tomllib
from pathlib import Path
from xml.sax.saxutils import escape
from . import STATE, claude_cli, machine, notify

INTERVAL_S = 300
SPAWN_WINDOW_S = 30 * 60
STALL_S = 60 * 60
CONFIRM_S = 20
DEFAULT_TIMEOUT_S = 900
SPAWN_MARKERS = ("claude CLI not found", "not found on PATH", "no_cli", "launch_error")
WORK_STATUSES = {"queued", "running", "held"}
CLOSED_GOALS = {"exited", "stopped", "done", "failed", "closed"}
CONFIG = STATE / "pool.toml"
PACKAGE_REPO = Path(__file__).resolve().parents[1]  # the orchestrator itself, run via uv --project
MAX_PID = 2 ** 31
# None = derived from machine.MACHINE_DIR at call time, so a test that patches MACHINE_DIR moves these too.
STATE_PATH = None
LOCK_PATH = None


def _state_path():
    return Path(STATE_PATH) if STATE_PATH else machine.MACHINE_DIR / "watchdog-state.json"


def _lock_path():
    return Path(LOCK_PATH) if LOCK_PATH else machine.MACHINE_DIR / "watchdog.lock"


# repo state, read-only ----------------------------------------------------------------------------------------

def _json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _valid_task(t):
    deps = t.get("depends_on")
    return (isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("status"), str)
            and (deps is None or isinstance(deps, list) and all(isinstance(d, str) for d in deps)))


def load_tasks(repo):
    """Well-formed task dicts; malformed ones and files that vanish between glob and stat are skipped."""
    tasks = []
    for f in sorted((Path(repo) / ".orchestrator" / "tasks").glob("*.json")):
        t = _json(f, None)
        if not isinstance(t, dict) or not _valid_task(t):
            continue
        try:
            t["_mtime"] = f.stat().st_mtime
        except OSError:
            continue
        for key in ("pipeline", "constraints", "result"):
            if not isinstance(t.get(key), dict):
                t[key] = {}
        tasks.append(t)
    return tasks


def _merged(t):
    return bool(t.get("merged_into") or (t.get("pipeline") or {}).get("merged_at"))


def read_lock(repo):
    """(pid, kind, start) from daemon.lock's "<pid> <kind> <start time>" line; kind and start are None for a
    pid-only lock written before the marker existed. None when missing, malformed or the pid is out of range."""
    try:
        lines = (Path(repo) / ".orchestrator" / "daemon.lock").read_text().strip().splitlines()
        parts = lines[0].split(maxsplit=2)
        pid = int(parts[0])
    except (OSError, UnicodeDecodeError, ValueError, OverflowError, IndexError):
        return None
    if not 0 < pid < MAX_PID:
        return None
    kind = parts[1] if len(parts) > 1 else None
    start = " ".join(parts[2].split()) if len(parts) > 2 else None
    return pid, kind, start


def daemon_pid(repo):
    lock = read_lock(repo)
    return lock[0] if lock else None


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _command(pid):
    return _run(["ps", "-o", "command=", "-p", str(pid)])


def _process_root(pid):
    """The repo a process serves: ORCH_ROOT from its environment, else its cwd; None when neither is readable."""
    found = re.search(r"(?:^|\s)ORCH_ROOT=(\S+)", _run(["ps", "eww", "-o", "command=", "-p", str(pid)]))
    if found:
        return found.group(1)
    for line in _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def is_daemon_command(cmd, kind):
    """A python running the orchestrator daemon: `orchestrator daemon` (cli) or `-m orchestrator.mcp` (mcp, the
    [daemon].autostart thread). A pid-only lock (kind None) accepts either."""
    tokens = cmd.split()
    if not tokens or not os.path.basename(tokens[0]).lower().startswith("python"):
        return False
    names = [os.path.basename(t) for t in tokens[1:]]
    mcp = "orchestrator.mcp" in names
    cli = ("daemon" in names and any(n in ("orchestrator", "orchestrator.cli") for n in names)
           or "orchestrator.daemon" in names)
    return {"cli": cli, "mcp": mcp, None: cli or mcp}.get(kind, False)


def _same_path(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def daemon_alive(repo):
    """Alive = the pid daemon.lock names exists, started when the lock says it did (a reused pid fails), is an
    orchestrator daemon of the lock's kind, and serves this repo where that is determinable. Never touches the
    flock."""
    lock = read_lock(repo)
    if lock is None:
        return False
    pid, kind, start = lock
    if not machine._pid_alive(pid):
        return False
    if start is not None and machine.process_start(pid) != start:
        return False
    if not is_daemon_command(_command(pid), kind):
        return False
    root = _process_root(pid)
    return root is None or _same_path(root, repo)


def open_goal(repo):
    records = _json(Path(repo) / ".orchestrator" / "runs" / "goals.json", [])
    return isinstance(records, list) and any(isinstance(r, dict) and r.get("status") not in CLOSED_GOALS
                                             for r in records)


def has_work(repo, tasks):
    return any(t.get("status") in WORK_STATUSES for t in tasks) or open_goal(repo)


def running_too_long(tasks, now, alive):
    """Running tasks past 2x their timeout; only counted while the daemon is dead (else reconcile_dead owns them)."""
    if alive:
        return []
    out = []
    for t in tasks:
        started = (t.get("pipeline") or {}).get("dispatched_at")
        timeout = (t.get("constraints") or {}).get("timeout_s")
        timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT_S
        if t.get("status") == "running" and isinstance(started, (int, float)) and now - started > 2 * timeout:
            out.append(t["id"])
    return out


def spawn_failures(tasks, now):
    """(task id, reason) for tasks updated in the last 30 min whose hold_reason or reason names a spawn failure."""
    out = []
    for t in tasks:
        if now - t.get("_mtime", 0) > SPAWN_WINDOW_S:
            continue
        result = t.get("result") if isinstance(t.get("result"), dict) else {}
        for reason in (t.get("hold_reason"), t.get("reason"), result.get("reason")):
            if isinstance(reason, str) and any(m in reason for m in SPAWN_MARKERS):
                out.append((t.get("id"), reason))
                break
    return out


def cooling(repo, now):
    """Any account (or Codex) cooldown in the future in the repo's pool_state.json (written by pool.py)."""
    st = _json(Path(repo) / ".orchestrator" / "pool_state.json", {})
    if not isinstance(st, dict):
        return False
    accounts = st.get("accounts")
    rows = (list(accounts.values()) if isinstance(accounts, dict) else []) + [st.get("codex")]
    return any(isinstance(r, dict) and isinstance(r.get("cooldown_until"), (int, float)) and r["cooldown_until"] > now
               for r in rows)


def queue_stalled(repo, tasks, now):
    """A ready queued execute task, no task file touched for 60 min, and no account cooling."""
    by_id = {t.get("id"): t for t in tasks}
    ready = any(t.get("status") == "queued" and t.get("role") == "execute"
                and all(d in by_id and _merged(by_id[d]) for d in t.get("depends_on") or [])
                for t in tasks)
    if not ready:
        return False
    latest = max(t.get("_mtime", 0) for t in tasks)
    return now - latest >= STALL_S and not cooling(repo, now)


# repos --------------------------------------------------------------------------------------------------------

def extra_repos(config=None):
    try:
        cfg = tomllib.loads(Path(config or CONFIG).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    repos = (cfg.get("watchdog") or {}).get("repos") or []
    return [str(Path(r).expanduser()) for r in repos if isinstance(r, str)]


def watched_repos(config=None):
    """(registered, extra, pruned): only real installs (machine.is_install) are watched; registered entries that
    are not (gone, temp fixtures, no pool.toml) are pruned from the registry, the watchdog's own state."""
    registered = machine.repos()
    pruned = [r for r in registered if not machine.is_install(r)]
    machine.prune_repos(pruned)
    present = [r for r in registered if r not in pruned]
    extras = [r for r in extra_repos(config) if r not in present and machine.is_install(r)]
    return present, extras, pruned


# daemon restart -----------------------------------------------------------------------------------------------

def daemon_env(repo):
    env = dict(os.environ, ORCH_ROOT=str(repo))
    parts = [claude_cli.HOMEBREW_BIN] + [p for p in env.get("PATH", "").split(os.pathsep)
                                         if p and p != claude_cli.HOMEBREW_BIN]
    for found in (claude_cli.resolve(), claude_cli.resolve_uv()):
        if found and os.path.dirname(found) not in parts:
            parts.append(os.path.dirname(found))
    env["PATH"] = os.pathsep.join(parts)
    return env


def daemon_argv(uv):
    """Installed repos run the orchestrator from its own package repo (install.py), not the target's project."""
    return [uv, "run", "--project", str(PACKAGE_REPO), "orchestrator", "daemon"]


def start_daemon(repo):
    """Start `uv run --project <orchestrator> orchestrator daemon` detached for repo; returns the log path."""
    runs = Path(repo) / ".orchestrator" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    log = runs / f"daemon-{time.strftime('%Y%m%dT%H%M%S')}.log"
    uv = claude_cli.resolve_uv() or "uv"
    with open(log, "a") as fh:
        subprocess.Popen(daemon_argv(uv), cwd=str(repo), env=daemon_env(repo),
                         stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return str(log)


def confirm_alive(repo, *, timeout=CONFIRM_S, sleep=time.sleep, clock=time.monotonic):
    end = clock() + timeout
    while True:
        if daemon_alive(repo):
            return True
        if clock() >= end:
            return False
        sleep(1)


# one pass -----------------------------------------------------------------------------------------------------

def evaluate(repo, now, *, registered):
    """Conditions that hold for repo right now: {condition: message}."""
    tasks = load_tasks(repo)
    alive = daemon_alive(repo)
    lock_exists = (Path(repo) / ".orchestrator" / "daemon.lock").exists()
    found = {}
    if not alive and has_work(repo, tasks) and (lock_exists or registered):
        found["daemon_dead"] = f"{repo}: daemon not running with work queued"
    stuck = running_too_long(tasks, now, alive)
    if stuck:
        found["running_stalled"] = f"{repo}: running past 2x timeout with no daemon: {', '.join(stuck)}"
    failures = spawn_failures(tasks, now)
    if failures:
        tid, reason = failures[0]
        found["spawn_failure"] = f"{repo}: spawn failure {tid}: {reason}"[:200]
    if queue_stalled(repo, tasks, now):
        found["queue_stalled"] = f"{repo}: queue stalled (ready tasks, nothing moved for 60 min)"
    return found


def run_once(*, now=None, config=None, start=None, confirm=None, send=None):
    """Evaluate every repo, restart dead daemons, clear resolved keys, then notify new conditions."""
    now = time.time() if now is None else now
    start = start or start_daemon
    confirm = confirm or confirm_alive
    send = send or notify.notify
    registered, extras, missing = watched_repos(config)
    current = {}
    for repo in registered + extras:  # one bad repo is logged and skipped, never aborts the pass
        try:
            for cond, msg in evaluate(repo, now, registered=repo in registered).items():
                current[f"{repo}:{cond}"] = msg
        except Exception as e:
            print(f"[watchdog] {repo}: evaluate failed: {e!r}", file=sys.stderr)
    for repo in registered + extras:
        key = f"{repo}:daemon_dead"
        if key in current:
            try:
                log = start(repo)
                ok = confirm(repo)
            except Exception as e:
                print(f"[watchdog] {repo}: restart failed: {e!r}", file=sys.stderr)
                current[key] += f"; restart failed: {e!r}"[:200]
                continue
            current[key] += f"; restarted, log {log}" if ok else f"; restart did not take the lock within {CONFIRM_S}s, log {log}"
    path = _state_path()
    state = _json(path, {})
    state = state if isinstance(state, dict) else {}
    state = {k: v for k, v in state.items() if k in current}  # cleared conditions and pruned repos drop out
    sent = []
    for key, msg in current.items():
        entry = state.setdefault(key, {"first_seen": now})
        if entry.get("notified_at") is None:
            send(f"watchdog: {msg}")
            entry["notified_at"] = now
            sent.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, path)
    return {"conditions": sorted(current), "notified": sent, "pruned": missing}


def run(once=False, **kw):
    """Single instance: a second watchdog finds the lock held and exits 0 silently."""
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return 0
    try:
        while True:
            try:
                run_once(**kw)
            except Exception as e:
                print(f"[watchdog] pass failed: {e}", file=sys.stderr)
            if once:
                return 0
            time.sleep(INTERVAL_S)
    finally:
        fh.close()


# launchd ------------------------------------------------------------------------------------------------------

LABEL = "com.orchestrator.watchdog"


def launchd_plist(repo_root=None):
    root = Path(repo_root or STATE.parent).resolve()
    uv = claude_cli.resolve_uv() or "/opt/homebrew/bin/uv"
    path = os.pathsep.join([claude_cli.HOMEBREW_BIN, os.path.dirname(uv), "/usr/local/bin", "/usr/bin", "/bin",
                            "/usr/sbin", "/sbin"])
    logs = root / ".orchestrator" / "runs"
    uv, path, root, logs = (escape(str(v)) for v in (uv, path, root, logs))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{uv}</string><string>run</string><string>orchestrator</string><string>watchdog</string><string>--once</string></array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string></dict>
  <key>StartInterval</key><integer>{INTERVAL_S}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{logs}/watchdog.log</string>
  <key>StandardErrorPath</key><string>{logs}/watchdog.log</string>
</dict>
</plist>
"""


def install_launchd_text(repo_root=None):
    target = f"~/Library/LaunchAgents/{LABEL}.plist"
    return (launchd_plist(repo_root) + "\n# Save the plist above to " + target + ", then run:\n"
            f"launchctl bootstrap gui/$(id -u) {target}\n"
            f"launchctl kickstart -k gui/$(id -u)/{LABEL}\n")
