"""Autonomous Planner decisions: launch a short-lived headless Planner to act on one specific decision point --
scouts finished (write specs), an execute task held (write the fix-round spec), or a goal closable (post the PR) --
without an interactive Planner session in the loop. ROOT only (this repo's own bus/pool/state), unlike goals.py
which operates on any target repo_path.

DEDUP: one record per (goal_id, kind, payload_key) in .orchestrator/runs/planner_runs.json. "scouts_done" and
"closable" key on goal_id; "held" keys on f"{task_id}:{hold_reason}:{held_at!r}" so a fresh hold (a new held_at)
is a new decision even if an old one for the same task already gave up. A record with status running, exited_ok
or gave_up BLOCKS re-decision (decision_points() will not re-yield the key); exited_early does not block while
attempts < 2 -- reconcile() retries it -- and turns permanently blocking (gave_up) once a second early exit would
push attempts to 2.
"""
import json, os, time
from . import ROOT, STATE, bus, goals, handover, spawn
from .pool import Pool

_BLOCKING_STATUSES = ("running", "exited_ok", "gave_up")


def _runs_path():
    """STATE looked up at call time, not import time (same reason as handover.write()'s `plan = STATE /
    "plan.md"`): lets a test swap planner_runs.STATE and have every subsequent call honor it."""
    return STATE / "runs" / "planner_runs.json"


def _load_records():
    path = _runs_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def _save_records(records):
    path = _runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n")


def _blocked(goal_id, kind, payload_key, records=None):
    records = records if records is not None else _load_records()
    return any(r["goal_id"] == goal_id and r["kind"] == kind and r["payload_key"] == payload_key
              and r.get("status") in _BLOCKING_STATUSES for r in records)


def _held_at(t):
    """review_held_at or spec_review_held_at (stamped by daemon.merge_reviewed/dispatch) at full float precision;
    falls back to the latest status=held entry in the task's own event log (e.g. gate_red, worktree missing --
    holds that stamp pipeline.gated_at, not a *_held_at field of their own)."""
    pipeline = t.get("pipeline") or {}
    if pipeline.get("review_held_at"):
        return pipeline["review_held_at"]
    if pipeline.get("spec_review_held_at"):
        return pipeline["spec_review_held_at"]
    held_ts = [e["ts"] for e in t.get("events", []) if e.get("status") == "held"]
    return max(held_ts) if held_ts else None


def _held_key(t):
    held_at = _held_at(t)
    if held_at is None:
        return None
    return f"{t['id']}:{t.get('hold_reason')}:{held_at!r}"


def decision_points():
    """Yield (goal_id, kind, payload_key) for every currently-unblocked decision: scouts_done (a goal has scout
    children, all done or failed, and no execute child yet -- the specs haven't been split off), held (each held
    execute child of a goal), closable (a goal's execute children are all merged and nothing is left queued or
    running)."""
    all_tasks = bus.read()
    children_by_parent = {}
    for t in all_tasks:
        parent = t.get("parent")
        if parent:
            children_by_parent.setdefault(parent, []).append(t)

    records = _load_records()

    for goal in all_tasks:
        if goal["role"] != "triage" or goal.get("parent"):
            continue
        goal_id = goal["id"]
        children = children_by_parent.get(goal_id, [])
        scouts = [c for c in children if c["role"] == "scout"]
        executes = [c for c in children if c["role"] == "execute"]

        if scouts and not executes and all(c["status"] in ("done", "failed") for c in scouts):
            if not _blocked(goal_id, "scouts_done", goal_id, records):
                yield goal_id, "scouts_done", goal_id

        for c in executes:
            if c["status"] != "held":
                continue
            key = _held_key(c)
            if key is None:
                continue
            if not _blocked(goal_id, "held", key, records):
                yield goal_id, "held", key

        if executes and all(c.get("merged_into") for c in executes) and \
                not any(ch["status"] in ("queued", "running") for ch in children):
            if not _blocked(goal_id, "closable", goal_id, records):
                yield goal_id, "closable", goal_id


def _session_attached():
    """True while an interactive Planner is already running against this repo: either this process is itself
    the MCP server backing that session (ORCH_DAEMON_HOST=mcp, set by mcp.register_planner_session at server
    start), or .orchestrator/planner_session.json names a still-live pid. A stale file (dead pid, or a reused
    one per goals.identity_of) is ignored, not treated as attached."""
    if os.environ.get("ORCH_DAEMON_HOST") == "mcp":
        return True
    path = STATE / "planner_session.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return goals.identity_of(data.get("pid"), data.get("pid_start"))


def _existing_attempts(goal_id, kind, payload_key):
    for r in _load_records():
        if r["goal_id"] == goal_id and r["kind"] == kind and r["payload_key"] == payload_key:
            return r.get("attempts", 0)
    return 0


def _record_skip(goal_id, kind, payload_key, reason):
    with bus.locked():
        records = _load_records()
        records.append({"goal_id": goal_id, "kind": kind, "payload_key": payload_key, "status": "skipped",
                        "skipped_reason": reason, "started_at": time.time(), "attempts": 0})
        _save_records(records)


def _record_running(goal_id, kind, payload_key, launched, acct_id, attempts):
    """Update the key's existing record in place on a retry (preserving attempts, set by reconcile()) rather
    than appending a second one -- one record per key is what lets reconcile() track attempts across retries."""
    with bus.locked():
        records = _load_records()
        for r in records:
            if r["goal_id"] == goal_id and r["kind"] == kind and r["payload_key"] == payload_key:
                r.update(pid=launched["pid"], pid_start=launched["pid_start"], started_at=time.time(),
                         account=acct_id, log=launched["log"], status="running")
                break
        else:
            records.append({"goal_id": goal_id, "kind": kind, "payload_key": payload_key,
                            "pid": launched["pid"], "pid_start": launched["pid_start"],
                            "started_at": time.time(), "account": acct_id, "log": launched["log"],
                            "status": "running", "attempts": attempts})
        _save_records(records)


def run(goal_id, kind, payload_key):
    """Launch a headless Planner for one decision, guarded against attaching alongside an interactive session or
    a saturated pool. Guard skips are recorded (status "skipped", no pid) but never block a later decision_points()
    or run() call for the same key."""
    if _blocked(goal_id, kind, payload_key):
        return {"launched": False, "reason": "already decided"}

    if _session_attached():
        _record_skip(goal_id, kind, payload_key, "planner session attached")
        return {"launched": False, "reason": "planner session attached"}

    pool = Pool()
    acct = pool.pick("planner")
    if acct is None:
        _record_skip(goal_id, kind, payload_key, "no account with headroom")
        return {"launched": False, "reason": "no account with headroom"}

    handover.write(f"decision {kind}")

    attempts = _existing_attempts(goal_id, kind, payload_key)
    prompt = spawn.render("planner-decision", kind=kind, goal_id=goal_id, payload=payload_key)
    budget = pool.cfg.get("limits", {}).get("max_budget_usd", {}).get("planner_decision", 3)
    log = STATE / "runs" / f"planner-decision-{goal_id}-{kind}-{attempts + 1}.log"
    launched = goals.launch_planner(ROOT, prompt, acct.id, budget, log)

    _record_running(goal_id, kind, payload_key, launched, acct.id, attempts)
    return {"launched": True, "pid": launched["pid"], "log": launched["log"]}


def _condition_resolved(r, tasks_by_id, children_by_parent):
    kind, goal_id, payload_key = r["kind"], r["goal_id"], r["payload_key"]
    if kind == "scouts_done":
        return any(c["role"] == "execute" for c in children_by_parent.get(goal_id, []))
    if kind == "closable":
        goal = tasks_by_id.get(goal_id)
        return bool(goal and goal["status"] == "done")
    if kind == "held":
        task_id = payload_key.split(":", 1)[0]
        t = tasks_by_id.get(task_id)
        if t is None or t["status"] != "held":
            return True
        return _held_key(t) != payload_key
    return True


def reconcile():
    """For each running record whose process is gone: exited_ok if the decision's own condition already resolved
    (someone else, or a prior attempt, finished it), else exited_early with attempts += 1 -- gave_up (and one
    notify) once that reaches 2. Call at the start of the autonomous block every tick, before decision_points()."""
    all_tasks = bus.read()
    tasks_by_id = {t["id"]: t for t in all_tasks}
    children_by_parent = {}
    for t in all_tasks:
        parent = t.get("parent")
        if parent:
            children_by_parent.setdefault(parent, []).append(t)

    gave_up = []
    with bus.locked():
        records = _load_records()
        changed = False
        for r in records:
            if r.get("status") != "running":
                continue
            if goals.identity_of(r.get("pid"), r.get("pid_start")):
                continue
            changed = True
            if _condition_resolved(r, tasks_by_id, children_by_parent):
                r["status"] = "exited_ok"
                continue
            r["attempts"] = r.get("attempts", 0) + 1
            if r["attempts"] >= 2:
                r["status"] = "gave_up"
                gave_up.append(r)
            else:
                r["status"] = "exited_early"
        if changed:
            _save_records(records)

    if gave_up:
        from . import daemon  # deferred: daemon imports this module at load time
        for r in gave_up:
            daemon.notify(f"{r['goal_id']}: planner decision {r['kind']} ({r['payload_key']}) "
                          f"gave up after {r['attempts']} attempts")
