"""Task bus: SQLite hot index + one JSON file per task (git-backed via the orchestrator-state worktree)."""
import atexit, contextlib, fcntl, hashlib, json, sqlite3, subprocess, threading, time
from datetime import date
from pathlib import Path
from . import ROOT, STATE

TASKS = STATE / "tasks"
RUNS = STATE / "runs"
MAX_RESULT_CHARS = 6000  # ~1,500 tokens
ROLES = {"scout", "triage", "execute", "review", "challenge", "spec_review"}
STATUSES = {"queued", "held", "running", "done", "failed"}


LOCK_NAME = "bus.lock"
LOCK = STATE / LOCK_NAME
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


_connection = None
_connection_path = None
_connection_lock = threading.Lock()


def _close_db():
    global _connection, _connection_path
    if _connection is not None:
        _connection.close()
        _connection = None
        _connection_path = None


atexit.register(_close_db)


def db():
    global _connection, _connection_path
    with _connection_lock:
        path = STATE / "bus.sqlite"
        if _connection is None or _connection_path != path:
            _close_db()
            STATE.mkdir(exist_ok=True); TASKS.mkdir(exist_ok=True)
            # The daemon and its worker threads share the cache; writes use locked().
            c = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            try:
                c.execute("create table if not exists tasks(id text primary key, status, role, tier, assigned_to, updated real)")
                c.execute("create table if not exists events(seq integer primary key autoincrement, task_id, ts real, kind, data)")
            except BaseException:
                c.close()
                raise
            _connection, _connection_path = c, path
        return _connection


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
    t = {"id": tid, "created_at": time.time(), "parent": parent, "role": role, "tier": tier, "complexity": complexity,
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


def _truncate(s, n):
    return s[:n] if isinstance(s, str) else s


def _compact_row(t):
    """A skimmable summary of one task: no spec, events, acceptance or scope, which is most of what makes the
    full shape big. See bus_mcp.bus_read for the field list."""
    result = t.get("result") or {}
    summary = result.get("summary")
    return {
        "id": t["id"], "parent": t.get("parent"), "role": t.get("role"), "status": t.get("status"),
        "complexity": t.get("complexity"), "tier": t.get("tier"), "title": _truncate(t.get("title"), 90),
        "depends_on": t.get("depends_on", []), "hold_reason": t.get("hold_reason"),
        "reason": _truncate(t.get("reason"), 120), "merged_into": t.get("merged_into"),
        "assigned_to": t.get("assigned_to"), "has_result": bool(t.get("result")),
        "result_summary": _truncate(summary, 160) if summary else None,
    }


def read(tid=None, status=None, status_not=None, role=None, compact=False):
    if tid:
        return get(tid)
    rows = db().execute("select id from tasks order by id").fetchall()
    out = [get(r[0]) for r in rows]
    filtered = [t for t in out if (status is None or t["status"] == status)
                and (status_not is None or t["status"] != status_not) and (role is None or t["role"] == role)]
    return [_compact_row(t) for t in filtered] if compact else filtered


def events(since=0, limit=200, role=None, task_ids=None):
    """Events in sequence order, optionally filtered without changing their cursor."""
    rows = db().execute("select seq,task_id,ts,kind,data from events where seq>? order by seq limit ?",
                        (since, limit)).fetchall()
    page = [{"seq": s, "task": t, "ts": ts, "kind": k, "data": json.loads(d)}
            for s, t, ts, k, d in rows]
    if role is None and task_ids is None:
        return page
    return _filter_events(page, role, task_ids)


def _filter_events(page, role=None, task_ids=None):
    """Filter an already bounded page using the current task index, preserving seq."""
    ids = set(task_ids) if task_ids is not None else None
    roles = dict(db().execute("select id,role from tasks").fetchall()) if role is not None else {}
    return [event for event in page
            if (role is None or roles.get(event["task"]) == role)
            and (ids is None or event["task"] in ids)]


def normalize_usage(provider, usage):
    """Return provider-independent token buckets.

    Codex includes cached input in input_tokens. Its output_tokens is assumed to
    already include reasoning, so reasoning_output_tokens is informational and
    is deliberately not added to total_tokens a second time.
    """
    usage = usage or {}
    if provider == "codex":
        cache_read = usage.get("cached_input_tokens", 0) or 0
        input_uncached = max(0, (usage.get("input_tokens", 0) or 0) - cache_read)
        cache_write = 0
        reasoning = usage.get("reasoning_output_tokens", 0) or 0
    else:
        input_uncached = usage.get("input_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        cache_write = usage.get("cache_creation_input_tokens", 0) or 0
        reasoning = usage.get("reasoning_tokens", usage.get("reasoning_output_tokens", 0)) or 0
    output = usage.get("output_tokens", 0) or 0
    return {
        "input_uncached_tokens": input_uncached, "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write, "output_tokens": output, "reasoning_tokens": reasoning,
        "total_tokens": input_uncached + cache_read + cache_write + output,
    }


_policy_cache = None
_policy_lock = threading.Lock()


def policy_version():
    """Hash policy bytes once, invalidating when policy paths or mtimes change."""
    global _policy_cache
    with _policy_lock:
        pool = STATE / "pool.toml"
        paths = [pool, *sorted((STATE / "prompts").glob("*.md"))]
        try:
            signature = tuple((path, path.stat().st_mtime_ns) for path in paths)
            if _policy_cache is None or _policy_cache[0] != signature:
                digest = hashlib.sha256()
                for path in paths:
                    digest.update(path.read_bytes())
                _policy_cache = signature, digest.hexdigest()[:12]
        except OSError:
            return None
        return _policy_cache[1]


def pool_config():
    """Load attribution policy without making telemetry depend on its availability."""
    from .pool import config
    try:
        return config()
    except (OSError, ValueError):
        return {}


def log_run(*, attempt=1, **fields):
    """Append one line per event to runs/<date>.jsonl: tokens, role, tier, account, executor, complexity, duration,
    outcome. Callers normalize cached tokens to cache_read_input_tokens so cli.cost sums one key across providers."""
    fields["attempt"] = attempt
    tool_context = {}
    if fields.get("provider") == "codex" or fields.get("account") == "codex":
        try:
            from . import decision_log, promotion, tool_catalog
            task = get(fields["task"])
            mode = promotion.mode("tool_disclosure")
            if mode in ("shadow", "active"):
                choice = tool_catalog.minimal_set(task, "codex_execute")
                decision_log.record(kind="tool_disclosure", subject=fields["task"],
                    candidates=["CODEX_TOOLS"], hard_constraints=choice["mandatory"],
                    deterministic={"task_class": tool_catalog._task_class(task), "role": "codex_execute",
                                   "kept": choice["keep"], "dropped": choice["drop"],
                                   "tokens_disclosed": 0, "tokens_minimal": 0},
                    selected="allowlist unchanged (shadow)", reason=choice["reason"], mode=mode)
                tool_context = {"tool_tokens_disclosed": 0, "tool_tokens_minimal": 0}
        except Exception:
            pass
    packet_meta = fields.get("packet_meta")
    if isinstance(packet_meta, dict) and (packet_meta.get("hash") or packet_meta.get("version")):
        fields["context"] = {key: {**packet_meta, **tool_context}.get(key) for key in
                             ("sections", "presented_tokens", "candidate_tokens", "candidate_known",
                              "instruction_tokens", "instruction_tokens_modular", "packet_version", "routed_tokens",
                              "routed_reduction_ratio", "routed_hidden", "routed_ambiguous",
                              "routed_rules_version", "evidence_ids", "tool_tokens_disclosed",
                              "tool_tokens_minimal", "skills_exposed", "skills_used",
                              "skill_tokens_l0", "skill_tokens_l2", "skills_selected",
                              "skill_tokens_selected_l0", "skill_tokens_selected_l2")}
        fields["context"]["packet_version"] = packet_meta.get("packet_version") or packet_meta.get("version")
    else:
        fields["context"] = None
    fields.setdefault("policy_version", policy_version())
    for key in ("attempt", "decision_kind", "payload_key", "route", "route_reason",
                "client_version", "policy_version"):
        if fields.get(key) is None:
            fields.pop(key, None)
    task_id = fields.get("task")
    if task_id:
        try:
            task = get(task_id)
            fields.setdefault("goal_id", task.get("parent") or task_id)
        except Exception:
            fields.setdefault("goal_id", None)
        fields.setdefault("provider", "codex" if fields.get("account") == "codex" else "claude")
    from . import attribution
    task = {}
    if task_id:
        try:
            task = get(task_id)
        except (KeyError, OSError, ValueError):
            pass
        chain = attribution.lineage({"id": task_id, **task})
        fields.setdefault("lineage_root", chain["root"])
        fields.setdefault("round_index", chain["round_index"])
        fields.setdefault("task_class", attribution.task_class(task))
    cfg = pool_config()
    provider = fields.get("provider") or ("codex" if fields.get("account") == "codex" else "claude")
    tier = fields.get("tier", task.get("tier"))
    fields.setdefault("bucket", attribution.bucket_of(fields.get("role", task.get("role")), task))
    fields.setdefault("band", attribution.band(fields.get("complexity", task.get("complexity"))))
    fields.setdefault("executor", task.get("executor") or (
        tier if provider == "codex" else f"claude:{tier}" if tier else None))
    fields.setdefault("model", attribution.model_of(fields["executor"], tier, cfg))
    usage = fields.get("usage")
    if not isinstance(usage, dict):
        # Older callers (including planner decisions) expand provider usage into kwargs.
        keys = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "reasoning_output_tokens", "reasoning_tokens")
        usage = {key: fields[key] for key in keys if key in fields}
    if usage:
        fields["usage"] = dict(usage)
        fields.update(normalize_usage(provider, usage))
        if provider == "codex":
            from .pool import Pool
            from types import SimpleNamespace
            pricing = SimpleNamespace(cfg=cfg, _usage_tokens=Pool._usage_tokens)
            source = next((row for row in cfg.get("executors", [])
                           if row.get("id") == fields["executor"]), {})
            fields.setdefault("usd", Pool.usd_of(pricing, {"usage": dict(usage)}, source))
            fields.setdefault("usd_source", "token_estimate")
        elif fields.get("usd") is not None:
            fields.setdefault("usd_source", "reported")
    RUNS.mkdir(parents=True, exist_ok=True)
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
