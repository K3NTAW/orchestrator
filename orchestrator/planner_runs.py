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
"""
import json, os, re, sys, tempfile, time, tomllib
from pathlib import Path
from . import ROOT, STATE, bus, goals, handover, jev, spawn
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


def decision_packet(goal_id, kind, payload, repo_path=ROOT):
    """Build a bounded decision packet, preserving its decision context before inventory."""
    repo_path = Path(repo_path)
    try:
        cfg = tomllib.loads((repo_path / ".orchestrator" / "pool.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        cfg = {}
    try:
        cap = max(1, int(cfg.get("planner", {}).get("decision_packet_chars", 6000)))
    except (TypeError, ValueError):
        cap = 6000

    expected = {"scouts_done": "write specs", "held": "write or approve a fix round",
                "closable": "close the goal"}.get(kind, "make the requested decision")
    try:
        goal = bus.get(goal_id)
    except KeyError:
        goal = None
    lines = [f"Decision: {kind} — expected to {expected}.", f"Goal id: {goal_id}", f"Payload id: {payload}"]
    lines += _fenced("Goal title", goal.get("title", "") if goal else "")
    if goal is None:
        return "\n".join(lines)[:cap]

    inventory = []
    if kind == "held":
        task_id = str(payload).split(":", 1)[0]
        lines.append(f"Task id: {task_id}")
        try:
            task = bus.get(task_id)
        except KeyError:
            task = None
        lines += _fenced("Task title", task.get("title", "") if task else "")
        if task is not None:
            lines += _fenced("Hold reason", task.get("hold_reason", ""))
            failures = (task.get("resume_hint") or {}).get("failures")
            if failures is None:
                failures = (task.get("result") or {}).get("failures")
            if failures:
                lines += _fenced("Failures", failures, 1500)
            comments = []
            reviews = [r for r in bus.read(role="review") if (r.get("inputs") or [])[:1] == [task_id]]
            for review in reviews:
                for comment in (review.get("result") or {}).get("comments", []):
                    comments.append(f"{comment.get('path', '?')}:{comment.get('line', '?')} "
                                    f"{str(comment.get('issue', ''))[:200]}")
            if comments:
                lines += _fenced("Review comments", "\n".join(comments))
            inventory = spawn.packet(task, repo_path).splitlines()
    elif kind == "scouts_done":
        children = [t for t in bus.read() if t.get("parent") == goal_id and t.get("role") == "scout"]
        summaries = []
        for scout in children:
            summaries.append(f"{scout['id']}: " + str((scout.get("result") or {}).get("summary", ""))[:300])
        if summaries:
            lines += _fenced("Scout summaries", "\n".join(summaries))
    elif kind == "closable":
        children = [t for t in bus.read() if t.get("parent") == goal_id and t.get("role") == "execute"
                    and t.get("merged_into")]
        if children:
            lines += _fenced("Merged tasks", "\n".join(f"{t['id']} {t.get('sha', '?')}" for t in children))

    prefix = "\n".join(lines)
    if inventory:
        inventory_text = re.sub(r"`{3,}", "[backticks elided]", "\n".join(inventory))
        empty_suffix = "\n" + "\n".join(_fenced("Spawn packet", ""))
        room = cap - len(prefix) - len(empty_suffix)
        suffix = empty_suffix if room < 0 else "\n" + "\n".join(_fenced("Spawn packet", inventory_text[:room]))
        return prefix[:cap] if room < 0 else prefix + suffix
    return prefix[:cap]

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
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(backup)
        except OSError:
            pass
        print(f"[planner_runs] ledger corrupt; backed up to {backup}", file=sys.stderr)
        return []


def _save_records(records):
    """mkstemp in the same directory + os.replace: a reader (another process's _load_records) never observes a
    partially-written file, only the old complete one or the new complete one."""
    path = _runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".planner_runs.json.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(records, indent=2) + "\n")
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


def _claim(goal_id, kind, payload_key, attempts):
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
    r.update(status="claimed", pid=None, started_at=time.time())
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
        r.update(pid=launched["pid"], pid_start=launched["pid_start"], started_at=time.time(),
                 account=acct_id, log=launched["log"], status="running", jev=jev_result, agreement=None)
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


def run(goal_id, kind, payload_key):
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
        _claim(goal_id, kind, payload_key, attempts)

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
        budget = pool.cfg.get("limits", {}).get("max_budget_usd", {}).get("planner_decision", 3)
        log = STATE / "runs" / f"planner-decision-{goal_id}-{kind}-{attempts + 1}.log"

        # Only spent once every guard above has passed and launch is about to happen for real (T-0232 review
        # item 1): a decision point skipped for session-attached/no-headroom/unsafe-key never spends a Jev
        # request, so skip records above never carry a jev field.
        jev_result = _safe_jev_triage(goal_id, kind, payload_key, attempts)

        launched = goals.launch_planner(ROOT, prompt, acct.id, budget, log)
    except (KeyboardInterrupt, SystemExit) as e:
        _record_failed_launch(goal_id, kind, payload_key, attempts, e)
        raise
    except Exception as e:
        status, new_attempts = _record_failed_launch(goal_id, kind, payload_key, attempts, e)
        if status == "gave_up":
            from . import daemon  # deferred: daemon imports this module at load time
            daemon.notify(f"{goal_id}: planner decision {kind} ({payload_key}) gave up after {new_attempts} "
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
                tokens=tokens, **{**usage, **normalized})
    r["tokens"] = tokens
    r["usd"] = usd
    r["usage_logged"] = True


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
        from . import daemon  # deferred: daemon imports this module at load time
        for r in gave_up:
            daemon.notify(f"{r['goal_id']}: planner decision {r['kind']} ({r['payload_key']}) "
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
