"""Deterministic harness depth and dispatch-local shadow evidence."""
import fnmatch
import re
from pathlib import Path

from . import STATE, decision_log, planner_taxonomy, promotion, scorecard

WOULD_SKIP = ["jev_route", "skill_routing", "spec_review"]
_TEST_ID = re.compile(r"tests/test_[\w/]+\.py::test_\w+")
_GATE = re.compile(r"\.?/?\.claude/hooks/tests-green\.sh(?: \.)?(?: exits? 0)?")


def task_class(task, cfg):
    explicit = (task.get("constraints") or {}).get("task_class")
    if explicit:
        return explicit
    if _matches(task, (cfg.get("review") or {}).get("security_paths", [])):
        return "security"
    if task.get("complexity", 0) >= 7:
        return "architectural"
    if (task.get("title") or "").lower().startswith("fix") or (task.get("constraints") or {}).get("fix_round_for"):
        return "debugging"
    return "mechanical" if task.get("complexity", 0) <= 3 else "unfamiliar"


def _matches(task, patterns):
    return any(fnmatch.fnmatchcase(path, pattern)
               for path in task.get("scope", []) for pattern in patterns)


def level(task, *, tasks, cfg, history):
    """Classify a ready task; dependencies have already been admitted by bus.ready()."""
    constraints = task.get("constraints") or {}
    review = cfg.get("review") or {}
    areas = review.get("areas") or {}
    cls = task_class(task, cfg)
    classified_task = {**task, "constraints": {**constraints, "task_class": cls}}
    taxonomy = planner_taxonomy.classify({"kind": "dispatch"}, {
        "touches_interfaces": _matches(task, (areas.get("interfaces") or {}).get("paths", [])),
        "touches_migrations": _matches(task, (areas.get("migrations") or {}).get("paths", [])),
    }, goal=classified_task, task=classified_task)
    complexity = task.get("complexity", 0)
    reasons = []
    if constraints.get("architectural") or taxonomy["architectural"]:
        reasons.append("architectural")
    if _matches(task, review.get("security_paths", [])):
        reasons.append("security_path")
    if _matches(task, (areas.get("migrations") or {}).get("paths", [])):
        reasons.append("migrations")
    if complexity >= 8:
        reasons.append("complexity>=8")
    if constraints.get("goal"):
        reasons.append("goal_container")
    if reasons:
        depth = 4
    elif complexity >= 6 or constraints.get("route") == "spec_review":
        depth, reasons = 3, ["spec_review" if constraints.get("route") == "spec_review" else "complexity6-7"]
    elif complexity >= 4 or len(task.get("scope", [])) > 3 or any(
            any(char in path for char in "*?[") for path in task.get("scope", [])):
        depth, reasons = 2, ["complexity4-5_or_broad_scope"]
    else:
        depth, reasons = 1, ["small_localized"]
        lines = [str(line).strip() for line in task.get("acceptance", [])]
        tests = [line for line in lines if not _GATE.fullmatch(line)]
        sample = history.get(cls) or {}
        if (tests and all(_TEST_ID.fullmatch(line) for line in tests)
                and sample.get("first_pass_defined_count", 0) >= 5
                and (sample.get("first_pass_rate") or 0) >= .8):
            depth, reasons = 0, ["test_ids_and_first_pass_history"]
    return {"level": depth, "reasons": reasons, "eligible_fast_path": depth <= 1}


def history_table(root=STATE):
    return scorecard.efficiency(root=Path(root), by="class")["groups"]


def begin_tick(pool, notify, root=STATE):
    mode = promotion.mode("fast_path", pool.cfg)
    history = history_table(root) if mode != "off" else {}
    if mode == "active":
        result = promotion.evaluate("fast_path", promotion.collect("fast_path", root=root), pool.cfg)
        if result["recommendation"] != "promote":
            mode = "shadow"
            notify("active fast path refused; using shadow: " + ", ".join(result["reasons"]))
    pool.harness_depth_tick = {"mode": mode, "history": history}
    return pool.harness_depth_tick


def active(task):
    pipeline = task.get("pipeline") or {}
    return pipeline.get("harness_mode") == "active" and pipeline.get("harness_level") in (0, 1)


def record(task, result, mode):
    decision_log.record("harness_depth", task["id"], candidates=list(range(5)),
                        hard_constraints=[], deterministic=result, selected=result["level"],
                        reason=", ".join(result["reasons"]), mode=mode,
                        extra={"reasons": result["reasons"],
                               "would_skip": WOULD_SKIP if result["eligible_fast_path"] else []})


def record_skips(task):
    decision_log.record("skill_selection", task["id"], role=task.get("role", "execute"),
                        candidates=[], hard_constraints=[], deterministic={}, selected=[],
                        reason="fast_path", mode="active")
    from . import bus
    bus.log_run(role="jev_route", task=task["id"], mode="active", reason="fast_path",
                eligible=[], baseline=task.get("executor") or task.get("tier"),
                hypothetical=task.get("executor") or task.get("tier"), agrees=True)


def promotion_evidence(root):
    """Compare completed active lineages with disjoint shadow dispatch subjects."""
    import json
    rows = [row for row in decision_log.read_all(root=root)
            if row.get("kind") == "harness_depth" and row.get("selected") in (0, 1)]
    active_ids = {row["subject"] for row in rows if row.get("mode") == "active"}
    shadow_rows = [row for row in rows if row.get("mode") == "shadow"]
    shadow_ids = {row["subject"] for row in shadow_rows} - active_ids
    tasks = {}
    for path in (Path(root) / "tasks").glob("*.json"):
        try:
            task = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        tasks[task.get("id", path.stem)] = task
    active_ids.update(tid for tid, task in tasks.items() if active(task))
    shadow_ids -= active_ids
    def fixes(tid):
        task = tasks.get(tid, {})
        recorded = task.get("lineage_fix_rounds", (task.get("pipeline") or {}).get("lineage_fix_rounds", 0)) or 0
        descendants, pending = set(), [tid]
        while pending:
            parent = pending.pop()
            for child, value in tasks.items():
                if child != tid and child not in descendants and (value.get("constraints") or {}).get("fix_round_for") == parent:
                    descendants.add(child)
                    pending.append(child)
        return max(recorded, len(descendants))
    card = scorecard.efficiency(root=Path(root))["tasks"]
    def metrics(ids):
        accepted = [card[tid] for tid in ids if tid in card]
        first = [row["first_pass"] for row in accepted if row["first_pass"] is not None]
        return (sum(first) / len(first) if first else None,
                sum(row["tokens"] for row in accepted) / len(accepted) if accepted else None)
    baseline, current = metrics(shadow_ids), metrics(active_ids)
    return {"n": len(shadow_rows), "active_n": sum(tid in card for tid in active_ids),
            "two_fix_rounds": any(fixes(tid) >= 2 for tid in active_ids),
            "first_pass_delta": current[0] - baseline[0] if None not in (current[0], baseline[0]) else None,
            "accepted_tokens_delta": current[1] - baseline[1] if None not in (current[1], baseline[1]) else None}
