"""Skill exposure, use, marginal-value, and redundancy reporting."""
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from . import STATE, attribution, decision_log, strategy


DIMENSIONS = ("role", "task_class", "band", "model", "strategy", "repo")
METRICS = ("first_pass", "fix_rounds", "gate_reds", "review_request_changes",
           "accepted", "accepted_tokens", "accepted_usd", "latency_s", "tool_calls")


def _avg(values):
    return round(sum(values) / len(values), 3) if values else None


def _json_rows(directory, pattern="*.jsonl"):
    for path in sorted(directory.glob(pattern)) if directory.exists() else []:
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                yield row


def _tasks(root):
    result = {}
    for path in sorted((Path(root) / "tasks").glob("*.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(row, dict):
            result[row.get("id", path.stem)] = row
    return result


def _execute_root(task, tasks):
    """Resolve the execute root served by a task, using durable task-graph links."""
    current, seen = task, set()
    while isinstance(current, dict) and current.get("id") not in seen:
        seen.add(current.get("id"))
        constraints = current.get("constraints") or {}
        target = constraints.get("fix_round_for")
        if not target and current.get("role") != "execute":
            inputs = current.get("inputs") or []
            target = (constraints.get("review_for") or constraints.get("spec_review_for")
                      or (inputs[0] if inputs else None))
        if isinstance(target, str) and target in tasks:
            current = tasks[target]
            continue
        if current.get("role") == "execute":
            return strategy._root_id(current, tasks)
        break
    parent = task.get("parent") if isinstance(task, dict) else None
    candidates = [row for row in tasks.values()
                  if row.get("role") == "execute" and row.get("parent") == parent
                  and not (row.get("constraints") or {}).get("fix_round_for")]
    if len(candidates) == 1:
        return candidates[0].get("id")
    if task.get("role") in ("scout", "spec_review") and candidates:
        created = task.get("created_at")
        later = [row for row in candidates if created is None or row.get("created_at") is None
                 or row.get("created_at") >= created]
        if len(later) == 1:
            return later[0].get("id")
    return None


def _tokens(row):
    usage = row.get("usage") or {}
    values = [row.get("input_tokens", usage.get("input_tokens")),
              row.get("output_tokens", usage.get("output_tokens")),
              row.get("cache_read_input_tokens", usage.get("cache_read_input_tokens"))]
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _pool_cfg(root):
    import tomllib
    for path in (Path(root) / "pool.toml", Path(root) / ".orchestrator" / "pool.toml"):
        try:
            return tomllib.loads(path.read_text())
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            pass
    return {}


def timestamp(value):
    """Unix seconds, accepting the ISO timestamps used by older run logs."""
    from datetime import datetime, timezone
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError):
            return None


def in_window(row, since_s=None, until_s=None):
    if since_s is None and until_s is None:
        return True
    stamp = timestamp(row.get("ts", row.get("at")))
    return (stamp is not None and (since_s is None or stamp >= since_s)
            and (until_s is None or stamp < until_s))


def usage_rows(root=STATE, since_s=None, until_s=None):
    """Return one outcome-attributed row for each run that records skills used."""
    root = Path(root)
    tasks = _tasks(root)
    raw = [row for row in _json_rows(root / "runs")
           if isinstance(row.get("context"), dict)
           and row["context"].get("skills_used") is not None
           and in_window(row, since_s, until_s)]
    roots = {tid: _execute_root(task, tasks) for tid, task in tasks.items()}
    root_runs = defaultdict(list)
    for row in raw:
        root_id = roots.get(row.get("task"))
        if root_id:
            root_runs[root_id].append(row)
    cfg = _pool_cfg(root)
    result = []
    for row in raw:
        task = tasks.get(row.get("task"), {})
        root_id = roots.get(row.get("task"))
        execute = tasks.get(root_id) if root_id else None
        pipeline = (execute or {}).get("pipeline") or {}
        lineage_ids = {tid for tid, rid in roots.items() if rid == root_id} if root_id else set()
        fixes = reds = None
        reviews = []
        if execute is not None:
            fixes = execute.get("lineage_fix_rounds", pipeline.get("lineage_fix_rounds"))
            if fixes is None:
                fixes = sum(tid != root_id and tasks[tid].get("role") == "execute"
                            for tid in lineage_ids)
            reds = pipeline.get("gate_reds", execute.get("gate_reds"))
            for candidate in tasks.values():
                inputs = candidate.get("inputs") or []
                constraints = candidate.get("constraints") or {}
                target = constraints.get("review_for") or (inputs[0] if inputs else None)
                if candidate.get("role") == "review" and isinstance(target, str) and target in lineage_ids:
                    verdict = candidate.get("review_verdict") or (candidate.get("result") or {}).get("verdict")
                    if verdict in ("approve", "request_changes"):
                        reviews.append(verdict)
        accepted = bool(execute.get("merged_into")) if execute is not None else None
        own = root_runs.get(root_id, [])
        own_tokens = [_tokens(item) for item in own]
        own_usd = [item.get("usd") for item in own if item.get("usd") is not None]
        own_latency = [item.get("duration_s") for item in own if item.get("duration_s") is not None]
        own_calls = [item.get("tool_calls", item.get("calls")) for item in own
                     if item.get("tool_calls", item.get("calls")) is not None]
        tier = row.get("tier")
        executor = row.get("executor") or row.get("executor_id")
        complexity = (execute or task).get("complexity")
        context = row["context"]
        result.append({
            **row, "skills_used": list(context.get("skills_used") or []),
            "skills_selected": (list(context.get("skills_selected") or [])
                                if context.get("skills_selected") is not None else None),
            "skill_tokens_l2": context.get("skill_tokens_l2"), "lineage_root": root_id,
            "first_pass": (fixes == 0 and reds == 0
                           if fixes is not None and reds is not None else None),
            "fix_rounds": fixes, "gate_reds": reds,
            "review_request_changes": (sum(value == "request_changes" for value in reviews)
                                       if reviews else None),
            "accepted": accepted,
            "accepted_tokens": (sum(value for value in own_tokens if value is not None)
                                if accepted and any(value is not None for value in own_tokens) else None),
            "accepted_usd": sum(own_usd) if accepted and own_usd else None,
            "latency_s": sum(own_latency) if own_latency else None,
            "tool_calls": sum(own_calls) if own_calls else None,
            "role": row.get("role"),
            "task_class": attribution.task_class(execute or task) if (execute or task) else None,
            "band": attribution.band(complexity),
            "model": row.get("model") or attribution.model_of(executor, tier, cfg),
            "strategy": (strategy.derive(execute, tasks, waves_rows=[]).get("strategy")
                         if execute is not None else None),
            "repo": "orchestrator",
        })
    return result


def _metric_summary(rows):
    result = {"n": len(rows)}
    for metric in METRICS:
        values = [row.get(metric) for row in rows if row.get(metric) is not None]
        name = metric + "_rate" if metric in ("first_pass", "accepted") else metric
        result[name] = _avg(values)
    result["skill_token_overhead"] = _avg(
        [row.get("skill_tokens_l2") for row in rows if row.get("skill_tokens_l2") is not None])
    return result


def by_skill(root=STATE, group_by=("role", "task_class")):
    group_by = tuple(group_by)
    invalid = set(group_by) - set(DIMENSIONS)
    if invalid:
        raise ValueError("unknown skill scorecard dimension: " + ",".join(sorted(invalid)))
    buckets = defaultdict(list)
    for row in usage_rows(root):
        for skill in row["skills_used"]:
            buckets[(skill,) + tuple(row.get(name) for name in group_by)].append(row)
    output = []
    for key, rows in sorted(buckets.items(), key=lambda item: tuple(str(v) for v in item[0])):
        output.append({"skill": key[0], **dict(zip(group_by, key[1:])), **_metric_summary(rows)})
    return output


def _configured_min_samples(root):
    value = (_pool_cfg(root).get("promotion") or {}).get("min_samples", 20)
    try:
        return int(value)
    except (ValueError, TypeError):
        return 20


def marginal(root, skill, group_by=("role", "task_class"), min_samples=None,
             since_s=None, until_s=None):
    group_by = tuple(group_by)
    invalid = set(group_by) - set(DIMENSIONS)
    if invalid:
        raise ValueError("unknown skill scorecard dimension: " + ",".join(sorted(invalid)))
    minimum = _configured_min_samples(root) if min_samples is None else int(min_samples)
    buckets = defaultdict(lambda: {"with": [], "without": []})
    for row in usage_rows(root, since_s=since_s, until_s=until_s):
        key = tuple(row.get(name) for name in group_by)
        if skill in row["skills_used"]:
            buckets[key]["with"].append(row)
        elif skill not in (row.get("skills_selected") or []):
            buckets[key]["without"].append(row)
    output = []
    for key, split in sorted(buckets.items(), key=lambda item: tuple(str(v) for v in item[0])):
        with_rows, without_rows = split["with"], split["without"]
        item = {**dict(zip(group_by, key)), "n_with": len(with_rows), "n_without": len(without_rows)}
        if len(with_rows) < minimum or len(without_rows) < minimum:
            output.append({**item, "insufficient": True})
            continue
        def delta(metric):
            left = _avg([r[metric] for r in with_rows if r.get(metric) is not None])
            right = _avg([r[metric] for r in without_rows if r.get(metric) is not None])
            return round(left - right, 3) if left is not None and right is not None else None
        deltas = {name + "_delta": delta(name) for name in
                  ("first_pass", "fix_rounds", "accepted_tokens", "accepted_usd", "latency_s")}
        overhead = _avg([r.get("skill_tokens_l2") for r in with_rows
                         if r.get("skill_tokens_l2") is not None])
        quality, cost = deltas["first_pass_delta"], deltas["accepted_tokens_delta"]
        without_cost = _avg([r["accepted_tokens"] for r in without_rows
                             if r.get("accepted_tokens") is not None])
        within_ten = (cost is not None and without_cost not in (None, 0)
                      and cost <= abs(without_cost) * .1)
        if quality is not None and quality < 0:
            verdict = "harmful"
        elif quality is not None and cost is not None and (quality >= 0 and cost <= 0
                                                            or quality > .05 and within_ten):
            verdict = "valuable"
        elif cost is not None and cost > 0 and (quality is None or abs(quality) <= .05):
            verdict = "costly"
        else:
            verdict = "neutral"
        output.append({**item, "insufficient": False, **deltas,
                       "skill_token_overhead": overhead, "verdict": verdict})
    return output


def _declared_scripts(root):
    candidates = [Path(root) / "skills" / "registry.json",
                  Path(root) / ".orchestrator" / "skills" / "registry.json"]
    document = {}
    for path in candidates:
        try:
            document = json.loads(path.read_text())
            break
        except (OSError, ValueError, TypeError):
            continue
    records = document.get("skills", document) if isinstance(document, dict) else {}
    return {skill: {tool for tool in (record.get("tools") or []) if tool.startswith("script:")}
            for skill, record in records.items() if isinstance(record, dict)}


def redundancy(root=STATE):
    rows = usage_rows(root)
    pairs = defaultdict(int)
    single_tasks = defaultdict(set)
    for row in rows:
        skills = sorted(set(row["skills_used"]))
        for pair in combinations(skills, 2):
            pairs[pair] += 1
        if len(skills) == 1 and row.get("task"):
            single_tasks[skills[0]].add(row["task"])
    paths = defaultdict(set)
    gate = Path(root) / "runs" / "jev" / "gate.jsonl"
    for row in _json_rows(gate.parent, gate.name):
        # The gate preserves Claude tool names; retain historical lowercase aliases.
        if str(row.get("tool") or "").lower() not in ("read", "grep", "glob", "search", "find") or not row.get("tool_target"):
            continue
        for skill, tasks in single_tasks.items():
            if row.get("task") in tasks:
                paths[skill].add(row["tool_target"])
    scripts = _declared_scripts(root)
    output = []
    for (left, right), count in sorted(pairs.items()):
        if not paths[left] or not paths[right]:
            overlap, note = None, "no single-skill runs"
        else:
            overlap = round(len(paths[left] & paths[right]) / len(paths[left] | paths[right]), 3)
            note = "path overlap is a single-skill-run proxy"
        duplicated = sorted(scripts.get(left, set()) & scripts.get(right, set()))
        output.append({"skill_a": left, "skill_b": right, "n": count,
                       "overlap": overlap, "overlap_proxy": overlap, "note": note,
                       "duplicated_script_invocations": duplicated,
                       "unique_findings_proxy": None,
                       "unique_findings_note": "needs Stage 6 evidence sharing"})
    return output


def _selection_rows(root, role, since_s):
    since = __import__("time").time() - since_s
    return [row for row in decision_log.read_all(root=root, since_ts=since)
            if row.get("kind") == "skill_selection"
            and (row.get("role") or (row.get("deterministic") or {}).get("role")
                 or (row.get("extra") or {}).get("role")) == role
            and row.get("mode") in ("shadow", "active")]


def selection_rows(root, role, since_s):
    """Count recent routing decisions for one role."""
    return len(_selection_rows(root, role, since_s))


def recovery_rate(root, role, since_s):
    """Return recoveries per recent selection, matching outcomes by subject."""
    selections = _selection_rows(root, role, since_s)
    if not selections:
        return 0.0
    since = __import__("time").time() - since_s
    subjects = {row.get("subject") for row in selections}
    recovered = {row.get("subject") for row in decision_log.read_all(root=root, since_ts=since)
                 if row.get("kind") == "outcome" and row.get("decision_kind") == "skill_selection"
                 and row.get("subject") in subjects and row.get("skill_recovery")}
    return round(len(recovered) / len(selections), 3)


def jev_metrics(root=STATE):
    """Return Jev selection/use agreement and actual calls per routed spawn by role."""
    rows = decision_log.read_all(root=Path(root))
    outcomes = {(row.get("subject"), row.get("decision_kind")): row for row in rows
                if row.get("kind") == "outcome"}
    buckets = defaultdict(lambda: {"spawns": 0, "calls": 0, "selected": 0, "used": 0})
    for row in rows:
        if row.get("kind") != "skill_selection" or row.get("reason") == "stage1 static":
            continue
        role = row.get("role") or (row.get("extra") or {}).get("role") or "unknown"
        bucket = buckets[role]
        bucket["spawns"] += 1
        verdict = row.get("jev")
        if not isinstance(verdict, dict):
            continue
        bucket["calls"] += verdict.get("source") == "jev"
        chosen = {skill_id for skill_id, decision in (verdict.get("decisions") or {}).items()
                  if isinstance(decision, dict) and decision.get("select")}
        used = set((outcomes.get((row.get("subject"), "skill_selection")) or {}).get("skills_used") or [])
        bucket["selected"] += len(chosen)
        bucket["used"] += len(chosen & used)
    return {role: {"jev_agreement_rate": round(value["used"] / value["selected"], 3)
                   if value["selected"] else 0.0,
                   "jev_calls_per_spawn": round(value["calls"] / value["spawns"], 3)
                   if value["spawns"] else 0.0}
            for role, value in sorted(buckets.items())}


def build(root=STATE):
    root = Path(root)
    rows = []
    for path in sorted((root / "runs").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row.get("context"), dict):
                rows.append(row)
    buckets = defaultdict(list)
    for row in rows:
        context = row["context"]
        for skill_id in context.get("skills_exposed") or []:
            buckets[(row.get("role") or "unknown", skill_id)].append(row)
    by_role_skill = {}
    for (role, skill_id), selected in sorted(buckets.items()):
        uses = sum(skill_id in (row["context"].get("skills_used") or []) for row in selected)
        overhead = []
        for row in selected:
            tokens = row.get("input_tokens") or (row.get("usage") or {}).get("input_tokens")
            if tokens:
                overhead.append(((row["context"].get("skill_tokens_l0") or 0) +
                                 (row["context"].get("skill_tokens_l2") or 0)) / tokens)
        by_role_skill[f"{role}/{skill_id}"] = {
            "role": role, "skill_id": skill_id, "exposures": len(selected), "uses": uses,
            "use_rate": round(uses / len(selected), 3),
            "skill_tokens_l0": _avg([row["context"].get("skill_tokens_l0", 0) for row in selected]),
            "skill_tokens_l2": _avg([row["context"].get("skill_tokens_presented_l2",
                                                         row["context"].get("skill_tokens_l2", 0))
                                      for row in selected]),
            "skill_overhead_ratio": _avg(overhead),
        }
    by_role = {}
    for role in sorted({row.get("role") or "unknown" for row in rows}):
        selected = [row for row in rows if (row.get("role") or "unknown") == role
                    and row["context"].get("skills_selected") is not None]
        reductions = [1 - row["context"].get("skill_tokens_selected_l0", 0) /
                      row["context"]["skill_tokens_l0"] for row in selected
                      if row["context"].get("skill_tokens_l0")]
        recoveries = [bool(set(row["context"].get("skills_used") or []) -
                           set(row["context"].get("skills_selected") or [])) for row in selected]
        by_role[role] = {"spawns_with_selection": len(selected),
                         "selected_set_size_avg": _avg([len(row["context"].get("skills_selected") or [])
                                                         for row in selected]),
                         "skill_reduction": _avg(reductions),
                         "skill_recovery_rate": _avg(recoveries)}
    accepted = {}
    for row in rows:
        task = row.get("task")
        try:
            record = json.loads((root / "tasks" / f"{task}.json").read_text())
        except (OSError, ValueError, TypeError):
            continue
        if not record.get("merged_into"):
            continue
        lineage = row.get("lineage_root") or row.get("goal_id") or task
        accepted.setdefault(lineage, 0)
        accepted[lineage] += (row["context"].get("skill_tokens_l0") or 0) + (row["context"].get("skill_tokens_l2") or 0)
    return {"by_role_skill": by_role_skill, "by_role": by_role,
            "skill_tokens_per_accepted_task": accepted, **economy(root, rows=rows)}


def format_report(card):
    lines = ["role\tselected avg\tskill reduction\tskill recovery rate"]
    for role, row in card["by_role"].items():
        lines.append(f"{role}\t{row['selected_set_size_avg']}\t{row['skill_reduction']}\t{row['skill_recovery_rate']}")
    lines += ["", "role/skill\texposures\tuses\tuse rate\tl0 avg\tl2 avg\toverhead ratio"]
    for key, row in card["by_role_skill"].items():
        lines.append(f"{key}\t{row['exposures']}\t{row['uses']}\t{row['use_rate']}\t{row['skill_tokens_l0']}\t{row['skill_tokens_l2']}\t{row['skill_overhead_ratio']}")
    lines += ["", "accepted lineage\tskill tokens", *
              (f"{key}\t{value}" for key, value in card["skill_tokens_per_accepted_task"].items())]
    lines += ["", json.dumps({name: card.get(name) for name in ("skill_reuse_rate", "skill_overhead_ratio", "skill_utility", "skill_recovery_rate")}, sort_keys=True)]
    return "\n".join(lines)


def format_skill_analysis(card, group_by=("role", "task_class")):
    columns = ["skill", *group_by, "n", "first_pass_rate", "fix_rounds",
               "accepted_tokens", "accepted_usd", "latency_s", "skill_token_overhead"]
    lines = ["\t".join(columns)]
    for row in card.get("by_skill", []):
        lines.append("\t".join(str(row.get(name)) for name in columns))
    if "economy" in card:
        lines += ["", json.dumps(card["economy"], sort_keys=True)]
    if "marginal" in card:
        lines += ["", "marginal", json.dumps(card["marginal"], sort_keys=True)]
    if "redundancy" in card:
        lines += ["", "skill_a\tskill_b\tn\toverlap\tduplicated scripts\tnote"]
        for row in card["redundancy"]:
            lines.append(f"{row['skill_a']}\t{row['skill_b']}\t{row['n']}\t{row['overlap']}\t"
                         f"{','.join(row['duplicated_script_invocations']) or '-'}\t{row['note']}")
    if "jev_by_role" in card:
        lines += ["", "role\tjev agreement rate\tjev calls per spawn"]
        for role, row in card["jev_by_role"].items():
            lines.append(f"{role}\t{row['jev_agreement_rate']}\t{row['jev_calls_per_spawn']}")
    return "\n".join(lines)


def economy(root=STATE, rows=None):
    """P30 metrics; unknown quality and costs remain unknown, never zero."""
    rows = list(_json_rows(Path(root) / "runs")) if rows is None else rows
    selected = [r for r in rows if (r.get("context") or {}).get("skills_selected")]
    reuse = [set(r["context"]["skills_selected"]) <= set(r["context"].get("skills_used") or [])
             for r in selected]
    recovery = [bool(set(r["context"].get("skills_used") or []) - set(r["context"]["skills_selected"]))
                for r in selected]
    overhead = []
    for row in rows:
        context = row.get("context") or {}
        tokens = row.get("input_tokens") or (row.get("usage") or {}).get("input_tokens")
        if tokens:
            overhead.append(((context.get("skill_tokens_l0") or 0) +
                             (context.get("skill_tokens_presented_l2", context.get("skill_tokens_l2")) or 0)) / tokens)
    utility = []
    skills = sorted({s for r in rows for s in (r.get("context") or {}).get("skills_used", [])})
    for skill in skills:
        for row in marginal(root, skill):
            quality, tokens = row.get("first_pass_delta"), row.get("skill_token_overhead")
            if quality is not None and tokens:
                utility.append(quality * 1000 / tokens)
    return {"skill_reuse_rate": _avg(reuse), "skill_overhead_ratio": _avg(overhead),
            "skill_utility": _avg(utility), "skill_recovery_rate": _avg(recovery)}
