"""Allowlisted worker environments with names-only, best-effort telemetry."""
from datetime import date
from fnmatch import fnmatchcase
import json
from pathlib import Path
import time

from . import STATE, bus

DEFAULT_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL",
    "TMPDIR", "XDG_*", "CLAUDE_*", "ORCH_*", "UV_*", "VIRTUAL_ENV",
    "PYTHONPATH", "NODE_*", "GIT_*", "SSH_AUTH_SOCK",
)
_CREDENTIAL_TOKENS = frozenset((
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "AUTH",
))


def worker_env(role, *, base, extra, cfg, task_id=None, root=None):
    """Filter parent keys only; log to the supplied repository without rebinding state."""
    secrets = cfg.get("secrets", {})
    mode = secrets.get("env_mode", "shadow")
    if mode == "off":
        return {**base, **extra}, []
    if mode not in ("shadow", "active"):
        raise ValueError("invalid secrets.env_mode")
    patterns = secrets.get("env_passthrough", DEFAULT_PASSTHROUGH)
    exact = {pattern for pattern in patterns if not any(c in pattern for c in "*?[")}
    explicit = set(extra) | set(secrets.get(role, {})) | exact

    def allowed(name):
        if name in explicit:
            return True
        if _CREDENTIAL_TOKENS.intersection(name.upper().split("_")):
            return False
        return any(fnmatchcase(name, pattern) for pattern in patterns)

    stripped = sorted(name for name in base if not allowed(name))
    env = dict(base) if mode == "shadow" else {k: v for k, v in base.items() if allowed(k)}
    env.update(extra)
    try:
        fields = dict(role="env_policy", task=task_id, outcome=mode,
                      stripped=stripped, count=len(stripped))
        target = Path(root).resolve() / ".orchestrator" if root is not None else STATE
        if target.resolve() == STATE.resolve():
            bus.log_run(**fields)
        else:
            # Use the bus JSONL envelope without its process-global task/config lookups.
            runs = target / "runs"
            runs.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.time(), "attempt": 1, "context": None, **fields}
            with (runs / f"{date.today().isoformat()}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
    except Exception:
        pass
    return env, stripped
