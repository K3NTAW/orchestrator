"""Auto-handover: refreshes a resumable snapshot at the end of plan.md so a fresh Planner session (or the daemon,
while both Claude accounts and Codex are cooling) can pick up open goals without depending on the last manual save.
write() replaces the trailing "## Auto-handover" section in place -- idempotent, never duplicated -- and leaves
everything above it byte-identical."""
import json, os, re, tempfile, time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ROOT, STATE, bus, jev_rank

TZ = ZoneInfo("Europe/Zurich")
MAX_SECTION_LINES = 120
MAX_ITEMS = 8        # per status-group list before "... and N more"
MAX_GOALS = 12
MAX_WORKTREES = 15
RESUME_SENTENCE = ("Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand "
                    "while Codex cools.")

PRUNE_THRESHOLD = 0.5  # done/failed/last-events entries below this p_relevant are dropped; merged/held never are

MAX_JEV_REQUESTS = 6  # per handover render: caps worst-case added latency at MAX_JEV_REQUESTS x jev timeout_s

HANDOVER_INTERVAL_S = 15 * 60  # matches daemon.HANDOVER_INTERVAL_S; kept in sync by hand, not imported (daemon
                                # imports this module, so the reverse import would be circular)

_HEADING_RE = re.compile(r"(?m)^## Auto-handover ")


def _join_truncated(items, limit=MAX_ITEMS):
    if not items:
        return "none"
    shown = items[:limit]
    text = ", ".join(shown)
    extra = len(items) - len(shown)
    if extra:
        text += f", … and {extra} more"
    return text


def _task_ref(t, extra=None):
    ref = f"{t['id']} {t['title']}"
    if extra:
        ref += f" ({extra})"
    return ref


_KNOWN_STATUSES = ("queued", "running", "held", "failed")


def _status_groups(children):
    """Bucket every child by status, "merged" (merged_into set) first regardless of status. Anything outside
    queued/running/held/done/failed/merged still lands somewhere -- under "other", keyed by its own status
    string -- so a child can never fall through and make the goal look childless."""
    groups = {"queued": [], "running": [], "held": [], "done": [], "merged": [], "failed": [], "other": {}}
    for t in children:
        if t.get("merged_into"):
            groups["merged"].append(t)
        elif t["status"] == "done":
            groups["done"].append(t)
        elif t["status"] in _KNOWN_STATUSES:
            groups[t["status"]].append(t)
        else:
            groups["other"].setdefault(t["status"], []).append(t)
    return groups


def _goal_text(goal):
    return f"{goal['title']}\n{goal.get('spec') or ''}"


def _take_jev_budget(budget):
    """budget is a single-key dict shared across one _render_section call. Returns False once MAX_JEV_REQUESTS
    Jev requests have already been spent this handover, so the caller skips ranking (fail-open: keeps its
    tasks/events unpruned) instead of placing another request -- the cap on latency matters more than a
    thorough prune."""
    if budget["n"] <= 0:
        return False
    budget["n"] -= 1
    return True


def _prune_many(named_lists, goal_text, budget):
    """Score every task across all of `named_lists` (e.g. {"failed": [...], "done": [...]}) against goal_text in
    a single jev_rank call -- one Jev request covers a whole goal's prunable groups instead of one request per
    group, so more goals fit inside MAX_JEV_REQUESTS. Returns (named_lists_with_low-scoring items dropped,
    pruned_count). Fail-open: no budget left, no tasks, or jev_rank can't score, leaves every list untouched."""
    tasks = [t for lst in named_lists.values() for t in lst]
    if not tasks:
        return named_lists, 0
    if not _take_jev_budget(budget):
        return named_lists, 0
    items = [{"id": t["id"], "text": _task_ref(t)} for t in tasks]
    ranked = jev_rank.rank(items, goal_text, threshold=PRUNE_THRESHOLD)
    if len(ranked) == len(items) and all(it["p_relevant"] is None for it in ranked):
        return named_lists, 0
    keep_ids = {it["id"] for it in ranked}
    kept_lists = {name: [t for t in lst if t["id"] in keep_ids] for name, lst in named_lists.items()}
    pruned = len(tasks) - sum(len(v) for v in kept_lists.values())
    return kept_lists, pruned


def _goal_lines(goal, children, budget):
    groups = _status_groups(children)
    goal_text = _goal_text(goal)
    lines = [f"### {goal['id']} {goal['title']}"]
    if groups["queued"]:
        refs = [_task_ref(t, f"depends_on={t.get('depends_on') or []}") for t in groups["queued"]]
        lines.append(f"- queued: {_join_truncated(refs)}")
    if groups["running"]:
        refs = [_task_ref(t, f"executor={t.get('executor') or '?'}, started={_fmt_ts(t.get('claimed_at'))}")
                for t in groups["running"]]
        lines.append(f"- running: {_join_truncated(refs)}")
    if groups["held"]:  # never pruned
        refs = [_task_ref(t, f"hold_reason={t.get('hold_reason') or '?'}, "
                              f"resume_hint_keys={sorted((t.get('resume_hint') or {}).keys())}")
                for t in groups["held"]]
        lines.append(f"- held: {_join_truncated(refs)}")
    pruned_groups, pruned = _prune_many({"failed": groups["failed"], "done": groups["done"]}, goal_text, budget)
    if pruned_groups["failed"]:
        refs = [_task_ref(t, f"reason={t.get('reason') or '?'}, "
                              f"resume_hint_keys={sorted((t.get('resume_hint') or {}).keys())}")
                for t in pruned_groups["failed"]]
        lines.append(f"- failed: {_join_truncated(refs)}")
    if pruned_groups["done"]:
        refs = [_task_ref(t) for t in pruned_groups["done"]]
        lines.append(f"- done (not merged): {_join_truncated(refs)}")
    if groups["merged"]:  # never pruned
        refs = [_task_ref(t, f"sha={(t.get('sha') or '?')[:8]}") for t in groups["merged"]]
        lines.append(f"- merged: {_join_truncated(refs)}")
    for status, ts in groups["other"].items():
        refs = [_task_ref(t) for t in ts]
        lines.append(f"- other ({status}): {_join_truncated(refs)}")
    if len(lines) == 1:
        lines.append("- no child tasks")
    return lines, pruned


def _fmt_ts(ts):
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


def _open_goals(all_tasks):
    return [t for t in all_tasks if t["role"] == "triage" and not t.get("parent") and t["status"] != "done"]


def _worktree_names(tasks_by_id):
    """Worktree directory names under wt/, excluding the state worktree (wt/_state, used by bus.commit_state, not
    a task) and any task whose work already merged (merged_into set) -- a merged task's worktree is stale, not
    something a resuming Planner needs to see."""
    wt = ROOT / "wt"
    if not wt.exists():
        return []
    names = []
    for p in sorted(wt.iterdir()):
        if not p.is_dir() or p.name == "_state":
            continue
        t = tasks_by_id.get(p.name)
        if t is not None and t.get("merged_into"):
            continue
        names.append(p.name)
    return names


def _last_events(limit=5):
    rows = bus.db().execute(
        "select seq,task_id,ts,kind,data from events order by seq desc limit ?", (limit,)
    ).fetchall()
    rows.reverse()
    return [{"seq": s, "task": t, "ts": ts, "kind": k, "data": json.loads(d)} for s, t, ts, k, d in rows]


def _prune_events(events, goal_text, budget):
    if not events or not goal_text:
        return events, 0
    if not _take_jev_budget(budget):
        return events, 0
    items = [{"id": str(e["seq"]), "text": f"{e['task']} {e['kind']} {json.dumps(e['data'])[:80]}"} for e in events]
    ranked = jev_rank.rank(items, goal_text, threshold=PRUNE_THRESHOLD)
    if len(ranked) == len(items) and all(it["p_relevant"] is None for it in ranked):
        return events, 0
    keep_ids = {it["id"] for it in ranked}
    kept = [e for e in events if str(e["seq"]) in keep_ids]
    return kept, len(events) - len(kept)


def _render_section(reason, all_tasks, events5):
    """Render the section from an already-fetched bus snapshot (`all_tasks`) and last-events tail (`events5`) --
    no bus/db reads happen in here, only jev_rank calls (network) and pure formatting, so write() can call this
    after releasing the bus lock. `budget` caps the number of Jev requests placed at MAX_JEV_REQUESTS for the
    whole call, split across goals and the events tail."""
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    footer = ["", RESUME_SENTENCE]
    budget = {"n": MAX_JEV_REQUESTS}

    tasks_by_id = {t["id"]: t for t in all_tasks}
    children_by_parent = {}
    for t in all_tasks:
        parent = t.get("parent")
        if parent:
            children_by_parent.setdefault(parent, []).append(t)

    body = []
    total_pruned = 0
    goals = _open_goals(all_tasks)
    if not goals:
        body.append("Open goals: none")
    else:
        shown, extra = goals[:MAX_GOALS], len(goals) - min(len(goals), MAX_GOALS)
        for goal in shown:
            lines, pruned = _goal_lines(goal, children_by_parent.get(goal["id"], []), budget)
            body.extend(lines)
            total_pruned += pruned
        if extra:
            body.append(f"… and {extra} more open goals")
    body.append("")

    wts = _worktree_names(tasks_by_id)
    body.append(f"Worktrees: {_join_truncated([f'wt/{n}' for n in wts], MAX_WORKTREES)}")
    body.append("")

    body.append("Last events:")
    # "The open goal": the primary open goal drives what's relevant for pruning the shared events tail. With no
    # open goal there's nothing to score events against, so they pass through unpruned.
    events, events_pruned = _prune_events(events5, _goal_text(goals[0]) if goals else None, budget)
    total_pruned += events_pruned
    if not events:
        body.append("- none")
    else:
        for e in events:
            when = _fmt_ts(e["ts"])
            body.append(f"- {when} {e['task']} {e['kind']} {json.dumps(e['data'])[:80]}")

    note = f" — pruned {total_pruned} by jev" if total_pruned else ""
    header = [f"## Auto-handover {ts} — {reason}{note}", ""]

    # The resume sentence must always survive: truncate the body only, never the header/footer, so a goal-heavy
    # snapshot loses list detail before it ever risks dropping the one line every resume depends on.
    line_budget = MAX_SECTION_LINES - len(header) - len(footer)
    if len(body) > line_budget:
        extra = len(body) - (line_budget - 1)
        body = body[:line_budget - 1] + [f"… and {extra} more lines truncated"]
    return "\n".join(header + body + footer)


def write(reason: str = "manual"):
    """Replace (or append when absent) the trailing "## Auto-handover" section of plan.md. Idempotent: a second
    call with the same repo state replaces the section in place and leaves everything above it byte-identical.
    Matches the LAST "## Auto-handover " heading, not the first, so Planner prose that quotes the heading text
    earlier in the file (e.g. inside a fenced code block) is never mistaken for the real section and deleted.
    Writes to a sibling temp file and os.replace()s it over plan.md, so a failure mid-write (disk full, replace
    raising) leaves the existing plan.md untouched instead of a half-written file. When the freshly rendered
    section is byte-identical to the one already on disk, skips the temp-file/os.replace dance entirely --
    a no-op tick (same reason, same repo state) never dirties plan.md's mtime.

    The bus lock is only held for the two things that actually need it: taking the bus snapshot up front, and
    the plan.md compare-and-swap at the end. _render_section()'s jev_rank calls (network, up to MAX_JEV_REQUESTS
    requests) run in between with no lock held, so a slow or unavailable Jev never blocks other bus writers."""
    plan = STATE / "plan.md"   # looked up at call time, not import time, so tests can swap handover.STATE
    with bus.locked():
        STATE.mkdir(parents=True, exist_ok=True)
        all_tasks = bus.read()
        events5 = _last_events(5)

    section = _render_section(reason, all_tasks, events5)

    with bus.locked():
        existing = plan.read_text() if plan.exists() else ""
        matches = list(_HEADING_RE.finditer(existing))
        m = matches[-1] if matches else None
        head = existing[:m.start()] if m else existing
        old_section = existing[m.start():].rstrip("\n") if m else None
        head = head.rstrip("\n")
        if section == old_section:
            return plan
        body = section if not head else f"{head}\n\n{section}"
        text = body + "\n"

        fd, tmp_name = tempfile.mkstemp(dir=str(plan.parent), prefix=".plan.md.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp_name, plan)
        except Exception:
            os.unlink(tmp_name)
            raise
    return plan


def _handover_last_at():
    try:
        return json.loads((STATE / "handover_state.json").read_text()).get("handover_last_at", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _save_handover_last_at(now):
    state = STATE / "handover_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"handover_last_at": now}, indent=1))


def maybe_write(reason: str = "auto", now=None, interval=HANDOVER_INTERVAL_S):
    """Throttled entry point for periodic callers (e.g. the daemon's tick loop): writes at most once every
    `interval` seconds. The throttle check and timestamp save happen inside one short bus.locked() acquisition
    -- claiming the write slot before write() runs -- so a second caller (another daemon process, or another
    thread here) racing this one sees the fresh timestamp and skips instead of also passing the throttle check.
    write() itself then runs with the lock released, since it places the (network, potentially slow) jev_rank
    requests and takes its own brief locks internally; holding this function's lock across that call would put
    Jev requests back under the bus flock."""
    now = now if now is not None else time.time()
    with bus.locked():
        if now - _handover_last_at() < interval:
            return False
        _save_handover_last_at(now)
    write(reason)
    return True
