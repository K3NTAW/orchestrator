"""Read-side Planner routing evidence; no routing decisions or ledger writes."""
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import tomllib

from . import STATE, promotion, scorecard
from .sched_scorecard import _stamp, _tasks

CRITERIA_MAP = {
    "first_pass_rate": ("first_pass_delta", ">=", promotion.CRITERIA["first_pass_delta"]),
    "avg_fix_rounds": ("fix_rounds_delta", "<=", promotion.CRITERIA["fix_rounds_delta"]),
    "gate_success": ("gate_success_delta", ">=", promotion.CRITERIA["gate_success_delta"]),
}
REVIEW_CHANGES_MAX_DELTA = 0.10
REESCALATION_MAX = 0.2


def _config(root, cfg):
    if cfg is not None:
        return cfg
    try:
        return tomllib.loads((Path(root) / "pool.toml").read_text())
    except (OSError, ValueError):
        return {}


def _min_samples(cfg, explicit):
    if explicit is not None:
        return explicit
    for table in (promotion._table(cfg, "planner.routing"), promotion._table(cfg, "promotion")):
        value = table.get("min_samples")
        if type(value) is int:
            return value
    return 20


def _read(root):
    try:
        from . import planner_telemetry
    except ImportError:
        return [], 0, None
    rows, malformed = planner_telemetry.read_invocations_with_malformed(root)
    return rows, malformed, planner_telemetry


def group_key(row):
    return "|".join(str(row.get(key) or "") for key in
                    ("model", "decision_type", "band", "task_class")) + (
                        "|arch" if row.get("architectural") else "|plain")


def _fix_target(task):
    return (task.get("constraints") or {}).get("fix_round_for")


def _lineage_root(task, tasks):
    """Same edge walk as attribution.lineage, against the supplied root's snapshot."""
    tid = task["id"]
    seen = {tid}
    while target := _fix_target(task):
        if target in seen:
            break
        seen.add(target)
        tid = target
        task = tasks.get(target, {})
    return tid


def _ownership(rows, tasks):
    """Exclusive execute-task sets indexed by invocation launch_id."""
    owned = {row["launch_id"]: set() for row in rows}
    owners = {}
    by_goal = defaultdict(list)
    for row in rows:
        if row.get("goal_id") and _stamp(row.get("started_at")) is not None:
            by_goal[row["goal_id"]].append(row)

    def latest(task, held_target=None):
        created = _stamp(task.get("created_at"))
        if created is None:
            return None
        candidates = [row for row in by_goal[task.get("parent")]
                      if _stamp(row["started_at"]) <= created
                      and (held_target is None or
                           (row.get("kind") == "held" and
                            (row.get("payload_keys") or [None])[0] == held_target))]
        return max(candidates, key=lambda row: (_stamp(row["started_at"]),
                                                str(row["launch_id"])))["launch_id"] if candidates else None

    execute = [task for task in tasks.values() if task.get("role") == "execute"]
    for task in execute:
        if not _fix_target(task):
            owners[task["id"]] = latest(task)
    for task in execute:
        if _fix_target(task):
            owner = latest(task, _fix_target(task))
            owners[task["id"]] = owner if owner is not None else owners.get(_lineage_root(task, tasks))
    for tid, owner in owners.items():
        if owner is not None:
            owned[owner].add(tid)
    return owned


def _mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _verdict(task):
    return task.get("review_verdict") or (task.get("result") or {}).get("verdict")


def build(root=STATE, *, cfg=None, min_samples=None):
    root = Path(root)
    cfg = _config(root, cfg)
    minimum = _min_samples(cfg, min_samples)
    rows, malformed, telemetry = _read(root)
    card = {"groups": {}, "n": len(rows), "malformed": malformed,
            "min_samples": minimum, "read_side_only": True}
    if telemetry is None:
        card["error"] = "planner_telemetry unavailable"
        return card
    tasks = _tasks(root)
    owned = _ownership(rows, tasks)
    children = defaultdict(set)
    for task in tasks.values():
        if task.get("role") == "execute" and _fix_target(task):
            children[_lineage_root(task, tasks)].add(task["id"])
    try:
        interactive = telemetry.interactive_by_goal(root=root, cfg=cfg)
    except Exception:
        interactive = []

    def human(row):
        started = _stamp(row.get("started_at"))
        if started is None:
            return False
        from .pool import TZ
        day = datetime.fromtimestamp(started, TZ).date().isoformat()
        return any(item.get("goal_id") == row.get("goal_id") and
                   any(d > day for d in item.get("days", [])) for item in interactive)

    grouped = defaultdict(list)
    for row in rows:
        grouped[group_key(row)].append(row)
    for key, members in grouped.items():
        ids = set().union(*(owned[row["launch_id"]] for row in members))
        roots = [tasks[tid] for tid in ids if not _fix_target(tasks[tid])]
        terminal = [task for task in roots if task.get("merged_into") or task.get("status") == "failed"
                    or (task.get("pipeline") or {}).get("first_green_at") is not None]
        fixes = [max((task.get("pipeline") or {}).get("lineage_fix_rounds") or 0,
                     len(children[task["id"]])) for task in terminal]
        reviews = [task for task in tasks.values()
                   if (task.get("inputs") or [None])[0] in ids]
        human_flags = [human(row) for row in members]
        card["groups"][key] = {
            "n": len(members),
            "material_rate": _mean(bool(row.get("material")) for row in members),
            "downstream_accepted_rate": _mean(bool(task.get("merged_into")) for task in roots),
            "first_pass_rate": _mean(n == 0 for n in fixes),
            "fix_round_rate": _mean(n > 0 for n in fixes),
            "avg_fix_rounds": _mean(fixes),
            "gate_red_rate": _mean(((tasks[tid].get("pipeline") or {}).get("gate_reds") or 0) > 0
                                   for tid in ids),
            "review_request_changes_rate": _mean(_verdict(task) == "request_changes" for task in reviews
                                                 if task.get("role") == "review"),
            "spec_review_approval_rate": _mean(_verdict(task) == "approve" for task in reviews
                                              if task.get("role") == "spec_review"),
            "replanning_rate": _mean(any(other.get("goal_id") == row.get("goal_id") and
                                         other.get("decision_type") == "architectural_replan" and
                                         _stamp(other.get("started_at")) is not None and
                                         _stamp(row.get("started_at")) is not None and
                                         _stamp(other["started_at"]) > _stamp(row["started_at"])
                                         for other in rows) for row in members),
            "reescalation_rate": _mean(bool(row.get("reescalation")) for row in members),
            "human_intervention_rate": _mean(human_flags) if any(human_flags) else None,
            "tokens_mean": _mean(row.get("total_tokens") for row in members),
            "usd_mean": _mean(row.get("usd") for row in members),
            "latency_mean": _mean(row.get("latency_s") for row in members),
            "terminal_roots": len(terminal),
            "evidence": "sufficient" if len(members) >= minimum else "insufficient",
        }
    return card


def class_evidence(model, decision_type, band, task_class, architectural, *,
                   root=STATE, cfg=None, min_samples=None):
    cfg = _config(root, cfg)
    card = build(root, cfg=cfg, min_samples=min_samples)
    dimensions = dict(decision_type=decision_type, band=band, task_class=task_class,
                      architectural=architectural)
    baseline = promotion._table(cfg, "models").get("planner")
    if not baseline:
        rows, _, _ = _read(root)
        baseline = next((row.get("model") for row in rows if row.get("tier") == "fable"
                         and group_key({**row, "model": ""}) == group_key(dimensions)), None)
    candidate = card["groups"].get(group_key({**dimensions, "model": model}), {})
    reference = card["groups"].get(group_key({**dimensions, "model": baseline}), {})
    result = {"n": candidate.get("n", 0), "noninferior": None, "deltas": {}, "failed": [],
              "reescalation_rate": candidate.get("reescalation_rate"), "baseline_model": baseline}
    if min(candidate.get("n", 0), reference.get("n", 0)) < card["min_samples"]:
        return result
    missing = False
    criteria = {**CRITERIA_MAP, "review_request_changes_rate":
                (None, "<=", REVIEW_CHANGES_MAX_DELTA)}
    for metric, (config_key, sign, default) in criteria.items():
        if metric == "gate_success":
            a, b = candidate.get("gate_red_rate"), reference.get("gate_red_rate")
            a, b = (1 - a if a is not None else None), (1 - b if b is not None else None)
        else:
            a, b = candidate.get(metric), reference.get(metric)
        if a is None or b is None:
            result["deltas"][metric] = None
            missing = True
            continue
        delta = a - b
        result["deltas"][metric] = delta
        limit = promotion._table(cfg, "promotion").get(config_key, default) if config_key else default
        if (sign == ">=" and delta < limit - 1e-12) or (sign == "<=" and delta > limit + 1e-12):
            result["failed"].append(metric)
    if (result["reescalation_rate"] or 0) > REESCALATION_MAX:
        result["failed"].append("reescalation")
    result["noninferior"] = False if result["failed"] else (None if missing else True)
    return result


def economics(root=STATE):
    root = Path(root)
    rows, _, _ = _read(root)
    owned = _ownership(rows, _tasks(root))
    costs = scorecard.by_task(root=root)
    accepted = set(scorecard.accepted_goals(root))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("model")].append(row)
    result = {}
    for model, members in grouped.items():
        accepted_rows = [row for row in members if row.get("goal_id") in accepted]
        goals = {row["goal_id"] for row in accepted_rows}
        pipeline, totals = [], defaultdict(list)
        for row in members:
            usd = sum(costs.get(tid, {}).get("usd", 0) or 0 for tid in owned[row["launch_id"]])
            pipeline.append(usd)
            totals[row.get("decision_type")].append((row.get("usd") or 0) + usd)
        result[model] = {
            "planner_usd_per_accepted_goal": sum(row.get("usd") or 0 for row in accepted_rows) / len(goals) if goals else None,
            "planner_tokens_per_accepted_goal": sum(row.get("total_tokens") or 0 for row in accepted_rows) / len(goals) if goals else None,
            "pipeline_usd_after_planning": _mean(pipeline),
            "expected_total_cost_after_planning": {key: _mean(values) for key, values in totals.items()},
        }
    return result


def format(card):
    lines = []
    if not card["n"]:
        lines.append("read-side only: rows appear once planner_runs emits invocations")
    if card.get("error"):
        lines.append(card["error"])
    columns = ("n", "material_rate", "downstream_accepted_rate", "first_pass_rate",
               "avg_fix_rounds", "reescalation_rate", "evidence")
    lines.append("group\t" + "\t".join(columns))
    for key, metrics in sorted(card["groups"].items()):
        lines.append(key + "\t" + "\t".join(str(metrics.get(col)) for col in columns))
    return "\n".join(lines)


def format_evidence(evidence):
    return (f"n={evidence['n']} baseline={evidence['baseline_model']} "
            f"noninferior={evidence['noninferior']} failed={','.join(evidence['failed']) or '-'}")
