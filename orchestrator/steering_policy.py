"""Deterministic steering from stale.evidence() and observable worker state."""
import ast
import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path

DEFAULTS = {"mode": "shadow", "stuck_after_s": 900, "out_of_scope_events": 3,
            "min_interval_s": 1800}
STALE_KEYS = {"stale_paths", "risk", "risk_reasons", "signals", "moved_count", "graph"}


def mode(cfg):
    section = cfg.get("steering", {}) if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        return "off"
    value = section.get("mode", "shadow")
    return value if value in ("off", "shadow", "active") else "off"


def matches(path, entries):
    return any(entry in (".", "./") or (fnmatch.fnmatchcase(path, entry) if any(c in entry for c in "*?[")
               else path == entry.rstrip("/") or path.startswith(entry.rstrip("/") + "/"))
               for entry in entries)



def read_scope(task):
    """Mirror spawn._packet_body: tests, scope parents and local Python imports.

    Bus tasks store scope, not packet read_scope. Keep this derivation aligned
    with spawn without building a packet or triggering its routing side effects.
    """
    scope = [str(p) for p in task.get("scope", [])]
    paths = {"tests/"}
    paths.update(str(Path(p).parent) + ("/" if str(Path(p).parent) != "." else "")
                 for p in scope)
    if task.get("worktree"):
        wt = Path(task["worktree"])
        for entry in scope:
            path = wt / entry
            if path.suffix != ".py" or not path.is_file():
                continue
            try:
                tree = ast.parse(path.read_text(errors="replace"))
            except (OSError, SyntaxError):
                continue
            for node in tree.body:
                names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                         [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in filter(None, names):
                    stem = Path(*name.split("."))
                    for candidate in (wt / stem.with_suffix(".py"), wt / stem / "__init__.py"):
                        if candidate.is_file():
                            paths.add(str(candidate.relative_to(wt)))
    return sorted(paths)

def _git(task, *args):
    worktree = task.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        raise OSError("missing worktree")
    result = subprocess.run(["git", *args], cwd=worktree, capture_output=True,
                            text=True, timeout=5, check=False)
    if result.returncode:
        raise OSError("git failed")
    return result.stdout


def changed_paths(task):
    """One porcelain query; -z renames list the destination before the source."""
    entries = iter(_git(task, "status", "--porcelain", "-z", "--untracked-files=all").split("\0"))
    paths = set()
    for entry in entries:
        if not entry:
            continue
        path = entry[3:]
        if "R" in entry[:2] or "C" in entry[:2]:
            next(entries, None)
        parts = Path(path).parts
        if (set(parts) & {"__pycache__", ".orchestrator", ".venv", "node_modules", ".git"}
                or path.endswith(".pyc")):
            continue
        paths.add(path)
    return sorted(paths)


def no_commits(task):
    base = _git(task, "merge-base", "HEAD", "goal/" + task["parent"]).strip()
    return int(_git(task, "rev-list", "--count", base + "..HEAD").strip()) == 0


def gate_history(task, tasks):
    """Stored signatures only, following fix_round_for to the root, newest last."""
    history, seen = [], set()
    while task:
        if task["id"] in seen:
            raise ValueError("cyclic fix lineage")
        seen.add(task["id"])
        constraints = task.get("constraints") or {}
        if (task.get("pipeline") or {}).get("gate_reds", 0) > 0:
            history.append(constraints["failure_signature"])
        parent = constraints.get("fix_round_for")
        task = tasks[parent] if parent else None
    return history[::-1]


def evaluate(task, *, tasks, registry_doc, stale_evidence, gate_history, cfg, critical, now):
    """Use stale.evidence's stale_paths, risk, risk_reasons, signals, moved_count,
    and graph keys. Missing inputs and unexpected errors never steer a worker.
    Git unavailability skips scope detection and prohibits a cancel candidate.
    """
    result = {"action": "continue", "trigger": None, "reasons": [], "message": None,
              "evidence": {}, "severity": "none"}
    try:
        if (not isinstance(stale_evidence, dict) or not STALE_KEYS <= stale_evidence.keys()
                or not registry_doc or gate_history is None or tasks is None
                or type(critical) is not bool or now is None):
            result["reasons"] = ["missing_input"]
            return result
        settings = {**DEFAULTS, **(cfg.get("steering") or {})}
        last_event_at = registry_doc["last_event_at"]
        status = task["status"]
        read = read_scope(task)
        write = task.get("write_scope", task.get("scope"))
        if write is None:
            raise KeyError("write_scope")
        changed = sorted(p for p in stale_evidence["stale_paths"] if matches(p, read))
        security = (cfg.get("review") or {}).get("security_paths") or ["orchestrator/*.py"]
        security_changed = [p for p in changed if matches(p, security)]
        try:
            outside = [p for p in changed_paths(task) if not matches(p, write + read)]
        except (OSError, subprocess.TimeoutExpired):
            outside = []
            result["reasons"].append("git_unavailable")
        result["evidence"] = {"changed_paths": changed, "outside_paths": outside,
                              "risk": stale_evidence["risk"], "gate_history": gate_history[-2:],
                              "critical": critical}
        trigger, details = None, []
        if security_changed:
            trigger, details = "security_concern", security_changed
            result["action"] = "steer"
            if not critical:
                try:
                    result["evidence"]["no_commits"] = no_commits(task)
                    if result["evidence"]["no_commits"]:
                        result["action"] = "cancel"
                except (OSError, subprocess.TimeoutExpired, KeyError, ValueError):
                    result["reasons"].append("git_unavailable")
        elif changed and stale_evidence["risk"] in ("medium", "high"):
            trigger, details = "dependency_changed", changed
        elif (status == "running" and
              now - last_event_at >= settings["stuck_after_s"]):
            trigger, details = "stuck", ["no registry event since " + str(registry_doc["last_event_at"])]
            result["evidence"]["last_event_at"] = registry_doc["last_event_at"]
        elif len(outside) > settings["out_of_scope_events"]:
            trigger, details = "out_of_scope", outside
        elif len(gate_history) >= 2 and gate_history[-1] and gate_history[-1] == gate_history[-2]:
            trigger, details = "repeated_failure", [str(gate_history[-1])]
        if trigger:
            result.update(trigger=trigger, severity="high" if trigger == "security_concern" else "medium",
                          message=trigger + ": " + ", ".join(details))
            if result["action"] == "continue":
                result["action"] = "steer"
            result["reasons"].append(trigger)
        return result
    except KeyError:
        return {**result, "action": "continue", "trigger": None, "message": None,
                "reasons": ["missing_input"]}
    except Exception:
        return {**result, "action": "continue", "trigger": None, "message": None,
                "reasons": ["evaluation_error"]}


def evidence_hash(result):
    return hashlib.sha256(json.dumps(result["evidence"], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
