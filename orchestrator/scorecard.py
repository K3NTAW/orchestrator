"""Per-executor outcome rollup: runs/*.jsonl + tasks/T-*.json, keyed by executor id. Feeds pick_executor's
scores() so routing reacts to live merge/fail/usage-limit history instead of static weights alone."""
import json
import statistics
import tomllib
from datetime import date, datetime
from pathlib import Path
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


def _class_resolved(executor_id, task_class_name, root):
    """Resolved execute tasks in a class that have a corresponding execute run."""
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
    return [task for tid, task in tasks.items()
            if tid in run_task_ids and task.get("role") == "execute"
            and task.get("executor") == executor_id and task_class(task) == task_class_name
            and (task.get("merged_into") or task.get("status") == "failed")]


def class_sample_size(executor_id, task_class_name, root=STATE):
    """Number of resolved tasks used by :func:`class_success`."""
    return len(_class_resolved(executor_id, task_class_name, root))


def class_success(executor_id, task_class_name, root=STATE, min_samples=None):
    """Return an executor's merge rate for a class, using only tasks with execute run rows."""
    if min_samples is None:
        try:
            from .pool import config
            min_samples = config().get("models", {}).get("min_samples", 3)
        except Exception:
            min_samples = 3

    resolved = _class_resolved(executor_id, task_class_name, root)
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


def _review_comments(task):
    """Return structured findings from either the current result envelope or legacy task fields."""
    result = task.get("result") or {}
    comments = result.get("comments", task.get("comments", []))
    return comments if isinstance(comments, list) else []


def _comment_components(reviews):
    """Collapse comments that identify the same defect by location or normalized issue prefix."""
    components = []
    for review_id, comments in reviews:
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            path, line = comment.get("path"), comment.get("line")
            issue = comment.get("issue", comment.get("body", comment.get("comment", "")))
            normalized = " ".join(str(issue).lower().split())[:60]
            keys = set()
            if path is not None and line is not None:
                keys.add(("location", str(path), str(line)))
            if normalized:
                keys.add(("issue", normalized))
            if not keys:
                continue
            matches = [item for item in components if item[0] & keys]
            if matches:
                merged_keys, owners = set(keys), {review_id}
                for item in matches:
                    merged_keys.update(item[0]); owners.update(item[1]); components.remove(item)
                components.append((merged_keys, owners))
            else:
                components.append((keys, {review_id}))
    return components


def review_quality(root=STATE, by="role"):
    """Review findings and cost, grouped without treating fewer reviews as an efficiency win."""
    if by not in ("role", "packet_version", "tier", "band", "reviewed_executor"):
        raise ValueError(f"unsupported review grouping: {by}")
    root = Path(root)
    tasks = {}
    for path in sorted((root / "tasks").glob("*.json")):
        try:
            task = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        tasks[task.get("id", path.stem)] = task
    run_rows, _ = _efficiency_rows(root, tasks)
    runs = {}
    for row in run_rows:
        if row.get("task"):
            runs.setdefault(row["task"], []).append(row)

    reviews = []
    for tid, task in tasks.items():
        if task.get("role") != "review":
            continue
        own_runs = runs.get(tid, [])
        facts = attribution.review_facts(task)
        # Persisted run facts win when present; summing usage keeps retries one review observation.
        for row in own_runs:
            facts = {**facts, **{key: value for key, value in row.items() if value is not None}}
        inputs = task.get("inputs") or []
        target_id = inputs[0] if inputs else None
        reviewed = tasks.get(target_id, {})
        target_meta = _attributed({"task": target_id, "role": reviewed.get("role", "execute")}, tasks)
        packet = facts.get("packet_version")
        keys = {
            "role": facts.get("reviewer_role") or "unknown",
            "packet_version": packet if packet is not None else "pre-packet",
            "tier": facts.get("tier") or task.get("tier") or "unknown",
            "band": target_meta.get("band") or "unknown",
            "reviewed_executor": target_meta.get("executor") or "unknown",
        }
        severities = facts.get("findings_by_severity") or {}
        count = facts.get("findings_count")
        if count is None:
            count = sum(v for v in severities.values() if isinstance(v, (int, float)))
        usd_values = [row.get("usd") for row in own_runs if row.get("usd") is not None]
        reviews.append({"id": tid, "target": target_id, "group": keys[by],
                        "verdict": facts.get("verdict"), "findings": count,
                        "severities": severities, "tokens": sum(_tokens_of(row) for row in own_runs),
                        "has_tokens": bool(own_runs), "usd": sum(usd_values) if usd_values else None,
                        "pass": facts.get("review_pass_index"), "status": task.get("status"),
                        "comments": _review_comments(task)})

    pair_metrics = {}
    by_target = {}
    for review in reviews:
        by_target.setdefault(review["target"], []).append(review)
    for target, members in by_target.items():
        done = [review for review in members if review["status"] == "done"]
        if target is None or len(done) != 2:
            continue
        components = _comment_components([(r["id"], r["comments"]) for r in done])
        shared = sum(len(owners) == 2 for _, owners in components)
        second = next((r for r in done if r["pass"] == 2), done[1])
        pair_metrics[target] = {"groups": {r["group"] for r in done}, "distinct_defects": len(components),
                                "shared_defects": shared,
                                "overlap_share": shared / len(components) if components else None,
                                "second_review_added": sum(owners == {second["id"]} for _, owners in components)}

    result = {}
    for group in sorted({review["group"] for review in reviews}, key=str):
        members = [review for review in reviews if review["group"] == group]
        findings = [review["findings"] for review in members]
        tokens = [review["tokens"] for review in members if review["has_tokens"]]
        costs = [review["usd"] for review in members if review["usd"] is not None]
        pairs = [pair for pair in pair_metrics.values() if group in pair["groups"]]
        total_tokens = sum(tokens)
        severity = {}
        for review in members:
            for name, value in review["severities"].items():
                severity[name] = severity.get(name, 0) + value
        verdicts = {name: 0 for name in ("approve", "request_changes", "other")}
        for review in members:
            verdict = review["verdict"]
            verdicts[verdict if verdict in verdicts else "other"] += 1
        result[group] = {
            "n_reviews": len(members), "verdicts": verdicts,
            "findings_per_review": {"mean": statistics.mean(findings) if findings else None,
                                    "median": statistics.median(findings) if findings else None},
            "findings_by_severity": severity,
            "share_of_reviews_with_a_high_finding": (sum((r["severities"].get("high") or 0) > 0 for r in members) / len(members) if members else None),
            "tokens_per_review": statistics.median(tokens) if tokens else None,
            "usd_per_review": statistics.median(costs) if costs else None,
            "findings_per_million_tokens": sum(findings) * 1_000_000 / total_tokens if total_tokens else None,
            "distinct_defects": sum(p["distinct_defects"] for p in pairs) if pairs else None,
            "overlap_share": (sum(p["shared_defects"] for p in pairs) /
                              sum(p["distinct_defects"] for p in pairs)) if pairs and sum(p["distinct_defects"] for p in pairs) else None,
            "second_review_added": sum(p["second_review_added"] for p in pairs) if pairs else None,
        }
    return result


def format_review_quality(card):
    columns = ("n_reviews", "findings_per_review", "share_of_reviews_with_a_high_finding",
               "tokens_per_review", "usd_per_review", "findings_per_million_tokens",
               "distinct_defects", "overlap_share", "second_review_added")
    lines = ["group\t" + "\t".join(columns)]
    for group, row in card.items():
        values = []
        for key in columns:
            value = row[key]
            if key == "findings_per_review" and isinstance(value, dict):
                value = f"mean={value['mean']},median={value['median']}"
            values.append("n/a" if value is None else str(value))
        lines.append(str(group) + "\t" + "\t".join(values))
    return "\n".join(lines)


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


def _routing_group(rows):
    """Descriptive outcome summary for a set of shadow routing decisions."""
    first = [row["first_pass"] for row in rows if row.get("first_pass") is not None]
    fixes = [row["fix_rounds"] for row in rows if row.get("fix_rounds") is not None]
    gate_reds = [row["gate_reds"] for row in rows if row.get("gate_reds") is not None]
    reviews = [row["review_request_changes"] for row in rows
               if row.get("review_request_changes") is not None]
    accepted = [row for row in rows if row.get("accepted")]
    token_values = [row["tokens"] for row in accepted if row.get("tokens") is not None]
    cost_values = [row["cost"] for row in accepted if row.get("cost") is not None]
    return {
        "n": len(rows), "accepted_share": sum(bool(r.get("accepted")) for r in rows) / len(rows) if rows else None,
        "accepted_defined_count": len(rows),
        "first_pass_rate": sum(first) / len(first) if first else None,
        "first_pass_defined_count": len(first),
        "fix_round_rate": sum(value >= 1 for value in fixes) / len(fixes) if fixes else None,
        "fix_round_defined_count": len(fixes),
        "avg_fix_rounds": statistics.mean(fixes) if fixes else None,
        "median_tokens_to_accepted": statistics.median(token_values) if token_values else None,
        "tokens_to_accepted_defined_count": len(token_values),
        "median_cost_to_accepted": statistics.median(cost_values) if cost_values else None,
        "cost_to_accepted_defined_count": len(cost_values),
        "gate_red_share": sum(value > 0 for value in gate_reds) / len(gate_reds) if gate_reds else None,
        "gate_red_defined_count": len(gate_reds),
        "review_request_changes_share": sum(reviews) / len(reviews) if reviews else None,
        "review_request_changes_defined_count": len(reviews),
    }


def routing_eval(root=STATE, min_samples=None):
    """Compare shadow Jev routing classifications with observed lineage-root outcomes.

    This deliberately reports side-by-side descriptive evidence only.  In particular, disagreement is
    not treated as improvement: deciding whether a hypothetical route predicts better downstream outcomes
    belongs to the later snapshot decision.
    """
    root = Path(root) if not hasattr(root, "glob") else root
    tasks = {}
    for path in sorted((root / "tasks").glob("*.json")):
        try:
            task = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        tasks[task.get("id", path.stem)] = task
    all_rows, _ = _efficiency_rows(root, tasks)
    route_rows = [row for row in all_rows if row.get("role") == "jev_route"]
    if not route_rows:
        return None
    if min_samples is None:
        min_samples = 20
        try:
            cfg = tomllib.loads((root / "pool.toml").read_text())
            min_samples = int(cfg.get("jev", {}).get("routing", {}).get("min_eval_samples", 20))
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            pass

    usage_rows = [row for row in all_rows if row.get("role") != "jev_route"]
    joined = []
    for route in route_rows:
        attributed = _attributed(route, tasks)
        tid = attributed.get("lineage_root") or route.get("task")
        task = tasks.get(tid, {})
        lineage_ids = {oid for oid, other in tasks.items() if _task_lineage(other, tasks)[0] == tid}
        own = [row for row in usage_rows if row.get("lineage_root") == tid]
        pipeline = task.get("pipeline") or {}
        fixes = task.get("lineage_fix_rounds", pipeline.get("lineage_fix_rounds"))
        if fixes is None and task:
            fixes = sum(oid != tid and tasks[oid].get("role") == "execute" for oid in lineage_ids)
        reds = pipeline.get("gate_reds", task.get("gate_reds"))
        green = pipeline.get("first_green_at")
        verdicts = []
        for review in tasks.values():
            inputs = review.get("inputs") or []
            constraints = review.get("constraints") or {}
            target = constraints.get("review_for") or (inputs[0] if inputs else None)
            if review.get("role") == "review" and target in lineage_ids:
                verdict = review.get("review_verdict") or (review.get("result") or {}).get("verdict")
                if verdict in ("approve", "request_changes"):
                    verdicts.append(verdict)
        accepted = bool(task.get("merged_into"))
        joined.append({**route, "lineage_root": tid, "accepted": accepted,
                       "first_pass": fixes == 0 and reds == 0 if green is not None and reds is not None else None,
                       "fix_rounds": fixes, "gate_reds": reds,
                       "tokens": sum(_tokens_of(row) for row in own),
                       "cost": sum(row.get("usd") or 0 for row in own),
                       "review_request_changes": (sum(v == "request_changes" for v in verdicts) / len(verdicts)
                                                  if verdicts else None),
                       "executor": attributed.get("executor"), "band": attributed.get("band"),
                       "task_class": attributed.get("task_class")})

    def split(key):
        values = {}
        for row in joined:
            values.setdefault(str(row.get(key) or "unknown"), []).append(row)
        return {name: _routing_group(members) for name, members in sorted(values.items())}

    agree = [row for row in joined if row.get("baseline") == row.get("hypothetical")]
    disagree = [row for row in joined if row.get("baseline") != row.get("hypothetical")]
    signal_keys = sorted({key for row in joined for key in (row.get("signals") or {})})
    signals = {}
    for key in signal_keys:
        high, low = [], []
        for row in joined:
            value = (row.get("signals") or {}).get(key)
            probability = value.get("p") if isinstance(value, dict) else value
            (high if isinstance(probability, (int, float)) and probability >= .6 else low).append(row)
        signals[key] = {"p>=0.6": _routing_group(high), "p<0.6": _routing_group(low)}

    execute_tasks = {row.get("task") for row in all_rows if row.get("role") == "execute" and row.get("task")}
    classified = {row.get("task") for row in route_rows if row.get("task") in execute_tasks}
    skips = {}
    for row in route_rows:
        reason = row.get("skip_reason") or row.get("reason")
        if not reason:
            reason = "error" if row.get("error") else "cache hit" if row.get("cache") else None
        if reason:
            normalized = str(reason).replace("_", " ")
            skips[normalized] = skips.get(normalized, 0) + 1
    latencies = [row.get("latency_ms") for row in route_rows if isinstance(row.get("latency_ms"), (int, float))]
    usage = [row.get("usage") or {} for row in route_rows]
    result = {"rows": joined, "groups": {"overall": _routing_group(joined),
              "agree": _routing_group(agree), "disagree": _routing_group(disagree),
              "by_band": split("band"), "by_task_class": split("task_class"),
              "by_baseline_executor": split("baseline"), "signals": signals},
              "coverage": {"classified": len(classified), "execute_dispatches": len(execute_tasks),
                           "share": len(classified) / len(execute_tasks) if execute_tasks else None},
              "skip_reasons": skips,
              "jev_latency_ms": {"median": statistics.median(latencies) if latencies else None,
                                  "p95": _percentile_value(latencies, 95)},
              "jev_usage": {"tokens": sum(int(u.get("tokens", u.get("input_tokens", 0) + u.get("output_tokens", 0))) for u in usage),
                            "usd": sum(float(u.get("usd") or 0) for u in usage)},
              "min_samples": min_samples,
              "evidence_verdict": "collected" if len(disagree) >= min_samples else "insufficient"}
    return result


def _percentile_value(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def format_routing_eval(card):
    if card is None:
        return "routing evidence: insufficient (no rows)"
    columns = ("group", "n", "accepted_share", "first_pass_rate", "first_pass_defined_count",
               "fix_round_rate", "fix_round_defined_count", "avg_fix_rounds",
               "median_tokens_to_accepted", "tokens_to_accepted_defined_count",
               "median_cost_to_accepted", "cost_to_accepted_defined_count", "gate_red_share",
               "gate_red_defined_count", "review_request_changes_share",
               "review_request_changes_defined_count")
    def cell(value):
        return "n/a" if value is None else str(round(value, 4) if isinstance(value, float) else value)
    lines = ["\t".join(columns)]
    rows = {key: card["groups"][key] for key in ("agree", "disagree")}
    for signal, splits in card["groups"]["signals"].items():
        for band, row in splits.items():
            rows[f"signal:{signal}:{band}"] = row
    for name, row in rows.items():
        lines.append(name + "\t" + "\t".join(cell(row[key]) for key in columns[1:]))
    coverage = card["coverage"]
    lines.append(f"coverage: {coverage['classified']}/{coverage['execute_dispatches']} ({cell(coverage['share'])}) "
                 f"skip_reasons={json.dumps(card['skip_reasons'], sort_keys=True)}")
    lines.append(f"jev: latency median={cell(card['jev_latency_ms']['median'])}ms "
                 f"p95={cell(card['jev_latency_ms']['p95'])}ms tokens={card['jev_usage']['tokens']} "
                 f"usd={round(card['jev_usage']['usd'], 4)}")
    lines.append("evidence verdict: " + card["evidence_verdict"])
    return "\n".join(lines)


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


def _parallelism_sweep(intervals):
    """Half-open intervals; average occupancy over the union of busy time."""
    if not intervals:
        return None, None
    changes = {}
    for start, end in intervals:
        changes[start] = changes.get(start, 0) + 1
        changes[end] = changes.get(end, 0) - 1
    active = peak = 0
    area = busy = 0.0
    previous = min(changes)
    for stamp, change in sorted(changes.items()):
        width = stamp - previous
        area += active * width
        if active:
            busy += width
        active += change
        peak = max(peak, active)
        previous = stamp
    return peak, area / busy if busy else None


def parallelism(root=STATE, goal=None):
    """Read-only scheduling telemetry. Missing timing evidence remains undefined.

    Wait medians use observed samples; totals require all applicable samples.
    Total wall time spans the earliest goal start to the latest goal completion;
    total critical path is the longest of the independent goal DAGs.
    """
    root = Path(root)
    malformed = 0

    def read_rows(path):
        nonlocal malformed
        if not path.exists():
            return []
        rows = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue
            rows.append(row)
        return rows

    tasks = {}
    for path in sorted((root / 'tasks').glob('*.json')):
        try:
            task = json.loads(path.read_text())
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(task, dict):
            tasks[task.get('id', path.stem)] = task
        else:
            malformed += 1
    runs = [row for path in sorted((root / 'runs').glob('*.jsonl')) for row in read_rows(path)]
    sched = {name: read_rows(root / 'runs' / 'sched' / (name + '.jsonl'))
             for name in ('dispatch', 'waves', 'stale')}

    def stamp(task, key):
        return _stamp((task.get('pipeline') or {}).get(key, task.get(key)))

    def done(task):
        values = [_stamp(e.get('ts')) for e in task.get('events', [])
                  if isinstance(e, dict) and e.get('status') == 'done']
        return max((v for v in values if v is not None), default=None)

    def difference(end, start):
        return end - start if end is not None and start is not None and end >= start else None

    def row_goal(row):
        return row.get('goal_id') or tasks.get(row.get('task'), {}).get('parent') or 'unknown'

    goal_ids = {t['parent'] for t in tasks.values() if t.get('parent')}
    goal_ids.update(row_goal(r) for r in runs)
    goal_ids.update(row_goal(r) for r in sched['stale'])
    goal_ids.update(row_goal(c) for r in sched['dispatch'] for c in r.get('considered', [])
                    if isinstance(c, dict))
    if goal is not None:
        goal_ids = {goal}
    grouped = {gid: {tid: t for tid, t in tasks.items() if t.get('parent') == gid}
               for gid in sorted(goal_ids)}
    wait_names = ('queue_wait_s', 'dependency_wait_s', 'execution_s', 'review_s', 'merge_wait_s')
    intervals, samples, boundaries, paths, path_tasks = {}, {}, {}, {}, {}
    cards = {}
    for gid, members in grouped.items():
        execute = {tid: t for tid, t in members.items() if t.get('role') == 'execute'}
        waits = {name: [] for name in wait_names}
        executor_intervals, claude_intervals = [], []
        durations = {}
        for tid, task in members.items():
            created, claimed, gated = (stamp(task, k) for k in ('created_at', 'claimed_at', 'gated_at'))
            if task.get('role') == 'review':
                waits['review_s'].append(difference(done(task), created))
            if tid not in execute:
                continue
            ready = stamp(task, 'first_ready_at')
            waits['queue_wait_s'].append(difference(stamp(task, 'dispatched_at'),
                                                    ready if ready is not None else created))
            waits['dependency_wait_s'].append(difference(ready, created))
            duration = difference(gated, claimed)
            if duration is None:
                recorded = [r['duration_s'] for r in runs if r.get('task') == tid
                            and r.get('role') == 'execute'
                            and isinstance(r.get('duration_s'), (int, float)) and r['duration_s'] >= 0]
                duration = sum(recorded) if recorded else None
            durations[tid] = duration
            waits['execution_s'].append(duration)
            waits['merge_wait_s'].append(difference(stamp(task, 'merged_at'), stamp(task, 'first_green_at')))
            end = gated if gated is not None else done(task)
            if difference(end, claimed) is not None:
                executor_intervals.append((claimed, end))
        for row in runs:
            if row_goal(row) != gid or row.get('account') == 'codex' or not row.get('role'):
                continue
            end, duration = _stamp(row.get('ts')), row.get('duration_s')
            if end is not None and isinstance(duration, (int, float)) and duration >= 0:
                claude_intervals.append((end - duration, end))

        memo, visiting = {}, set()

        def longest(tid):
            if tid in memo:
                return memo[tid]
            if tid in visiting:
                return None
            duration = durations[tid]
            if duration is None and not execute[tid].get('merged_into'):
                return (0, 0)
            visiting.add(tid)
            parents = [longest(dep) for dep in execute[tid].get('depends_on', []) if dep in execute]
            visiting.remove(tid)
            if any(v is None for v in parents):
                memo[tid] = None
            else:
                parent = max((v for v in parents if v is not None), default=(0, 0))
                memo[tid] = (parent[0] + (duration or 0), parent[1] + (duration is not None))
            return memo[tid]

        lengths = [longest(tid) for tid in execute if durations[tid] is not None]
        best = max(lengths, default=None) if lengths and all(v is not None for v in lengths) else None
        paths[gid] = best[0] if best is not None else None
        path_tasks[gid] = best[1] if best is not None else None
        path_partial = any(duration is None for duration in durations.values())
        goal_task = tasks.get(gid, {})
        start, end = stamp(goal_task, 'created_at'), done(goal_task)
        if end is None:
            end = max((stamp(t, 'merged_at') for t in members.values()
                       if stamp(t, 'merged_at') is not None), default=None)
        boundaries[gid] = (start, end)
        logged_stale = [r for r in sched['stale'] if row_goal(r) == gid and r.get('risk') == 'high']
        logged_tasks = {r.get('task') for r in logged_stale}
        fixes = [t for t in execute.values() if (t.get('constraints') or {}).get('fix_round_for')]
        concurrent_fixes = 0
        for fix in fixes:
            parent_id = fix['constraints']['fix_round_for']
            parent = members.get(parent_id, {})
            claimed, gated = stamp(parent, 'claimed_at'), stamp(parent, 'gated_at')
            if claimed is not None and gated is not None and any(
                    claimed < merged < gated for tid, t in execute.items() if tid != parent_id
                    for merged in [stamp(t, 'merged_at')] if merged is not None):
                concurrent_fixes += 1
        # Recorded lineage counts cover legacy repairs that lack a linked task.
        fix_count = len(fixes)
        for tid, task in execute.items():
            if (task.get('constraints') or {}).get('fix_round_for'):
                continue
            recorded = task.get('lineage_fix_rounds', (task.get('pipeline') or {}).get('lineage_fix_rounds', 0)) or 0
            linked = sum(_task_lineage(f, tasks)[0] == tid for f in fixes)
            fix_count += max(0, recorded - linked)
        skips = {}
        for row in sched['dispatch']:
            for considered in row.get('considered', []):
                if (not isinstance(considered, dict) or row_goal(considered) != gid
                        or considered.get('action') not in ('skip', 'skipped')):
                    continue
                reason = considered.get('reason') or 'other'
                skips[reason] = skips.get(reason, 0) + 1
        cards[gid] = {
            'wall_clock_s': difference(end, start), 'critical_path_s': paths[gid],
            'critical_path_tasks': path_tasks[gid], 'critical_path_partial': path_partial,
            'merge_conflicts': sum(t.get('reason') == 'rebase_conflict' or
                                   (t.get('last_merge') or {}).get('status') == 'conflict' for t in execute.values()),
            'rebase_failures': sum('rebase' in str(t.get('failure_kind') or
                                   (t.get('pipeline') or {}).get('failure_kind') or '').lower() for t in execute.values()),
            'stale_work_events': len(logged_stale) + sum(tid not in logged_tasks and
                ((t.get('pipeline') or {}).get('stale_check') or {}).get('risk') == 'high'
                for tid, t in members.items()),
            'fix_rounds': fix_count, 'fix_rounds_after_concurrent_merge': concurrent_fixes,
            'skip_reasons': skips}
        intervals[gid] = (executor_intervals, claude_intervals)
        samples[gid] = waits

    totals = {key: sum(card[key] for card in cards.values()) for key in
              ('merge_conflicts', 'rebase_failures', 'stale_work_events', 'fix_rounds', 'fix_rounds_after_concurrent_merge')}
    totals['skip_reasons'] = {}
    for card in cards.values():
        for reason, count in card['skip_reasons'].items():
            totals['skip_reasons'][reason] = totals['skip_reasons'].get(reason, 0) + count
    defined_paths = [(duration, path_tasks[gid]) for gid, duration in paths.items() if duration is not None]
    best_path = max(defined_paths, default=None)
    totals['critical_path_s'] = best_path[0] if best_path is not None else None
    totals['critical_path_tasks'] = best_path[1] if best_path is not None else None
    totals['critical_path_partial'] = any(card['critical_path_partial'] for card in cards.values())
    complete = boundaries and all(difference(end, start) is not None for start, end in boundaries.values())
    totals['wall_clock_s'] = (max(end for start, end in boundaries.values()) -
                              min(start for start, end in boundaries.values())) if complete else None
    for gid, card in [*cards.items(), (None, totals)]:
        selected = [gid] if gid is not None else list(cards)
        for index, name in enumerate(('executors', 'claude_workers')):
            maximum, average = _parallelism_sweep([pair for g in selected for pair in intervals[g][index]])
            card['max_concurrent_' + name] = maximum
            card['avg_concurrent_' + name] = average
        for name in wait_names:
            values = [v for g in selected for v in samples[g][name]]
            defined = [v for v in values if v is not None]
            card['median_' + name] = statistics.median(defined) if defined else None
            card['total_' + name] = sum(values) if values and len(defined) == len(values) else None
    waves = [r for r in sched['waves'] if goal is None or row_goal(r) == goal
             or any(t in grouped.get(goal, {}) for t in r.get('wave', []))]
    return {'goals': cards, 'totals': totals, 'skip_reasons': totals['skip_reasons'],
            'waves': {'rows': len(waves), 'applied': sum(bool(r.get('applied')) for r in waves)},
            'malformed': malformed}


def format_parallelism(card):
    columns = list(card['totals'])
    def cell(value):
        if value is None:
            return 'undefined'
        if isinstance(value, dict):
            return json.dumps(value, sort_keys=True)
        return str(round(value, 4) if isinstance(value, float) else value)
    lines = ['goal\t' + '\t'.join(columns)]
    for name, metrics in [*card['goals'].items(), ('total', card['totals'])]:
        lines.append(str(name) + '\t' + '\t'.join(cell(metrics[k]) for k in columns))
    lines.append('waves: ' + cell(card['waves']))
    lines.append('malformed: ' + str(card['malformed']))
    return '\n'.join(lines)
