"""Auto-handover: refreshes a resumable snapshot at the end of plan.md so a fresh Planner session (or the daemon,
while both Claude accounts and Codex are cooling) can pick up open goals without depending on the last manual save.
write() replaces the trailing "## Auto-handover" section in place -- idempotent, never duplicated -- and leaves
everything above it byte-identical."""
import json, os, re, tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ROOT, STATE, bus

TZ = ZoneInfo("Europe/Zurich")
MAX_SECTION_LINES = 120
MAX_ITEMS = 8        # per status-group list before "... and N more"
MAX_GOALS = 12
MAX_WORKTREES = 15
RESUME_SENTENCE = ("Resume: skill resume; re-spawn held spec reviews; dispatch ready execute tasks by hand "
                    "while Codex cools.")

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


def _goal_lines(goal, children):
    groups = _status_groups(children)
    lines = [f"### {goal['id']} {goal['title']}"]
    if groups["queued"]:
        refs = [_task_ref(t, f"depends_on={t.get('depends_on') or []}") for t in groups["queued"]]
        lines.append(f"- queued: {_join_truncated(refs)}")
    if groups["running"]:
        refs = [_task_ref(t, f"executor={t.get('executor') or '?'}, started={_fmt_ts(t.get('claimed_at'))}")
                for t in groups["running"]]
        lines.append(f"- running: {_join_truncated(refs)}")
    if groups["held"]:
        refs = [_task_ref(t, f"hold_reason={t.get('hold_reason') or '?'}, "
                              f"resume_hint_keys={sorted((t.get('resume_hint') or {}).keys())}")
                for t in groups["held"]]
        lines.append(f"- held: {_join_truncated(refs)}")
    if groups["failed"]:
        refs = [_task_ref(t, f"reason={t.get('reason') or '?'}, "
                              f"resume_hint_keys={sorted((t.get('resume_hint') or {}).keys())}")
                for t in groups["failed"]]
        lines.append(f"- failed: {_join_truncated(refs)}")
    if groups["done"]:
        refs = [_task_ref(t) for t in groups["done"]]
        lines.append(f"- done (not merged): {_join_truncated(refs)}")
    if groups["merged"]:
        refs = [_task_ref(t, f"sha={(t.get('sha') or '?')[:8]}") for t in groups["merged"]]
        lines.append(f"- merged: {_join_truncated(refs)}")
    for status, ts in groups["other"].items():
        refs = [_task_ref(t) for t in ts]
        lines.append(f"- other ({status}): {_join_truncated(refs)}")
    if len(lines) == 1:
        lines.append("- no child tasks")
    return lines


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


def _render_section(reason):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    header = [f"## Auto-handover {ts} — {reason}", ""]
    footer = ["", RESUME_SENTENCE]

    # One bus.read() for the whole section: goals, their children and the worktree/task cross-check below all
    # come out of this single snapshot instead of one bus.read per goal.
    all_tasks = bus.read()
    tasks_by_id = {t["id"]: t for t in all_tasks}
    children_by_parent = {}
    for t in all_tasks:
        parent = t.get("parent")
        if parent:
            children_by_parent.setdefault(parent, []).append(t)

    body = []
    goals = _open_goals(all_tasks)
    if not goals:
        body.append("Open goals: none")
    else:
        shown, extra = goals[:MAX_GOALS], len(goals) - min(len(goals), MAX_GOALS)
        for goal in shown:
            body.extend(_goal_lines(goal, children_by_parent.get(goal["id"], [])))
        if extra:
            body.append(f"… and {extra} more open goals")
    body.append("")

    wts = _worktree_names(tasks_by_id)
    body.append(f"Worktrees: {_join_truncated([f'wt/{n}' for n in wts], MAX_WORKTREES)}")
    body.append("")

    body.append("Last events:")
    events = _last_events(5)
    if not events:
        body.append("- none")
    else:
        for e in events:
            when = _fmt_ts(e["ts"])
            body.append(f"- {when} {e['task']} {e['kind']} {json.dumps(e['data'])[:80]}")

    # The resume sentence must always survive: truncate the body only, never the header/footer, so a goal-heavy
    # snapshot loses list detail before it ever risks dropping the one line every resume depends on.
    budget = MAX_SECTION_LINES - len(header) - len(footer)
    if len(body) > budget:
        extra = len(body) - (budget - 1)
        body = body[:budget - 1] + [f"… and {extra} more lines truncated"]
    return "\n".join(header + body + footer)


def write(reason: str = "manual"):
    """Replace (or append when absent) the trailing "## Auto-handover" section of plan.md. Idempotent: a second
    call with the same repo state replaces the section in place and leaves everything above it byte-identical.
    Matches the LAST "## Auto-handover " heading, not the first, so Planner prose that quotes the heading text
    earlier in the file (e.g. inside a fenced code block) is never mistaken for the real section and deleted.
    Writes to a sibling temp file and os.replace()s it over plan.md, so a failure mid-write (disk full, replace
    raising) leaves the existing plan.md untouched instead of a half-written file."""
    plan = STATE / "plan.md"   # looked up at call time, not import time, so tests can swap handover.STATE
    with bus.locked():
        STATE.mkdir(parents=True, exist_ok=True)
        existing = plan.read_text() if plan.exists() else ""
        matches = list(_HEADING_RE.finditer(existing))
        m = matches[-1] if matches else None
        head = existing[:m.start()] if m else existing
        head = head.rstrip("\n")
        section = _render_section(reason)
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
