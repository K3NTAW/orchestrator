"""Task bus: SQLite hot index + one JSON file per task (git-backed via the orchestrator-state worktree)."""
import contextlib, fcntl, json, sqlite3, subprocess, threading, time
from datetime import date
from pathlib import Path
from . import ROOT, STATE

TASKS = STATE / "tasks"
RUNS = STATE / "runs"
MAX_RESULT_CHARS = 6000  # ~1,500 tokens
ROLES = {"scout", "triage", "execute", "review", "challenge", "spec_review"}
STATUSES = {"queued", "held", "running", "done", "failed"}


LOCK = STATE / "bus.lock"
_held = threading.local()


@contextlib.contextmanager
def locked():
    """Serialize bus writes across the daemon, its worker threads and the Planner: an flock on .orchestrator/bus.lock,
    the same pattern as merge.lock. Reentrant per thread — flock keys on the open file description, so update() ->
    _save() opening the lock a second time would block on itself without the depth counter."""
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth = depth
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0


def db():
    STATE.mkdir(exist_ok=True); TASKS.mkdir(exist_ok=True)
    c = sqlite3.connect(STATE / "bus.sqlite", isolation_level=None)
    c.execute("create table if not exists tasks(id text primary key, status, role, tier, assigned_to, updated real)")
    c.execute("create table if not exists events(seq integer primary key autoincrement, task_id, ts real, kind, data)")
    return c


def _save(t):
    with locked():
        TASKS.mkdir(parents=True, exist_ok=True)
        (TASKS / f"{t['id']}.json").write_text(json.dumps(t, indent=2) + "\n")
        db().execute("insert or replace into tasks values(?,?,?,?,?,?)",
                     (t["id"], t["status"], t["role"], t["tier"], t.get("assigned_to"), time.time()))


def _event(tid, kind, data=None):
    db().execute("insert into events(task_id,ts,kind,data) values(?,?,?,?)", (tid, time.time(), kind, json.dumps(data or {})))


def get(tid):
    p = TASKS / f"{tid}.json"
    if not p.exists():
        raise KeyError(tid)
    return json.loads(p.read_text())


def next_id():
    ids = sorted(int(p.stem[2:]) for p in TASKS.glob("T-*.json")) if TASKS.exists() else []
    return f"T-{(ids[-1] + 1) if ids else 1:04d}"


def create_task(title, spec, acceptance, scope, role="scout", tier="sonnet", complexity=3,
                parent=None, inputs=None, constraints=None, depends_on=None):
    """Planner-only. Mirrors the require-acceptance hook: no acceptance or scope -> rejected."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    if not acceptance or not scope:
        raise ValueError("acceptance and scope must be non-empty lists")
    tid = next_id()
    deps = list(depends_on or [])
    for dep in deps:
        if dep == tid:
            raise ValueError(f"task cannot depend on itself: {dep}")
        try:
            get(dep)
        except KeyError:
            raise ValueError(f"depends_on references unknown task: {dep}")
    t = {"id": tid, "parent": parent, "role": role, "tier": tier, "complexity": complexity,
         "title": title, "spec": spec, "inputs": inputs or [], "acceptance": list(acceptance), "scope": list(scope),
         "depends_on": deps,
         "constraints": {"read_only": role != "execute", "budget_turns": 20, "timeout_s": 900, **(constraints or {})},
         "status": "queued", "assigned_to": None, "worktree": None, "codex_thread": None, "result": None, "events": []}
    _save(t); _event(t["id"], "created")
    return t


def update(tid, **fields):
    """Non-Planner fields only: status, assigned_to, worktree, codex_thread, executor, pid, resume_hint.
    executor is the routed model id ("astra", "luna", ...) or "claude:<tier>" for a Claude fallback run."""
    with locked():   # read-modify-write: without the lock a concurrent update drops the other's fields
        t = get(tid)
        for k, v in fields.items():
            if k in {"spec", "acceptance", "scope", "complexity", "depends_on"}:
                raise PermissionError(f"only the Planner may set {k}; create a new task instead")
            t[k] = v
        t["events"].append({"ts": time.time(), **fields})
        _save(t); _event(tid, "update", fields)
    return t


def ready(task_or_id):
    """True iff every depends_on task is merged (merged_into set), or done for non-execute roles."""
    t = task_or_id if isinstance(task_or_id, dict) else get(task_or_id)
    for dep_id in t.get("depends_on", []):
        dep = get(dep_id)
        if dep.get("merged_into"):
            continue
        if dep["role"] != "execute" and dep["status"] == "done":
            continue
        return False
    return True


def dependents(tid):
    """Tasks that declare tid in their depends_on."""
    return [t for t in read() if tid in t.get("depends_on", [])]


def claim(tid, assigned_to, worktree=None):
    return update(tid, status="running", assigned_to=assigned_to, worktree=worktree, claimed_at=time.time())


def post_result(tid, result, status="done"):
    """Results are structured JSON, capped at ~1,500 tokens; oversize is rejected so the Planner's context stays small."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    if len(json.dumps(result)) > MAX_RESULT_CHARS:
        raise ValueError(f"result exceeds {MAX_RESULT_CHARS} chars; compress it (summary + artifact paths, not transcripts)")
    result.setdefault("confidence", 0.0); result.setdefault("provenance", ["repo"])
    return update(tid, status=status, result=result)


def read(tid=None, status=None, status_not=None, role=None):
    if tid:
        return get(tid)
    rows = db().execute("select id from tasks order by id").fetchall()
    out = [get(r[0]) for r in rows]
    return [t for t in out if (status is None or t["status"] == status)
            and (status_not is None or t["status"] != status_not) and (role is None or t["role"] == role)]


def events(since=0, limit=200):
    rows = db().execute("select seq,task_id,ts,kind,data from events where seq>? order by seq limit ?", (since, limit)).fetchall()
    return [{"seq": s, "task": t, "ts": ts, "kind": k, "data": json.loads(d)} for s, t, ts, k, d in rows]


def log_run(**fields):
    """Append one line per event to runs/<date>.jsonl: tokens, role, tier, account, executor, complexity, duration,
    outcome. Callers normalize cached tokens to cache_read_input_tokens so cli.cost sums one key across providers."""
    RUNS.mkdir(exist_ok=True)
    with open(RUNS / f"{date.today().isoformat()}.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), **fields}) + "\n")


def commit_state():
    """Mirror tasks/*.json into the orchestrator-state branch. ponytail: uses a dedicated worktree wt/_state;
    created on first call. Skip silently when not a git repo."""
    wt = ROOT / "wt" / "_state"
    g = lambda *a, **k: subprocess.run(["git", *a], cwd=k.pop("cwd", ROOT), capture_output=True, text=True, **k)
    if g("rev-parse", "--git-dir").returncode:
        return False
    if not wt.exists():
        if g("rev-parse", "--verify", "orchestrator-state").returncode:
            g("worktree", "add", "--orphan", "-b", "orchestrator-state", str(wt))
        else:
            g("worktree", "add", str(wt), "orchestrator-state")
    dst = wt / "tasks"; dst.mkdir(exist_ok=True)
    for p in TASKS.glob("T-*.json"):
        (dst / p.name).write_text(p.read_text())
    g("add", "-A", cwd=wt)
    return g("commit", "-qm", f"state {time.strftime('%Y-%m-%dT%H:%M')}", cwd=wt).returncode == 0
