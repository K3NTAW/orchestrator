"""Machine-readable implementation status for the orchestrator roadmaps."""
import json
import subprocess
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import acceptance, promotion


# Keep this as data, rather than deriving it from prose, so changes to the roadmap are
# explicit and reviewable.  Test references are also checked as part of status().
REQUIREMENTS = [
    {"id": "phase-i-p0", "program": "phase-i", "priority": "P0", "title": "Measurement foundation", "experimental": False, "files": ["orchestrator/scorecard.py"], "tests": ["tests/test_scorecard.py::test_build_counts_and_scores"], "config": [], "telemetry": [".orchestrator/runs/*.jsonl"]},
    {"id": "phase-i-p1", "program": "phase-i", "priority": "P1", "title": "Role-specific context compilation", "experimental": False, "files": ["orchestrator/spawn.py"], "tests": ["tests/test_spawn.py::test_review_packet_role_section_for_each_role"], "config": [], "telemetry": [".orchestrator/runs/*.jsonl"]},
    {"id": "phase-i-p2", "program": "phase-i", "priority": "P2", "title": "Fix-round economics", "experimental": False, "files": ["orchestrator/scorecard.py"], "tests": ["tests/test_scorecard.py::test_executor_economics_first_pass_and_fix_round_probability"], "config": [], "telemetry": ["scorecard.executor_economics"]},
    {"id": "phase-i-p3", "program": "phase-i", "priority": "P3", "title": "Jev executor routing, shadow mode", "experimental": False, "files": ["orchestrator/jev.py"], "tests": ["tests/test_scorecard.py::test_routing_eval_joins_rows_to_outcomes_by_lineage_root"], "config": ["jev.routing.mode"], "telemetry": [".orchestrator/runs/jev/routing.jsonl"], "mode_key": "jev.routing.mode"},
    {"id": "phase-i-p4", "program": "phase-i", "priority": "P4", "title": "Routing telemetry and evaluation", "experimental": False, "files": ["orchestrator/scorecard.py"], "tests": ["tests/test_scorecard.py::test_routing_eval_coverage_and_skip_reasons"], "config": ["models.rank_by"], "telemetry": ["scorecard.routing_eval"]},
    {"id": "phase-i-p5", "program": "phase-i", "priority": "P5", "title": "Active Jev routing", "experimental": False, "files": ["orchestrator/jev.py"], "tests": ["tests/test_scorecard.py::test_routing_eval_per_signal_threshold_split"], "config": ["jev.routing.mode"], "telemetry": [".orchestrator/runs/jev/routing.jsonl"], "mode_key": "jev.routing.mode"},
    {"id": "phase-i-p6", "program": "phase-i", "priority": "P6", "title": "Review token optimization", "experimental": False, "files": ["orchestrator/spawn.py"], "tests": ["tests/test_spawn.py::test_review_prompt_diff_is_bounded"], "config": ["limits.review_diff_chars"], "telemetry": [".orchestrator/runs/*.jsonl"]},
    # All four Jev decision points share the scout_necessity default.
    {"id": "phase-i-p7", "program": "phase-i", "priority": "P7", "title": "Experimental Jev decision points", "experimental": True, "files": ["orchestrator/jev.py"], "tests": ["tests/test_jev_points.py::test_review_escalation_never_reduces_or_replaces_security_review"], "config": ["jev.points.scout_necessity"], "telemetry": [".orchestrator/runs/jev/points.jsonl"], "mode_key": "jev.points.scout_necessity"},
    {"id": "adaptive-parallelism-p0", "program": "adaptive-parallelism", "priority": "P0", "title": "Parallelism telemetry", "experimental": False, "files": ["orchestrator/scorecard.py"], "tests": ["tests/test_scorecard.py::test_parallelism_concurrency_sweep_and_waits"], "config": [], "telemetry": [".orchestrator/runs/scheduler.jsonl"]},
    {"id": "adaptive-parallelism-p1", "program": "adaptive-parallelism", "priority": "P1", "title": "Explicit task interference model", "experimental": False, "files": ["orchestrator/interference.py"], "tests": ["tests/test_interference.py::test_same_file_is_hard"], "config": ["scheduler.mode"], "telemetry": [".orchestrator/runs/scheduler.jsonl"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p2", "program": "adaptive-parallelism", "priority": "P2", "title": "Graph-aware interference", "experimental": False, "files": ["orchestrator/interference.py"], "tests": ["tests/test_interference.py::test_graph_signal_soft_on_cross_file_link"], "config": ["scheduler.mode"], "telemetry": [".orchestrator/runs/scheduler.jsonl"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p3", "program": "adaptive-parallelism", "priority": "P3", "title": "Execution waves", "experimental": False, "files": ["orchestrator/interference.py"], "tests": ["tests/test_interference.py::test_select_wave_respects_capacity_and_is_deterministic"], "config": ["scheduler.mode"], "telemetry": [".orchestrator/runs/scheduler.jsonl"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p4", "program": "adaptive-parallelism", "priority": "P4", "title": "Critical-path scheduling", "experimental": False, "files": ["orchestrator/critical_path.py"], "tests": ["tests/test_critical_path.py::test_critical_path_sums_along_the_whole_chain"], "config": ["scheduler.mode"], "telemetry": ["scheduler.priority_order"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p5", "program": "adaptive-parallelism", "priority": "P5", "title": "Duration-aware scheduling", "experimental": False, "files": ["orchestrator/duration.py"], "tests": ["tests/test_duration.py::test_sufficient_history_uses_class_executor_median"], "config": ["scheduler.duration_mode"], "telemetry": ["scheduler.duration_estimate"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p6", "program": "adaptive-parallelism", "priority": "P6", "title": "Capacity-aware scheduling", "experimental": False, "files": ["orchestrator/capacity.py"], "tests": ["tests/test_capacity.py::test_imminent_critical_task_reserves_last_slot"], "config": ["scheduler.concurrency_mode"], "telemetry": ["scheduler.capacity"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p7", "program": "adaptive-parallelism", "priority": "P7", "title": "Orthogonal scout strategy", "experimental": False, "files": ["orchestrator/scout_evidence.py"], "tests": ["tests/test_scout_evidence.py::test_orthogonal_objectives_allowed_and_duplicates_flagged"], "config": [], "telemetry": ["scheduler.scout_decision"]},
    {"id": "adaptive-parallelism-p8", "program": "adaptive-parallelism", "priority": "P8", "title": "Scout result structure", "experimental": False, "files": ["orchestrator/scout_evidence.py"], "tests": ["tests/test_scout_evidence.py::test_normalize_findings_produces_structured_entries_and_flags_low_confidence"], "config": [], "telemetry": ["tasks/*.json.scout_findings"]},
    {"id": "adaptive-parallelism-p9", "program": "adaptive-parallelism", "priority": "P9", "title": "Parallel specialized reviews", "experimental": False, "files": ["orchestrator/spawn.py"], "tests": ["tests/test_spawn.py::test_review_packet_role_section_for_each_role"], "config": ["review.complementary"], "telemetry": ["tasks/*.json.reviews"]},
    {"id": "adaptive-parallelism-p10", "program": "adaptive-parallelism", "priority": "P10", "title": "Review deduplication", "experimental": False, "files": ["orchestrator/scorecard.py"], "tests": ["tests/test_scorecard.py::test_review_quality_distinct_defects_and_overlap_for_two_reviews"], "config": [], "telemetry": ["scorecard.review_overlap"]},
    {"id": "adaptive-parallelism-p11", "program": "adaptive-parallelism", "priority": "P11", "title": "Merge-pressure awareness", "experimental": False, "files": ["orchestrator/daemon.py"], "tests": ["tests/test_daemon.py::test_wave_row_carries_priority_order"], "config": ["scheduler.merge_pressure_mode"], "telemetry": ["scheduler.merge_pressure"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p12", "program": "adaptive-parallelism", "priority": "P12", "title": "Stale-work detection", "experimental": False, "files": ["orchestrator/stale.py"], "tests": ["tests/test_stale.py::test_interface_change_in_moved_file_consumed_by_task_is_high_risk"], "config": [], "telemetry": ["scheduler.stale_check"]},
    {"id": "adaptive-parallelism-p13", "program": "adaptive-parallelism", "priority": "P13", "title": "Adaptive parallelism", "experimental": False, "files": ["orchestrator/capacity.py"], "tests": ["tests/test_capacity.py::test_idle_capacity_admits_everything_and_no_unnecessary_reservation"], "config": ["scheduler.concurrency_mode"], "telemetry": ["scheduler.concurrency_limit"], "mode_key": "scheduler.mode"},
    {"id": "adaptive-parallelism-p14", "program": "adaptive-parallelism", "priority": "P14", "title": "Scheduling scorecard", "experimental": False, "files": ["orchestrator/sched_scorecard.py"], "tests": ["tests/test_sched_scorecard.py::test_predicted_hard_pair_that_conflicted_counts_toward_precision"], "config": [], "telemetry": ["scorecard.scheduling"]},
    {"id": "adaptive-parallelism-p15", "program": "adaptive-parallelism", "priority": "P15", "title": "Jev as a scheduling signal", "experimental": False, "files": ["orchestrator/jev.py"], "tests": ["tests/test_daemon.py::test_scheduler_cfg_defaults_cover_new_keys"], "config": ["scheduler.jev_mode"], "telemetry": ["scheduler.jev_recommendation"], "mode_key": "scheduler.jev_mode"},
    {"id": "adaptive-parallelism-p16", "program": "adaptive-parallelism", "priority": "P16", "title": "Critical-path model allocation", "experimental": False, "files": ["orchestrator/critical_path.py"], "tests": ["tests/test_critical_path.py::test_rank_prefers_longer_downstream_chain"], "config": ["allocation.mode"], "telemetry": ["scheduler.allocation"], "mode_key": "allocation.mode"},
    {"id": "adaptive-parallelism-p17", "program": "adaptive-parallelism", "priority": "P17", "title": "Selective speculative execution", "experimental": True, "files": ["orchestrator/speculation.py"], "tests": ["tests/test_speculation.py::test_default_mode_off_never_speculates"], "config": ["speculation.mode"], "telemetry": ["scheduler.speculation"], "mode_key": "speculation.mode"},
    {"id": "adaptive-parallelism-p18", "program": "adaptive-parallelism", "priority": "P18", "title": "Workflow strategy learning", "experimental": False, "files": ["orchestrator/strategy.py"], "tests": ["tests/test_strategy.py::test_shadow_recommendation_prefers_evidence_without_changing_policy"], "config": ["strategy.mode"], "telemetry": ["scheduler.strategy"], "mode_key": "strategy.mode"},
    {"id": "brief-p14-decision-log", "program": "phase-i", "priority": "P14", "title": "Decision log", "experimental": False, "files": ["orchestrator/decision_log.py"], "tests": ["tests/test_decision_log.py::test_record_writes_structured_row_with_required_fields"], "config": [], "telemetry": [".orchestrator/runs/decisions.jsonl"]},
    {"id": "brief-p15-promotion", "program": "phase-i", "priority": "P15", "title": "Promotion", "experimental": False, "files": ["orchestrator/promotion.py"], "tests": ["tests/test_promotion.py::test_active_feature_with_regression_demotes"], "config": ["promotion"], "telemetry": ["promotion.report"]},
]


def _config(root, cfg):
    if cfg is not None:
        return cfg
    path = Path(root) / ".orchestrator" / "pool.toml"
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _value(cfg, dotted):
    value = cfg
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def status(req, root, cfg=None):
    """Derive one requirement's implementation and activation status."""
    root = Path(root)
    if req.get("not_applicable"):
        if not req.get("reason"):
            raise ValueError("not_applicable requirements need a reason")
        return "not_applicable"
    if any(not (root / name).is_file() for name in req.get("files", [])):
        return "missing"
    if acceptance.missing_tests(root, req.get("tests", []), runner="unittest"):
        return "partial"
    mode_key = req.get("mode_key")
    if not mode_key:
        return "implemented_active"
    mode = _value(_config(root, cfg), mode_key)
    feature = next((spec for spec in promotion.FEATURES.values()
                    if f'{spec["table"]}.{spec["key"]}' == mode_key), None)
    if feature is not None and mode not in feature["modes"]:
        mode = feature["default"]
    return {
        "off": "implemented_off",
        "shadow": "implemented_shadow",
        "active": "implemented_active",
    }.get(mode, "implemented_active")


def build(root, cfg=None):
    """Build the complete roadmap report for *root*."""
    root = Path(root)
    loaded_cfg = _config(root, cfg)
    rows = [{**req, "status": status(req, root, loaded_cfg)} for req in REQUIREMENTS]
    counts = Counter(row["status"] for row in rows)
    summary = {name: counts.get(name, 0) for name in (
        "implemented_active", "implemented_shadow", "implemented_off",
        "partial", "missing", "not_applicable",
    )}
    try:
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        base_sha = None
    complete = not any(
        row["status"] in ("partial", "missing") and not row["experimental"]
        for row in rows
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_sha": base_sha,
        "requirements": rows,
        "summary": summary,
        "complete": complete,
    }


def write(path):
    """Write a report, inferring the repository root from its output path."""
    path = Path(path)
    root = path.parent.parent if path.parent.name == ".orchestrator" else Path.cwd()
    report = build(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    return report
