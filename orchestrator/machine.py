"""Machine-wide leases and account usage shared by all orchestrator repos."""

import errno
import fcntl
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


MACHINE_DIR = Path(os.environ.get("ORCH_MACHINE_DIR", "~/.orchestrator-machine")).expanduser()
LEASES = MACHINE_DIR / "leases.json"
ACCOUNTS = MACHINE_DIR / "accounts.json"
LOCK = MACHINE_DIR / "machine.lock"
REPOS = MACHINE_DIR / "repos.json"
WINDOW_S = 5 * 3600
NOFILE_SOFT_DEFAULT = 65536  # [daemon].nofile_soft; 0 leaves the limit alone


def _nofile_setting(cfg) -> int:
    if cfg is None:
        try:
            from .pool import config
            cfg = config()
        except Exception:
            cfg = {}
    value = (cfg.get("daemon") or {}).get("nofile_soft", NOFILE_SOFT_DEFAULT)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return NOFILE_SOFT_DEFAULT
    return value


def raise_nofile(cfg=None, *, label: str = "orchestrator", log=None):
    """Raise the soft RLIMIT_NOFILE to min(hard, [daemon].nofile_soft) at daemon/watchdog start.

    launchd starts agents with a soft limit of 256, which gates and workers inherit and then hit EMFILE.
    Children spawned afterwards inherit the raised limit. Never lowers a higher soft limit.
    Returns (old, new) when the limit changed, else None."""
    target = _nofile_setting(cfg)
    if target == 0:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if hard != resource.RLIM_INFINITY:
        target = min(target, hard)
    if soft == resource.RLIM_INFINITY or soft >= target:
        return None
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError) as exc:
        print(f"[{label}] could not raise open-file soft limit {soft} -> {target}: {exc}", file=log or sys.stderr)
        return None
    print(f"[{label}] open-file soft limit {soft} -> {target}", file=log or sys.stderr)
    return soft, target


def _day_key(now: float) -> str:
    local = time.localtime(now)
    return f"{local.tm_year:04d}-{local.tm_yday:03d}"


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


@contextmanager
def _machine_state():
    MACHINE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(path: Path, default):
    try:
        with path.open() as stream:
            return json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, value) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=MACHINE_DIR)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


_START_CACHE: dict = {}
START_CACHE_S = 5.0


def _cached_start(pid):
    """process_start(pid), cached per pid for START_CACHE_S. Callers hold the machine flock, so `ps` (10 s
    timeout) can still run under the flock, but at most once per pid per START_CACHE_S."""
    now = time.time()
    hit = _START_CACHE.get(pid)
    if hit is not None and now - hit[0] < START_CACHE_S:
        return hit[1]
    value = process_start(pid)
    _START_CACHE[pid] = (now, value)
    return value


def _entry_dead(entry: dict, check_start: bool = True) -> bool:
    """Dead when the pid is dead, or when the entry carries a non-empty proc_start and process_start(pid) is a
    different non-None string. process_start None keeps the entry alive; entries without proc_start use pid
    liveness only."""
    pid = entry.get("pid")
    if not _pid_alive(pid):
        return True
    recorded = entry.get("proc_start")
    if not check_start or not recorded:
        return False
    current = _cached_start(pid)
    return current is not None and current != recorded


def _sweep(leases: dict, kind: str | None = None, check_start: bool = True) -> bool:
    dead = [lease_id for lease_id, lease in leases.items()
            if (kind is None or lease.get("kind") == kind)
            and _entry_dead(lease.get("holder") or {}, check_start)]
    for lease_id in dead:
        del leases[lease_id]
    return bool(dead)


def _waiters_path(kind: str) -> Path:
    return MACHINE_DIR / f"waiters-{kind}.json"


def _read_waiters(kind: str) -> list:
    waiters = _read(_waiters_path(kind), [])
    return [w for w in waiters if isinstance(w, dict)] if isinstance(waiters, list) else []


def _sweep_waiters(waiters: list, check_start: bool = True) -> list:
    return [w for w in waiters if not _entry_dead(w, check_start)]


def _count(leases: dict, kind: str) -> int:
    return sum(lease.get("kind") == kind for lease in leases.values())


def _blocked(leases: dict, waiters: list, kind: str, cap: int, ticket) -> bool:
    if _count(leases, kind) >= cap:
        return True
    if ticket is None:
        return False
    return not waiters or waiters[0].get("ticket") != ticket


def acquire(kind: str, cap: int, holder: dict, ticket: str | None = None) -> str | None:
    """Acquire a slot of *kind*, returning its lease id when capacity permits.

    With *ticket*, the slot is granted only to the oldest live waiter ticket of *kind* (FIFO), and the ticket
    is removed on success. A free slot never runs `ps`: proc_start checks (cached) run only when blocked."""
    with _machine_state():
        leases = _read(LEASES, {})
        waiters = _read_waiters(kind) if ticket is not None else []
        before = len(waiters)
        changed = _sweep(leases, kind, check_start=False)
        waiters = _sweep_waiters(waiters, check_start=False)
        if _blocked(leases, waiters, kind, cap, ticket):
            changed = _sweep(leases, kind) or changed
            waiters = _sweep_waiters(waiters)
        if _blocked(leases, waiters, kind, cap, ticket):
            if changed:
                _write(LEASES, leases)
            if len(waiters) != before:
                _write(_waiters_path(kind), waiters)
            return None
        lease_id = uuid.uuid4().hex
        leases[lease_id] = {"kind": kind, "holder": {**holder, "since": time.time()}}
        _write(LEASES, leases)
        if ticket is not None:
            _write(_waiters_path(kind), waiters[1:])
        return lease_id


def release(lease_id: str) -> None:
    """Release a lease; releasing an unknown lease is harmless."""
    with _machine_state():
        leases = _read(LEASES, {})
        if lease_id in leases:
            del leases[lease_id]
            _write(LEASES, leases)


def holders(kind: str) -> list:
    """Holder dicts of the live leases of *kind* (copies; sweeps first)."""
    with _machine_state():
        leases = _read(LEASES, {})
        if _sweep(leases, kind):
            _write(LEASES, leases)
        return [dict(lease.get("holder") or {}) for lease in leases.values() if lease.get("kind") == kind]


def wait_ticket(kind: str, holder: dict) -> str:
    """Register a FIFO waiter for *kind* and return its ticket; pass it to acquire, drop it with drop_ticket."""
    with _machine_state():
        waiters = _sweep_waiters(_read_waiters(kind))
        ticket = uuid.uuid4().hex
        waiters.append({"ticket": ticket, "pid": holder.get("pid"), "proc_start": holder.get("proc_start"),
                        "since": time.time()})
        _write(_waiters_path(kind), waiters)
        return ticket


def drop_ticket(kind: str, ticket: str) -> None:
    """Remove a waiter ticket; dropping an unknown ticket is harmless."""
    with _machine_state():
        waiters = _read_waiters(kind)
        kept = [w for w in waiters if w.get("ticket") != ticket]
        if len(kept) != len(waiters):
            _write(_waiters_path(kind), kept)


def count(kind: str) -> int:
    with _machine_state():
        leases = _read(LEASES, {})
        changed = _sweep(leases, kind)
        if changed:
            _write(LEASES, leases)
        return sum(lease.get("kind") == kind for lease in leases.values())


def _fresh_account() -> dict:
    return {
        "window_start": None,
        "window_tokens": 0,
        "planner_window_tokens": 0,
        "day": None,
        "day_tokens": 0,
        "planner_day_tokens": 0,
    }


def _roll(account: dict, now: float) -> bool:
    changed = False
    start = account.get("window_start")
    if start is None or now - start >= WINDOW_S:
        if start is not None:
            changed = True
        account["window_start"] = now
        account["window_tokens"] = 0
        account["planner_window_tokens"] = 0
    day = _day_key(now)
    if account.get("day") != day:
        if account.get("day") is not None:
            changed = True
        account["day"] = day
        account["day_tokens"] = 0
        account["planner_day_tokens"] = 0
    return changed


def record_usage(account_id: str, tokens: int, *, repo: str, planner: bool = False,
                 now: float | None = None) -> None:
    del repo  # The machine ledger intentionally aggregates all repositories.
    now = time.time() if now is None else now
    with _machine_state():
        accounts = _read(ACCOUNTS, {})
        account = accounts.setdefault(account_id, _fresh_account())
        _roll(account, now)
        window_field = "planner_window_tokens" if planner else "window_tokens"
        day_field = "planner_day_tokens" if planner else "day_tokens"
        account[window_field] += tokens
        account[day_field] += tokens
        _write(ACCOUNTS, accounts)


def usage(account_id: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _machine_state():
        accounts = _read(ACCOUNTS, {})
        account = accounts.setdefault(account_id, _fresh_account())
        changed = _roll(account, now)
        if changed or account_id not in accounts:
            _write(ACCOUNTS, accounts)
        return {
            "window_tokens": account["window_tokens"],
            "day_tokens": account["day_tokens"],
            "planner_window_tokens": account["planner_window_tokens"],
            "planner_day_tokens": account["planner_day_tokens"],
        }


def process_start(pid) -> str | None:
    """A process's start time as `ps -o lstart=` prints it in the C locale and UTC, whitespace-normalised, so a
    daemon and the watchdog render the same string whatever their own locale or TZ. None when unreadable."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout
    except Exception:
        return None
    return " ".join(out.split()) if isinstance(out, str) and out.strip() else None


def _temp_roots() -> tuple:
    return (Path(tempfile.gettempdir()).resolve(), Path("/private/var/folders"))


def is_install(path) -> bool:
    """A real orchestrator install: <path>/.orchestrator/pool.toml exists and the path is no temp fixture."""
    try:
        path = Path(path).resolve()
        if not (path / ".orchestrator" / "pool.toml").is_file():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    return not any(path == root or path.is_relative_to(root) for root in _temp_roots())


def register_repo(path) -> None:
    """Record a repo whose daemon took its lock, so the watchdog watches it without configuration. Only real
    installs are recorded (is_install), so test fixtures never reach the machine registry."""
    if not is_install(path):
        return
    path = str(Path(path).resolve())
    with _machine_state():
        known = _read(REPOS, [])
        if not isinstance(known, list):
            known = []
        if path not in known:
            known.append(path)
            _write(REPOS, known)


def repos() -> list:
    with _machine_state():
        known = _read(REPOS, [])
    return [p for p in known if isinstance(p, str)] if isinstance(known, list) else []


def prune_repos(paths) -> None:
    """Drop the given paths (repos that are gone or no real install) from the registry."""
    drop = {str(p) for p in paths}
    if not drop:
        return
    with _machine_state():
        known = _read(REPOS, [])
        if isinstance(known, list):
            _write(REPOS, [p for p in known if p not in drop])
