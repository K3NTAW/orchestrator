"""Evidence-based, shadow-first feature promotion recommendations."""

from collections import OrderedDict
import json
from pathlib import Path
import tomllib

from . import STATE, decision_log


FEATURES = OrderedDict((
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
))

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
        return spec["default"], ["invalid_config"]
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


def evaluate(feature, evidence, cfg=None):
    """Evaluate evidence and return a recommendation; never changes configuration."""
    if feature not in FEATURES:
        raise KeyError(feature)
    evidence = evidence if isinstance(evidence, dict) else {}
    mode, mode_flags = current_mode(feature, cfg)
    criteria = _criteria(cfg)
    n = evidence.get("n", 0) or 0
    reasons = list(mode_flags)
    if n < criteria["min_samples"]:
        reasons.append("insufficient_evidence")
        return {"feature": feature, "mode": mode, "n": n, "recommendation": "stay",
                "reasons": reasons, "criteria": criteria}

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
                "recommendation": "demote" if mode == "active" else "stay",
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
    if feature == "handoff_routing":
        from . import handoff_scorecard
        rows = handoff_scorecard.lineage_rows(root)
        return {"n": sum(row.get("rounds", 0) >= 1 for row in rows),
                "reason": "shadow_quality_unmeasured"}
    if feature in {"context_router", "tool_disclosure", "conditional_instructions"}:
        # Evidence arrives with P27/P28 context scorecards.
        return {"n": 0}
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
    return [evaluate(feature, collect(feature, root=root), cfg=cfg) for feature in FEATURES]


def format_report(rows):
    return "\n".join("{feature}: {recommendation} ({mode}, n={n}){reasons}".format(
        **{key: value for key, value in row.items() if key != "reasons"},
        reasons=(" — " + ", ".join(row.get("reasons", [])) if row.get("reasons") else ""))
        for row in rows)
