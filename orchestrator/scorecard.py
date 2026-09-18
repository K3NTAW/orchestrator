"""Per-executor outcome rollup: runs/*.jsonl + tasks/T-*.json, keyed by executor id. Feeds pick_executor's
scores() so routing reacts to live merge/fail/usage-limit history instead of static weights alone."""
import json
from datetime import date, datetime
from . import STATE, bench

BANDS = ("1-3", "4-6", "7-10")


def _band(complexity):
    if complexity is None:
        return None
    if complexity <= 3:
        return "1-3"
    if complexity <= 6:
        return "4-6"
    return "7-10"


def _row():
    return {"merged": 0, "failed": 0, "held_usage_limit": 0, "usage_limit_today": 0,
            "rounds_total": 0, "rounds_n": 0, "wall_s": 0.0,
            "tokens": {"in": 0, "out": 0, "cache_read": 0}, "usd": 0.0,
            "review_request_changes": 0, "by_complexity": {b: {"merged": 0, "failed": 0} for b in BANDS}}


def build(root=STATE, by="executor"):
    """root: base dir with tasks/ and runs/ subdirs. Defaults to the live STATE so callers (merge, executor)
    read current state; tests pass a scratch root so synthesized fixtures don't mix with other tests' tasks.
    by="tier" regroups the same rows under each record's tier (e.g. "sonnet") instead of its executor id --
    a coarser view for `orchestrator scorecard --by tier`; pick_executor callers always use the default."""
    card = {}
    today = date.today().isoformat()

    def row(eid):
        return card.setdefault(eid, _row())

    def key_of(entry):
        if by == "tier":
            return entry.get("tier") or entry.get("executor") or "?"
        return entry.get("executor") or (f"claude:{entry['tier']}" if entry.get("tier") else None)

    runs_dir = root / "runs"
    for p in sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []:
        is_today = p.stem == today
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if by != "tier" and e.get("role") != "execute":
                continue  # scout/review/challenge runs don't have a per-executor identity to score
            eid = key_of(e)
            if not eid:
                continue
            r = row(eid)
            r["wall_s"] += e.get("duration_s") or 0
            r["usd"] += e.get("usd") or 0
            r["tokens"]["in"] += e.get("input_tokens") or 0
            r["tokens"]["out"] += e.get("output_tokens") or 0
            r["tokens"]["cache_read"] += e.get("cache_read_input_tokens") or 0
            if e.get("outcome") in ("usage_limit", "rate_limit"):
                r["held_usage_limit"] += 1
                if is_today:
                    r["usage_limit_today"] += 1

    tasks_dir = root / "tasks"
    for p in sorted(tasks_dir.glob("T-*.json")) if tasks_dir.exists() else []:
        t = json.loads(p.read_text())
        if t.get("role") != "execute":
            continue  # review/scout/challenge verdicts already landed on the reviewed execute task
        eid = key_of(t)
        if not eid:
            continue
        r = row(eid)
        band = _band(t.get("complexity"))
        if t.get("merged_into"):
            r["merged"] += 1
            if band:
                r["by_complexity"][band]["merged"] += 1
        if t.get("status") == "failed":
            r["failed"] += 1
            if band:
                r["by_complexity"][band]["failed"] += 1
        if t.get("status") == "done":
            r["rounds_total"] += t.get("rounds", 0)
            r["rounds_n"] += 1
        if t.get("review_verdict") == "request_changes":
            r["review_request_changes"] += 1

    for r in card.values():
        n = r.pop("rounds_n")
        r["rounds_avg"] = round(r.pop("rounds_total") / n, 2) if n else 0.0
    return card


def prior_weights(executors):
    """Cold-start bias for pick_executor from bench.json (B4a): an executor's model coding score if it has
    one, else its intelligence score, scaled to 0.5-1.5 relative to the best-scoring executor in the pool.
    No usable number for a model -> 1.0. bench.json missing/unreadable -> 1.0 for every executor, never raise
    (B4a may not have run yet, or the fetch broke)."""
    try:
        models = bench.load().get("models") or {}
    except Exception:
        models = {}
    values = {}
    for eid, ex in executors.items():
        rec = models.get(ex.model)
        if not rec:
            continue
        v = rec.get("coding")
        if v is None:
            v = rec.get("intelligence")
        if v is not None:
            values[eid] = v
    if not values:
        return {eid: 1.0 for eid in executors}
    max_v = max(values.values())
    return {eid: (0.5 + values[eid] / max_v if eid in values and max_v else 1.0) for eid in executors}


def scores(card, min_runs=5):
    """success = merged/(merged+failed) over resolved tasks; below min_runs samples an executor is cold, so
    fall back to its bench.json-derived prior_weights entry (1.0 if bench.json has nothing on it) instead of
    a flat 1.0 -- warm executors keep their live score regardless of prior. A hot quota group (>=3 usage-limit
    hits today) is halved either way."""
    try:
        from .pool import Pool
        executors = Pool().executors
    except Exception:
        executors = {}
    priors = prior_weights(executors)
    out = {}
    for eid in set(executors) | set(card):
        r = card.get(eid, {})
        total = r.get("merged", 0) + r.get("failed", 0)
        score = 0.5 + r["merged"] / total if total >= min_runs and total > 0 else priors.get(eid, 1.0)
        if r.get("usage_limit_today", 0) >= 3:
            score *= 0.5
        out[eid] = score
    return out


def write(card):
    STATE.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().astimezone().isoformat(), "executors": card}
    (STATE / "scorecard.json").write_text(json.dumps(payload, indent=2))
    return payload


ROLE_BUCKETS = ("execute", "review", "spec_review", "scout")


def _tokens_of(e):
    return (e.get("input_tokens") or 0) + (e.get("output_tokens") or 0) + (e.get("cache_read_input_tokens") or 0) // 10


def by_task(root=STATE):
    """usd/tokens/wall_s per task id, summed across every runs/*.jsonl line naming that task (task, role, tier,
    duration_s, usd, input_tokens/output_tokens/cache_read_input_tokens -- the fields bus.log_run writes).
    tokens uses the same input+output+cache_read//10 formula pool.record and Pool.tally_planner use elsewhere."""
    totals = {}
    runs_dir = root / "runs"
    for p in sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []:
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            tid = e.get("task")
            if not tid:
                continue
            t = totals.setdefault(tid, {"usd": 0.0, "tokens": 0, "wall_s": 0.0, "role": None, "tier": None})
            t["usd"] += e.get("usd") or 0
            t["tokens"] += _tokens_of(e)
            t["wall_s"] += e.get("duration_s") or 0
            t["role"] = t["role"] or e.get("role")
            t["tier"] = t["tier"] or e.get("tier")
    return totals


def _planner_runs_for_goal(root, goal_id):
    try:
        runs = json.loads((root / "runs" / "planner_runs.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [r for r in runs if r.get("goal_id") == goal_id]


def _planner_usage_tokens(root):
    """Sum of planner_day_tokens across every account in planner_usage.json (C-O7a). None when the file is
    missing or unreadable so callers can print "-" instead of a misleading 0."""
    try:
        data = json.loads((root / "planner_usage.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return sum(v.get("planner_day_tokens", 0) for v in data.values())


def by_goal(root=STATE):
    """Roll task-level run costs up to the goal (parent task id) that owns them, split by role bucket, plus
    the goal's own planner cost: a per-goal run count from runs/planner_runs.json (D3/T-0122; may not exist yet)
    and, when planner_usage.json exists, the pool-wide planner_day_tokens total as a tokens-only line (transcripts
    carry no per-task cost, so usd stays None/"-"). planner is None only when neither source has any data."""
    task_totals = by_task(root)
    tasks_dir = root / "tasks"
    tasks = [json.loads(p.read_text()) for p in sorted(tasks_dir.glob("T-*.json"))] if tasks_dir.exists() else []
    goal_ids = sorted({t["parent"] for t in tasks if t.get("parent")})
    planner_tokens = _planner_usage_tokens(root)

    card = {}
    for gid in goal_ids:
        roles = {b: {"usd": 0.0, "tokens": 0} for b in ROLE_BUCKETS}
        for t in tasks:
            if t.get("parent") != gid:
                continue
            bucket = t.get("role")
            totals = task_totals.get(t["id"])
            if not totals or bucket not in ROLE_BUCKETS:
                continue
            roles[bucket]["usd"] += totals["usd"]
            roles[bucket]["tokens"] += totals["tokens"]
        total_usd = sum(r["usd"] for r in roles.values())
        total_tokens = sum(r["tokens"] for r in roles.values())
        planner_runs = _planner_runs_for_goal(root, gid)
        planner = None
        if planner_runs or planner_tokens is not None:
            planner = {"n_runs": len(planner_runs), "tokens": planner_tokens, "usd": None}
        card[gid] = {"roles": roles, "total_usd": total_usd, "total_tokens": total_tokens, "planner": planner}
    return card


def goal_percentages(entry):
    """execute/review/spec_review/scout as percent of the goal's total usd; planner (if present) as percent of
    tokens instead, since its usd is always unknown -- callers must mark that figure with the asterisk footnote."""
    total_usd = entry["total_usd"]
    pct = {b: (entry["roles"][b]["usd"] / total_usd * 100 if total_usd else 0.0) for b in ROLE_BUCKETS}
    planner = entry.get("planner")
    planner_pct = None
    if planner and planner.get("tokens") is not None:
        denom = entry["total_tokens"] + planner["tokens"]
        planner_pct = planner["tokens"] / denom * 100 if denom else 0.0
    pct["planner"] = planner_pct
    return pct


def format_planner_cell(entry):
    """(planner_pct_str, planner_tokens_str) for the --by goal table: "-", "-" when there is no planner data
    at all (both runs and usage sources absent), otherwise a tokens-based percent marked with the asterisk
    that flags the footnote, since planner usd is always unknown."""
    planner = entry.get("planner")
    if planner is None:
        return "-", "-"
    pct = goal_percentages(entry)["planner"]
    pct_str = f"{round(pct, 1)}%*" if pct is not None else "-"
    tok_str = planner["tokens"] if planner["tokens"] is not None else "-"
    return pct_str, tok_str
