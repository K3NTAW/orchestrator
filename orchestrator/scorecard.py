"""Per-executor outcome rollup: runs/*.jsonl + tasks/T-*.json, keyed by executor id. Feeds pick_executor's
scores() so routing reacts to live merge/fail/usage-limit history instead of static weights alone."""
import json
from datetime import date, datetime
from . import STATE, bench

BANDS = ("1-3", "4-6", "7-10")

_last_malformed_lines = 0


def _read_jsonl_entries(root):
    """Parse every line of root/runs/*.jsonl, skipping and counting lines that fail json.loads instead of
    raising -- a partial write or crash mid-flush shouldn't take the whole scorecard down. Sets
    _last_malformed_lines as a side channel for the CLI footer: build()/by_task() can't change their return
    shape to carry the count without breaking every caller that treats their result as pure executor/task
    rows (card.items(), card[eid][...]). Yields (path, entry) so callers needing the source file (build's
    is_today check) don't have to re-glob."""
    global _last_malformed_lines
    runs_dir = root / "runs"
    entries = []
    malformed = 0
    for p in sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []:
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entries.append((p, json.loads(line)))
            except json.JSONDecodeError:
                malformed += 1
    _last_malformed_lines = malformed
    return entries


def malformed_run_lines():
    """Count of malformed runs/*.jsonl lines skipped by the most recent build()/by_task() call."""
    return _last_malformed_lines


def malformed_footer():
    """Trailing scorecard line reporting how many runs/*.jsonl lines were skipped as malformed on the most
    recent build()/by_task() call. "" when none, so a caller can print it unconditionally without adding a
    stray blank line -- keeps default output (no malformed lines) byte-identical."""
    n = malformed_run_lines()
    return f"malformed run lines skipped: {n}" if n else ""


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

    for p, e in _read_jsonl_entries(root):
        is_today = p.stem == today
        if by != "tier" and e.get("role") != "execute":
            continue  # scout/review/challenge runs don't have a per-executor identity to score
        eid = key_of(e)
        if not eid:
            continue
        r = row(eid)
        r["wall_s"] += e.get("duration_s") or 0
        r["usd"] += e.get("usd") or 0
        r["tokens"]["in"] += int(e.get("input_tokens") or 0)
        r["tokens"]["out"] += int(e.get("output_tokens") or 0)
        r["tokens"]["cache_read"] += int(e.get("cache_read_input_tokens") or 0)
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
ALL_BUCKETS = ROLE_BUCKETS + ("other",)


def _tokens_of(e):
    """int cast on each field: a run logged with a float token count (e.g. cache_read_input_tokens=20.0)
    would otherwise make // 10 return a float and poison every downstream sum with a trailing ".0"."""
    inp = int(e.get("input_tokens") or 0)
    out = int(e.get("output_tokens") or 0)
    cache_read = int(e.get("cache_read_input_tokens") or 0)
    return inp + out + cache_read // 10


def by_task(root=STATE):
    """usd/tokens/wall_s per task id, summed across every runs/*.jsonl line naming that task (task, role, tier,
    duration_s, usd, input_tokens/output_tokens/cache_read_input_tokens -- the fields bus.log_run writes).
    tokens uses the same input+output+cache_read//10 formula pool.record and Pool.tally_planner use elsewhere.
    runs_no_usd counts lines with no usd field at all (Codex runs log none) so callers rolling usd into a
    percent split can also show what that split leaves uncounted."""
    totals = {}
    for p, e in _read_jsonl_entries(root):
        tid = e.get("task")
        if not tid:
            continue
        t = totals.setdefault(tid, {"usd": 0.0, "tokens": 0, "wall_s": 0.0, "role": None, "tier": None,
                                     "runs_no_usd": 0})
        t["usd"] += e.get("usd") or 0
        t["tokens"] += _tokens_of(e)
        t["wall_s"] += e.get("duration_s") or 0
        t["role"] = t["role"] or e.get("role")
        t["tier"] = t["tier"] or e.get("tier")
        if "usd" not in e:
            t["runs_no_usd"] += 1
    return totals


def _planner_runs_for_goal(root, goal_id):
    try:
        runs = json.loads((root / "runs" / "planner_runs.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [r for r in runs if r.get("goal_id") == goal_id]


def _planner_usage_totals(root):
    """(day_tokens summed, n_accounts) from planner_usage.json -- the field pool._save_planner_account actually
    writes per account ({"window_tokens", "day_tokens", "offsets"}), not "planner_day_tokens" (that name only
    exists on the in-memory Account object). This is a pool-wide daily transcript tally that resets at midnight
    and isn't tied to any one goal. None when the file is missing/unreadable so callers can print a dash instead
    of a misleading 0."""
    try:
        data = json.loads((root / "planner_usage.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return sum(v.get("day_tokens", 0) for v in data.values()), len(data)


def planner_footer(root=STATE):
    """One footer line for the --by goal table: today's pool-wide planner transcript tokens, or "planner: -"
    when planner_usage.json is missing. Never per-goal -- see by_goal's docstring for why."""
    totals = _planner_usage_totals(root)
    if totals is None:
        return "planner: -"
    tokens, n_accounts = totals
    return f"planner (transcripts, today): {tokens} tokens across {n_accounts} accounts"


def by_goal(root=STATE):
    """Roll task-level run costs up to the goal (parent task id) that owns them, split by role bucket plus an
    "other" bucket for any role outside the four (challenge, triage, ...) so total_usd covers every child task
    with a run, and that goal's own planner decision-run count/usd from runs/planner_runs.json (D3/T-0122; may
    not exist yet). Planner *transcript* tokens (planner_usage.json) are pool-wide and reset daily -- they have
    no meaningful per-goal share, so they never land in a goal row; see planner_footer() for those."""
    task_totals = by_task(root)
    tasks_dir = root / "tasks"
    tasks = [json.loads(p.read_text()) for p in sorted(tasks_dir.glob("T-*.json"))] if tasks_dir.exists() else []
    goal_ids = sorted({t["parent"] for t in tasks if t.get("parent")})
    runs_path = root / "runs" / "planner_runs.json"

    card = {}
    for gid in goal_ids:
        roles = {b: {"usd": 0.0, "tokens": 0} for b in ALL_BUCKETS}
        runs_no_usd = 0
        for t in tasks:
            if t.get("parent") != gid:
                continue
            totals = task_totals.get(t["id"])
            if not totals:
                continue
            bucket = t.get("role") if t.get("role") in ROLE_BUCKETS else "other"
            roles[bucket]["usd"] += totals["usd"]
            roles[bucket]["tokens"] += totals["tokens"]
            runs_no_usd += totals.get("runs_no_usd", 0)
        total_usd = sum(r["usd"] for r in roles.values())
        total_tokens = sum(r["tokens"] for r in roles.values())
        if runs_path.exists():
            runs = _planner_runs_for_goal(root, gid)
            usd_vals = [r["usd"] for r in runs if "usd" in r]
            planner = {"n_runs": len(runs), "usd": sum(usd_vals) if usd_vals else None}
        else:
            planner = None
        card[gid] = {"roles": roles, "total_usd": total_usd, "total_tokens": total_tokens, "planner": planner,
                      "runs_no_usd": runs_no_usd}
    return card


def goal_percentages(entry):
    """Each bucket (execute/review/spec_review/scout/other) as percent of the goal's total usd. Runs with no
    usd field (Codex logs none) contribute 0 to both sides of the ratio, so this is already a split over
    usd-carrying runs alone -- entry["runs_no_usd"] tells the reader how many runs that leaves out."""
    total_usd = entry["total_usd"]
    return {b: (entry["roles"][b]["usd"] / total_usd * 100 if total_usd else 0.0) for b in ALL_BUCKETS}


def format_planner_runs_cell(entry):
    """Per-goal planner column: that goal's own decision runs from runs/planner_runs.json -- a count, plus their
    usd sum when the records carry one. "-" when the file is missing entirely, "0 runs" when it exists but has
    none for this goal -- that distinction matters (missing means we never wrote it; zero means genuinely none)."""
    planner = entry.get("planner")
    if planner is None:
        return "-"
    n = planner["n_runs"]
    if n == 0:
        return "0 runs"
    usd = planner.get("usd")
    return f"{n} runs (${round(usd, 2)})" if usd is not None else f"{n} runs"
