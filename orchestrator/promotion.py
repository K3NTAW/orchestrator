"""Evidence-based, shadow-first feature promotion recommendations."""

from collections import OrderedDict
import json
from pathlib import Path
import tomllib

from . import STATE, decision_log


FEATURES = OrderedDict((
    ("steering_policy", {"table": "steering", "key": "mode", "modes": ("off", "shadow", "active"),
                         "default": "shadow", "evidence": "steering_policy",
                         "criteria": {"min_shadow_samples": 20, "fix_rounds_delta": "<0",
                                      "accepted_tokens_delta": "<=0"}}),
    ("memory_tiers", {"table": "memory", "key": "mode",
                      "modes": ("off", "shadow", "active"),
                      "default": "shadow", "evidence": "retrieval"}),
    ("contracts", {"table": "contracts", "key": "mode", "modes": ("off", "shadow", "active"),
                   "default": "shadow", "evidence": "contracts",
                   "criteria": {"min_shadow_samples": 30, "repair_success_rate": .9,
                                "failed_results_delta": 0}}),
    ("fast_path", {"table": "harness", "key": "depth_mode",
                   "modes": ("off", "shadow", "active"),
                   "default": "shadow", "evidence": "fast_path"}),
    ("jev_routing", {"table": "jev.routing", "key": "mode", "modes": ("off", "shadow", "active"),
                      "default": "shadow", "evidence": "jev_routing"}),
    ("scheduler", {"table": "scheduler", "key": "mode", "modes": ("off", "shadow", "active"),
                    "default": "shadow", "evidence": "scheduler"}),
    ("jev_sched", {"table": "scheduler", "key": "jev_mode", "modes": ("off", "shadow", "active"),
                    "default": "shadow", "evidence": "scheduler"}),
    ("allocation", {"table": "allocation", "key": "mode", "modes": ("off", "shadow", "active"),
                     "default": "shadow", "evidence": "allocation"}),
    ("strategy", {"table": "strategy", "key": "mode", "modes": ("off", "shadow", "active"),
                   "default": "shadow", "evidence": "strategy"}),
    ("speculation", {"table": "speculation", "key": "mode", "modes": ("off", "shadow", "active"),
                      "default": "off", "evidence": "speculation"}),
    ("planner_routing", {"table": "planner.routing", "key": "mode",
                         "modes": ("off", "shadow", "active"),
                         "default": "shadow", "evidence": "planner_routing"}),
    ("context_router", {"table": "context_router", "key": "mode",
                        "modes": ("off", "shadow", "active"),
                        "default": "shadow", "evidence": "context_router"}),
    ("tool_disclosure", {"table": "tool_disclosure", "key": "mode",
                         "modes": ("off", "shadow", "active"),
                         "default": "shadow", "evidence": "tool_disclosure"}),
    ("conditional_instructions", {"table": "instructions", "key": "mode",
                                  "modes": ("off", "shadow", "active"),
                                  "default": "shadow", "evidence": "conditional_instructions"}),
    ("handoff_routing", {"table": "handoff", "key": "mode",
                          "modes": ("off", "shadow", "active"),
                          "default": "shadow", "evidence": "handoff_routing"}),
    ("skill_routing", {"table": "skills", "key": "mode",
                       "modes": ("off", "shadow", "active"),
                       "default": "shadow", "evidence": "skill_routing"}),
    ("jev_skill_routing", {"table": "skills", "key": "jev_mode",
                           "modes": ("off", "shadow", "active"),
                           "default": "shadow", "evidence": "jev_skill_routing"}),
    ("read_suppression", {"table": "jev", "key": "read_suppression",
                           "modes": ("off", "shadow", "active"),
                           "default": "shadow", "evidence": "read_suppression"}),
))

# Cache-aware disclosure uses independent, single-key rollout controls.
HERMES_FEATURES = {
    "context_cache": ("context_router", "cache_mode", "context_selection", 30),
    "tool_cache": ("tool_disclosure", "cache_mode", "tool_disclosure", 30),
    "skill_cache": ("skills", "cache_mode", "skill_selection", 30),
    "stale_steering": ("steering", "stale_mode", "steering", 20),
}
for _name, (_section, _key, _kind, _minimum) in HERMES_FEATURES.items():
    FEATURES[_name] = {"table": _section, "key": _key, "evidence": _kind,
                       "modes": ("off", "shadow", "active"), "default": "shadow",
                       "criteria": {"min_shadow_samples": _minimum, "first_pass_delta": ">=0",
                                    "fix_rounds_delta": "<=0", "accepted_economics_delta": "<0",
                                    "hermes_eval_max_days": 7, "demotion_tasks": 10}}


CRITERIA = {
    "min_samples": 20,
    "first_pass_delta": -0.02,
    "fix_rounds_delta": 0.05,
    "gate_success_delta": -0.02,
    "review_findings_delta": -0.10,
    "security_ok": True,
}


def _table(cfg, path):
    value = cfg or {}
    for part in path.split("."):
        if not isinstance(value, dict):
            return {}
        value = value.get(part, {})
    return value if isinstance(value, dict) else {}


def current_mode(feature, cfg):
    """Return ``(mode, flags)`` without mutating the supplied pool config."""
    spec = FEATURES[feature]
    value = _table(cfg, spec["table"]).get(spec["key"], spec["default"])
    if value not in spec["modes"]:
        return ("off" if feature == "steering_policy" else spec["default"]), ["invalid_config"]
    return value, []


def mode(feature, cfg=None):
    """Return a feature's configured mode, loading pool.toml when omitted."""
    if cfg is None:
        try:
            cfg = tomllib.loads((Path(STATE) / "pool.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    return current_mode(feature, cfg)[0]


def _criteria(cfg):
    result = dict(CRITERIA)
    override = (cfg or {}).get("promotion", {}) if isinstance(cfg, dict) else {}
    if isinstance(override, dict):
        result.update({key: value for key, value in override.items() if key in result})
    return result


def evaluate(feature, evidence, cfg=None, *, root=None, now=None):
    """Evaluate evidence and return a recommendation; never changes configuration."""
    if feature not in FEATURES:
        raise KeyError(feature)
    evidence = evidence if isinstance(evidence, dict) else {}
    mode, mode_flags = current_mode(feature, cfg)
    criteria = _criteria(cfg)
    n = evidence.get("n", 0) or 0
    reasons = list(mode_flags)
    if feature in HERMES_FEATURES:
        from . import hermes_eval
        spec = FEATURES[feature]["criteria"]
        if root is None:
            reasons.append("eval_root_missing")
        elif not hermes_eval.fresh(root, now=now):
            reasons.append("hermes_eval_missing_or_stale")
            return {"feature": feature, "mode": mode, "n": n, "recommendation": "stay",
                    "reasons": reasons, "criteria": spec, "evidence": evidence}
        if evidence.get("invalid_config"):
            reasons.append("invalid_config")
        if evidence.get("shadow_n", 0) < spec["min_shadow_samples"]:
            reasons.append("insufficient_evidence")
        if any(evidence.get(key) is None for key in ("first_pass_delta", "fix_rounds_delta")):
            reasons.append("shadow_quality_unmeasured")
        elif evidence["first_pass_delta"] < 0 or evidence["fix_rounds_delta"] > 0:
            reasons.append("quality_regression")
        if not any(isinstance(evidence.get(key), (int, float)) and evidence[key] < 0
                   for key in ("accepted_tokens_delta", "accepted_cost_delta")):
            reasons.append("missing_cost_improvement")
        demote = mode == "active" and evidence.get("active_n", 0) >= 10 and evidence.get("last10_regression")
        if demote:
            reasons.append("last10_quality_regression")
        blocking = [reason for reason in reasons if reason != "eval_root_missing"]
        return {"feature": feature, "mode": mode, "n": n,
                "recommendation": "demote" if demote else "stay" if blocking else "promote",
                "reasons": reasons, "criteria": spec, "evidence": evidence}
    if (feature == "memory_tiers" and mode == "active"
            and evidence.get("active_n", 0) >= 10 and evidence.get("last10_regression")):
        return {"feature": feature, "mode": mode, "n": n, "recommendation": "demote",
                "reasons": ["last10_quality_regression"], "criteria": criteria}
    if feature == "steering_policy":
        if evidence.get("shadow_n", 0) < 20:
            reasons.append("insufficient_evidence")
        if mode == "active" and evidence.get("active_n", 0):
            fixes, tokens = evidence.get("fix_rounds_delta"), evidence.get("accepted_tokens_delta")
            if fixes is None or fixes >= 0:
                reasons.append("fix_rounds_not_lower_than_shadow")
            if tokens is None or tokens > 0:
                reasons.append("token_cost_unmeasured_or_increased")
        return {"feature": feature, "mode": mode, "n": n,
                "recommendation": "stay" if reasons else "promote", "reasons": reasons,
                "criteria": FEATURES[feature]["criteria"], "evidence": evidence}
    if feature == "contracts":
        contract_criteria = FEATURES[feature]["criteria"]
        if evidence.get("shadow_n", n) < contract_criteria["min_shadow_samples"]:
            reasons.append("insufficient_evidence")
        if evidence.get("repair_success_rate") is None or evidence["repair_success_rate"] < .9:
            reasons.append("repair_success_unmeasured_or_low")
        delta = evidence.get("failed_results_delta")
        if delta is None or delta > 0:
            reasons.append("failed_results_unmeasured_or_increased")
        return {"feature": feature, "mode": mode, "n": n,
                "recommendation": "demote" if mode == "active" and delta is not None and delta > 0
                                  else "stay" if reasons else "promote",
                "reasons": reasons, "criteria": contract_criteria, "evidence": evidence}
    if feature == "fast_path":
        if evidence.get("two_fix_rounds"):
            reasons.append("fast_path_two_fix_rounds")
        elif n < 20:
            reasons.append("insufficient_evidence")
        elif evidence.get("active_n", 0):
            if evidence.get("first_pass_delta") is None or evidence["first_pass_delta"] < 0:
                reasons.append("first_pass_below_shadow_or_unknown")
            if evidence.get("accepted_tokens_delta") is None or evidence["accepted_tokens_delta"] >= 0:
                reasons.append("tokens_not_lower_than_shadow")
        return {"feature": feature, "mode": mode, "n": n,
                "recommendation": ("demote" if mode == "active" and evidence.get("two_fix_rounds")
                                   else "stay") if reasons else "promote",
                "reasons": reasons, "criteria": {"min_shadow_samples": 20,
                    "first_pass_delta": 0, "accepted_tokens_delta": "<0", "max_fix_rounds": 1}}
    shadow_features = {"context_router", "tool_disclosure", "conditional_instructions", "handoff_routing"}
    if n < criteria["min_samples"]:
        reasons.append("insufficient_evidence")
        return {"feature": feature, "mode": mode, "n": n, "recommendation": "stay",
                "reasons": reasons, "criteria": criteria}
    if feature == "memory_tiers" and evidence.get("fix_rounds_delta") is None:
        reasons.append("shadow_quality_unmeasured")
        return {"feature": feature, "mode": mode, "n": n, "recommendation": "stay",
                "reasons": reasons, "criteria": criteria}
    if feature in shadow_features:
        if evidence.get("suite_present") is False:
            reasons.append("context_eval_missing")
        elif evidence.get("suite_present") and not evidence.get("suite_passed"):
            reasons.append("context_eval_failed")
        if any(evidence.get(key) is None for key in
               ("first_pass_delta", "fix_rounds_delta", "gate_success_delta", "review_findings_delta")):
            reasons.append("shadow_quality_unmeasured")
            return {"feature": feature, "mode": mode, "n": n,
                    "recommendation": "stay", "reasons": reasons, "criteria": criteria}

    quality = (
        ("first_pass", evidence.get("first_pass_delta"), lambda value: value < criteria["first_pass_delta"]),
        ("fix_rounds", evidence.get("fix_rounds_delta"), lambda value: value > criteria["fix_rounds_delta"]),
        ("gate_success", evidence.get("gate_success_delta"), lambda value: value < criteria["gate_success_delta"]),
        ("review_findings", evidence.get("review_findings_delta"), lambda value: value < criteria["review_findings_delta"]),
    )
    regressions = [name for name, value, bad in quality if value is not None and bad(value)]
    if evidence.get("security_ok") is not None and evidence.get("security_ok") is not criteria["security_ok"]:
        regressions.append("security")
    if regressions:
        reasons.extend("quality_regression:" + name for name in regressions)
        return {"feature": feature, "mode": mode, "n": n,
                "recommendation": "demote" if mode == "active" and feature != "memory_tiers" else "stay",
                "reasons": reasons, "criteria": criteria}

    if feature == "planner_routing":
        if (evidence.get("classes_inferior", 0) or 0) > 0:
            reasons.append("inferior_planner_class")
            return {"feature": feature, "mode": mode, "n": n,
                    "recommendation": "demote" if mode == "active" else "stay",
                    "reasons": reasons, "criteria": criteria}
        if (evidence.get("classes_noninferior", 0) or 0) < 1:
            reasons.append("insufficient_planner_classes")
            return {"feature": feature, "mode": mode, "n": n, "recommendation": "stay",
                    "reasons": reasons, "criteria": criteria}

    improvements = [key for key in ("accepted_cost_delta", "accepted_tokens_delta", "latency_delta")
                    if isinstance(evidence.get(key), (int, float)) and evidence[key] < 0]
    if not improvements:
        if evidence.get("jev_disagreement_rate") is not None:
            reasons.append("jev_disagreement_not_promotion_criterion")
        reasons.append("missing_cost_improvement")
        return {"feature": feature, "mode": mode, "n": n, "recommendation": "stay",
                "reasons": reasons, "criteria": criteria}
    return {"feature": feature, "mode": mode, "n": n, "recommendation": "promote",
            "reasons": reasons, "criteria": criteria}


def _jsonl(path):
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def collect(feature, root=STATE):
    """Collect available telemetry, tolerating missing and malformed state."""
    root = Path(root)
    if feature in HERMES_FEATURES:
        return _collect_hermes(feature, root)
    if feature == "steering_policy":
        return _collect_steering(root)
    if feature == "memory_tiers":
        rows = [row for row in decision_log.read_all(root=root)
                if row.get("kind") == "retrieval"]
        legacy = sum((row.get("extra") or {}).get("tokens_legacy", 0) for row in rows)
        tiered = sum((row.get("extra") or {}).get("tokens_tiered", 0) for row in rows)
        # A task contributes once, in its most recently observed mode.
        modes = {row["subject"]: row.get("mode") for row in rows if row.get("subject")}
        tasks = []
        for path in (root / "tasks").glob("*.json"):
            try:
                task = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(task, dict):
                tasks.append(task)
        fixes = {}
        for task in tasks:
            parent = (task.get("constraints") or {}).get("fix_round_for")
            if parent:
                fixes[parent] = fixes.get(parent, 0) + 1
        cohorts = {mode: [fixes.get(task_id, 0) for task_id, observed in modes.items()
                          if observed == mode] for mode in ("active", "shadow")}
        delta = None
        if all(len(values) >= 5 for values in cohorts.values()):
            delta = (sum(cohorts["active"]) / len(cohorts["active"])
                     - sum(cohorts["shadow"]) / len(cohorts["shadow"]))
        return {"n": len(rows), "tokens_legacy": legacy, "tokens_tiered": tiered,
                "accepted_tokens_delta": (tiered - legacy) / legacy if legacy else None,
                "fix_rounds_delta": delta,
                **{key: value for key, value in _cohort_metrics(rows, root, economics=False).items()
                   if key in ("active_n", "last10_regression")}}
    if feature == "contracts":
        rows = [row for row in decision_log.read_all(root=root)
                if row.get("kind") == "output_contract"]
        n = len(rows)
        repairs = [row for row in rows if not (row.get("extra") or {}).get("raw_ok", True)]
        # Compare actual task outcomes across recorded modes, never schema failures.
        rates = {}
        for mode_name in ("shadow", "active"):
            ids = {row.get("subject") for row in rows if row.get("mode") == mode_name}
            statuses = []
            for tid in ids:
                if not isinstance(tid, str) or Path(tid).name != tid:
                    continue
                try:
                    task = json.loads((root / "tasks" / (tid + ".json")).read_text())
                except (OSError, ValueError):
                    continue
                if task.get("status") in ("done", "failed"):
                    statuses.append(task["status"])
            rates[mode_name] = statuses.count("failed") / len(statuses) if statuses else None
        return {"n": n, "shadow_n": sum(row.get("mode") == "shadow" for row in rows),
                "ok_rate": sum(bool((row.get("extra") or {}).get("raw_ok")) for row in rows) / n if n else None,
                "repair_rate": sum(row.get("selected") == "repair" for row in rows) / n if n else None,
                "repair_success_rate": sum(bool((row.get("deterministic") or {}).get("ok")) or
                    (row.get("extra") or {}).get("repaired_by") == "model" for row in repairs) / len(repairs)
                    if repairs else None,
                "failed_results_delta": rates["active"] - rates["shadow"]
                    if all(value is not None for value in rates.values()) else None}
    if feature == "fast_path":
        from . import harness_depth
        return harness_depth.promotion_evidence(root)
    if feature == "jev_skill_routing":
        rows = [row for row in decision_log.read_all(root=Path(root))
                if row.get("kind") == "skill_selection" and isinstance(row.get("jev"), dict)]
        return {"n": len(rows)}
    if feature == "skill_routing":
        rows = [row for row in decision_log.read_all(root=root)
                if row.get("kind") == "skill_selection"
                and row.get("mode") in ("shadow", "active")
                and row.get("reason") != "stage1 static"]
        return {"n": len(rows)}
    if feature == "read_suppression":
        from . import read_economy
        total = read_economy.summary(root, since_s=__import__("time").time() - 7 * 86400)["total"]
        return {"n": total["would_suppress"], **total}
    shadow_keys = {"context_router": "routed_tokens", "tool_disclosure": "tool_tokens_minimal",
                   "conditional_instructions": "instruction_tokens_modular"}
    if feature in {*shadow_keys, "handoff_routing"}:
        contexts = []
        for path in (root / "runs").glob("*.jsonl") if (root / "runs").exists() else ():
            contexts.extend(row.get("context") for row in _jsonl(path) if isinstance(row.get("context"), dict))
        if feature == "handoff_routing":
            from . import handoff_scorecard
            rows = [row for row in decision_log.read_all(root=root) if row.get("kind") == "handoff"]
            n = max(len(rows), len(handoff_scorecard.lineage_rows(root)))
            reductions = [(row.get("deterministic") or {}).get("reduction_ratio") for row in rows]
        else:
            key = shadow_keys[feature]
            measured = [row for row in contexts if row.get(key) is not None]
            n = len(measured)
            reductions = [row.get("routed_reduction_ratio") for row in measured]
            if feature == "tool_disclosure":
                reductions = [1 - row["tool_tokens_minimal"] / row["tool_tokens_disclosed"]
                              for row in measured if row.get("tool_tokens_disclosed")]
            elif feature == "conditional_instructions":
                reductions = [1 - row["instruction_tokens_modular"] / row["instruction_tokens"]
                              for row in measured if row.get("instruction_tokens")]
            else:
                reductions = [1 - value for value in reductions if isinstance(value, (int, float))]
        eval_path = root / "context_eval.json"
        try:
            suite = json.loads(eval_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            suite = None
        passed = bool(suite and suite.get("suite_passed"))
        from . import context_scorecard
        recoveries = context_scorecard.recovery(root)
        recovery_reads = sum(row["recovery_reads"] for row in recoveries.values())
        hidden_items = sum(row["hidden_items"] for row in recoveries.values())
        result = {"n": n, "first_pass_delta": None, "fix_rounds_delta": None,
                  "gate_success_delta": None, "review_findings_delta": None,
                  "accepted_cost_delta": None, "accepted_tokens_delta": None, "latency_delta": None,
                  "security_ok": None, "suite_present": suite is not None, "suite_passed": passed,
                  "context_recovery_rate": recovery_reads / hidden_items if hidden_items else 0, "context_recovery_n": hidden_items}
        if passed and reductions:
            result["accepted_tokens_delta"] = -(sum(reductions) / len(reductions))
        return result
    if feature == "planner_routing":
        try:
            import tomllib
            from . import planner_scorecard, planner_shadow, planner_telemetry
        except ImportError:
            return {"n": 0}
        try:
            cfg = tomllib.loads((root / "pool.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        rows = planner_telemetry.read_invocations(root)
        tier = _table(cfg, "planner.routing").get("default_tier", "opus")
        candidates = [row for row in rows if row.get("tier") == tier]
        shadow = planner_shadow.summary(root)
        result = {"n": len(candidates), "shadow_n": shadow.get("n", 0),
                  "classes_noninferior": 0, "classes_inferior": 0,
                  "classes_insufficient": 0,
                  "shadow_agreement_rate": shadow.get("agreement_rate")}
        model = _table(cfg, "models").get(tier)
        dimensions = {(row.get("decision_type"), row.get("band"), row.get("task_class"),
                       bool(row.get("architectural"))) for row in candidates}
        evidence_rows = [planner_scorecard.class_evidence(
            model, decision_type, band, task_class, architectural, root=root, cfg=cfg)
            for decision_type, band, task_class, architectural in dimensions]
        for item in evidence_rows:
            bucket = ("classes_noninferior" if item.get("noninferior") is True else
                      "classes_inferior" if item.get("noninferior") is False else
                      "classes_insufficient")
            result[bucket] += 1
        delta_keys = {"first_pass_rate": "first_pass_delta",
                      "avg_fix_rounds": "fix_rounds_delta",
                      "gate_success": "gate_success_delta",
                      "review_request_changes_rate": "review_findings_delta"}
        for source, target in delta_keys.items():
            values = [item.get("deltas", {}).get(source) for item in evidence_rows]
            values = [value for value in values if isinstance(value, (int, float))]
            result[target] = sum(values) / len(values) if values else None
        return result
    if feature == "jev_routing":
        rows = []
        runs = root / "runs"
        if runs.exists():
            for path in runs.glob("*.jsonl"):
                rows.extend(row for row in _jsonl(path) if row.get("role") == "jev_route")
        result = {"n": len(rows)}
        if rows:
            result["jev_disagreement_rate"] = sum(not row.get("agrees") for row in rows) / len(rows)
            tasks = {}
            for path in (root / "tasks").glob("*.json"):
                try:
                    task = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(task, dict):
                    tasks[task.get("id", path.stem)] = task
            first_pass = []
            merged = []
            for row in rows:
                task = tasks.get(row.get("task"), {})
                pipeline = task.get("pipeline") or {}
                fixes = pipeline.get("lineage_fix_rounds", task.get("lineage_fix_rounds"))
                if fixes is not None:
                    first_pass.append(fixes == 0)
                if "merged_into" in task:
                    merged.append(bool(task.get("merged_into")))
            if first_pass:
                result["first_pass_rate"] = sum(first_pass) / len(first_pass)
            if merged:
                result["accepted_rate"] = sum(merged) / len(merged)
        return result
    if feature == "jev_sched":
        rows = [row for row in decision_log.read_all(root=root) if row.get("kind") == "jev_sched"]
        return {"n": len(rows),
                "jev_disagreement_rate": (sum(bool(row.get("rejected")) for row in rows) / len(rows)
                                          if rows else 0),
                "applied": sum(bool(row.get("selected")) for row in rows)}
    if feature == "scheduler":
        waves = _jsonl(root / "runs" / "sched" / "waves.jsonl")
        stale = _jsonl(root / "runs" / "sched" / "stale.jsonl")
        return {"n": len(waves), "applied": sum(bool(row.get("applied")) for row in waves),
                "stale": len(stale)}
    return {"n": 0}


def report(cfg=None, root=STATE):
    return [evaluate(feature, collect(feature, root=root), cfg=cfg, root=root) for feature in FEATURES]


def format_report(rows):
    return "\n".join("{feature}: {recommendation} ({mode}, n={n}){reasons}".format(
        **{key: value for key, value in row.items() if key != "reasons"},
        reasons=(" — " + ", ".join(row.get("reasons", [])) if row.get("reasons") else ""))
        for row in rows)


def _collect_steering(root):
    """Compare observed lineages over the decision log's retained window."""
    from . import scorecard
    rows = [r for r in decision_log.read_all(root=root) if r.get("kind") == "steering"]
    tasks = {}
    for path in (root / "tasks").glob("*.json"):
        try:
            task = json.loads(path.read_text())
            tasks[task["id"]] = task
        except (OSError, ValueError, KeyError, TypeError):
            continue
    def root_id(tid):
        seen = set()
        while tid in tasks and tid not in seen:
            seen.add(tid)
            parent = (tasks[tid].get("constraints") or {}).get("fix_round_for")
            if not parent:
                break
            tid = parent
        return tid
    fixes = {}
    for tid, task in tasks.items():
        if (task.get("constraints") or {}).get("fix_round_for"):
            ancestor = root_id(tid)
            fixes[ancestor] = fixes.get(ancestor, 0) + 1
    active = {root_id(r["subject"]) for r in rows if r.get("mode") == "active"
              and (r.get("extra") or {}).get("outcome") == "applied"}
    shadow = {root_id(r["subject"]) for r in rows if r.get("mode") == "shadow"} - active
    observed = {root_id(r["subject"]) for r in rows}
    def mean(ids):
        values = [fixes.get(tid, 0) for tid in ids if tid in tasks]
        return sum(values) / len(values) if values else None
    card = scorecard.efficiency(root=root)["tasks"]
    def tokens(ids):
        values = [card[tid]["tokens"] for tid in ids if tid in card and card[tid].get("calls", 0)]
        return sum(values) / len(values) if values else None
    current, baseline = mean(active), mean(shadow)
    current_tokens, baseline_tokens = tokens(active), tokens(shadow)
    candidates = [(r.get("extra") or {}).get("candidate_action", r.get("selected")) for r in rows]
    return {"n": len(rows), "steer": candidates.count("steer"), "cancel": candidates.count("cancel"),
            "shadow_n": sum(r.get("mode") == "shadow" and action in ("steer", "cancel")
                            for r, action in zip(rows, candidates)),
            "active_n": len(active), "mean_fix_rounds_steered": current,
            "mean_fix_rounds_non_steered": mean(observed - active),
            "mean_fix_rounds_shadow": baseline,
            "fix_rounds_delta": current - baseline if None not in (current, baseline) else None,
            "accepted_tokens_delta": current_tokens - baseline_tokens
                if None not in (current_tokens, baseline_tokens) else None}


def _cohort_metrics(rows, root, *, economics=True):
    """Use disjoint task cohorts and a stateless window of ten active subjects."""
    tasks = {}
    for path in (root / "tasks").glob("*.json"):
        try:
            task = json.loads(path.read_text())
            if isinstance(task, dict):
                tasks[task.get("id", path.stem)] = task
        except (OSError, ValueError):
            continue
    rows = sorted(rows, key=lambda row: row.get("ts", 0))
    active = list(dict.fromkeys(row.get("subject") for row in reversed(rows)
                               if row.get("mode") == "active" and row.get("subject") in tasks))
    shadow = {row.get("subject") for row in rows if row.get("mode") == "shadow"
              and row.get("subject") in tasks} - set(active)
    fixes = {}
    for task in tasks.values():
        parent = (task.get("constraints") or {}).get("fix_round_for")
        if parent:
            fixes[parent] = fixes.get(parent, 0) + 1
    def quality(ids):
        values = [fixes.get(tid, 0) for tid in ids]
        return (sum(n == 0 for n in values) / len(values), sum(values) / len(values)) if values else (None, None)
    baseline, current, recent = quality(shadow), quality(active), quality(active[:10])
    def delta(a, b):
        return a - b if a is not None and b is not None else None
    result = {"active_n": len(active), "shadow_tasks": sorted(shadow), "active_tasks": active,
              "first_pass_delta": delta(current[0], baseline[0]),
              "fix_rounds_delta": delta(current[1], baseline[1]),
              "last10_regression": bool(len(active) >= 10 and shadow and
                  (recent[0] < baseline[0] or recent[1] > baseline[1]))}
    if not economics:
        return result
    from . import scorecard, cache_telemetry
    usage = [row for _, row in scorecard._read_jsonl_entries(root)]
    goals = set(scorecard.accepted_goals(root))
    cache_cfg = cache_telemetry._pool_cfg(root)
    def economics(ids, metric):
        goal_ids = {tasks[tid].get("parent") for tid in ids} & goals
        measured = [r for r in usage if r.get("goal_id") in goal_ids]
        if not goal_ids or {r.get("goal_id") for r in measured} != goal_ids:
            return None
        if metric == "usd":
            if any(r.get("usd") is None for r in measured):
                return None
            return sum(r["usd"] for r in measured) / len(goal_ids)
        if any(not any(k in r for k in ("input_tokens", "input_uncached_tokens", "effective_tokens")) for r in measured):
            return None
        return sum(r.get("effective_tokens") if r.get("effective_tokens") is not None else
                   cache_telemetry.effective_cost({**(r if "input_uncached_tokens" in r else cache_telemetry.normalize(r.get("provider") or "claude", r)),
                                                   "provider": r.get("provider") or "claude"}, cache_cfg)
                   for r in measured) / len(goal_ids)
    result["accepted_tokens_delta"] = delta(economics(active, "tokens"), economics(shadow, "tokens"))
    result["accepted_cost_delta"] = delta(economics(active, "usd"), economics(shadow, "usd"))
    return result


def _collect_hermes(feature, root):
    kind = FEATURES[feature]["evidence"]
    rows = []
    observed_rows = decision_log.read_all(root=root)
    gated = {r.get("subject") for r in observed_rows
             if r.get("kind") == kind and r.get("reason") == "cache_promotion_gate"}
    for row in observed_rows:
        if feature != "stale_steering" and row.get("subject") in gated and row.get("reason") != "cache_promotion_gate":
            continue
        if row.get("kind") != kind:
            continue
        extra, deterministic = row.get("extra") or {}, row.get("deterministic") or {}
        if feature == "stale_steering":
            if (extra.get("trigger") or deterministic.get("trigger") or row.get("trigger")) != "stale_severity":
                continue
            observed = row.get("mode")
        else:
            observed = extra.get("cache_mode", deterministic.get("cache_mode", row.get("cache_mode")))
            if observed is None:
                continue
        rows.append({**row, "mode": observed})
    try:
        cfg = tomllib.loads((root / "pool.toml").read_text())
    except (OSError, ValueError):
        cfg = {}
    spec = FEATURES[feature]
    invalid = bool(current_mode(feature, cfg)[1]) or (
        feature == "stale_steering" and spec["key"] not in _table(cfg, spec["table"]))
    return {"n": len(rows), "shadow_n": sum(r["mode"] == "shadow" for r in rows),
            "invalid_config": invalid, **_cohort_metrics(rows, root)}
