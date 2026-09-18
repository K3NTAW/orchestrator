"""Goal lifecycle: launch a headless Planner session against any target repo, track it, reconcile status, stop it.

Import boundary (T-0115): this module may import only install.install, spawn.trust_workspace and
spawn.resolve_secrets from the rest of the package. It never calls Pool(), never calls any
other bus.*/spawn.* function, and never references orchestrator.ROOT/STATE. Every path it touches derives from
the realpath'd repo_path argument, so the same code works against this repo or any target repo scaffolded by
install.install.
"""
import fcntl, json, os, re, signal, subprocess, sys, time, tomllib
from pathlib import Path
from subprocess import Popen  # distinct from subprocess.run: tests fake this call without disturbing
                               # subprocess.run itself, which internally resolves Popen dynamically too

from .install import install
from .spawn import trust_workspace, resolve_secrets

PACKAGE_REPO = Path(__file__).resolve().parents[1]  # this repo, regardless of the target repo_path

GOALS_LOCK_NAME = "goals.lock"  # dedicated lock file: bus.locked() is non-reentrant on the same fd,
                                 # and this module never shares state with the bus

SCAFFOLD_PATHS = [".orchestrator", ".claude", "skills", ".gitignore",
                   ".mcp.planner.json", ".mcp.review.json", ".mcp.scout.json", ".mcp.triage.json"]

PR_URL_RE = re.compile(r"https?://github\.com/\S+?/pull/\d+")

# Reads ORCH_GOAL_TEXT from the environment, never from string interpolation: goal_text is untrusted (Planner
# input), and a formatted script would let it inject Python. Keep this a plain constant, never .format()/f-string'd.
SCRIPT = """import os
from orchestrator import bus

text = os.environ["ORCH_GOAL_TEXT"]
t = bus.create_task(title=("GOAL: " + text)[:200], spec=text, acceptance=["Planner closes the goal with a PR"],
                     scope=["**"], role="triage", complexity=5)
print(t["id"])
"""


def _git(repo_path, *args):
    return subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, text=True)


def _precheck_git(repo_path):
    r = _git(repo_path, "rev-parse", "--show-toplevel")
    if r.returncode or os.path.realpath(r.stdout.strip()) != os.path.realpath(repo_path):
        return "repo_path is not a git toplevel"
    if _git(repo_path, "symbolic-ref", "-q", "HEAD").returncode:
        return "HEAD is detached; attach it to a branch first"
    git_dir = _git(repo_path, "rev-parse", "--git-dir").stdout.strip()
    git_dir_path = Path(repo_path) / git_dir if not Path(git_dir).is_absolute() else Path(git_dir)
    if (git_dir_path / "rebase-merge").exists() or (git_dir_path / "rebase-apply").exists():
        return "a rebase is in progress"
    if (git_dir_path / "MERGE_HEAD").exists():
        return "a merge is in progress"
    return None


def _staged_changes_conflict(repo_path):
    """True if the target's index already has staged changes. `git commit --only -- <paths>` would happily
    leave those staged and uncommitted, but a caller who staged something on purpose (mid rebase, a manual fix)
    should not have a goal-start silently ignore it -- refuse instead and leave everything untouched."""
    return _git(repo_path, "diff", "--cached", "--quiet").returncode == 1


def _parse_status_z(output):
    """Parse `git status -z --porcelain` output into (code, path) pairs. -z NUL-delimits entries and never
    quotes/escapes paths (unlike the line-oriented form), so a path containing a space round-trips correctly.
    A rename/copy entry ("R" or "C" in either status column) carries an extra NUL-separated field -- the
    original path -- which is consumed here but not returned; only the current path is needed to add/commit."""
    tokens = output.split("\0")
    entries = []
    i = 0
    while i < len(tokens) and tokens[i]:
        tok = tokens[i]
        code, path = tok[:2], tok[3:]
        entries.append((code, path))
        i += 1
        if code[0] in "RC" or code[1] in "RC":
            i += 1  # skip the rename/copy source-path field
    return entries


def _scaffold_commit(repo_path, install_report):
    """git status -z --porcelain --ignored over SCAFFOLD_PATHS. One of the SCAFFOLD_PATHS entries itself reported
    wholesale-ignored (git collapses a fully-untracked, fully-ignored directory to one "!!" entry matching it
    exactly) refuses -- a worktree cut from a branch with that scaffold would come up empty. A nested path
    ignored on purpose (install.py's own .gitignore additions: bus.lock, pool_state.json, runs/, ...) is left
    out of the add/commit but doesn't block it. Otherwise stages and commits (via `commit --only`, so nothing
    else in the index rides along) the (non-ignored) paths status names, returning the resulting sha (or None
    when nothing changed)."""
    r = _git(repo_path, "status", "-z", "--porcelain", "--ignored", "--", *SCAFFOLD_PATHS)
    entries = _parse_status_z(r.stdout)
    for code, path in entries:
        if code == "!!" and path.rstrip("/") in SCAFFOLD_PATHS:
            return None, {"launched": False,
                          "reason": f"{path} is gitignored in the target; worktrees would lack the scaffold"}
    paths = [path for code, path in entries if code != "!!"]
    if not paths:
        return None, None
    add = _git(repo_path, "add", "--", *paths)
    if add.returncode:
        return None, {"launched": False, "reason": f"git add failed: {add.stderr.strip()[:300]}"}
    body = "\n".join(install_report or [])
    commit = _git(repo_path, "commit", "--only", "-m", "orchestrator: scaffold (goal start)", "-m", body, "--", *paths)
    if commit.returncode:
        return None, {"launched": False, "reason": f"scaffold commit failed: {commit.stderr.strip()[:300]}"}
    sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    return sha, None


def _create_goal_task(repo_path, goal_text):
    env = {**os.environ, "ORCH_ROOT": str(repo_path), "ORCH_GOAL_TEXT": goal_text}
    try:
        r = subprocess.run(["uv", "run", "--project", str(PACKAGE_REPO), "python", "-"],
                           input=SCRIPT, capture_output=True, text=True, env=env)
    except FileNotFoundError as e:
        return None, {"launched": False, "reason": f"uv not found on PATH: {e}"}
    out = (r.stdout or "").strip().splitlines()
    goal_id = out[-1].strip() if out else ""
    if r.returncode != 0 or not re.fullmatch(r"T-\d+", goal_id):
        return None, {"launched": False,
                       "reason": f"GOAL task creation failed (rc={r.returncode}): {(r.stderr or r.stdout)[-500:]}"}
    return goal_id, None


def _filter_target_secrets(mapping):
    """The target repo's own pool.toml is untrusted content, not instructions. resolve_secrets() normally runs
    an arbitrary shell command for any value that isn't "env:NAME" -- fine for this repo's own pool.toml, not
    safe for one a target repo authored. Keep only env:NAME passthroughs; anything else is dropped (never
    executed) with a stderr note naming it."""
    out = {}
    for name, value in mapping.items():
        if isinstance(value, str) and value.startswith("env:"):
            out[name] = value
        else:
            print(f"ignored non-env secret {name} from target pool.toml", file=sys.stderr)
    return out


def _goals_path(repo_path):
    return Path(repo_path) / ".orchestrator" / "runs" / "goals.json"


def _lock_path(repo_path):
    return Path(repo_path) / ".orchestrator" / GOALS_LOCK_NAME


def _with_goals_lock(repo_path, fn):
    """Run fn(records) -> (result, new_records_or_None) under an flock on <repo_path>/.orchestrator/goals.lock --
    a lock dedicated to this module, never bus.lock (bus.locked() is reentrant per-thread only, and goals.py
    isn't part of that call tree). new_records_or_None: pass None to leave goals.json untouched, or a list to
    overwrite it."""
    lock_path = _lock_path(repo_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    goals_path = _goals_path(repo_path)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            records = json.loads(goals_path.read_text()) if goals_path.exists() else []
            result, new_records = fn(records)
            if new_records is not None:
                goals_path.parent.mkdir(parents=True, exist_ok=True)
                goals_path.write_text(json.dumps(new_records, indent=2) + "\n")
            return result
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _append_goal_record(repo_path, record):
    def fn(records):
        records.append(record)
        return None, records
    _with_goals_lock(repo_path, fn)


def _set_goal_status(repo_path, goal_id, status, **extra):
    def fn(records):
        for r in records:
            if r.get("goal_id") == goal_id:
                r["status"] = status
                r.update(extra)
        return None, records
    _with_goals_lock(repo_path, fn)


def _proc_start(pid):
    """Best-effort process start time, used only to tell a live pid apart from a reused one. None means
    "can't tell on this platform" -- identity() then trusts liveness alone."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            btime = None
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    btime = int(line.split()[1])
            rparen = stat.rfind(")")
            fields = stat[rparen + 2:].split()
            starttime_ticks = int(fields[19])
            clk_tck = os.sysconf("SC_CLK_TCK")
            return btime + starttime_ticks / clk_tck if btime is not None else None
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           env={**os.environ, "LC_ALL": "C"})
        if r.returncode or not r.stdout.strip():
            return None
        try:
            from datetime import datetime
            return datetime.strptime(r.stdout.strip(), "%a %b %e %H:%M:%S %Y").timestamp()
        except ValueError:
            return None
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def identity_of(pid, pid_start):
    """True unless the pid is dead, or alive but demonstrably a different, reused pid."""
    if not _alive(pid):
        return False
    if pid_start is None:
        return True
    now_start = _proc_start(pid)
    return now_start is None or abs(now_start - pid_start) < 5


def identity(record):
    return identity_of(record["pid"], record.get("pid_start"))


def _read_task(repo_path, tid):
    p = Path(repo_path) / ".orchestrator" / "tasks" / f"{tid}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _read_all_tasks(repo_path):
    tasks_dir = Path(repo_path) / ".orchestrator" / "tasks"
    if not tasks_dir.exists():
        return []
    out = []
    for p in sorted(tasks_dir.glob("T-*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return out


def _running_goal(repo_path):
    """A record in this repo's goals.json whose status is "running" and whose pid identity still checks out --
    i.e. a Planner session that's actually still alive, not just one nobody has reconciled yet."""
    for r in _load_goal_records(repo_path):
        if r.get("status") == "running" and identity(r):
            return r
    return None


def _preview_cfg(pool_toml):
    """What [claude_accounts]/[models]/[limits] would look like once install() runs: the target's own pool.toml
    when it already has one (install() keeps it, "kept"), otherwise this repo's default pool.toml (install()
    would copy it verbatim since the target has none). Lets start() validate account/model/budget config before
    install() writes a single file, so a refusal here leaves the target untouched."""
    src = pool_toml if pool_toml.exists() else (PACKAGE_REPO / ".orchestrator" / "pool.toml")
    return tomllib.loads(src.read_text())


def launch_planner(repo_path, prompt, account_id, max_budget_usd, log_path, extra_env=None):
    """Start one headless Planner subprocess against repo_path. Reads the target's own pool.toml fresh on every
    call -- so an account's oauth_token_env or secrets.planner section added since a previous launch takes effect
    immediately, and this never risks disagreeing with a caller's own (possibly stale) cfg about which pool.toml
    won. Returns {pid, pid_start, log} for the caller to persist; never touches goals.json itself, so callers that
    don't track a goal (e.g. an autonomous decision run) can use it too."""
    repo_path = Path(os.path.realpath(repo_path))
    cfg = tomllib.loads((repo_path / ".orchestrator" / "pool.toml").read_text())
    accounts = {a["id"]: a for a in cfg.get("claude_accounts", [])}
    account = accounts[account_id]
    trust_workspace(account["config_dir"], repo_path)

    env = {**os.environ, "CLAUDE_CONFIG_DIR": os.path.expanduser(account["config_dir"]), "ORCH_ROOT": str(repo_path)}
    oauth_var = account.get("oauth_token_env")
    if oauth_var and os.environ.get(oauth_var):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ[oauth_var]
    env.update(resolve_secrets(_filter_target_secrets(cfg.get("secrets", {}).get("planner", {}))))
    if extra_env:
        env.update(extra_env)

    system_prompt = (repo_path / ".orchestrator" / "prompts" / "planner.md").read_text()
    argv = ["claude", "-p", prompt, "--model", cfg["models"]["planner"], "--output-format", "json",
            "--max-budget-usd", str(max_budget_usd), "--mcp-config", ".mcp.planner.json", "--strict-mcp-config",
            "--append-system-prompt", system_prompt, "--dangerously-skip-permissions"]

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as log_fh:
        proc = Popen(argv, cwd=str(repo_path), env=env, stdout=log_fh, stderr=log_fh, start_new_session=True)
    return {"pid": proc.pid, "pid_start": _proc_start(proc.pid), "log": str(log_path)}


def start(repo_path, goal_text, account_id="A", reinstall=False, requester=None):
    repo_path = Path(os.path.realpath(repo_path))
    pool_toml = repo_path / ".orchestrator" / "pool.toml"

    running = _running_goal(repo_path)
    if running:
        return {"launched": False, "reason": f"goal {running['goal_id']} is still running; stop it first"}

    reason = _precheck_git(repo_path)
    if reason:
        return {"launched": False, "reason": reason}

    if _staged_changes_conflict(repo_path):
        return {"launched": False, "reason": "target index has staged changes; commit or unstage them first"}

    cfg = _preview_cfg(pool_toml)
    accounts = {a["id"]: a for a in cfg.get("claude_accounts", [])}
    if account_id not in accounts:
        return {"launched": False, "reason": f"unknown account: {account_id}"}
    if not cfg.get("models", {}).get("planner"):
        return {"launched": False, "reason": "pool.toml [models].planner is not configured"}

    report = None
    if reinstall or not pool_toml.exists():
        try:
            report = install(str(repo_path))
        except SystemExit as e:
            return {"launched": False, "reason": f"install failed (exit code {e.code})"}

    def refuse(reason):
        d = {"launched": False, "reason": reason}
        if report is not None:
            d["install"] = report
        return d

    # pool_toml is guaranteed to exist now (it did already, or install() just wrote it); re-read the real file --
    # install() never overwrites an existing pool.toml, so this always agrees with the preview cfg above.
    cfg = tomllib.loads(pool_toml.read_text())
    try:
        max_budget = cfg["limits"]["max_budget_usd"]["planner"]
    except KeyError:
        return refuse("target pool.toml lacks limits.max_budget_usd.planner")

    prompt_path = repo_path / ".orchestrator" / "prompts" / "planner.md"
    if not prompt_path.exists():
        return refuse(".orchestrator/prompts/planner.md is missing from the target")

    commit, err = _scaffold_commit(repo_path, report)
    if err:
        if report is not None:
            err["install"] = report
        return err

    goal_id, err = _create_goal_task(repo_path, goal_text)
    if err:
        if report is not None:
            err["install"] = report
        return err

    prompt = ("Skill(orchestrate) with the goal: " + goal_text + "\nThe GOAL task is " + goal_id +
              "; use it as the parent of every task you create and post the PR url as its result before you finish.")
    runs_dir = repo_path / ".orchestrator" / "runs"
    log_path = runs_dir / f"planner-{goal_id}.log"
    launched = launch_planner(repo_path, prompt, account_id, max_budget, log_path)

    record = {"goal_id": goal_id, "repo": str(repo_path), "text": goal_text, "pid": launched["pid"],
              "pid_start": launched["pid_start"], "started_at": time.time(), "account": account_id,
              "commit": commit, "status": "running", "requester": requester}
    _append_goal_record(repo_path, record)

    return {"launched": True, "goal_id": goal_id, "pid": launched["pid"], "log": launched["log"], "commit": commit,
            "install": report,
            "note": "goal-Planner spend is not counted against any account budget until C-O7a tallies "
                     "transcripts; the daemon may schedule other work on this account meanwhile (accepted gap)"}


def _reconcile(repo_path, record):
    if record.get("status") != "running" or identity(record):
        return record
    exited_at = time.time()
    _set_goal_status(repo_path, record["goal_id"], "exited", exited_at=exited_at)
    return {**record, "status": "exited", "exited_at": exited_at}


def _status_entry(repo_path, record):
    record = _reconcile(repo_path, record)
    goal_id = record["goal_id"]
    goal_task = _read_task(repo_path, goal_id)
    children = [t for t in _read_all_tasks(repo_path) if t.get("parent") == goal_id]
    by_status = {}
    for c in children:
        by_status.setdefault(c["status"], []).append({"id": c["id"], "hold_reason": c.get("hold_reason")})
    merged = [c["merged_into"] for c in children if c.get("merged_into")]
    pr_url = None
    if goal_task and goal_task.get("result"):
        m = PR_URL_RE.search(json.dumps(goal_task["result"]))
        pr_url = m.group(0) if m else None
    planner_alive, note = None, None
    if record["status"] == "running":
        planner_alive = identity(record)
        if record.get("pid_start") is not None and _proc_start(record["pid"]) is None:
            note = "process-start verification unavailable on this platform; liveness check only"
    return {"goal_id": goal_id, "repo": record.get("repo"), "record_status": record["status"],
            "task_status": goal_task["status"] if goal_task else None, "children": by_status,
            "merged": merged, "pr_url": pr_url, "planner_alive": planner_alive, "note": note,
            "requester": record.get("requester")}


def _load_goal_records(repo_path, goal_id=None):
    goals_path = _goals_path(repo_path)
    if not goals_path.exists():
        return []
    records = json.loads(goals_path.read_text())
    return [r for r in records if goal_id is None or r.get("goal_id") == goal_id]


def status(repo_path, goal_id=None):
    repo_path = Path(os.path.realpath(repo_path))
    return [_status_entry(repo_path, r) for r in _load_goal_records(repo_path, goal_id)]


def list_goals(repo_path):
    return status(repo_path)


def stop(repo_path, goal_id):
    repo_path = Path(os.path.realpath(repo_path))
    record = next((r for r in _load_goal_records(repo_path, goal_id)), None)
    if record is None:
        return {"error": "unknown goal"}
    if identity(record):
        os.killpg(os.getpgid(record["pid"]), signal.SIGTERM)
    _set_goal_status(repo_path, goal_id, "stopped")
    return {"stopped": goal_id}
