"""Per-executor outcome rollup: runs/*.jsonl + tasks/T-*.json, keyed by executor id. Feeds pick_executor's
scores() so routing reacts to live merge/fail/usage-limit history instead of static weights alone."""
import json
import statistics
from datetime import date, datetime
from . import STATE, bench, attribution

BANDS = ("1-3", "4-6", "7-10")
TASK_CLASSES = ("mechanical", "unfamiliar", "debugging", "architectural", "security")

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
                entry = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if "role" not in entry:
                continue  # e.g. jev usage lines -- not a worker run, counted nowhere
            entries.append((p, entry))
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
    return attribution.band(complexity)


def task_class(task):
    return attribution.task_class(task)


def class_success(executor_id, task_class_name, root=STATE, min_samples=None):
    """Return an executor's merge rate for a class, using only tasks with execute run rows."""
    if min_samples is None:
        try:
            from .pool import config
            min_samples = config().get("models", {}).get("min_samples", 3)
        except Exception:
            min_samples = 3

    tasks_dir = root / "tasks"
    tasks = {}
    for p in sorted(tasks_dir.glob("T-*.json")) if tasks_dir.exists() else []:
        try:
            task = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        tasks[task.get("id", p.stem)] = task

    run_task_ids = {entry.get("task") for _, entry in _read_jsonl_entries(root)
                    if entry.get("role") == "execute" and entry.get("task")}
    resolved = [task for tid, task in tasks.items()
                if tid in run_task_ids and task.get("role") == "execute"
                and task.get("executor") == executor_id and task_class(task) == task_class_name
                and (task.get("merged_into") or task.get("status") == "failed")]
    if len(resolved) < min_samples:
        return None
    merged = sum(bool(task.get("merged_into")) for task in resolved)
    return merged / len(resolved)


def _median(values):
    return statistics.median(values) if values else 0


def expected_cost(executor_id, task_class_name, root=STATE, min_samples=None):
    """Expected token cost of routing a class to an executor, from local completed work only.

    Initial execution, a probability-weighted repair, and review/spec-review overhead are deliberately
    measured separately.  A class remains cold until it has the configured number of merged initial tasks.
    """
    if min_samples is None:
        try:
            from .pool import config
            min_samples = config().get("models", {}).get("min_samples", 3)
        except Exception:
            min_samples = 3
    tasks = {}
    tasks_dir = root / "tasks"
    for p in sorted(tasks_dir.glob("T-*.json")) if tasks_dir.exists() else []:
        try:
            task = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        tasks[task.get("id", p.stem)] = task

    def constraints_of(task):
        value = task.get("constraints") or {}
        return value if isinstance(value, dict) else {}

    def class_of(task):
        # A repair inherits the class whose repair probability it is estimating, rather than making every
        # repaired architectural/security task look like a debugging sample.
        original = next((constraints_of(task).get(key) for key in
                         ("fix_round_for", "review_for", "spec_review_for")
                         if constraints_of(task).get(key)), None)
        return task_class(tasks[original]) if original in tasks else task_class(task)

    initial = [t for t in tasks.values() if t.get("role") == "execute" and t.get("executor") == executor_id
               and not constraints_of(t).get("fix_round_for") and class_of(t) == task_class_name]
    merged = [t for t in initial if t.get("merged_into")]
    if len(merged) < min_samples:
        return None

    run_tokens = {}
    for _, run in _read_jsonl_entries(root):
        tid = run.get("task")
        if tid and run.get("role"):
            run_tokens.setdefault(tid, {}).setdefault(run["role"], 0)
            run_tokens[tid][run["role"]] += _tokens_of(run)

    execute_tokens = [run_tokens.get(t.get("id"), {}).get("execute", 0) for t in initial]
    repairs = [t for t in tasks.values() if t.get("role") == "execute" and t.get("executor") == executor_id
               and constraints_of(t).get("fix_round_for") and class_of(t) == task_class_name]
    repair_tokens = [run_tokens.get(t.get("id"), {}).get("execute", 0) for t in repairs]
    resolved = [t for t in initial if t.get("merged_into") or t.get("status") == "failed"]
    repaired_ids = {constraints_of(t).get("fix_round_for") for t in repairs}
    repair_probability = (sum(t.get("status") == "failed" or t.get("id") in repaired_ids for t in resolved)
                          / len(resolved)) if resolved else 0

    overhead = 0
    for role in ("review", "spec_review"):
        values = [roles.get(role, 0) for tid, roles in run_tokens.items()
                  if tid in tasks and tasks[tid].get("role") == role and class_of(tasks[tid]) == task_class_name]
        overhead += _median(values)
    return _median(execute_tokens) + repair_probability * _median(repair_tokens) + overhead


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


def scores(card, min_runs=5, rank_by=None):
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
        score = 0.5 + r.get("merged", 0) / total if total >= min_runs and total > 0 else priors.get(eid, 1.0)
        if rank_by == "cost_to_accepted":
            accepted_n = r.get("cost_to_accepted_defined_count", r.get("accepted_tasks", 0))
            cost = r.get("cost_to_accepted")
            try:
                min_samples = Pool().cfg.get("models", {}).get("min_samples", 3)
            except Exception:
                min_samples = 3
            if accepted_n >= min_samples and cost is not None and cost > 0:
                score = 1.0 / cost
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

JEV_USD_PER_M = 0.042


def _gate_stats(root):
    """Per-task {calls, waste, blocked} raw counts from runs/jev/gate.jsonl (E3 v2, T-0229). None when that
    file doesn't exist at all -- distinct from a task simply having no rows in it (which yields zero counts) --
    so by_task/by_goal can tell "never measured" ('-') apart from "measured, found nothing" (0). calls counts
    only scored rows; waste counts the scored rows that were a bad call (p_needed<0.3 or p_redundant>0.7);
    blocked counts every row the gate actually blocked, scored or not."""
    path = root / "runs" / "jev" / "gate.jsonl"
    if not path.exists():
        return None
    stats = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = e.get("task")
        if not tid:
            continue
        s = stats.setdefault(tid, {"calls": 0, "waste": 0, "blocked": 0})
        if e.get("scored"):
            s["calls"] += 1
            p_needed, p_redundant = e.get("p_needed"), e.get("p_redundant")
            if (p_needed is not None and p_needed < 0.3) or (p_redundant is not None and p_redundant > 0.7):
                s["waste"] += 1
        if e.get("blocked"):
            s["blocked"] += 1
    return stats


def _turns_by_task(root):
    """Per-task sum of the "turns" field run records carry (E7, T-0212). A task absent from the returned dict
    has no run record with a turns field at all -- callers render '-' for that case rather than 0."""
    turns = {}
    for _, e in _read_jsonl_entries(root):
        tid, val = e.get("task"), e.get("turns")
        if tid and val is not None:
            turns[tid] = turns.get(tid, 0) + val
    return turns


def jev_footer(root=STATE):
    """Trailing scorecard line for --by task|goal: today-and-all-time question count and USD cost from
    runs/jev/<date>.jsonl input_tokens (gate.jsonl is decisions, not questions, so it's excluded by name).
    Missing runs/jev/ dir -> "jev: 0 questions, 0.0 USD", never raise."""
    n, tokens = 0, 0
    jev_dir = root / "runs" / "jev"
    for p in sorted(jev_dir.glob("*.jsonl")) if jev_dir.exists() else []:
        if p.name == "gate.jsonl":
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            tokens += int(e.get("input_tokens") or 0)
    usd = tokens / 1_000_000 * JEV_USD_PER_M
    return f"jev: {n} questions, {round(usd, 4)} USD"


def _tokens_of(e):
    """int cast on each field: a run logged with a float token count (e.g. cache_read_input_tokens=20.0)
    would otherwise make // 10 return a float and poison every downstream sum with a trailing ".0"."""
    if "input_uncached_tokens" in e:
        return (int(e.get("input_uncached_tokens") or 0) + int(e.get("output_tokens") or 0) +
                int(e.get("cache_read_tokens") or 0) // 10 + int(e.get("cache_write_tokens") or 0))
    if _legacy_total_only(e):
        return int(e["total_tokens"])
    inp = int(e.get("input_tokens") or 0)
    out = int(e.get("output_tokens") or 0)
    cache_read = int(e.get("cache_read_input_tokens") or 0)
    return inp + out + cache_read // 10


def _legacy_total_only(e):
    """Whether a total_tokens value has no accompanying usage buckets to recompute from."""
    bucket_keys = ("input_uncached_tokens", "cache_read_tokens", "cache_write_tokens",
                   "input_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
                   "cache_creation_input_tokens", "output_tokens", "reasoning_tokens")
    return e.get("total_tokens") is not None and not any(key in e for key in bucket_keys)


def by_task(root=STATE):
    """usd/tokens/wall_s per task id, summed across every runs/*.jsonl line naming that task (task, role, tier,
    duration_s, usd, input_tokens/output_tokens/cache_read_input_tokens -- the fields bus.log_run writes).
    tokens uses the same input+output+cache_read//10 formula pool.record and Pool.tally_planner use elsewhere.
    runs_no_usd counts lines with no usd field at all (Codex runs log none) so callers rolling usd into a
    percent split can also show what that split leaves uncounted. calls/waste_pct/blocked come from the E3 v2
    jev gate log (T-0229) and turns from E7 run records (T-0212) -- see _gate_stats/_turns_by_task for the
    '-' fallback rules when that data doesn't exist."""
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

    gate = _gate_stats(root)
    turns = _turns_by_task(root)
    for tid, t in totals.items():
        if gate is None:
            t["calls"], t["waste_pct"], t["blocked"] = "-", "-", "-"
        else:
            g = gate.get(tid, {"calls": 0, "waste": 0, "blocked": 0})
            t["calls"] = g["calls"]
            t["waste_pct"] = round(g["waste"] / g["calls"] * 100, 1) if g["calls"] else 0.0
            t["blocked"] = g["blocked"]
        t["turns"] = turns.get(tid, "-")
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
    gate = _gate_stats(root)

    # Planner transcript usage is pool-wide.  A planner run with no token field is
    # therefore assigned an approximation: the day's transcript total multiplied
    # by this goal's share of that day's worker-run rows.
    planner_usage = _planner_usage_totals(root)
    dated_runs = {}
    for path, entry in _read_jsonl_entries(root):
        goal_id = entry.get("goal_id")
        if goal_id:
            dated_runs.setdefault(path.stem, []).append(goal_id)

    def tokens(entry):
        if "input_uncached_tokens" in entry:
            return {
                "uncached": int(entry.get("input_uncached_tokens") or 0),
                "cache_read": int(entry.get("cache_read_tokens") or 0),
                "cache_write": int(entry.get("cache_write_tokens") or 0),
                "output": int(entry.get("output_tokens") or 0),
                "reasoning": int(entry.get("reasoning_tokens") or 0),
            }
        return {
            "uncached": int(entry.get("input_tokens") or 0),
            "cache_read": int(entry.get("cache_read_input_tokens") or 0),
            "cache_write": int(entry.get("cache_write_input_tokens") or entry.get("cache_creation_input_tokens") or 0),
            "output": int(entry.get("output_tokens") or 0),
            "reasoning": int(entry.get("reasoning_tokens") or 0),
        }

    def effective(bucket):
        return bucket["uncached"] + bucket["output"] + bucket["cache_read"] // 10 + bucket["cache_write"] + bucket["reasoning"]

    # Jev records deliberately have no role, and live below runs/jev rather than
    # the worker JSONL files read by by_task().
    jev_by_goal = {}
    jev_dir = root / "runs" / "jev"
    for path in sorted(jev_dir.glob("*.jsonl")) if jev_dir.exists() else []:
        if path.name == "gate.jsonl":
            continue
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            goal_id = entry.get("goal_id")
            if not goal_id:
                continue
            amount = int(entry.get("input_tokens") or 0)
            value = jev_by_goal.setdefault(goal_id, {"gate": 0, "rank": 0})
            if str(entry.get("caller") or "").startswith("gate"):
                value["gate"] += amount
            elif str(entry.get("caller") or "").startswith("rank"):
                value["rank"] += amount

    card = {}
    for gid in goal_ids:
        roles = {b: {"usd": 0.0, "tokens": 0} for b in ALL_BUCKETS}
        runs_no_usd = 0
        token_buckets = {"uncached": 0, "cache_read": 0, "cache_write": 0, "output": 0, "reasoning": 0}
        failed_tokens = 0
        task_tokens = []
        legacy_tokens = 0
        calls = waste = blocked = turns = 0
        has_turns = False
        for t in tasks:
            if t.get("parent") != gid:
                continue
            totals = task_totals.get(t["id"])
            if not totals:
                continue
            task_tokens.append(totals["tokens"])
            bucket = t.get("role") if t.get("role") in ROLE_BUCKETS else "other"
            roles[bucket]["usd"] += totals["usd"]
            roles[bucket]["tokens"] += totals["tokens"]
            runs_no_usd += totals.get("runs_no_usd", 0)
            for path, entry in _read_jsonl_entries(root):
                if entry.get("task") != t["id"]:
                    continue
                raw = tokens(entry)
                if _legacy_total_only(entry):
                    # Preserve the recorded total, including legacy total-only rows.
                    legacy_tokens += _tokens_of(entry) - effective(raw)
                for key, value in raw.items():
                    token_buckets[key] += value
                if t.get("status") == "failed" or t.get("merged_via") == "superseded":
                    failed_tokens += _tokens_of(entry) if "input_uncached_tokens" in entry or _legacy_total_only(entry) else effective(raw)
            if gate is not None:
                g = gate.get(t["id"], {"calls": 0, "waste": 0, "blocked": 0})
                calls += g["calls"]; waste += g["waste"]; blocked += g["blocked"]
            if totals.get("turns") != "-":
                turns += totals.get("turns") or 0
                has_turns = True
        total_usd = sum(r["usd"] for r in roles.values())
        worker_tokens = effective(token_buckets) + legacy_tokens
        if runs_path.exists():
            runs = _planner_runs_for_goal(root, gid)
            usd_vals = [r["usd"] for r in runs if "usd" in r]
            planner = {"n_runs": len(runs), "usd": sum(usd_vals) if usd_vals else None}
        else:
            planner = None
        explicit_planner_tokens = sum(int(run.get("tokens") or 0) for run in (runs if runs_path.exists() else []))
        if any("tokens" in run for run in (runs if runs_path.exists() else [])):
            planner_tokens = explicit_planner_tokens
        elif planner_usage is not None:
            today_rows = dated_runs.get(date.today().isoformat(), [])
            own_rows = sum(1 for run_goal_id in today_rows if run_goal_id == gid)
            planner_tokens = round(planner_usage[0] * own_rows / len(today_rows)) if today_rows else 0
        else:
            planner_tokens = 0
        jev = jev_by_goal.get(gid, {"gate": 0, "rank": 0})
        # total_tokens keeps the established discounted cache-read convention.
        total_tokens = worker_tokens + jev["gate"] + jev["rank"] + planner_tokens
        route_counts = {}
        for record in _planner_runs_for_goal(root, gid):
            for launch in record.get("launches", [record]):
                if launch.get("status") in ("skipped", "claimed", "failed_launch"):
                    continue
                route = launch.get("route") or "escalate"
                route_counts[route] = route_counts.get(route, 0) + 1
        card[gid] = {"roles": roles, "total_usd": total_usd, "total_tokens": total_tokens, "planner": planner,
                      "routes": route_counts,
                      "n_tasks": len(task_tokens),
                      "tokens_median_per_task": statistics.median(task_tokens) if task_tokens else None,
                      "tokens_min_per_task": min(task_tokens) if task_tokens else None,
                      "tokens_max_per_task": max(task_tokens) if task_tokens else None,
                      "tokens_by_role": {name: roles[name]["tokens"] for name in ALL_BUCKETS},
                      "tokens_uncached": token_buckets["uncached"], "tokens_cache_read": token_buckets["cache_read"],
                      "tokens_cache_write": token_buckets["cache_write"], "tokens_output": token_buckets["output"],
                      "tokens_reasoning": token_buckets["reasoning"], "jev_tokens": jev["gate"] + jev["rank"],
                      "jev_tokens_gate": jev["gate"], "jev_tokens_rank": jev["rank"], "planner_tokens": planner_tokens,
                      "failed_tokens": failed_tokens,
                      "runs_no_usd": runs_no_usd,
                      "calls": calls if gate is not None else "-",
                      "waste_pct": (round(waste / calls * 100, 1) if calls else 0.0) if gate is not None else "-",
                      "blocked": blocked if gate is not None else "-",
                      "turns": turns if has_turns else "-"}
    return card


def accepted_goals(root=STATE):
    """Goal ids accepted by a PR result or by fully merged execute children."""
    from . import goals
    tasks_dir = root / "tasks"
    tasks = [json.loads(path.read_text()) for path in sorted(tasks_dir.glob("T-*.json"))] if tasks_dir.exists() else []
    task_by_id = {task["id"]: task for task in tasks}
    goal_ids = sorted(task["id"] for task in tasks
                      if task.get("role") in ("triage", "goal") and task.get("parent") is None)
    accepted = []
    for goal_id in goal_ids:
        goal = task_by_id.get(goal_id)
        children = [task for task in tasks if task.get("parent") == goal_id and task.get("role") == "execute"]
        if (goal and (goal.get("pr_url") or isinstance(goal.get("result"), dict) and goal["result"].get("pr_url"))) or goals.task_pr_url(goal) or (goal and goal.get("status") == "done" and all(child.get("merged_into") for child in children)):
            accepted.append(goal_id)
    return accepted


def tokens_per_accepted_goal(root=STATE):
    """Mean accepted-lineage goal tokens; retain historical goal totals without merged roots."""
    goal_ids = accepted_goals(root)
    count = len(goal_ids)
    tasks_dir = root / "tasks"
    tasks = [json.loads(path.read_text()) for path in sorted(tasks_dir.glob("T-*.json"))] if tasks_dir.exists() else []
    has_accepted = {task.get("parent") for task in tasks
                    if task.get("role") == "execute" and task.get("merged_into")}
    if goal_ids and all(gid in has_accepted for gid in goal_ids):
        attributed = efficiency(root)["goals"]
        total = sum(attributed.get(gid, {}).get("tokens", 0) for gid in goal_ids)
    else:
        card = by_goal(root)
        total = sum(card.get(gid, {}).get("total_tokens", 0) for gid in goal_ids)
    return {"tokens": total / count if count else None, "count": count, "goal_ids": goal_ids}


def format_task_tokens_cell(entry):
    """Small samples show their size and range rather than a median."""
    n = entry["n_tasks"]
    if n < 5:
        low, high = entry["tokens_min_per_task"], entry["tokens_max_per_task"]
        return f"n={n} range {low if low is not None else '-'}-{high if high is not None else '-'}"
    return str(entry["tokens_median_per_task"])


def usd_per_accepted_goal(root=STATE):
    """Mean accepted-lineage goal USD; retain historical goal totals without merged roots."""
    goal_ids = accepted_goals(root)
    count = len(goal_ids)
    tasks_dir = root / "tasks"
    tasks = [json.loads(path.read_text()) for path in sorted(tasks_dir.glob("T-*.json"))] if tasks_dir.exists() else []
    has_accepted = {task.get("parent") for task in tasks
                    if task.get("role") == "execute" and task.get("merged_into")}
    if goal_ids and all(gid in has_accepted for gid in goal_ids):
        attributed = efficiency(root)["goals"]
        total = sum(attributed.get(gid, {}).get("usd", 0.0) for gid in goal_ids)
    else:
        card = by_goal(root)
        total = sum(card.get(gid, {}).get("total_usd", 0.0) for gid in goal_ids)
    return {"usd": total / count if count else 0.0, "count": count, "goal_ids": goal_ids}


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


def format_premium_summary(summary):
    headless = summary["headless"]
    lines = [f"headless: {headless['count']} invocations; mean input {headless['mean_input_tokens']}; "
             f"max input {headless['max_input_tokens']}; output {headless['output_tokens']}; "
             f"cache share {headless['cache_read_share']:.1%}; usd {headless['usd']:.2f}; "
             f"gave_up {headless['gave_up']}"]
    for key, label in (("by_kind", "kinds"), ("by_route", "routes"), ("top_reasons", "top reasons")):
        lines.append(label + ": " + (", ".join(f"{k}={v}" for k, v in headless[key].items()) or "-"))
    interactive = summary["interactive"]
    lines.append(f"interactive: {interactive['sessions_count']} sessions")
    for row in interactive["sessions"]:
        lines.append(f"interactive {row['account']} {row['session']}: in={row['input_tokens']} "
                     f"out={row['output_tokens']} cache read={row['cache_read_tokens']}")
    for row in interactive["day_totals"]:
        lines.append(f"interactive day {row['day']} {row['account']}: in={row['input_tokens']} "
                     f"out={row['output_tokens']} cache read={row['cache_read_tokens']}")
    for row in summary["exceptions"]:
        lines.append(f"soft-budget exception {row['goal_id']}: {row['launches']}>{row['limit']} "
                     f"(advisory); reasons: {row['reasons']}")
    return "\n".join(lines)


EFFICIENCY_BUCKETS = {
    "planner": "Planner", "scout": "Scout", "execute": "Execution",
    "fix_round": "Fix rounds", "spec_review": "Spec review", "review": "Code review",
    "challenge": "Challenge", "jev": "Jev", "other": "Other",
}


def _task_lineage(task, tasks):
    """Resolve fix_round_for against this snapshot, counting edges with cycle protection."""
    seen = set()
    index = 0
    while task.get("id") not in seen:
        seen.add(task.get("id"))
        target = (task.get("constraints") or {}).get("fix_round_for")
        if not target or target in seen:
            break
        index += 1
        task = tasks.get(target, {"id": target})
    terminal = dict(task, constraints={})
    return attribution.lineage(terminal)["root"], index


def _attributed(row, tasks):
    """Fill absent P0 attribution from task metadata; preserve persisted attribution."""
    fields = ("bucket", "lineage_root", "round_index", "band", "task_class", "executor", "model")
    if all(key in row for key in fields):
        return row
    task = tasks.get(row.get("task"), {})
    lineage_root, index = _task_lineage(task, tasks)
    executor_id = row.get("executor") or task.get("executor")
    try:
        cfg = attribution.bus.pool_config()
    except (OSError, ValueError):
        cfg = {}
    defaults = {
        "bucket": attribution.bucket_of(row.get("role") or task.get("role"), task or None),
        "lineage_root": lineage_root or row.get("task"), "round_index": index,
        "band": attribution.band(task.get("complexity")),
        "task_class": attribution.task_class(task) if task else None,
        "executor": executor_id,
        "model": task.get("model") or attribution.model_of(executor_id, row.get("tier") or task.get("tier"), cfg),
    }
    return {**defaults, **row}


def _efficiency_rows(root, tasks):
    """Read worker and Jev usage once, excluding Jev's non-usage gate decision ledger."""
    malformed_lines = 0
    paths = sorted((root / "runs").glob("*.jsonl"))
    paths += [p for p in sorted((root / "runs" / "jev").glob("*.jsonl")) if p.name != "gate.jsonl"]
    rows = []
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if not isinstance(row, dict):
                malformed_lines += 1
                continue
            if path.parent.name == "jev":
                row = {**row, "bucket": "jev"}
            row = _attributed(row, tasks)
            task = tasks.get(row.get("task"), {})
            # Review/challenge tasks name their subject in constraints or inputs.
            if row["bucket"] in ("review", "spec_review", "challenge"):
                constraints = task.get("constraints") or {}
                inputs = row.get("inputs") or task.get("inputs") or []
                target = constraints.get("review_for") or constraints.get("spec_review_for") or (inputs[0] if inputs else None)
                if isinstance(target, str) and target in tasks:
                    row = dict(row, lineage_root=_task_lineage(tasks[target], tasks)[0])
            lineage_root = row.get("lineage_root") or row.get("task") or "unknown"
            parent = task.get("parent") or tasks.get(lineage_root, {}).get("parent")
            rows.append(dict(row, lineage_root=lineage_root, goal_id=row.get("goal_id") or parent or "unknown"))
    return rows, malformed_lines


def _stamp(value):
    """Epoch seconds from numeric or ISO stamps; absent/invalid stamps are undefined."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        from datetime import timezone
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _efficiency_summary(rows, tasks):
    """Rates use defined tasks only; median/max use lineage tokens; amplification = Total/Execution.

    Tokens sum _tokens_of (cache reads discounted 10:1); usd and turns sum recorded values;
    calls count usage rows. Distribution counts rows and effective tokens per model per bucket.
    """
    breakdown = dict.fromkeys(EFFICIENCY_BUCKETS.values(), 0)
    models = {}
    for row in rows:
        tokens = _tokens_of(row)
        bucket = row.get("bucket") or "other"
        breakdown[EFFICIENCY_BUCKETS.get(bucket, "Other")] += tokens
        model = models.setdefault(row.get("model") or "unknown", {})
        entry = model.setdefault(bucket, {"rows": 0, "tokens": 0})
        entry["rows"] += 1
        entry["tokens"] += tokens
    breakdown["Total"] = sum(breakdown.values())
    amplification = breakdown["Total"] / breakdown["Execution"] if breakdown["Execution"] else None
    breakdown["Amplification"] = amplification
    first = [t["first_pass"] for t in tasks if t["first_pass"] is not None]
    fixes = [t["fix_rounds"] for t in tasks if t["fix_rounds"] is not None]
    tokens = [t["tokens"] for t in tasks]
    return {"tokens": breakdown["Total"], "usd": sum(r.get("usd") or 0 for r in rows),
            "calls": len(rows), "turns": sum(r.get("turns") or 0 for r in rows),
            "accepted_tasks": len(tasks), "first_pass_rate": sum(first) / len(first) if first else None,
            "first_pass_defined_count": len(first),
            "fix_round_rate": sum(n >= 1 for n in fixes) / len(fixes) if fixes else None,
            "fix_round_defined_count": len(fixes), "avg_fix_rounds": statistics.mean(fixes) if fixes else None,
            "median_tokens_per_accepted_task": statistics.median(tokens) if tokens else None,
            "max_tokens_per_accepted_task": max(tokens) if tokens else None,
            "model_distribution": models, "breakdown": breakdown, "pipeline_amplification": amplification}


def efficiency(root=STATE, by=None):
    """P0 efficiency across all usage, with accepted execute lineages as the rate denominator.

    Task tokens/cost/calls/turns sum all lineage rows. Tokens to first green include ts <=
    first_green_at; elapsed times subtract created_at. Fix rounds use the recorded count or
    count descendants linked by fix_round_for. First pass requires first green, zero fixes,
    and zero gate reds; missing stamps remain None. Goal totals sum accepted lineage rows
    plus goal planner/scout rows. Unknown and unaccepted usage remain in window breakdowns.
    """
    if by not in (None, "goal", "executor", "band", "class", "role"):
        raise ValueError(f"unsupported efficiency grouping: {by}")
    tasks = {}
    for path in sorted((root / "tasks").glob("*.json")):
        try:
            task = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        tasks[task.get("id", path.stem)] = task
    rows, malformed_lines = _efficiency_rows(root, tasks)
    accepted = {}
    for tid, task in tasks.items():
        if task.get("role") != "execute" or not task.get("merged_into") or _task_lineage(task, tasks)[0] != tid:
            continue
        own = [r for r in rows if r["lineage_root"] == tid]
        pipeline = task.get("pipeline") or {}
        green = _stamp(pipeline.get("first_green_at"))
        created = _stamp(task.get("created_at"))
        end = _stamp(task.get("accepted_at"))
        fixes = task.get("lineage_fix_rounds", pipeline.get("lineage_fix_rounds"))
        if fixes is None:
            fixes = sum(other != tid and _task_lineage(t, tasks)[0] == tid for other, t in tasks.items())
        reds = pipeline.get("gate_reds", task.get("gate_reds"))
        meta = _attributed({"task": tid, "role": "execute"}, tasks)
        accepted[tid] = {**_efficiency_summary(own, []),
            "goal_id": task.get("parent") or "unknown", "executor": meta["executor"],
            "band": meta["band"], "task_class": meta["task_class"], "accepted_at": task.get("accepted_at"),
            "tokens_to_first_green": sum(_tokens_of(r) for r in own if _stamp(r.get("ts")) is not None and _stamp(r["ts"]) <= green) if green is not None else None,
            "time_to_first_green_s": green - created if green is not None and created is not None else None,
            "time_to_accepted_s": end - created if end is not None and created is not None else None,
            "fix_rounds": fixes, "fix_round_tokens": sum(_tokens_of(r) for r in own if r["bucket"] == "fix_round"),
            "first_pass": fixes == 0 and reds == 0 if green is not None and reds is not None else None}
    goal_card = {}
    for gid in accepted_goals(root):
        members = {tid for tid, task in accepted.items() if task["goal_id"] == gid}
        own = [r for r in rows if r["lineage_root"] in members or (r["goal_id"] == gid and r["bucket"] in ("planner", "scout"))]
        goal_card[gid] = _efficiency_summary(own, [accepted[tid] for tid in members])
        for metric in ("tokens_to_first_green", "time_to_first_green_s", "time_to_accepted_s",
                       "fix_rounds", "fix_round_tokens"):
            values = [accepted[tid][metric] for tid in members]
            goal_card[gid][metric] = sum(values) if values and all(v is not None for v in values) else None
    summary = _efficiency_summary(rows, list(accepted.values()))
    summary.update({"by": by, "tasks": accepted, "goals": goal_card, "malformed_lines": malformed_lines,
                    "tokens_per_accepted_goal": sum(g["tokens"] for g in goal_card.values()) / len(goal_card) if goal_card else None,
                    "usd_per_accepted_goal": sum(g["usd"] for g in goal_card.values()) / len(goal_card) if goal_card else None})
    grouped_rows = {"unknown": []}
    grouped_tasks = {"unknown": []}
    for row in rows:
        task = accepted.get(row["lineage_root"])
        if by == "role":
            key = row.get("role") or ("jev" if row["bucket"] == "jev" else "unknown")
        elif by == "goal":
            key = row["goal_id"]
        elif by and task:
            key = task.get("task_class" if by == "class" else by) or "unknown"
        elif by is None and task:
            key = "all"
        else:
            key = "unknown"
        grouped_rows.setdefault(key, []).append(row)
    for tid, task in accepted.items():
        if by == "role":
            continue  # Roles group calls, not task outcomes.
        key = task["goal_id"] if by == "goal" else task.get("task_class" if by == "class" else by) if by else "all"
        grouped_tasks.setdefault(key or "unknown", []).append(task)
    summary["groups"] = {key: _efficiency_summary(grouped_rows.get(key, []), grouped_tasks.get(key, []))
                         for key in sorted(grouped_rows.keys() | grouped_tasks.keys())}
    return summary


def _failure_reason(task):
    """Return the stable economics bucket for a gate-red task."""
    pipeline = task.get("pipeline") or {}
    if pipeline.get("failure_kind"):
        return pipeline["failure_kind"]
    failures = (task.get("resume_hint") or {}).get("failures") or []
    head = failures[0] if isinstance(failures, list) and failures else failures
    text = str(head or "").lower()
    if "missing" in text and ("test" in text or "coverage" in text):
        return "missing_tests"
    if any(word in text for word in ("pytest", "unittest", "test failed", "test_failure", "failure")):
        return "test_failure"
    if any(word in text for word in ("lint", "ruff", "flake", "format")):
        return "lint"
    if any(word in text for word in ("environment", "missing command", "not found", "permission", "timeout")):
        return "environment"
    return "unknown"


def executor_economics(root=STATE, by="executor"):
    """Measure accepted/failed execute lineages without embedding routing policy."""
    if by not in ("executor", "band", "class"):
        raise ValueError(f"unsupported economics grouping: {by}")
    tasks = {}
    for path in sorted((root / "tasks").glob("*.json")):
        try:
            task = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        tasks[task.get("id", path.stem)] = task
    rows, _ = _efficiency_rows(root, tasks)
    roots = []
    for tid, task in tasks.items():
        if task.get("role") != "execute" or _task_lineage(task, tasks)[0] != tid:
            continue
        accepted_flag = bool(task.get("merged_into"))
        if not accepted_flag and task.get("status") != "failed":
            continue
        meta = _attributed({"task": tid, "role": "execute"}, tasks)
        lineage_ids = {oid for oid, other in tasks.items() if _task_lineage(other, tasks)[0] == tid}
        own = [row for row in rows if row.get("lineage_root") == tid]
        initial = [row for row in own if row.get("task") == tid and row.get("bucket") == "execute"]
        pipeline = task.get("pipeline") or {}
        fixes = task.get("lineage_fix_rounds", pipeline.get("lineage_fix_rounds"))
        if fixes is None:
            fixes = sum(oid != tid and tasks[oid].get("role") == "execute" for oid in lineage_ids)
        reds = pipeline.get("gate_reds", task.get("gate_reds"))
        green = pipeline.get("first_green_at")
        verdicts = []
        for review in tasks.values():
            inputs = review.get("inputs") or []
            if review.get("role") == "review" and inputs and inputs[0] in lineage_ids:
                verdict = review.get("review_verdict") or (review.get("result") or {}).get("verdict")
                if verdict:
                    verdicts.append(verdict)
        reasons = {}
        for member_id in lineage_ids:
            member = tasks[member_id]
            count = int((member.get("pipeline") or {}).get("gate_reds", member.get("gate_reds", 0)) or 0)
            if count:
                reason = _failure_reason(member)
                reasons[reason] = reasons.get(reason, 0) + count
        roots.append({"executor": meta.get("executor"), "band": meta.get("band"),
                      "class": meta.get("task_class"), "accepted": accepted_flag,
                      "initial_tokens": sum(_tokens_of(row) for row in initial),
                      "tokens": sum(_tokens_of(row) for row in own),
                      "usd": sum(row.get("usd") or 0 for row in own), "fix_rounds": fixes,
                      "first_pass": fixes == 0 and reds == 0 if green is not None and reds is not None else None,
                      "reasons": reasons, "verdicts": verdicts})
    grouped = {}
    for root_row in roots:
        executor_id = root_row["executor"] or "unknown"
        key = executor_id if by == "executor" else (executor_id, root_row[by] or "unknown")
        grouped.setdefault(key, []).append(root_row)
    result = {}
    for key, members in grouped.items():
        first = [m["first_pass"] for m in members if m["first_pass"] is not None]
        accepted_rows = [m for m in members if m["accepted"]]
        verdicts = [v for m in members for v in m["verdicts"]]
        initial = [m["initial_tokens"] for m in members]
        reasons = {}
        for member in members:
            for reason, count in member["reasons"].items():
                reasons[reason] = reasons.get(reason, 0) + count
        result[key] = {
            "n_tasks": len(members),
            "initial_execution_tokens": {"median": statistics.median(initial) if initial else None,
                                         "mean": statistics.mean(initial) if initial else None},
            "first_pass_green_rate": sum(first) / len(first) if first else None,
            "first_pass_defined_count": len(first),
            "fix_round_probability": sum(m["fix_rounds"] >= 1 for m in members) / len(members) if members else None,
            "avg_fix_rounds": statistics.mean(m["fix_rounds"] for m in members) if members else None,
            "tokens_to_accepted": statistics.median(m["tokens"] for m in accepted_rows) if accepted_rows else None,
            "cost_to_accepted": statistics.median(m["usd"] for m in accepted_rows) if accepted_rows else None,
            "cost_to_accepted_defined_count": len(accepted_rows),
            "gate_failure_reasons": reasons,
            "review_request_changes_rate": (sum(v == "request_changes" for v in verdicts) / len(verdicts)
                                             if verdicts else None),
            "review_request_changes_defined_count": len(verdicts),
        }
    return result


def format_executor_economics(card):
    def cell(value):
        return "n/a" if value is None else str(round(value, 4) if isinstance(value, float) else value)
    columns = ("n_tasks", "initial_execution_tokens_median", "initial_execution_tokens_mean",
               "first_pass_green_rate", "first_pass_defined_count", "fix_round_probability", "avg_fix_rounds",
               "tokens_to_accepted", "cost_to_accepted", "cost_to_accepted_defined_count",
               "gate_failure_reasons", "review_request_changes_rate", "review_request_changes_defined_count")
    lines = ["group\t" + "\t".join(columns)]
    for key, row in sorted(card.items(), key=lambda item: str(item[0])):
        name = "/".join(key) if isinstance(key, tuple) else key
        flat = dict(row, initial_execution_tokens_median=row["initial_execution_tokens"]["median"],
                    initial_execution_tokens_mean=row["initial_execution_tokens"]["mean"])
        flat["gate_failure_reasons"] = json.dumps(flat["gate_failure_reasons"], sort_keys=True)
        lines.append(str(name) + "\t" + "\t".join(cell(flat[column]) for column in columns))
    return "\n".join(lines)


def format_efficiency(card):
    """Render undefined ratios/stamps as n/a and the ordered amplification footer."""
    def cell(value):
        return "n/a" if value is None else str(round(value, 4) if isinstance(value, float) else value)
    columns = ("tokens", "usd", "accepted_tasks", "first_pass_rate", "first_pass_defined_count",
               "fix_round_rate", "fix_round_defined_count", "avg_fix_rounds",
               "median_tokens_per_accepted_task", "max_tokens_per_accepted_task", "pipeline_amplification")
    lines = ["group\t" + "\t".join(columns)]
    for name, group in [("total", card), *card["groups"].items()]:
        lines.append(name + "\t" + "\t".join(cell(group[key]) for key in columns))
    for name, group in [("total", card), *card["groups"].items()]:
        lines.append(name + " breakdown: " + ", ".join(f"{key}={cell(value)}" for key, value in group["breakdown"].items()))
    return "\n".join(lines)
