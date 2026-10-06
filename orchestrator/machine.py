"""Machine-wide leases and account usage shared by all orchestrator repos."""

import errno
import fcntl
import json
import os
import subprocess
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


def _sweep(leases: dict, kind: str | None = None) -> bool:
    dead = [lease_id for lease_id, lease in leases.items()
            if (kind is None or lease.get("kind") == kind)
            and not _pid_alive(lease.get("holder", {}).get("pid"))]
    for lease_id in dead:
        del leases[lease_id]
    return bool(dead)


def acquire(kind: str, cap: int, holder: dict) -> str | None:
    """Acquire a slot of *kind*, returning its lease id when capacity permits."""
    with _machine_state():
        leases = _read(LEASES, {})
        changed = _sweep(leases, kind)
        if sum(lease.get("kind") == kind for lease in leases.values()) >= cap:
            if changed:
                _write(LEASES, leases)
            return None
        lease_id = uuid.uuid4().hex
        leases[lease_id] = {"kind": kind, "holder": dict(holder)}
        _write(LEASES, leases)
        return lease_id


def release(lease_id: str) -> None:
    """Release a lease; releasing an unknown lease is harmless."""
    with _machine_state():
        leases = _read(LEASES, {})
        if lease_id in leases:
            del leases[lease_id]
            _write(LEASES, leases)


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
