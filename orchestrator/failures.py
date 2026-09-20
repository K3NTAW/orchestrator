"""Validated failure evidence and conservative change-risk classification."""
import fnmatch, hashlib, json, re, subprocess, tomllib
from pathlib import Path
from . import gitutil, bus, spawn, merge

_PATH_TEST_ID = re.compile(r"[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_.\[\]]+)*\Z")
_DOTTED_TEST_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z")


def root(task):
    """Return the root task of a fix-round chain."""
    seen = set()
    current = task
    while current.get("constraints", {}).get("fix_round_for") and current["id"] not in seen:
        seen.add(current["id"])
        try:
            current = bus.get(current["constraints"]["fix_round_for"])
        except KeyError:
            break
    return current

def lineage(task):
    root_id = root(task)["id"]
    return [t for t in bus.read() if root(t)["id"] == root_id]

def _valid_test_id(value):
    return (isinstance(value, str) and value and ".." not in value and not value.startswith("-")
            and (_PATH_TEST_ID.fullmatch(value) or _DOTTED_TEST_ID.fullmatch(value)))

def _test_id_candidates(failures):
    if isinstance(failures, list):
        failures = "\n".join(str(line) for line in failures)
    if not isinstance(failures, str):
        return []
    candidates = []
    for line in failures.splitlines():
        if line.startswith("FAILED "):
            candidates.extend(line[7:].split(" - ", 1)[0].split())
        else:
            match = re.match(r"^(?:FAIL|ERROR):\s+[^\n]*\(([^)]+)\)", line)
            if match:
                candidates.append(match.group(1))
    return candidates

def _test_ids_with_rejections(failures):
    ids, rejected = [], []
    for candidate in _test_id_candidates(failures):
        (ids if _valid_test_id(candidate) else rejected).append(candidate)
    return (ids or None), rejected

def _test_ids(failures):
    return _test_ids_with_rejections(failures)[0]

def _path_in_scope(path, scope):
    return bool(path and any(fnmatch.fnmatch(path, p) or path.startswith(p.rstrip("/") + "/")
                             for p in scope))

def _rejecting_reviews(task):
    reviews = [r for r in bus.read(role="review") if r.get("inputs", [])[:1] == [task["id"]]]
    result = []
    for review in reviews:
        verdict = _review_verdict(review, task, len(reviews) == 1)
        if verdict == "request_changes":
            result.append((review, (review.get("result") or {}).get("comments") or []))
    return result

def _normal_issue(issue):
    """Make review prose stable across line-number and incidental numeric changes."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", str(issue or ""))).strip().lower()

def failure_signature(task):
    """A stable, content-based identity for the failure that caused a hold."""
    hint = task.get("resume_hint") or {}
    ids = sorted(_test_ids(hint.get("failures")) or [])
    comments = sorted((str(c.get("path") or ""), _normal_issue(c.get("issue")))
                      for _, cs in _rejecting_reviews(task) for c in cs)
    payload = json.dumps({"kind": (task.get("pipeline") or {}).get("failure_kind") or "unknown",
                          "tests": ids, "comments": comments}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]

def _failure_text(task):
    hint = task.get("resume_hint") or {}
    return "\n".join(str(x) for x in (hint.get("failures") or "")) if isinstance(hint.get("failures"), list) \
        else str(hint.get("failures") or "")

def _node_id_to_unittest(node_id):
    if not _valid_test_id(node_id):
        return None
    if _DOTTED_TEST_ID.fullmatch(node_id):
        return node_id
    path, *parts = node_id.split("::")
    if not path.endswith(".py") or not parts:
        return None
    dotted_path = path[:-3].replace("/", ".").replace("\\", ".")
    if not dotted_path or any(not part for part in dotted_path.split(".")):
        return None
    return ".".join([dotted_path, *parts])

class _RunnerProbeTimeout(Exception):
    def __init__(self, timeout_s):
        self.timeout_s = timeout_s

def _flaky_rerun_command(ids, worktree, *, probe_timeout=60):
    """Match the project's test convention, including its unittest fallback."""
    try:
        probe = subprocess.run(["uv", "run", "--project", worktree, "python", "-c", "import pytest"],
                               cwd=worktree, capture_output=True, text=True, timeout=probe_timeout)
    except subprocess.TimeoutExpired as exc:
        raise _RunnerProbeTimeout(probe_timeout) from exc
    if probe.returncode == 0:
        return ["uv", "run", "--project", worktree, "pytest", "-q", "-x", *ids]
    converted = [_node_id_to_unittest(node_id) for node_id in ids]
    if any(node_id is None for node_id in converted):
        return None
    return ["uv", "run", "--project", worktree, "python", "-m", "unittest", *converted]

def failure_kind(task, worktree, *, rerun_max=1, rerun_timeout=600):
    """Classify a held execution failure without relying on an LLM judgment."""
    reason = str(task.get("hold_reason") or "").lower()
    text = (reason + "\n" + _failure_text(task)).lower()
    if "conflict" in reason or "rebase_conflict" in text:
        return "conflict"
    if any(marker in text for marker in ("modulenotfounderror", "no module named", "enoent",
                                          "command not found", "missing venv", "missing .venv", "uv: error")):
        return "environment"
    if any(marker in text for marker in ("eacces", "permission denied", "sandbox")):
        return "permissions"
    if any(marker in text for marker in ("cooling", "usage limit", "usage-limit", "quota", "rate limit")):
        return "quota"
    comments = _rejecting_reviews(task)
    issues = "\n".join(str(c.get("issue") or "").lower() for _, cs in comments for c in cs)
    if ("spec" in issues and ("contradict" in issues or "impossible" in issues)) or "acceptance cannot" in issues:
        return "invalid_spec"
    ids, rejected = _test_ids_with_rejections((task.get("resume_hint") or {}).get("failures"))
    if rejected:
        hint = dict(task.get("resume_hint") or {})
        hint["rejected_ids"] = list(dict.fromkeys([*(hint.get("rejected_ids") or []), *rejected]))
        bus.update(task["id"], resume_hint=hint)
    if not ids:
        return "unknown"
    # A rerun is deliberately restricted to the failing ids.  A missing worktree is not evidence of flakiness.
    runs = (task.get("resume_hint") or {}).get("flaky_runs") or []
    if reason == "gate_red" and any(run.get("returncode") == 0 and run.get("ids") == ids for run in runs):
        return "flaky"
    if (reason == "gate_red" and ids and worktree and Path(worktree).is_dir()
            and len(runs) < rerun_max):
        try:
            probe_timeout = max(30, min(60, rerun_timeout / 10))
            command = _flaky_rerun_command(ids, worktree, probe_timeout=probe_timeout)
            if command is None:
                return "unknown"
            rerun = subprocess.run(command, cwd=worktree, capture_output=True, text=True,
                                   timeout=rerun_timeout)
        except _RunnerProbeTimeout as exc:
            hint = dict(task.get("resume_hint") or {})
            hint["runner_probe"] = "timeout"
            hint["runner_probe_timeout_s"] = exc.timeout_s
            bus.update(task["id"], resume_hint=hint)
            return "unknown"
        except subprocess.TimeoutExpired as exc:
            hint = dict(task.get("resume_hint") or {})
            runs = list(hint.get("flaky_runs") or [])
            output = (str(getattr(exc, "stdout", "") or getattr(exc, "output", "")) +
                      str(getattr(exc, "stderr", "") or ""))[-4000:]
            runs.append({"ids": ids, "timed_out": True, "timeout_s": rerun_timeout, "output": output})
            hint["flaky_runs"] = runs
            bus.update(task["id"], resume_hint=hint)
            return "code_defect"
        except OSError:
            return "code_defect"
        hint = dict(task.get("resume_hint") or {})
        runs = list(hint.get("flaky_runs") or [])
        runs.append({"ids": ids, "returncode": rerun.returncode, "output": (rerun.stdout + rerun.stderr)[-4000:]})
        hint["flaky_runs"] = runs
        bus.update(task["id"], resume_hint=hint)
        if rerun.returncode == 0:
            return "flaky"
    return "code_defect"

def _review_verdict(r, src, allow_src_fallback):
    """A review task's own review_verdict/result -- not src's -- is the reliable source once a task can carry two
    reviews: spawn.run_worker writes review_verdict onto both the review task and src, so with two reviews the
    second to finish clobbers src's field with its own verdict. Each review's own field is never touched by its
    sibling, so it is checked first; src is only a fallback for older data that predates this field existing on
    r, and only when allow_src_fallback is true -- callers pass that as (len(reviews) == 1), since with two or
    more reviews src's single field cannot speak for more than one of them."""
    v = r.get("review_verdict") or (r.get("result") or {}).get("verdict")
    if v:
        return v
    return src.get("review_verdict") if allow_src_fallback else None


test_ids = _test_ids
rejecting_reviews = _rejecting_reviews
path_in_scope = _path_in_scope

DEFAULT_AREAS = {
    "auth": {"paths": ["**/auth*", "auth*", "**/permissions*"],
             "patterns": [r"(?i)\b(?:bearer|authenticate|authorize|permission)\b"]},
    "migrations": {"paths": ["**/migrations/**", "migrations/**"],
                   "patterns": [r"(?i)\b(?:ALTER|CREATE|DROP) TABLE\b"]},
    "interfaces": {"paths": ["**/api/**", "orchestrator/mcp.py", "orchestrator/cli.py"],
                   "patterns": [r"^\s*__all__\s*=", r"@mcp\.tool|add_parser\("]},
    "data_deletion": {"paths": [], "patterns": [r"(?i)\b(?:DELETE FROM|TRUNCATE|DROP TABLE)\b", r"\b(?:rmtree|unlink)\("]},
}


def touch_areas(task, cfg=None):
    """Inspect both paths and added lines; unavailable evidence marks every area."""
    if cfg is None:
        try:
            cfg = tomllib.loads((bus.STATE / "pool.toml").read_text())
        except (OSError, ValueError):
            cfg = {}
    tables = cfg.get("review", {}).get("areas", {})
    try:
        paths = gitutil.changed_paths(task)
        lines = gitutil._added_diff_lines(task)
    except (OSError, ValueError):
        paths = lines = None
    if paths is None or lines is None:
        return {area: True for area in DEFAULT_AREAS}
    result = {}
    for area, default in DEFAULT_AREAS.items():
        table = tables.get(area, default)
        try:
            patterns = table.get("patterns", [])
            if isinstance(patterns, dict):
                patterns = patterns.values()
            result[area] = (any(fnmatch.fnmatch(path, glob) for path in paths for glob in table.get("paths", []))
                            or any(re.search(pattern, line) for line in lines for pattern in patterns))
        except (AttributeError, TypeError, re.error):
            result[area] = True
    return result
