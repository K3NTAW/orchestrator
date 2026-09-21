"""Expected-value executor allocation with hard eligibility boundaries."""

import tomllib

from . import STATE, critical_path, decision_log, scorecard


DEFAULTS = {
    "mode": "shadow",
    "min_samples": 5,
    "latency_weight_usd_per_hour": 2.0,
    "critical_factor": 1.0,
    "non_critical_factor": 0.25,
}


def _config(cfg):
    if cfg is None:
        try:
            cfg = tomllib.loads((STATE / "pool.toml").read_text()).get("allocation", {})
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            cfg = {}
    elif "allocation" in cfg:
        cfg = cfg["allocation"]
    result = {**DEFAULTS, **(cfg or {})}
    if result["mode"] not in ("off", "shadow", "active"):
        result["mode"] = "shadow"
    return result


def evidence_for(executor_ids, task_class, *, economics=None, root=STATE):
    """Return class-scoped economics for each executor.

    ``first_pass_p`` deliberately uses ``1 - fix_round_probability`` rather
    than first_pass_green_rate because fix-round probability covers every
    resolved lineage.  Since economics has no initial/fix cost split, expected
    rework is represented by the fix-round share of accepted cost.
    """
    if economics is None:
        economics = scorecard.executor_economics(root=root, by="class")
    result = {}
    for executor_id in executor_ids:
        row = economics.get((executor_id, task_class))
        if row is None:
            result[executor_id] = {
                "cost_to_accepted_usd": None, "first_pass_p": None,
                "expected_rework_usd": None, "n": 0,
            }
            continue
        cost = row.get("cost_to_accepted")
        fix_p = row.get("fix_round_probability")
        result[executor_id] = {
            "cost_to_accepted_usd": cost,
            "first_pass_p": 1 - fix_p if fix_p is not None else None,
            "expected_rework_usd": cost * fix_p if cost is not None and fix_p is not None else None,
            "n": row.get("n_tasks", 0),
        }
    return result


def _empty_result(baseline, mode, reason, executor=None):
    return {"executor": executor, "mode": mode, "reason": reason, "candidates": [],
            "rejected": {}, "baseline": baseline, "would_pick": None}


def choose(task, eligible_ids, *, baseline, priority, ready_priorities, evidence,
           durations=None, cfg=None):
    """Choose an eligible executor using accepted-cost plus weighted latency."""
    settings = _config(cfg)
    mode = settings["mode"]
    eligible_ids = list(dict.fromkeys(eligible_ids))
    if not eligible_ids:
        return _empty_result(baseline, mode, "no_eligible")
    if baseline not in eligible_ids:
        raise ValueError("baseline must be eligible")

    current_path = priority["critical_path_s"]
    factor = (settings["critical_factor"]
              if current_path >= max(ready_priorities) else settings["non_critical_factor"])
    fallback_duration = critical_path.est_duration_s(task)
    candidates = []
    for executor_id in eligible_ids:
        row = evidence.get(executor_id) or {}
        cost = row.get("cost_to_accepted_usd")
        n = row.get("n", 0) or 0
        measured = n >= settings["min_samples"] and cost is not None
        duration = (durations or {}).get(executor_id, fallback_duration)
        score = (cost + settings["latency_weight_usd_per_hour"] * duration / 3600 * factor
                 if measured else None)
        candidates.append({
            "id": executor_id, "score_usd": score, "cost_to_accepted_usd": cost,
            "first_pass_p": row.get("first_pass_p"), "est_duration_s": duration,
            "n": n, "measured": measured,
        })

    measured = [candidate for candidate in candidates if candidate["measured"]]
    rejected = {candidate["id"]: "sparse_evidence" for candidate in candidates
                if not candidate["measured"]}
    if len(measured) < 2:
        return {"executor": baseline, "mode": mode, "reason": "sparse_evidence",
                "candidates": candidates, "rejected": rejected, "baseline": baseline,
                "would_pick": None}

    best_score = min(candidate["score_usd"] for candidate in measured)
    tied = [candidate for candidate in measured
            if candidate["score_usd"] <= best_score * 1.05]
    picked = min(tied, key=lambda candidate: (
        candidate["cost_to_accepted_usd"], candidate["id"] != baseline, candidate["id"]))["id"]
    rejected.update({candidate["id"]: "higher_expected_value_cost"
                     for candidate in measured if candidate["id"] != picked})
    if mode == "off":
        return {"executor": baseline, "mode": mode, "reason": "mode_off",
                "candidates": candidates, "rejected": rejected, "baseline": baseline,
                "would_pick": None}
    if mode == "shadow":
        return {"executor": baseline, "mode": mode, "reason": "shadow",
                "candidates": candidates, "rejected": rejected, "baseline": baseline,
                "would_pick": picked}
    return {"executor": picked, "mode": mode, "reason": "expected_value",
            "candidates": candidates, "rejected": rejected, "baseline": baseline,
            "would_pick": picked}


def record(task_id, result):
    """Record allocation evidence; capacity and budget eligibility are enforced upstream."""
    measured_n = [candidate["n"] for candidate in result["candidates"] if candidate["measured"]]
    historical = {candidate["id"]: {
        key: candidate[key] for key in ("cost_to_accepted_usd", "first_pass_p", "n")
    } for candidate in result["candidates"]}
    return decision_log.record(
        kind="allocation", subject=task_id,
        candidates=[candidate["id"] for candidate in result["candidates"]],
        hard_constraints=["eligibility"], deterministic={"baseline": result["baseline"]},
        historical=historical, selected=result["executor"], rejected=result["rejected"],
        reason=result["reason"], n=min(measured_n) if measured_n else None, mode=result["mode"],
    )
