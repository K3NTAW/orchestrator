"""Autonomous Planner decisions: launch a short-lived headless Planner to act on one specific decision point --
scouts finished (write specs), an execute task held (write the fix-round spec), or a goal closable (post the PR) --
without an interactive Planner session in the loop. ROOT only (this repo's own bus/pool/state), unlike goals.py
which operates on any target repo_path.

DEDUP: one record per (goal_id, kind, payload_key) in .orchestrator/runs/planner_runs.json. "scouts_done" and
"closable" key on goal_id; "held" keys on f"{task_id}:{held_at!r}" (no hold_reason -- untrusted task content
never becomes part of a key or a rendered prompt, see _held_key/run()) so a fresh hold (a new held_at) is a new
decision even if an old one for the same task already gave up. A record with status running, claimed, exited_ok
or gave_up BLOCKS re-decision (decision_points() will not re-yield the key); skipped never blocks; exited_early
does not block while attempts < 2 -- reconcile() retries it -- and turns permanently blocking (gave_up) once a
second early exit would push attempts to 2. failed_launch (an exception between claim and launch, or a claimed
row reconcile() aged out because the process never got as far as recording "running") behaves like exited_early:
it does not block, but counts toward the same attempts/gave_up-at-2 rule.

Routed ticks coalesce non-routine points per goal, retaining a record for each point.
The ledger gains a goal_launches mapping: goal_launches[goal_id] stores
last_state_version and cursor. Equal child-state versions cannot launch again;
infra failures restore the previous guard and cursor. Legacy list ledgers remain
readable and per-point APIs retain their original blocking semantics.
"""
import dataclasses, random
import fnmatch, hashlib, json, os, re, subprocess, sys, tempfile, time, tomllib, uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from . import ROOT, STATE, bus, goals, handover, jev, spawn, decision, failures, gitutil, notify
from . import planner_taxonomy, planner_router, planner_telemetry, planner_shadow, jev_planner
from . import planner_packet, scout_evidence
from .pool import Pool

_BLOCKING_STATUSES = ("running", "claimed", "exited_ok", "gave_up")
_STALE_CLAIM_S = 120
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_JEV_TIMEOUT_S = 3.0


def _fenced(label, value, limit=None):
    value = str(value or "")
    if limit is not None:
        value = value[:limit]
    value = re.sub(r"`{3,}", "[backticks elided]", value)
    return [f"{label}:", "```data", value, "```"]


def _packet_cap(repo_path=None):
    path = Path(repo_path) / ".orchestrator" if repo_path is not None else STATE
    try:
        cfg = tomllib.loads((path / "pool.toml").read_text())
        return max(1, int(cfg.get("planner", {}).get("decision_packet_chars", 6000)))
    except (OSError, ValueError, TypeError):
        return 6000


def decision_packet(goal_id, kind, payload, repo_path=ROOT):
    """Legacy single-point entry point using the same compact packet format."""
    point = _point((goal_id, kind, payload))
    try:
        goal = bus.get(goal_id)
        ctx = build_ctx(point)
    except KeyError:
        goal, ctx = {"id": goal_id}, {}
    task = _decision_task(goal_id, kind, payload)
    classification = planner_taxonomy.classify(point, ctx, goal=goal, task=task)
    return planner_packet.build(
        _packet_sections([(point, ctx, None, classification)]), goal=goal,
        scout_findings=_scout_findings(goal_id), memory_hits=_memory_hits(goal),
        cap_chars=_packet_cap(repo_path))["text"]

# next_action options offered to Jev for a decision-point shadow triage (D3, T-0217). scouts_done gets its own
# set (there is no held task/review to react to yet); held and closable share the fix_round/respec/escalate/noop
# set, matching what an interactive decision Planner actually does with a held execute task (CLAUDE.md's "Never"
# section: write a fix-round spec, never edit the held task).
_SCOUTS_DONE_OPTIONS = {
    "synthesize_now": "enough scouts have reported to synthesize the plan now",
    "wait_for_more": "wait for more scouts to finish before synthesizing",
    "drop_low_confidence": "drop the low-confidence scout findings and synthesize with what's left",
}
_NEXT_ACTION_OPTIONS = {
    "fix_round": "write a fix-round task from the review comments",
    "respec": "the spec itself is wrong; recreate the task",
    "escalate": "a human must decide: auth, billing, data deletion, or three failed rounds",
    "noop": "the hold is stale or already superseded; nothing to do",
}


def _runs_path():
    """STATE looked up at call time, not import time (same reason as handover.write()'s `plan = STATE /
    "plan.md"`): lets a test swap planner_runs.STATE and have every subsequent call honor it."""
    return STATE / "runs" / "planner_runs.json"


def _load_records():
    path = _runs_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("records", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(backup)
        except OSError:
            pass
        print(f"[planner_runs] ledger corrupt; backed up to {backup}", file=sys.stderr)
        return []


def _save_records(records, goal_launches=None):
    """mkstemp in the same directory + os.replace: a reader (another process's _load_records) never observes a
    partially-written file, only the old complete one or the new complete one."""
    path = _runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if goal_launches is None:
        goal_launches = _goal_launches()
    data = {"records": records, "goal_launches": goal_launches} if goal_launches else records
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".planner_runs.json.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        os.unlink(tmp_name)
        raise


def _find_record(records, goal_id, kind, payload_key):
    for r in records:
        if r["goal_id"] == goal_id and r["kind"] == kind and r["payload_key"] == payload_key:
            return r
    return None


def _blocked(goal_id, kind, payload_key, records=None):
    records = records if records is not None else _load_records()
    return any(r["goal_id"] == goal_id and r["kind"] == kind and r["payload_key"] == payload_key
              and r.get("status") in _BLOCKING_STATUSES for r in records)


def _held_at(t):
    """The latest status=held entry in the task's own event log, at full float precision, preferred over
    review_held_at/spec_review_held_at (stamped once by daemon.stamp and never refreshed on a later hold of the
    same task -- daemon.stamp's `if pipeline.get(stage): return False` guard means a second hold leaves those
    stamps stale). Falls back to the pipeline stamps only when the event log has no held entry at all (e.g.
    gate_red, worktree missing -- holds that stamp pipeline.gated_at, not a *_held_at field of their own)."""
    held_ts = [e["ts"] for e in t.get("events", []) if e.get("status") == "held"]
    if held_ts:
        return max(held_ts)
    pipeline = t.get("pipeline") or {}
    if pipeline.get("review_held_at"):
        return pipeline["review_held_at"]
    if pipeline.get("spec_review_held_at"):
        return pipeline["spec_review_held_at"]
    return None


def _held_key(t):
    """task_id and held_at only -- never hold_reason. hold_reason is untrusted task content (T-0198 review item 2):
    it must never end up in a key that later gets rendered into the decision Planner's prompt. The decision
    Planner reads hold_reason itself, from the bus, as data."""
    held_at = _held_at(t)
    if held_at is None:
        return None
    return f"{t['id']}:{held_at!r}"


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
        goal_container = bool((goal.get("constraints") or {}).get("goal"))
        if (goal["role"] != "triage" and not goal_container) or goal.get("parent"):
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
            if any(x.get("status") != "failed" and
                   (x.get("constraints") or {}).get("fix_round_for") == c["id"] for x in all_tasks):
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
    start), or .orchestrator/planner_session.json names a still-live pid. A stale file (dead pid, a reused one
    per goals.identity_of, a body that isn't a dict, or a dict lacking an int pid) is ignored, not treated as
    attached -- the guard fails closed to "not attached" without raising."""
    if os.environ.get("ORCH_DAEMON_HOST") == "mcp":
        return True
    path = STATE / "planner_session.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    return goals.identity_of(pid, data.get("pid_start"))


def _existing_attempts(goal_id, kind, payload_key):
    r = _find_record(_load_records(), goal_id, kind, payload_key)
    return r.get("attempts", 0) if r else 0


def _claim(goal_id, kind, payload_key, attempts, route=None):
    """Write/refresh this key's record to status "claimed" (pid None) -- called only from inside run()'s own
    bus.locked() block, immediately after _blocked() found nothing there yet, so the check and the claim are
    atomic together. "claimed" is itself a blocking status (_BLOCKING_STATUSES), so a concurrent run() for the
    same key -- however it interleaves -- sees this the instant it can acquire the lock, before either guard or
    launch_planner ever runs."""
    records = _load_records()
    r = _find_record(records, goal_id, kind, payload_key)
    if r is None:
        r = {"goal_id": goal_id, "kind": kind, "payload_key": payload_key, "attempts": attempts}
        records.append(r)
    r.update(status="claimed", pid=None, started_at=time.time(),
             state_version=state_version(goal_id), cursor_at_launch=_cursor(),
             launch_id=uuid.uuid4().hex, policy_version=bus.policy_version(), prompt_hash=None,
             route=route.name if route is not None else "escalate",
             reason=route.reason if route is not None else "legacy:autonomous",
             evidence=list(route.evidence) if route is not None else [goal_id, payload_key],
             cheaper_steps=list(route.cheaper_steps) if route is not None else [],
             decision_requested={"held": "write or approve a fix round", "scouts_done": "write specs",
                                 "closable": "close the goal"}.get(kind, "make the requested decision"))
    for field in ("usage_logged", "tokens", "usd", "session_id", "telemetry_before",
                  "materiality_logged", "materiality", "shadow_logged", "shadow_log",
                  "shadow_pid", "shadow_pid_start", "shadow_agreement"):
        r.pop(field, None)
    _save_records(records)


def _record_skip(goal_id, kind, payload_key, reason):
    """Guard skips update the one row for this key in place (last_skip_at, skip_count, skipped_reason) rather
    than appending a fresh row per tick -- and "skipped" is never in _BLOCKING_STATUSES, so a skip can never
    block the next decision_points()/run() call for the same key, including the very next tick's retry."""
    with bus.locked():
        records = _load_records()
        r = _find_record(records, goal_id, kind, payload_key)
        if r is None:
            r = {"goal_id": goal_id, "kind": kind, "payload_key": payload_key, "attempts": 0,
                "started_at": time.time()}
            records.append(r)
        r.update(status="skipped", skipped_reason=reason, last_skip_at=time.time(),
                 skip_count=r.get("skip_count", 0) + 1)
        _save_records(records)


def _record_running(goal_id, kind, payload_key, launched, acct_id, attempts, jev_result=None):
    """Update the key's existing record in place on a retry (preserving attempts, set by reconcile()) rather
    than appending a second one -- one record per key is what lets reconcile() track attempts across retries.
    jev_result (from jev_triage(), shadow mode only) is stored as-is -- {choice, probabilities, confidence,
    latency_ms} or None -- and never influences anything else recorded here. agreement is reset to None
    alongside it: a prior attempt's agreement was scored against that attempt's own jev choice, and must never
    be left dangling against a new (or newly-absent) jev value that reconcile() hasn't scored yet."""
    with bus.locked():
        records = _load_records()
        r = _find_record(records, goal_id, kind, payload_key)
        if r is None:
            r = {"goal_id": goal_id, "kind": kind, "payload_key": payload_key, "attempts": attempts}
            records.append(r)
        defaults = {"launch_id": uuid.uuid4().hex, "state_version": state_version(goal_id),
                    "cursor_at_launch": _cursor(), "route": "escalate", "reason": "legacy:autonomous",
                    "evidence": [goal_id, payload_key], "cheaper_steps": []}
        for field, value in defaults.items():
            r.setdefault(field, value)
        r.update(pid=launched["pid"], pid_start=launched["pid_start"], started_at=time.time(),
                 account=acct_id, log=launched["log"], stderr_log=launched.get("stderr_log"),
                 status="running", jev=jev_result, agreement=None)
        r.setdefault("launches", []).append({k: v for k, v in r.items() if k != "launches"})
        _save_records(records)


def _record_failed_launch(goal_id, kind, payload_key, attempts, error):
    """A failure anywhere between _claim() and _record_running() (an exception raised while rendering the prompt,
    picking an account, or launching the process) must release the key rather than leave it stuck "claimed"
    forever: flip to "failed_launch" (not in _BLOCKING_STATUSES) so decision_points() yields it again next tick,
    same as exited_early. attempts is incremented here and follows the same gave_up-at-2 rule as reconcile()'s
    exited_early path, so a launch that keeps throwing does not retry forever either."""
    with bus.locked():
        records = _load_records()
        r = _find_record(records, goal_id, kind, payload_key)
        if r is None:
            r = {"goal_id": goal_id, "kind": kind, "payload_key": payload_key, "attempts": attempts}
            records.append(r)
        new_attempts = attempts + 1
        status = "gave_up" if new_attempts >= 2 else "failed_launch"
        r.update(status=status, attempts=new_attempts, error=str(error)[:300], last_failed_at=time.time())
        _save_records(records)
    return status, new_attempts


def _decision_task(goal_id, kind, payload_key):
    """The task the decision is actually about: the held execute task itself for "held" (its id is the part of
    payload_key before the first ":"), the goal (triage) task for "scouts_done"/"closable". None if it has since
    been purged -- callers must treat that the same as Jev being unavailable, never raise."""
    task_id = payload_key.split(":", 1)[0] if kind == "held" else goal_id
    try:
        return bus.get(task_id)
    except KeyError:
        return None


def _review_comments_for(task_id, limit=10):
    """Up to `limit` "path:line severity issue[:200]" lines from the most recent review(s) of task_id, newest
    first -- the same {"comments": [{"path","line","issue","severity"}]} shape review.md's prompt asks reviewers
    to return (bus_post_result's result.comments), read here as data, never rendered into any prompt."""
    reviews = [r for r in bus.read(role="review") if (r.get("inputs") or [])[:1] == [task_id]]
    reviews.sort(key=lambda r: (r.get("events") or [{}])[-1].get("ts", 0), reverse=True)
    out = []
    for r in reviews:
        for c in ((r.get("result") or {}).get("comments") or []):
            out.append(f"{c.get('path', '?')}:{c.get('line', '?')} {c.get('severity', '?')} "
                       f"{str(c.get('issue', ''))[:200]}")
            if len(out) >= limit:
                return out
    return out


def _depends_on_statuses(task):
    statuses = {}
    for dep_id in task.get("depends_on") or []:
        try:
            statuses[dep_id] = bus.get(dep_id)["status"]
        except KeyError:
            statuses[dep_id] = None
    return statuses


def _jev_state(kind, task, attempts):
    return {
        "kind": kind,
        "task_title": task.get("title"),
        "spec": (task.get("spec") or "")[:1500],
        "hold_reason": task.get("hold_reason"),
        "review_comments": _review_comments_for(task["id"]),
        "depends_on": _depends_on_statuses(task),
        "attempts": attempts,
    }


def _coerce_float(v):
    """Non-numeric (a bad string, None, a list -- whatever a malformed Jev response hands back) becomes None
    rather than raising, so one bad field never sinks the whole shadow triage row."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_probabilities(probs):
    if not isinstance(probs, dict):
        return {}
    return {k: _coerce_float(v) for k, v in probs.items()}


def jev_triage(kind, task, attempts=0):
    """Ask Jev what it would decide for this decision point, purely to record for later agreement measurement
    (E2/T-0214's typed-question client) -- shadow mode only, never consulted by run()/launch_planner, and its
    result changes nothing about whether or how the real headless Planner launches. Returns
    {choice, probabilities, confidence, latency_ms} or None -- fail-open the same way jev.ask() itself is
    fail-open (disabled, no key, budget exhausted, timeout, bad response all return None, never raise), with an
    explicit 3s cap (jev.ask's own pool.toml timeout may be longer/shorter) so a slow Jev endpoint can never be
    on the critical path to a real launch. confidence/probabilities are coerced to float (non-numeric -> None,
    see _coerce_float) rather than trusted as already-numeric, since Jev's response is untrusted external data --
    a None confidence is excluded from summary()'s mean_confidence, never treated as 0.0."""
    if task is None:
        return None
    state = _jev_state(kind, task, attempts)
    options = _SCOUTS_DONE_OPTIONS if kind == "scouts_done" else _NEXT_ACTION_OPTIONS
    started = time.monotonic()
    result = jev.ask(state, {"next_action": {"type": "choice",
                     "instructions": "Given this decision-point state, what should the Planner do next?",
                     "criteria": options}}, timeout_s=_JEV_TIMEOUT_S)
    latency_ms = (time.monotonic() - started) * 1000
    if result is None:
        return None
    try:
        a = result["answers"]["next_action"]
        return {"choice": a["choice"], "probabilities": _coerce_probabilities(a["probabilities"]),
                "confidence": _coerce_float(a["confidence"]), "latency_ms": latency_ms}
    except (KeyError, TypeError):
        return None


def _safe_jev_triage(goal_id, kind, payload_key, attempts):
    """jev_triage should never raise (jev.ask() is documented fail-open) -- this is defense in depth, the same
    posture run() already takes around launch_planner itself, so a bug in state-building can never turn into a
    launch failure for what is meant to be a pure side channel."""
    try:
        return jev_triage(kind, _decision_task(goal_id, kind, payload_key), attempts)
    except Exception:
        return None


def run(goal_id, kind, payload_key, route=None, *, routed=False):
    """Launch a headless Planner for one decision, guarded against attaching alongside an interactive session or
    a saturated pool. The already-decided check and the claim that follows it run inside one bus.locked() block
    (T-0196 review item 2): two concurrent callers for the same key can never both pass the check, since whichever
    loses the race to the lock sees the winner's "claimed" record and returns "already decided" immediately --
    before either guard or launch_planner runs on either thread. Guard skips are recorded (status "skipped", no
    pid) but never block a later decision_points() or run() call for the same key. Everything from here to the
    launch itself is wrapped in try/except Exception (T-0198 review item 1): any failure -- a guard raising,
    render() rejecting an unsafe key, launch_planner itself throwing -- flips the claimed row to "failed_launch"
    instead of leaving it stuck "claimed" forever, and is never re-raised. KeyboardInterrupt/SystemExit are
    caught separately: the claim is released the same way, but the signal is re-raised afterward so Ctrl-C
    still stops `orchestrator daemon --once` instead of being swallowed as a launch failure."""
    with bus.locked():
        if _blocked(goal_id, kind, payload_key):
            return {"launched": False, "reason": "already decided"}
        attempts = _existing_attempts(goal_id, kind, payload_key)
        _claim(goal_id, kind, payload_key, attempts, route)

    acct = None
    pool = None
    try:
        if _session_attached():
            _record_skip(goal_id, kind, payload_key, "planner session attached")
            return {"launched": False, "reason": "planner session attached"}

        pool = Pool()
        acct = pool.pick("planner")
        if acct is None:
            _record_skip(goal_id, kind, payload_key, "no account with headroom")
            return {"launched": False, "reason": "no account with headroom"}

        # Defense in depth (T-0198 review item 2): kind/goal_id/payload_key are ids and reprs of timestamps, never
        # free text -- but refuse to render a prompt from any of them if that ever stops being true, rather than
        # trust it silently.
        if not all(_SAFE_KEY.match(v) for v in (kind, goal_id, payload_key)):
            _record_skip(goal_id, kind, payload_key, "unsafe decision key")
            return {"launched": False, "reason": "unsafe decision key"}

        handover.write(f"decision {kind}")

        prompt = spawn.render("planner-decision", packet=decision_packet(goal_id, kind, payload_key, ROOT))
        with bus.locked():
            records = _load_records()
            record = _find_record(records, goal_id, kind, payload_key)
            record["prompt_hash"] = hashlib.sha256(prompt.encode()).hexdigest()[:12]
            _save_records(records)
        budget = pool.cfg.get("limits", {}).get("max_budget_usd", {}).get("planner_decision", 3)
        log = STATE / "runs" / f"planner-decision-{goal_id}-{kind}-{attempts + 1}.log"

        # Only spent once every guard above has passed and launch is about to happen for real (T-0232 review
        # item 1): a decision point skipped for session-attached/no-headroom/unsafe-key never spends a Jev
        # request, so skip records above never carry a jev field.
        jev_result = _safe_jev_triage(goal_id, kind, payload_key, attempts)

        launched = (goals.launch_planner(ROOT, prompt, acct.id, budget, log) if not routed else
                    launch_routed(pool, route, prompt, acct, budget, log))
    except (KeyboardInterrupt, SystemExit) as e:
        _record_failed_launch(goal_id, kind, payload_key, attempts, e)
        raise
    except Exception as e:
        infra = _infra_kind(str(e))
        if infra:
            if pool is not None:
                _cool_infra(pool, acct.id if acct else None, infra, goal_id)
            with bus.locked():
                records = _load_records()
                _find_record(records, goal_id, kind, payload_key).update(status="infra_failure", infra_failure_kind=infra)
                _save_records(records)
            return {"launched": False, "reason": infra}
        status, new_attempts = _record_failed_launch(goal_id, kind, payload_key, attempts, e)
        if status == "gave_up":
            notify.notify(f"{goal_id}: planner decision {kind} ({payload_key}) gave up after {new_attempts} "
                          f"attempts (last: launch failed)")
        return {"launched": False, "reason": "failed_launch", "error": str(e)[:300]}

    _record_running(goal_id, kind, payload_key, launched, acct.id, attempts, jev_result)
    return {"launched": True, "pid": launched["pid"], "log": launched["log"]}


def _first_event_ts(task_id):
    """The ts of the earliest bus event ever logged for task_id (its "created" event, since that is always the
    first one _event() writes) -- used to tell a fix-round task the Planner just created apart from some
    unrelated, pre-existing task that happens to share a depends_on/fix_round_for reference."""
    row = bus.db().execute("select ts from events where task_id=? order by seq limit 1", (task_id,)).fetchone()
    return row[0] if row else None


def _bus_event_after(since, *task_ids):
    """True if any bus event exists for one of task_ids with ts > since."""
    placeholders = ",".join("?" for _ in task_ids)
    row = bus.db().execute(f"select 1 from events where task_id in ({placeholders}) and ts>? limit 1",
                           (*task_ids, since)).fetchone()
    return row is not None


def _record_decision_usage(r):
    """Parse a completed headless Claude JSON response once and account for its usage."""
    if r.get("usage_logged"):
        return
    if not r.get("log"):
        r["usage_logged"] = False
        r["usage_warning_count"] = r.get("usage_warning_count", 0) + 1
        print("[planner_runs] decision log missing; usage absent", file=sys.stderr)
        return
    try:
        lines = Path(r["log"]).read_text(errors="replace").splitlines()
    except OSError:
        r["usage_logged"] = False
        r["usage_warning_count"] = r.get("usage_warning_count", 0) + 1
        print(f"[planner_runs] unable to read decision log {r['log']}; usage absent", file=sys.stderr)
        return
    output = None
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            output = candidate
            break
    if output is None:
        r["usage_logged"] = False
        r["usage_warning_count"] = r.get("usage_warning_count", 0) + 1
        print(f"[planner_runs] no JSON usage record in {r['log']}", file=sys.stderr)
        return
    usage = output.get("usage") or {}
    if not usage:
        r["usage_logged"] = False
        r["usage_warning_count"] = r.get("usage_warning_count", 0) + 1
        print(f"[planner_runs] usage absent in {r['log']}", file=sys.stderr)
        return
    normalized = bus.normalize_usage("claude", usage)
    tokens = normalized.get("total_tokens", 0)
    usd = output.get("total_cost_usd")
    bus.log_run(task=r["goal_id"], goal_id=r["goal_id"], role="planner_decision",
                tier="planner", account=r.get("account"), provider="claude",
                outcome="done" if not output.get("is_error") else "error", usd=usd,
                tokens=tokens, **{**usage, **normalized,
                    **{key: r.get(key) for key in ("launch_id", "route", "reason", "evidence",
                       "cheaper_steps", "decision_requested", "policy_version", "prompt_hash", "payload_key")},
                    "route_reason": r.get("reason"), "decision_kind": r["kind"],
                    "attempt": r.get("attempts", 0) + 1, "started_at": r.get("started_at"),
                    "session_id": output.get("session_id")})
    if r.get("launch_id"):
        planner_telemetry.record_usage(
            r["launch_id"], **_usage_buckets(normalized), usd=usd,
            latency_s=time.time() - r.get("launch_started_at", r.get("started_at", time.time())),
            outcome="error" if output.get("is_error") else "done",
            session_id=output.get("session_id"), root=STATE)
    r["tokens"] = tokens
    r["usd"] = usd
    r["usage_logged"] = True
    r["session_id"] = output.get("session_id")
    if r.get("launches"):
        r["launches"][-1].update(usd=usd, **normalized, session_id=r["session_id"])


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
        # A held task never leaves status "held" by itself (CLAUDE.md: the Planner clears a hold by writing a
        # new task, never by editing the held one) -- so "no longer held" only fires once the task is gone
        # (id reused/purged) or the daemon requeued it some other way; the real signal is the fix-round task.
        if t is None or t["status"] != "held":
            return True
        started_at = r.get("started_at", 0)
        for other in tasks_by_id.values():
            if other["id"] == task_id:
                continue
            constraints = other.get("constraints") or {}
            if task_id in (other.get("depends_on") or []) or constraints.get("fix_round_for") == task_id:
                fts = _first_event_ts(other["id"])
                if fts is not None and fts > started_at:
                    return True
        # No fix-round task, but the decision Planner may still have legitimately concluded no fix round was
        # needed -- any bus event it posted on the held task or its goal after launch (a result, a status
        # change) counts as that conclusion, so reconcile() scores exited_ok rather than exited_early/gave_up.
        return _bus_event_after(started_at, task_id, goal_id)
    return True


def _observed_outcome(r, tasks_by_id):
    """Map a finished "held"/"closable" decision back to one of the four next_action choices Jev was offered
    (_NEXT_ACTION_OPTIONS), purely so reconcile() can score jev_triage()'s shadow choice against what actually
    happened -- this has no effect on run()/decision_points()/_condition_resolved. fix_round/respec key off the
    same depends_on/fix_round_for convention _condition_resolved already uses for "a new task exists to handle
    this", plus a parallel constraints.respec_for for a Planner that decided the spec itself was wrong and
    recreated the task without tying the new one to the old. escalate is the held/goal task itself ending up
    status "failed" (bus_post_result status="failed" is the one MCP-reachable, unambiguous way a decision
    Planner can say "a human must decide"). noop is "nothing changed, but the Planner did look" (any bus event
    after started_at, same signal _condition_resolved's held branch already treats as a legitimate conclusion).
    None means genuinely unresolved -- scouts_done (no such mapping exists for it) or nothing observable yet."""
    kind, goal_id, payload_key = r["kind"], r["goal_id"], r["payload_key"]
    if kind not in ("held", "closable"):
        return None
    task_id = payload_key.split(":", 1)[0] if kind == "held" else goal_id
    started_at = r.get("started_at", 0)
    for other in tasks_by_id.values():
        if other["id"] == task_id:
            continue
        fts = _first_event_ts(other["id"])
        if fts is None or fts <= started_at:
            continue
        constraints = other.get("constraints") or {}
        if task_id in (other.get("depends_on") or []) or constraints.get("fix_round_for") == task_id:
            return "fix_round"
        if constraints.get("respec_for") == task_id:
            return "respec"
    t = tasks_by_id.get(task_id)
    if t is not None and t["status"] == "failed":
        return "escalate"
    if _bus_event_after(started_at, task_id, goal_id):
        return "noop"
    return None


def _score_shadow(r, records, now):
    if not r.get("shadow_log") or r.get("shadow_logged") or not r.get("materiality_logged"):
        return False
    if goals.identity_of(r.get("shadow_pid"), r.get("shadow_pid_start")):
        return False
    sibling = next((other for other in records if other.get("launch_id") == r.get("launch_id")
                    and other.get("shadow_logged")), None)
    if sibling:
        r["shadow_agreement"] = sibling["shadow_agreement"]
    else:
        row = planner_shadow.record(
            launch_id=r["launch_id"], goal_id=r["goal_id"], decision_type=r["decision_type"],
            state_version=r["state_version"], model=r["shadow_model"], tier=r["shadow_tier"],
            started_at=r["shadow_started_at"], parsed=planner_shadow.parse(r["shadow_log"]),
            latency_s=now - r["shadow_started_at"], root=STATE)
        r["shadow_agreement"] = planner_shadow.score(row, r.get("materiality"))
    r["shadow_logged"] = True
    return True


def reconcile():
    """For each running record whose process is gone: exited_ok if the decision's own condition already resolved
    (someone else, or a prior attempt, finished it), else exited_early with attempts += 1 -- gave_up (and one
    notify) once that reaches 2. Also ages out any "claimed" record (pid still None -- run() died, or its host
    process was killed, between _claim() and _record_running()) older than _STALE_CLAIM_S: without this, a claim
    whose process never got far enough to record "running" would block re-decision forever, since "claimed" is
    itself a blocking status. Aged-out claims go to failed_launch/gave_up via the same attempts-based rule as a
    failed launch (T-0198 review item 1). Also the one place agreement gets scored (T-0217 D3): the instant a
    running record's process is found gone, _observed_outcome() maps what actually happened back to a
    next_action choice and compares it against the row's own jev.choice (from jev_triage(), shadow mode only) --
    True/False if both a jev choice and an outcome exist, else None. Scored once, here, never revisited: once a
    record leaves "running" it is never seen by this branch again. Call at the start of the autonomous block
    every tick, before decision_points()."""
    all_tasks = bus.read()
    tasks_by_id = {t["id"]: t for t in all_tasks}
    children_by_parent = {}
    for t in all_tasks:
        parent = t.get("parent")
        if parent:
            children_by_parent.setdefault(parent, []).append(t)

    gave_up = []
    now = time.time()
    with bus.locked():
        records = _load_records()
        changed = False
        for r in records:
            if r.get("materiality_logged") and _score_shadow(r, records, now):
                changed = True
            if r.get("status") == "claimed" and r.get("pid") is None:
                if now - r.get("started_at", 0) <= _STALE_CLAIM_S:
                    continue
                changed = True
                r["attempts"] = r.get("attempts", 0) + 1
                r["error"] = f"stale claim: no pid recorded within {_STALE_CLAIM_S}s"
                if r["attempts"] >= 2:
                    r["status"] = "gave_up"
                    gave_up.append(r)
                else:
                    r["status"] = "failed_launch"
                continue
            if r.get("status") != "running":
                continue
            if goals.identity_of(r.get("pid"), r.get("pid_start")):
                continue
            changed = True
            if r.get("telemetry_before") is not None and not r.get("materiality_logged"):
                sibling = next((other for other in records if other.get("launch_id") == r.get("launch_id")
                                and other.get("materiality_logged")), None)
                r["materiality"] = sibling["materiality"] if sibling else planner_telemetry.record_materiality(
                    r["launch_id"], r["telemetry_before"],
                    planner_telemetry.snapshot(r["goal_id"], root=STATE), root=STATE)
                r["materiality_logged"] = True
            _score_shadow(r, records, now)
            if _reconcile_infra(r, records):
                continue
            if not any(other is not r and other.get("launch_id") == r.get("launch_id")
                       and other.get("usage_logged") for other in records):
                _record_decision_usage(r)
            outcome = _observed_outcome(r, tasks_by_id)
            r["agreement"] = (outcome == r["jev"]["choice"]) if (outcome is not None and r.get("jev")) else None
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
        for r in gave_up:
            notify.notify(f"{r['goal_id']}: planner decision {r['kind']} ({r['payload_key']}) "
                          f"gave up after {r['attempts']} attempts")


def summary():
    """{decisions, jev_scored, agreement_rate, mean_confidence} over the whole ledger, printed by
    `orchestrator planner-runs` (D3, T-0217; T-0232). decisions is every row ever written; jev_scored counts rows
    that got a Jev shadow choice (row["jev"] is a dict); agreement_rate is computed only over rows that both have
    a jev dict and a scored agreement (True/False, set once by reconcile()) -- the fraction of those that
    agreed, or None with zero such rows; mean_confidence averages jev.confidence over the jev_scored rows whose
    confidence coerced to a real float (jev_triage()'s _coerce_float already turns a non-numeric confidence into
    None; a None confidence is excluded here, never treated as 0.0), or None with zero. Never raises on a
    malformed row (jev not a dict, confidence not numeric) -- isinstance checks throughout, not direct indexing --
    since older or hand-edited ledger rows may predate the coercion in jev_triage(). An empty ledger returns
    all-zero/None rather than raising, since the CLI must print cleanly with no decisions recorded yet."""
    records = _load_records()
    jev_rows = [r for r in records if isinstance(r.get("jev"), dict)]
    scored = [r for r in jev_rows if r.get("agreement") is not None]
    confidences = [r["jev"]["confidence"] for r in jev_rows if isinstance(r["jev"].get("confidence"), (int, float))]
    return {
        "decisions": len(records),
        "jev_scored": len(jev_rows),
        "agreement_rate": (sum(1 for r in scored if r["agreement"]) / len(scored)) if scored else None,
        "mean_confidence": (sum(confidences) / len(confidences)) if confidences else None,
    }


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _usage_buckets(row):
    return {"input_tokens": int(row.get("input_uncached_tokens", row.get("input_tokens")) or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens", row.get("cache_read_input_tokens")) or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens", row.get("cache_creation_input_tokens")) or 0)}


def _interactive_summary(root, cfg, cutoff, now, headless):
    """Read only transcript bytes already accounted by Pool.tally_planner, without mutating offsets."""
    from .pool import TZ, encode_project_dir
    usage = _read_json(root / "planner_usage.json", {})
    sessions, day_totals = [], {}
    known = {r.get("session_id") for r in headless if r.get("session_id")}
    for account in cfg.get("claude_accounts", []):
        account_id = account["id"]
        project = Path(account["config_dir"]).expanduser() / "projects" / encode_project_dir(str(ROOT.resolve()))
        for name, offset in usage.get(account_id, {}).get("offsets", {}).items():
            if Path(name).name != name or Path(name).stem in known:
                continue
            totals = _usage_buckets({})
            daily = {}
            try:
                with (project / name).open("rb") as stream:
                    raw = stream.read(max(0, int(offset)))
            except (OSError, TypeError, ValueError):
                continue
            for line in raw.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    continue
                try:
                    row = json.loads(line)
                    if row.get("type") != "assistant" or row.get("sessionId") in known:
                        continue
                    dt = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    if not cutoff <= dt.timestamp() <= now:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
                buckets = _usage_buckets((row.get("message") or {}).get("usage") or {})
                day = dt.astimezone(TZ).date().isoformat()
                day_row = daily.setdefault(day, _usage_buckets({}))
                for key, value in buckets.items():
                    totals[key] += value
                    day_row[key] += value
            if daily:
                sessions.append({"label": "interactive", "account": account_id,
                                 "session": Path(name).stem, **totals})
                for day, buckets in daily.items():
                    target = day_totals.setdefault((day, account_id), _usage_buckets({}))
                    for key, value in buckets.items():
                        target[key] += value
    return {"label": "interactive", "sessions_count": len(sessions), "sessions": sessions,
            "day_totals": [{"day": day, "account": account, **totals}
                           for (day, account), totals in sorted(day_totals.items())]}


def premium_summary(days=7, root=None):
    """Rolling launch audit. Limits are advisory; this function never schedules or blocks work.

    Ledger launch snapshots include unfinished/unmetered invocations. Finished run rows
    supply usage and replace matching snapshots, so retries count without double counting.
    Input means uncached input; cache share includes read and write input in its denominator.
    """
    root = Path(root) if root is not None else STATE
    now = time.time()
    cutoff = now - days * 86400
    records = _read_json(root / "runs" / "planner_runs.json", [])
    try:
        cfg = tomllib.loads((root / "pool.toml").read_text())
    except (OSError, ValueError):
        cfg = {}
    if isinstance(records, dict):
        records = records.get("records", [])
    launches = []
    for record in records:
        if "launches" in record:
            launches.extend(dict(r) for r in record["launches"])
        elif record.get("pid") or record.get("status") in ("running", "exited_ok", "exited_early", "gave_up"):
            launches.append(dict(record))
    unique, seen_launches = [], set()
    for launch in launches:
        launch_id = launch.get("launch_id")
        if launch_id and launch_id in seen_launches:
            continue
        if launch_id:
            seen_launches.add(launch_id)
        if launch.get("launch_route"):
            launch["route"] = launch["launch_route"]
        unique.append(launch)
    launches = unique
    for path in sorted((root / "runs").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("role") != "planner_decision":
                continue
            match = next((r for r in launches if
                          (row.get("launch_id") and r.get("launch_id") == row["launch_id"]) or
                          (not row.get("launch_id") and not r.get("_matched") and
                           r.get("goal_id") == row.get("goal_id", row.get("task")) and
                           (not row.get("payload_key") or r.get("payload_key") == row["payload_key"]))), None)
            if match is None:
                match = {}
                launches.append(match)
            match.update(row, _matched=True)
    recent = [r for r in launches if cutoff <= (r.get("started_at") or r.get("ts") or 0) <= now]
    kinds, routes, reasons = Counter(), Counter(), Counter()
    totals = _usage_buckets({})
    inputs, per_goal = [], {}
    for row in recent:
        kind = row.get("decision_kind", row.get("kind", "unknown"))
        route = row.get("route") or "escalate"
        reason = row.get("reason") or row.get("route_reason") or "legacy:autonomous"
        kinds[kind] += 1
        routes[route] += 1
        reasons[reason] += 1
        buckets = _usage_buckets(row)
        # Missing usage is unknown, not a zero-token invocation.
        if any(key in row for key in ("input_tokens", "input_uncached_tokens")):
            inputs.append(buckets["input_tokens"])
        for key, value in buckets.items():
            totals[key] += value
        if route == "escalate":
            goal = row.get("goal_id", row.get("task"))
            per_goal.setdefault(goal, []).append(reason)
    limit = cfg.get("planner", {}).get("routes", {}).get("premium_launches_soft_per_goal", 2)
    exceptions = [{"goal_id": goal, "launches": len(why), "limit": limit,
                   "reasons": dict(Counter(why))} for goal, why in per_goal.items() if len(why) > limit]
    input_total = totals["input_tokens"] + totals["cache_read_tokens"] + totals["cache_write_tokens"]
    from . import planner_telemetry
    try:
        interactive_by_goal = planner_telemetry.interactive_by_goal(root=root, cfg=cfg, days=days)
    except Exception:
        interactive_by_goal = None
    try:
        accepted_goals = planner_telemetry.accepted_goal_summary(root=root)
    except Exception:
        accepted_goals = None
    return {"days": days, "headless": {"count": len(recent), "by_kind": dict(kinds),
            "by_route": dict(routes), "top_reasons": dict(reasons.most_common(5)),
            "mean_input_tokens": sum(inputs) / len(inputs) if inputs else None,
            "max_input_tokens": max(inputs) if inputs else None, **totals,
            "cache_read_share": totals["cache_read_tokens"] / input_total if input_total else 0,
            "usd": sum(r.get("usd") or 0 for r in recent),
            "gave_up": sum(r.get("status") == "gave_up" and
                           cutoff <= r.get("started_at", 0) <= now for r in records)},
            "interactive": _interactive_summary(root, cfg, cutoff, now, launches),
            "interactive_by_goal": interactive_by_goal,
            "accepted_goals": accepted_goals,
            "exceptions": exceptions}


def _goal_launches():
    data = _read_json(_runs_path(), {})
    return data.get("goal_launches", {}) if isinstance(data, dict) else {}


def _cursor():
    return bus.db().execute("select coalesce(max(seq), 0) from events").fetchone()[0]


def state_version(goal_id):
    rows = []
    for task in bus.read():
        if task.get("parent") != goal_id:
            continue
        seq = bus.db().execute("select coalesce(max(seq), 0) from events where task_id=?",
                               (task["id"],)).fetchone()[0]
        rows.append((task["id"], task["status"], task.get("hold_reason"), task.get("merged_into"), seq))
    return hashlib.sha256(json.dumps(sorted(rows), separators=(",", ":")).encode()).hexdigest()[:12]


def _point(point):
    if isinstance(point, dict):
        return point
    goal_id, kind, payload_key = point
    return {"goal_id": goal_id, "kind": kind, "payload_key": payload_key,
            "task_id": payload_key.split(":", 1)[0] if kind == "held" else goal_id}


def _infra_kind(text):
    text = str(text or "").lower()
    if re.search(r"unauthori[sz]ed|authentication|invalid.api.key|oauth|permission denied|\bauth\b|\b40[13]\b", text):
        return "auth"
    if re.search(r"quota|usage.?limit|rate.?limit|hit your limit|out of usage credits|\b429\b|cooling", text):
        return "quota"
    if re.search(r"unavailable|overloaded|connection refused|connection reset|\b50[234]\b", text):
        return "unavailable"
    return None


def _premium_launches(goal_id):
    launches = set()
    for record in _load_records():
        if record["goal_id"] != goal_id:
            continue
        for row in record.get("launches", [record] if record.get("pid") else []):
            if row.get("launch_route", row.get("route")) == "escalate":
                launches.add(row.get("launch_id") or (record["kind"], record["payload_key"], row.get("started_at")))
    return len(launches)


def _gate_state(goal, children):
    try:
        result = gitutil._git_in(ROOT, "rev-parse", f"goal/{goal['id']}")
        head = result.stdout.strip() if result.returncode == 0 else None
    except OSError:
        head = None
    if not head:
        return "unknown"
    last = (goal.get("pipeline") or {}).get("last_merge") or {}
    if last.get("status") == "tests_red" and last.get("head_sha") == head:
        return "red"
    if last:
        return "green" if last.get("status") == "merged" and last.get("sha") == head else "unknown"
    # merge.merge writes these fields only after its gate and fast-forward succeed.
    merged = [t for t in children if t.get("sha") and t.get("merged_into") == f"goal/{goal['id']}"]
    if merged:
        def last_seq(t):
            return bus.db().execute("select coalesce(max(seq),0) from events where task_id=?", (t["id"],)).fetchone()[0]
        return "green" if max(merged, key=last_seq)["sha"] == head else "unknown"
    return "unknown"


def build_ctx(point, pool=None):
    """Gather complete routing evidence without running tests or mutating tasks."""
    point = _point(point)
    pool = pool or Pool()
    cfg = pool.cfg
    goal = bus.get(point["goal_id"])
    task = _decision_task(point["goal_id"], point["kind"], point["payload_key"]) or goal
    children = [t for t in bus.read() if t.get("parent") == goal["id"]]
    scouts = [t for t in children if t.get("role") == "scout"]
    blocked = [t["id"] for t in scouts if t.get("status") in ("held", "failed")
               or (t.get("result") or {}).get("blocked")]
    kind = (task.get("pipeline") or {}).get("failure_kind")
    infra = {"quota": "quota", "permissions": "auth"}.get(kind)
    involved = {task.get("account"), task.get("assigned_to"), task.get("executor")}
    for source in [*pool.accounts, *getattr(pool, "executors", {}).values()]:
        if source.id in involved and (source.cooling() or "usage" in source.hold_reason.lower()):
            infra = _infra_kind(source.hold_reason) or infra
    ctx = {"routes": cfg.get("planner", {}).get("routes", {}), "infra_failure_kind": infra,
           "premium_launches": _premium_launches(goal["id"]), "blocked_scouts": blocked}
    if point["kind"] == "held":
        chain = failures.lineage(task)
        signature = failures.failure_signature(task)
        comments = [c for _, cs in failures.rejecting_reviews(task) for c in cs]
        chain_ids = {t["id"] for t in chain}
        ctx.update(failing_ids=failures.test_ids((task.get("resume_hint") or {}).get("failures")) or [],
                   in_scope_review_comments=bool(comments) and all(
                       failures.path_in_scope(c.get("path"), task.get("scope") or []) for c in comments),
                   auto_fix_rounds_used=sum((t.get("constraints") or {}).get("auto_round") is not None for t in chain),
                   auto_fix_rounds=cfg.get("daemon", {}).get("auto_fix_rounds", 2),
                   failure_signature=signature,
                   signature_repeated=any((t.get("constraints") or {}).get("failure_signature") == signature for t in chain),
                   spec_review_request_changes=sum(
                       bool((r.get("inputs") or [])[:1] and r["inputs"][0] in chain_ids)
                       and (r.get("review_verdict") or (r.get("result") or {}).get("verdict")) == "request_changes"
                       for r in bus.read(role="spec_review")),
                   hold_reason=task.get("hold_reason"))
        ctx.update({f"touches_{area}": value for area, value in failures.touch_areas(task, cfg).items()})
    elif point["kind"] == "scouts_done":
        review = cfg.get("review", {})
        scope = goal.get("scope") or []
        patterns = review.get("semantic_patterns", {})
        if isinstance(patterns, dict):
            patterns = patterns.values()
        semantic_text = str(goal.get("spec") or "") + "\n" + "\n".join(scope)
        try:
            semantic = any(re.search(pattern, semantic_text) for pattern in patterns)
        except (TypeError, re.error):
            semantic = True
        ctx.update(goal_complexity=goal.get("complexity"), scout_count=len(scouts),
                   all_scouts_posted=bool(scouts) and all(t["status"] in ("done", "failed") for t in scouts),
                   security_trigger=any(fnmatch.fnmatch(path, glob) for path in scope
                                        for glob in review.get("security_paths", [])),
                   semantic_trigger=semantic or any(fnmatch.fnmatch(path, glob) for path in [*scope, str(goal.get("spec") or "")]
                                                    for glob in review.get("semantic_paths", [])))
    elif point["kind"] == "closable":
        executes = [t for t in children if t["role"] == "execute"]
        ctx.update(all_children_merged=bool(executes) and all(t["status"] == "done" and t.get("merged_into") for t in executes),
                   gate_state=_gate_state(goal, children))
    return ctx


def _packet_sections(sections):
    result = []
    for point, ctx, route, classification in sections:
        task = _decision_task(point["goal_id"], point["kind"], point["payload_key"])
        comments, dependencies, failure_text = [], [], None
        if task is not None:
            comments = [{key: comment.get(key) for key in ("path", "line", "issue")}
                        for _, reviews in failures.rejecting_reviews(task) for comment in reviews][:10]
            failure_text = (task.get("resume_hint") or {}).get("failures")
            if failure_text is None:
                failure_text = (task.get("result") or {}).get("failures")
            for dep in task.get("depends_on") or []:
                try:
                    status = bus.get(dep)["status"]
                except KeyError:
                    status = "missing"
                dependencies.append({"id": dep, "status": status})
        result.append({"point": point, "ctx": ctx, "route": route, "classification": classification,
                       "task": task, "reviews": comments, "failures": failure_text,
                       "depends_on_statuses": dependencies})
    return result


def _previous_launch(goal_id):
    return max((r for r in _load_records() if r.get("goal_id") == goal_id and r.get("launch_id")),
               key=lambda r: r.get("started_at") or 0, default=None)


def _packet_tasks():
    """Ignore stale index entries left behind when a task file is purged."""
    try:
        return bus.read()
    except KeyError:
        tasks = []
        for (task_id,) in bus.db().execute("select id from tasks").fetchall():
            try:
                tasks.append(bus.get(task_id))
            except KeyError:
                continue
        return tasks


def _changes_since(goal_id, cursor):
    changes, seen = [], {}
    with bus.locked():
        ids = [goal_id] + [t["id"] for t in _packet_tasks() if t.get("parent") == goal_id]
        placeholders = ",".join("?" for _ in ids)
        rows = bus.db().execute(
            f"select task_id, seq, ts, data from events where seq > ? "
            f"and task_id in ({placeholders}) order by seq", [cursor, *ids])
        for task_id, _, ts, data in rows:
            data = json.loads(data)
            if not isinstance(data, dict):
                continue
            for field in ("status", "hold_reason", "merged_into"):
                if field in data:
                    key = (task_id, field)
                    changes.append({"task": task_id, "field": field, "from": seen.get(key),
                                    "to": data[field], "ts": ts})
                    seen[key] = data[field]
    return changes[-20:]


def _scout_findings(goal_id):
    findings = [finding for task in _packet_tasks()
                if task.get("parent") == goal_id and task.get("role") == "scout" and task.get("status") == "done"
                for finding in scout_evidence.normalize_findings(task.get("result"))]
    return sorted(findings, key=lambda f: f["confidence"], reverse=True)[:10]


def _memory_hits(goal):
    try:
        return [{"title": hit.get("title", "")} for hit in
                scout_evidence.memory_recall(goal.get("title", ""), root=STATE)["hits"][:10]]
    except Exception:
        return []


def _build_grouped(sections):
    goal_id = sections[0][0]["goal_id"]
    goal, prev = bus.get(goal_id), _previous_launch(goal_id)
    meta = planner_packet.build(
        _packet_sections(sections), goal=goal,
        previous={"state_version": prev.get("state_version")} if prev else None,
        changes=_changes_since(goal_id, prev.get("cursor_at_launch", 0)) if prev else None,
        scout_findings=_scout_findings(goal_id), memory_hits=_memory_hits(goal), cap_chars=_packet_cap())
    return meta["text"], meta


def grouped_packet(sections):
    """Public text-only interface; launch callers retain the build metadata."""
    return _build_grouped(sections)[0]


def _previous_shadow(goal_id):
    path = STATE / "runs/sched/planner_shadow.jsonl"
    rows = []
    try:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("goal_id") == goal_id:
                rows.append(row)
    except OSError:
        return None
    return max(rows, key=lambda row: row.get("started_at") or 0, default=None)


def launch_routed(pool, route, prompt, account, budget, log):
    """Use the selected model without changing global config or the legacy launcher."""
    tier = route.tier or "fable"
    model = pool.cfg.get("models", {}).get("planner" if tier == "fable" else tier, tier)
    return goals.launch_planner(ROOT, prompt, account.id, budget, log, model=model)


def _cool_infra(pool, account_id, kind, goal_id):
    try:
        account = pool.get(account_id)
        pool.cooldown(account, 1800, reason=kind)
    except (StopIteration, KeyError):
        if account_id in getattr(pool, "executors", {}):
            pool.cooldown_executor(account_id, 1800, reason=kind)
    notify.notify_once(goal_id, f"infra:{account_id}:{kind}", f"{goal_id}: Planner {account_id} cooling ({kind})")


def _reconcile_infra(record, records):
    text = ""
    for path in (record.get("log"), record.get("stderr_log") or (str(record.get("log")) + ".stderr")):
        if path:
            try:
                content = Path(path).read_text(errors="replace")[-12000:]
                if path == record.get("log"):
                    for line in content.splitlines():
                        try:
                            output = json.loads(line)
                        except ValueError:
                            text += line + "\n"
                            continue
                        if isinstance(output, dict) and output.get("is_error"):
                            text += str(output.get("result") or output.get("error") or "")
                else:
                    text += content
            except OSError:
                pass
    kind = _infra_kind(text)
    if not kind:
        return False
    _cool_infra(Pool(), record.get("account"), kind, record["goal_id"])
    record.update(status="infra_failure", infra_failure_kind=kind)
    guards = _goal_launches()
    guard = guards.get(record["goal_id"], {})
    if guard.get("launch_id") == record.get("launch_id"):
        previous = record.get("previous_goal_launch")
        if previous:
            guards[record["goal_id"]] = previous
        else:
            guards.pop(record["goal_id"], None)
        _save_records(records, guards)
    return True


def _telemetry_skip(point, event, reason):
    planner_telemetry.record_skip(goal_id=point["goal_id"], event=event, reason=reason,
                                  kind=point["kind"], payload_key=point["payload_key"], root=STATE)


def _class_evidence(model, classification, pool):
    evidence = {}
    try:
        from . import planner_scorecard
        evidence = planner_scorecard.class_evidence(
            model, classification["decision_type"], classification["band"],
            classification["task_class"], classification["architectural"], root=STATE, cfg=pool.cfg)
    except Exception:
        pass
    return {"n": int(evidence.get("n") or 0), "noninferior": evidence.get("noninferior"),
            "reescalation_rate": float(evidence.get("reescalation_rate") or 0.0)}


def run_group(sections, pool):
    goal_id = sections[0][0]["goal_id"]
    def skip(reason, selected=None):
        for point, _, _, _ in sections if selected is None else selected:
            _telemetry_skip(point, "run_group", reason)

    with bus.locked():
        version = state_version(goal_id)
        guards = _goal_launches()
        previous = guards.get(goal_id)
        if previous and previous.get("last_state_version") == version:
            skip("same_state_version")
            return {"launched": False, "reason": "same state_version"}
        records = _load_records()
        if any(r["goal_id"] == goal_id and r.get("status") in ("claimed", "running") for r in records):
            skip("goal_launch_active")
            return {"launched": False, "reason": "goal launch active"}
        skip("already_decided", [s for s in sections if _blocked(goal_id, s[0]["kind"], s[0]["payload_key"], records)])
        sections = [s for s in sections if not _blocked(goal_id, s[0]["kind"], s[0]["payload_key"], records)]
        if not sections:
            return {"launched": False, "reason": "already decided"}
        launch_id, cursor = uuid.uuid4().hex, _cursor()
        sections = sorted(sections, key=lambda s: (
            -{"investigate": 1, "escalate": 2}[s[2].name],
            -planner_router.effective_complexity(s[3], s[1]), str(s[0]["payload_key"])))
        leader, ctx, route, classification = sections[0]
        ctx = dict(ctx, state_version=version)
    account = None
    try:
        reason = "planner session attached" if _session_attached() else None
        if not reason:
            account = pool.pick("planner")
            reason = "no account with headroom" if account is None else None
        if reason:
            for point, _, _, _ in sections:
                _record_skip(goal_id, point["kind"], point["payload_key"], reason)
                _telemetry_skip(point, "run_group", "planner_session_attached" if _session_attached() else "no_account_headroom")
            return {"launched": False, "reason": reason}
        rcfg = planner_router.load_cfg(pool.cfg)
        models = pool.cfg.get("models", {})
        availability = {}
        for tier in (rcfg["default_tier"], rcfg["escalation_tier"]):
            configured = bool(models.get("planner" if tier == "fable" else tier))
            availability[tier] = {"available": configured and account is not None,
                                  "reason": "ok" if configured else "model_not_configured"}
        headroom = (1 - (account.day_tokens + account.planner_day_tokens) / account.daily_budget
                    if account.daily_budget else None)
        availability["fable_reserve_ok"] = planner_router.fable_reserve_ok(headroom, rcfg)
        evidence = _class_evidence(models.get("planner" if rcfg["default_tier"] == "fable"
                                              else rcfg["default_tier"]), classification, pool)
        history = [row for row in planner_telemetry.read_invocations(root=STATE)
                   if row.get("goal_id") == goal_id]
        latest = max(history, key=lambda row: row.get("started_at") or 0, default={})
        used, cap = ctx.get("auto_fix_rounds_used"), ctx.get("auto_fix_rounds")
        if latest.get("tier") == rcfg["default_tier"] and (
                classification["decision_type"] == "architectural_replan"
                or (ctx.get("spec_review_request_changes") or 0) >= rcfg["hard_spec_review_request_changes_min"]
                or (type(used) is int and type(cap) is int and used >= cap)):
            ctx["reescalation"] = True
        hard = planner_router.hard_escalation(classification, ctx, route, rcfg)
        signal = None
        if jev_planner.should_ask(rcfg["mode"], rcfg["jev_mode"], hard, availability, route.name)[0]:
            signal = jev_planner.ask(
                classification, ctx, bus.get(goal_id), cfg=rcfg, router_mode=rcfg["mode"],
                hard_reasons=hard, availability=availability, route=route.name,
                state_version=version, task=_decision_task(goal_id, leader["kind"], leader["payload_key"])
                if leader["kind"] == "held" else None, ask_fn=jev.ask, root=STATE)
        sample = random.random()
        routed = planner_router.decide(classification, ctx, route, cfg=rcfg,
                                       availability=availability, evidence=evidence,
                                       jev_signal=signal, sample=sample)
        planner_router.record(routed, subject=goal_id, route=route, jev_signal=signal, root=STATE)
        with bus.locked():
            records = _load_records()
            # Recheck after policy evaluation so concurrent ticks cannot both claim.
            guard = _goal_launches().get(goal_id) or {}
            if guard.get("last_state_version") == version:
                skip("same_state_version")
                return {"launched": False, "reason": "same state_version"}
            if any(r["goal_id"] == goal_id and r.get("status") in ("claimed", "running") for r in records):
                skip("goal_launch_active")
                return {"launched": False, "reason": "goal launch active"}
            if routed.hold:
                for point, _, _, _ in sections:
                    r = _find_record(records, goal_id, point["kind"], point["payload_key"])
                    if r is None:
                        r = dict(point, attempts=0)
                        records.append(r)
                    r.update(status="held_for_fable", hold_reason=routed.hold_reason)
                _save_records(records)
                notify.notify_once(goal_id, f"held_for_fable:{launch_id}",
                                   f"{goal_id}: {routed.hold_reason}")
                return {"launched": False, "reason": routed.hold_reason}
            if not all(_SAFE_KEY.fullmatch(str(p[k])) for p, _, _, _ in sections
                       for k in ("goal_id", "kind", "payload_key")):
                raise ValueError("unsafe decision key")
            shadow_row = _previous_shadow(goal_id) if ctx.get("reescalation") else None
            packet_kind = "escalation" if shadow_row is not None else "decision"
            if shadow_row is not None:
                goal = bus.get(goal_id)
                packet_meta = planner_packet.escalation_packet(
                    goal=goal, original_sections=_packet_sections(sections),
                    opus_decision={key: shadow_row.get(key) for key in
                                   ("proposed_action", "summary", "tasks_proposed", "confidence",
                                    "needs_fable", "unresolved")},
                    unresolved=shadow_row.get("unresolved"), conflicting_evidence=None,
                    scout_findings=_scout_findings(goal_id), memory_hits=_memory_hits(goal),
                    reason=";".join(hard), cap_chars=_packet_cap())
                packet_text = packet_meta["text"]
            else:
                packet_text, packet_meta = _build_grouped(sections)
            for point, section_ctx, section_route, _ in sections:
                _claim(goal_id, point["kind"], point["payload_key"],
                       _existing_attempts(goal_id, point["kind"], point["payload_key"]), section_route)
            records = _load_records()
            for point, section_ctx, _, _ in sections:
                r = _find_record(records, goal_id, point["kind"], point["payload_key"])
                r.update(launch_id=launch_id, state_version=version, cursor_at_launch=cursor,
                         previous_goal_launch=previous, launch_route=route.name, tier=routed.tier,
                         exception=route.name == "escalate" and section_ctx["premium_launches"] >=
                         section_ctx["routes"].get("premium_launches_soft_per_goal", 2))
            _save_records(records)
        if not all(_SAFE_KEY.fullmatch(str(p[k])) for p, _, _, _ in sections for k in ("goal_id", "kind", "payload_key")):
            raise ValueError("unsafe decision key")
        handover.write("goal decision")
        prompt = spawn.render("planner-decision", packet=packet_text)
        log = STATE / "runs" / f"planner-decision-{goal_id}-{launch_id}.log"
        budget = pool.cfg.get("limits", {}).get("max_budget_usd", {}).get("planner_decision", 3)
        before = planner_telemetry.snapshot(goal_id, root=STATE)
        now = time.time()
        launched = launch_routed(pool, dataclasses.replace(route, tier=routed.tier), prompt, account, budget, log)
        model = models.get("planner" if routed.tier == "fable" else routed.tier, routed.tier)
        planner_telemetry.record_launch(
            launch_id=launch_id, goal_id=goal_id, event="tick",
            decision_type=classification["decision_type"], kind=leader["kind"],
            payload_keys=[p["payload_key"].split(":")[0] if p["kind"] == "held"
                          else p["payload_key"] for p, _, _, _ in sections],
            model=model, tier=routed.tier, account=account.id, complexity=ctx.get("complexity"),
            band=classification["band"], task_class=classification["task_class"],
            architectural=classification["architectural"], route=route.name, route_reason=route.reason,
            mode=rcfg["mode"], state_version=version, packet_chars=packet_meta["chars"], started_at=now,
            reescalation=bool(ctx.get("reescalation")), root=STATE)
        shadow_fields = {}
        if planner_shadow.eligible(routed, rcfg, sample=sample)[0]:
            try:
                shadow_account = next(a for a in pool.cfg["claude_accounts"] if a["id"] == account.id)
                shadow = planner_shadow.launch(
                    prompt, model=models[routed.shadow_tier], account=shadow_account,
                    budget_usd=pool.cfg.get("limits", {}).get("max_budget_usd", {}).get("planner_shadow", 1.5),
                    log=STATE / "runs" / f"planner-shadow-{goal_id}-{launch_id}.log", root=ROOT)
                if shadow is not None:
                    shadow_fields = dict(shadow_pid=shadow["pid"], shadow_pid_start=goals._proc_start(shadow["pid"]),
                                         shadow_log=shadow["log"], shadow_started_at=time.time(),
                                         shadow_tier=routed.shadow_tier, shadow_model=models[routed.shadow_tier])
            except Exception as exc:
                print(f"[planner_runs] shadow launch failed: {exc}", file=sys.stderr)
        infra = _infra_kind(launched.get("stderr") or launched.get("error"))
        if infra:
            raise RuntimeError(infra)
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        infra = _infra_kind(str(exc))
        if infra:
            _cool_infra(pool, account.id if account else None, infra, goal_id)
            with bus.locked():
                records = _load_records()
                for point, _, _, _ in sections:
                    _find_record(records, goal_id, point["kind"], point["payload_key"]).update(status="infra_failure", infra_failure_kind=infra)
                _save_records(records)
        else:
            for point, _, _, _ in sections:
                attempts = _existing_attempts(goal_id, point["kind"], point["payload_key"])
                status, attempts = _record_failed_launch(goal_id, point["kind"], point["payload_key"], attempts, exc)
                if status == "gave_up":
                    notify.notify_once(goal_id, f"gave_up:{launch_id}", f"{goal_id}: Planner gave up after {attempts} attempts")
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return {"launched": False, "reason": infra or "failed_launch"}
    with bus.locked():
        records = _load_records()
        for point, _, _, _ in sections:
            r = _find_record(records, goal_id, point["kind"], point["payload_key"])
            r.update(status="running", pid=launched["pid"], pid_start=launched["pid_start"],
                     log=launched["log"], stderr_log=launched.get("stderr_log"), account=account.id,
                     prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:12],
                     telemetry_before=before, launch_started_at=now,
                     packet_kind=packet_kind, packet_chars=packet_meta["chars"],
                     packet_hash=packet_meta["hash"], packet_delta=packet_meta["delta"],
                     packet_truncated=packet_meta["truncated"],
                     decision_type=classification["decision_type"], **shadow_fields)
            r.setdefault("launches", []).append({k: v for k, v in r.items() if k != "launches"})
        guards = _goal_launches()
        guards[goal_id] = {"last_state_version": version, "cursor": cursor, "launch_id": launch_id}
        _save_records(records, guards)
    return {"launched": True, **launched}


def stamp(goal_id, stage):
    """Claim close before any effects; evidence, not this stamp, reconciles crashes."""
    with bus.locked():
        goal = bus.get(goal_id)
        pipeline = dict(goal.get("pipeline") or {})
        if pipeline.get(stage):
            return False
        pipeline[stage] = time.time()
        bus.update(goal_id, pipeline=pipeline)
        return True


def _retrospective(goal_id):
    memory = STATE / "memory"
    for path in memory.glob("*.md"):
        if re.search(rf"\bgoal:\s*{re.escape(goal_id)}\b", path.read_text()):
            return
    plan = STATE / "plan.md"
    text = plan.read_text() if plan.exists() else ""
    marker = f"no learnings — goal: {goal_id}"
    if marker not in text:
        plan.parent.mkdir(parents=True, exist_ok=True)
        with plan.open("a") as stream:
            stream.write(f"\n{datetime.now().date().isoformat()}: {marker}\n")


def _open_pr(goal):
    branch = f"goal/{goal['id']}"
    listed = subprocess.run(["gh", "pr", "list", "--head", branch, "--base", "main", "--state", "all",
                             "--json", "url"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if listed.returncode:
        raise RuntimeError(listed.stderr[:500] or "gh pr list failed")
    prs = json.loads(listed.stdout)
    if prs:
        return prs[0]["url"]
    body = (f"Goal: {goal['id']}\n\nAll execute children merged into `{branch}`. "
            "The goal head passed the merge gate.\n\nValidation: tests-green before fast-forward.\n")
    created = subprocess.run(["gh", "pr", "create", "--head", branch, "--base", "main",
                              "--title", f"{goal['id']}: {goal['title'][:150]}", "--body", body],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
    if created.returncode or not created.stdout.strip():
        raise RuntimeError(created.stderr[:500] or "gh pr create failed")
    return created.stdout.strip()


def routine_close(point, ctx, pool):
    goal_id = point["goal_id"]
    with bus.locked():
        version = state_version(goal_id)
        goal = bus.get(goal_id)
        pipeline = dict(goal.get("pipeline") or {})
        guard = _goal_launches().get(goal_id, {})
        # A matching prior model launch owns this state. A stamped close can resume effects.
        if guard.get("last_state_version") == version and not pipeline.get("closed_at"):
            return
        if ctx["gate_state"] != "green" or not ctx["all_children_merged"]:
            return
        stamp(goal_id, "closed_at")
    _retrospective(goal_id)
    with bus.locked():
        goal = bus.get(goal_id)
        pipeline = dict(goal.get("pipeline") or {})
        if (goal.get("result") or {}).get("goal_closed"):
            return
        auto_pr = ctx["routes"].get("auto_open_pr", False)
        if auto_pr and pipeline.get("close_error"):
            if time.time() - pipeline.get("close_attempted_at", 0) < pool.cfg.get("daemon", {}).get("close_retry_s", 900):
                return
    try:
        pr_url = _open_pr(goal) if auto_pr else None
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        with bus.locked():
            goal = bus.get(goal_id)
            if (goal.get("result") or {}).get("goal_closed"):
                return
            pipeline = dict(goal.get("pipeline") or {})
            pipeline.update(close_error=str(exc)[:500], close_attempted_at=time.time(),
                            close_attempts=pipeline.get("close_attempts", 0) + 1)
            bus.update(goal_id, pipeline=pipeline)
        notify.notify_once(goal_id, "close_error", f"{goal_id}: PR close failed: {str(exc)[:100]}")
        return
    with bus.locked():
        goal = bus.get(goal_id)
        if (goal.get("result") or {}).get("goal_closed"):
            return
        result = {"goal_closed": True, "pr_url": pr_url,
                  "summary": "Goal complete", "note": "PR opened" if pr_url else "PR pending: Planner opens goal/<id> to main"}
        bus.post_result(goal_id, result)
    with bus.locked():
        pipeline = dict(bus.get(goal_id).get("pipeline") or {})
        pipeline.pop("close_error", None)
        bus.update(goal_id, pipeline=pipeline)
    notify.notify_once(goal_id, "closed", f"{goal_id}: {result['note']}")


def tick(pool=None):
    """Route ready points, performing routine effects and one grouped launch per goal."""
    pool = pool or Pool()
    config = pool.cfg.get("planner", {})
    # Old configurations predate routing and retain their one-point tick contract.
    if "routes" not in config:
        for goal_id, kind, key in decision_points():
            run(goal_id, kind, key)
            break
        return
    groups = {}
    enabled = decision.routes_enabled(config)
    for raw in list(decision_points()):
        point = _point(raw)
        if point["kind"] == "held" and any(t.get("status") != "failed" and
                (t.get("constraints") or {}).get("fix_round_for") == point["task_id"] for t in bus.read()):
            _telemetry_skip(point, "tick", "fix_round_in_flight")
            continue
        ctx = build_ctx(point, pool)
        goal = bus.get(point["goal_id"])
        task = _decision_task(point["goal_id"], point["kind"], point["payload_key"])
        ctx["complexity"] = (task if point["kind"] == "held" else goal).get("complexity")
        ctx["state_version"] = state_version(point["goal_id"])
        ctx["state_unchanged"] = (_goal_launches().get(point["goal_id"]) or {}).get("last_state_version") == ctx["state_version"]
        classification = planner_taxonomy.classify(point, ctx, goal=goal, task=task)
        if point["kind"] == "closable" and ctx["gate_state"] == "unknown":
            _telemetry_skip(point, "tick", "unknown_gate")
            print(f"[planner_runs] {point['goal_id']}: unknown goal gate; skipping tick", file=sys.stderr)
            continue
        route = decision.route(point, ctx)
        if point["kind"] == "closable" and route.name == "routine":
            pipeline = bus.get(point["goal_id"]).get("pipeline") or {}
            if pipeline.get("close_error") and pipeline.get("close_attempts", 0) >= pool.cfg.get("daemon", {}).get("close_max_attempts", 3):
                route = decision.Route("escalate", "close_failed", [f"close_error:{pipeline['close_error']}"],
                                       [f"close_attempts:{pipeline['close_attempts']}"], ctx["routes"].get("escalate_tier", "fable"))
            else:
                routine_close(point, ctx, pool)
        if route.name in ("none", "routine"):
            _telemetry_skip(point, "tick", "routine" if route.name == "routine" else f"none:{route.reason}")
            continue
        if not enabled:
            run(point["goal_id"], point["kind"], point["payload_key"], route, routed=True)
        else:
            groups.setdefault(point["goal_id"], []).append((point, ctx, route, classification))
    for sections in groups.values():
        run_group(sections, pool)
