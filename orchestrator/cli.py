"""orchestrator status | cost [--by role|tier|account|task] | hold A [--minutes] | resume A | pick planner|scout|review|execute | daemon [--once] | merge T-0001 | repomap [--budget N] [--stdout] | install /path/to/target | post T-0001 --summary ... | planner-runs --summary | jev diagnose"""
import argparse, json, os, random, sys
from collections import defaultdict
from datetime import datetime
from . import ROOT, bus, scorecard
from .bus import RUNS
from .pool import Pool


def _percentile(values, percentile):
    if not values:
        return "-"
    values = sorted(values)
    index = (len(values) - 1) * percentile / 100
    low, high = int(index), min(int(index) + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (index - low), 1)


def _jev_diagnose(since=None, export=None, n=10):
    path = ROOT / ".orchestrator" / "runs" / "jev" / "gate.jsonl"
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and datetime.fromtimestamp(row.get("ts", 0)).date().isoformat() < since:
                continue
            rows.append(row)

    roles = {}
    tasks = {}
    for row in rows:
        tid = row.get("task")
        if tid not in tasks:
            try:
                tasks[tid] = bus.get(tid).get("role") or "?"
            except Exception:
                tasks[tid] = "?"
        row["role"] = tasks[tid]

    def bucket(key):
        out = {}
        for row in rows:
            name = row.get(key) or "?"
            item = out.setdefault(name, {"calls": 0, "scored": 0, "waste": 0, "repeat": 0})
            item["calls"] += 1
            item["scored"] += bool(row.get("scored"))
            item["repeat"] += bool(row.get("repeat"))
            if row.get("scored") and ((row.get("p_needed") is not None and row["p_needed"] < 0.3) or
                                       (row.get("p_redundant") is not None and row["p_redundant"] > 0.7)):
                item["waste"] += 1
        for item in out.values():
            item["waste_pct"] = round(item["waste"] / item["scored"] * 100, 1) if item["scored"] else 0.0
            item["repeat_pct"] = round(item["repeat"] / item["calls"] * 100, 1) if item["calls"] else 0.0
        return out

    scored = [r for r in rows if r.get("scored")]
    latencies = [float(r.get("latency_ms") or 0) for r in scored]
    startup = [float(r.get("startup_ms") or 0) for r in scored]
    network = [max(0.0, l - s) for l, s in zip(latencies, startup)]
    print(f"calls={len(rows)} scored={len(scored)} sampled_share={round(len(scored) / len(rows) * 100, 1) if rows else 0.0}%")
    print(f"blocked={sum(bool(r.get('blocked')) for r in rows)}")
    print(f"latency_ms_p50={_percentile(latencies, 50)} latency_ms_p95={_percentile(latencies, 95)} "
          f"startup_ms_p50={_percentile(startup, 50)} network_ms_p50={_percentile(network, 50)}")
    print("by_tool=" + json.dumps(bucket("tool"), sort_keys=True))
    print("by_role=" + json.dumps(bucket("role"), sort_keys=True))
    if export:
        chosen = random.sample(scored, min(max(0, n), len(scored)))
        with open(export, "w") as fh:
            for row in chosen:
                fh.write(json.dumps({**row, "label": ""}) + "\n")
        print(f"exported {len(chosen)} rows to {export}")


def _format_goal_line(e):
    counts = ",".join(f"{status}={len(items)}" for status, items in sorted(e["children"].items())) or "-"
    line = (f"{e['goal_id']}\tstatus={e['record_status']}\tplanner_alive={e['planner_alive']}\t"
            f"children=[{counts}]\tpr_url={e['pr_url'] or '-'}")
    if e.get("note"):
        line += f"\tnote={e['note']}"
    return line


def cost(by):
    agg = defaultdict(lambda: defaultdict(int))
    for f in sorted(RUNS.glob("*.jsonl")) if RUNS.exists() else []:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if "role" not in e:
                continue  # e.g. jev usage lines -- not a worker run, counted nowhere
            k = e.get(by, "?")
            for m in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                agg[k][m] += e.get(m, 0)
            agg[k]["runs"] += 1
    return dict(agg)


def _scorecard_measurement_totals(card, by):
    totals = {}
    for field in ("calls", "blocked", "turns"):
        values = [r[field] for r in card.values() if r[field] != "-"]
        totals[field] = sum(values) if values else "-"
    task_ids = set(card)
    if by == "goal":
        task_ids = set()
        for path in (scorecard.STATE / "tasks").glob("T-*.json"):
            task = json.loads(path.read_text())
            if task.get("parent") in card:
                task_ids.add(task["id"])
        task_ids.intersection_update(scorecard.by_task(root=scorecard.STATE))
    gate = scorecard._gate_stats(scorecard.STATE) or {}
    counts = [r for tid, r in gate.items() if tid in task_ids]
    calls = sum(r["calls"] for r in counts)
    totals["waste_pct"] = round(sum(r["waste"] for r in counts) / calls * 100, 1) if calls else "-"
    return totals


def main():
    ap = argparse.ArgumentParser(prog="orchestrator"); sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status"); st.add_argument("--plain", action="store_true")
    c = sub.add_parser("cost"); c.add_argument("--by", default="role", choices=["role", "tier", "account", "task"])
    h = sub.add_parser("hold"); h.add_argument("account"); h.add_argument("--minutes", type=int, default=30)
    sub.add_parser("resume").add_argument("account")
    pk = sub.add_parser("pick"); pk.add_argument("role", choices=["planner", "scout", "review", "execute"])
    pk.add_argument("--model", action="store_true")
    dm = sub.add_parser("daemon"); dm.add_argument("--once", action="store_true", help="run one pipeline tick and exit")
    ho = sub.add_parser("handover"); ho.add_argument("--reason", default="manual")
    m = sub.add_parser("merge"); m.add_argument("task"); m.add_argument("--target")
    rm = sub.add_parser("repomap"); rm.add_argument("--budget", type=int, default=4000)
    rm.add_argument("--stdout", action="store_true")
    ins = sub.add_parser("install"); ins.add_argument("target")
    p = sub.add_parser("post"); p.add_argument("task"); p.add_argument("--summary", required=True); p.add_argument("--status", default="done")
    sc = sub.add_parser("scorecard")
    sc.add_argument("--by", choices=["executor", "tier", "task", "goal", "band", "class", "role", "packet_version", "reviewed_executor"])
    sc.add_argument("--efficiency", action="store_true")
    sc.add_argument("--economics", action="store_true")
    sc.add_argument("--routing", action="store_true")
    sc.add_argument("--reviews", action="store_true")
    sc.add_argument("--parallelism", action="store_true")
    sc.add_argument("--scheduling", action="store_true")
    sc.add_argument("--strategies", action="store_true")
    sc.add_argument("--goal")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--planner", action="store_true")
    sc.add_argument("--planner-routing", action="store_true")
    sc.add_argument("--context", action="store_true")
    sc.add_argument("--reads", action="store_true")
    sc.add_argument("--handoffs", action="store_true")
    sc.add_argument("--economy", action="store_true")
    sc.add_argument("--skills", action="store_true")
    sc.add_argument("--group-by")
    sc.add_argument("--marginal")
    sc.add_argument("--redundancy", action="store_true")
    ce = sub.add_parser("context-eval"); ce.add_argument("--json", action="store_true"); ce.add_argument("--root")
    ex = sub.add_parser("explain"); ex.add_argument("task"); ex.add_argument("--json", action="store_true")
    pm = sub.add_parser("promotion"); pm.add_argument("--json", action="store_true")
    pr = sub.add_parser("planner-runs"); pr.add_argument("--summary", action="store_true")
    j = sub.add_parser("jev"); jsub = j.add_subparsers(dest="jev_cmd", required=True)
    jd = jsub.add_parser("diagnose"); jd.add_argument("--since"); jd.add_argument("--export"); jd.add_argument("--n", type=int, default=10)
    g = sub.add_parser("goal"); gsub = g.add_subparsers(dest="goal_cmd", required=True)
    gs = gsub.add_parser("start"); gs.add_argument("repo"); gs.add_argument("text")
    gs.add_argument("--account", default="A"); gs.add_argument("--reinstall", action="store_true")
    gst = gsub.add_parser("status"); gst.add_argument("repo"); gst.add_argument("id", nargs="?")
    gst.add_argument("--json", action="store_true")
    gl = gsub.add_parser("list"); gl.add_argument("repo"); gl.add_argument("--json", action="store_true")
    gsp = gsub.add_parser("stop"); gsp.add_argument("repo"); gsp.add_argument("id")
    sv = sub.add_parser("serve"); sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8090)
    bn = sub.add_parser("bench"); bsub = bn.add_subparsers(dest="bench_cmd", required=True)
    bf = bsub.add_parser("fetch"); bf.add_argument("--force", action="store_true"); bf.add_argument("--by", default="orchestrator")
    bsub.add_parser("show")
    bs = bsub.add_parser("set"); bs.add_argument("model_id"); bs.add_argument("--by", required=True); bs.add_argument("metrics", nargs="+", metavar="key=value")
    bl = sub.add_parser("baseline"); blsub = bl.add_subparsers(dest="baseline_cmd", required=True)
    blsave = blsub.add_parser("save"); blsave.add_argument("label"); blsave.add_argument("--since")
    blshow = blsub.add_parser("show"); blshow.add_argument("label"); blshow.add_argument("--json", action="store_true")
    blsub.add_parser("list")
    blcompare = blsub.add_parser("compare"); blcompare.add_argument("a"); blcompare.add_argument("b")
    blcompare.add_argument("--json", action="store_true")
    rs = sub.add_parser("roadmap-status")
    rs.add_argument("--json", action="store_true")
    rs.add_argument("--write", action="store_true")
    sk = sub.add_parser("skills"); sksub = sk.add_subparsers(dest="skills_cmd", required=True)
    sksync = sksub.add_parser("sync"); sksync.add_argument("--json", action="store_true")
    sklist = sksub.add_parser("list"); sklist.add_argument("--json", action="store_true")
    skshow = sksub.add_parser("show"); skshow.add_argument("id"); skshow.add_argument("--json", action="store_true")
    sklearn = sksub.add_parser("learn"); sklearn.add_argument("--since")
    sklearn.add_argument("--min-support", type=int, default=3); sklearn.add_argument("--propose", action="store_true")
    sktransition = sksub.add_parser("transition"); sktransition.add_argument("id"); sktransition.add_argument("state")
    sktransition.add_argument("--reason", required=True)
    skrollback = sksub.add_parser("rollback"); skrollback.add_argument("id")
    for command in ("discover", "import"):
        sksource = sksub.add_parser(command); sksource.add_argument("source")
    for command in ("inspect", "check-upstream", "quarantine"):
        skaction = sksub.add_parser(command); skaction.add_argument("id")
        if command == "quarantine":
            skaction.add_argument("--reason", default="external skill quarantine")
    a = ap.parse_args()
    if a.cmd == "scorecard":
        if (a.group_by is not None or a.marginal is not None or a.redundancy) and not a.skills:
            ap.error("--group-by, --marginal and --redundancy require --skills")
        skill_group_by = ("role", "task_class")
        if a.group_by is not None:
            skill_group_by = tuple(part.strip() for part in a.group_by.split(",") if part.strip())
            invalid = set(skill_group_by) - {"role", "task_class", "band", "model", "strategy", "repo"}
            if not skill_group_by or invalid:
                ap.error(("unknown --group-by dimension: " + ",".join(sorted(invalid))) if invalid
                         else "--group-by requires at least one dimension")
        if a.planner_routing and (a.planner or a.parallelism):
            ap.error("--planner-routing conflicts with --planner and --parallelism")
        if sum((a.planner_routing, a.context, a.reads, a.handoffs, a.economy, a.skills, a.economics, a.efficiency, a.routing, a.reviews, a.parallelism, a.scheduling, a.strategies)) > 1:
            ap.error("choose one of --economics, --efficiency, --routing, --reviews, --parallelism, --scheduling, --strategies, --planner-routing")
        groupings = {
            "default": ("executor", "tier", "task", "goal"),
            "--efficiency": ("goal", "executor", "band", "class", "role"),
            "--economics": ("executor", "band", "class"),
            "--routing": (),
            "--parallelism": (),
            "--reviews": ("role", "packet_version", "tier", "band", "reviewed_executor"),
            "--scheduling": (),
            "--strategies": (),
            "--planner-routing": (),
            "--context": (),
            "--reads": (),
            "--handoffs": (),
            "--economy": (),
            "--skills": (),
        }
        mode = next(("--" + name for name in ("efficiency", "economics", "routing", "reviews", "parallelism", "scheduling", "strategies")
                     if getattr(a, name)), "default")
        if a.planner_routing:
            mode = "--planner-routing"
        if a.context:
            mode = "--context"
        if a.reads:
            mode = "--reads"
        if a.handoffs:
            mode = "--handoffs"
        if a.economy:
            mode = "--economy"
        if a.skills:
            mode = "--skills"
        if a.goal is not None and not a.parallelism:
            ap.error("--goal requires --parallelism")
        if a.parallelism and a.planner:
            ap.error("--parallelism conflicts with --planner")
        allowed = groupings[mode]
        if a.by is not None and a.by not in allowed:
            choices = "|".join(allowed) if allowed else "none (omit --by)"
            ap.error(f"--by {a.by} is not supported by {mode}; {mode} supports --by {choices}")
        if mode == "default":
            a.by = a.by or "executor"
    if a.cmd == "skills":
        from . import skills_registry
        try:
            if a.skills_cmd in ("discover", "import", "inspect", "check-upstream", "quarantine"):
                from . import skill_discovery
                if a.skills_cmd in ("discover", "import"):
                    result = skill_discovery.discover(a.source)
                elif a.skills_cmd == "quarantine":
                    result = skills_registry.transition(a.id, "quarantined", a.reason)
                elif a.skills_cmd == "inspect":
                    result = skill_discovery.inspect(a.id)
                else:
                    result = skill_discovery.check_upstream(a.id)
                print(json.dumps(result, indent=2))
            elif a.skills_cmd == "sync":
                document = skills_registry.sync()
                print(json.dumps(document, indent=2) if a.json else f"synced {len(document['skills'])} skills")
            elif a.skills_cmd == "list":
                document = skills_registry.load()
                rows = document["skills"]
                if a.json:
                    print(json.dumps(rows, indent=2))
                else:
                    print("id\tstate\ttrust\troles\tl0/l2\tversion")
                    for skill_id, record in sorted(rows.items()):
                        print(f"{skill_id}\t{record['state']}\t{record['trust']}\t{','.join(record['roles'])}\t"
                              f"{record['est_tokens_l0']}/{record['est_tokens_l2']}\t{record['version']}")
            elif a.skills_cmd == "show":
                record = skills_registry.load()["skills"].get(a.id)
                if record is None:
                    raise KeyError(a.id)
                print(json.dumps(record, indent=2) if a.json else "\n".join(f"{key}: {value}" for key, value in record.items()))
            elif a.skills_cmd == "learn":
                from . import skill_learning
                found = skill_learning.patterns(since_s=a.since, min_support=a.min_support)
                if a.propose:
                    for pattern in found:
                        print(skill_learning.propose(pattern)["id"])
                else:
                    for pattern in found:
                        print(f"{pattern['source']}\t{pattern['support']}\t{pattern['confidence']:.3f}\t{pattern['procedure_signature']}")
            elif a.skills_cmd == "transition":
                print(json.dumps(skills_registry.transition(a.id, a.state, a.reason), indent=2))
            else:
                print(json.dumps(skills_registry.rollback(a.id), indent=2))
        except (KeyError, ValueError, OSError) as error:
            print(f"skills: {error}", file=sys.stderr)
            raise SystemExit(2)
    elif a.cmd == "roadmap-status":
        from . import roadmap
        output = ROOT / ".orchestrator" / "roadmap-status.json"
        report = roadmap.write(output) if a.write else roadmap.build(ROOT)
        if a.json:
            print(json.dumps(report, indent=2))
        elif a.write:
            print(output)
        else:
            print(json.dumps({"summary": report["summary"], "complete": report["complete"]}, indent=2))
    elif a.cmd == "status":
        if a.plain:
            s = Pool().status()
            for acc in s["accounts"]:
                print(f"{acc['id']}\tutil={acc['utilization']:.3f}\tcooling={acc['cooling_s']}s\t"
                     f"reason={acc['reason'] or '-'}\tplanner_day_tokens={acc['planner_day_tokens']}")
            c = s["codex"]
            print(f"codex\tavailable={c['available']}\trunning={c['running']}\tday_tasks={c['day_tasks']}\tcooling={c['cooling_s']}s")
        else:
            print(json.dumps({**Pool().status(), "queue": {s: len(bus.read(status=s)) for s in ("queued", "held", "running")}}, indent=1))
    elif a.cmd == "baseline":
        from . import baseline
        try:
            if a.baseline_cmd == "save":
                print(baseline.save(a.label, since=a.since))
            elif a.baseline_cmd == "list":
                print("label\tsaved_at")
                for label in baseline.list_labels():
                    print(f"{label}\t{baseline.load(label)['saved_at']}")
            elif a.baseline_cmd == "show":
                snapshot = baseline.load(a.label)
                print(json.dumps(snapshot, indent=2) if a.json else
                      scorecard.format_efficiency(snapshot['efficiency']['all']))
            else:
                result = baseline.compare(a.a, a.b)
                print(json.dumps(result, indent=2) if a.json else baseline.format_comparison(result))
        except (ValueError, OSError) as error:
            ap.error(str(error))
    elif a.cmd == "cost":
        print(json.dumps(cost(a.by), indent=1))
    elif a.cmd == "hold":
        pl = Pool(); pl.cooldown(pl.get(a.account), a.minutes * 60, "manual"); print(json.dumps(pl.status(), indent=1))
    elif a.cmd == "resume":
        pl = Pool(); pl.resume(a.account); print(json.dumps(pl.status(), indent=1))
    elif a.cmd == "pick":
        pl = Pool()
        try:
            pl.tally_planner()
        except Exception as e:
            print(f"pick: planner tally failed: {e}", file=sys.stderr)
        picked = pl.pick(a.role)
        if picked is None:
            print("hold: no account with headroom", file=sys.stderr)
            raise SystemExit(3)
        print(f"{picked.id}\t{os.path.expanduser(picked.config_dir)}")
        if a.model:
            from . import planner_router
            rcfg = planner_router.load_cfg(pl.cfg)
            mode = rcfg["mode"]
            if mode == "active":
                tier = rcfg["default_tier"]
                model = pl.cfg["models"][tier]
                reason = f"planner_routing active default tier {tier}"
            else:
                model = pl.cfg["models"]["planner"]
                reason = f"planner_routing {mode}: interactive Planner stays on the escalation tier"
            print(f"model\t{model}\t{reason}")
    elif a.cmd == "daemon":
        from .daemon import main as d; d(once=a.once)
    elif a.cmd == "handover":
        from . import handover
        print(handover.write(a.reason))
    elif a.cmd == "merge":
        from .merge import merge; print(json.dumps(merge(a.task, a.target), indent=1))
    elif a.cmd == "repomap":
        from .repomap import build
        # Leave a little room for command wrappers while keeping the requested value an upper bound.
        result = build(ROOT, max(0, a.budget - 100))
        if a.stdout:
            print(result, end="")
        else:
            output = ROOT / ".orchestrator" / "memory" / "architecture.md"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(result)
            print(output)
    elif a.cmd == "install":
        from .install import install
        for line in install(a.target):
            print(line)
        print(f'Next: orchestrator goal start {a.target} "<goal>"')
    elif a.cmd == "goal":
        from . import goals
        if a.goal_cmd == "start":
            r = goals.start(a.repo, a.text, account_id=a.account, reinstall=a.reinstall)
            if not r.get("launched"):
                print(r.get("reason", "not launched"))
                raise SystemExit(2)
            for k, v in r.items():
                print(f"{k}: {v}")
            if r.get("commit"):
                print(f"revert: git revert {r['commit']}")
        elif a.goal_cmd == "status":
            entries = goals.status(a.repo, a.id)
            if a.json:
                for e in entries:
                    print(json.dumps(e, indent=1))
            else:
                print("no goals" if not entries else "\n".join(_format_goal_line(e) for e in entries))
        elif a.goal_cmd == "list":
            entries = goals.list_goals(a.repo)
            if a.json:
                print("no goals" if not entries else "\n".join(json.dumps(e, indent=1) for e in entries))
            else:
                print("no goals" if not entries else "\n".join(_format_goal_line(e) for e in entries))
        elif a.goal_cmd == "stop":
            print(json.dumps(goals.stop(a.repo, a.id), indent=1))
    elif a.cmd == "serve":
        from .serve import main as serve_main
        serve_main(host=a.host, port=a.port)
    elif a.cmd == "post":
        print(json.dumps(bus.post_result(a.task, {"summary": a.summary}, a.status)["result"]))
    elif a.cmd == "context-eval":
        from . import context_eval
        real_state = ROOT / ".orchestrator"
        results = context_eval.run_all(a.root)
        document = context_eval.result_document(results)
        real_state.mkdir(parents=True, exist_ok=True)
        (real_state / "context_eval.json").write_text(json.dumps(document, indent=2) + "\n")
        print(json.dumps(document, indent=1) if a.json else context_eval.format_report(results))
        if not document["suite_passed"]:
            raise SystemExit(1)
    elif a.cmd == "scorecard":
        if a.skills:
            from . import skill_scorecard
            card = {"by_skill": skill_scorecard.by_skill(scorecard.STATE, skill_group_by)}
            if a.marginal:
                card["marginal"] = skill_scorecard.marginal(
                    scorecard.STATE, a.marginal, skill_group_by,
                    Pool().cfg.get("promotion", {}).get("min_samples", 20))
            if a.redundancy:
                card["redundancy"] = skill_scorecard.redundancy(scorecard.STATE)
            print(json.dumps(card, indent=1) if a.json else
                  skill_scorecard.format_skill_analysis(card, skill_group_by))
        elif a.economy:
            from . import context_scorecard, handoff_scorecard, promotion, read_economy, skill_scorecard
            context = context_scorecard.build(scorecard.STATE)
            handoffs = handoff_scorecard.by_start(scorecard.STATE)
            reads = read_economy.summary(scorecard.STATE)
            keys = set(context["by_role_task_class"])
            joined = {"/".join(key): {"context_and_tools": context["by_role_task_class"].get(key),
                                      "reads": {"role": reads["by_role"].get(key[0]),
                                                "task_class": reads["by_task_class"].get(key[1])}}
                      for key in sorted(keys)}
            handoffs_by_executor = {"/".join(key): value for key, value in sorted(handoffs.items())}
            try:
                last_eval = json.loads((scorecard.STATE / "context_eval.json").read_text())
            except (OSError, ValueError):
                last_eval = None
            features = ("context_router", "tool_disclosure", "conditional_instructions", "handoff_routing")
            card = {"by_role_task_class": joined, "skills": skill_scorecard.build(scorecard.STATE),
                    "handoffs_by_executor_task_class": handoffs_by_executor,
                    "promotion": [promotion.evaluate(name, promotion.collect(name, scorecard.STATE), Pool().cfg)
                                  for name in features], "last_context_eval": last_eval}
            warnings = []
            skill_ids = sorted({row["skill"] for row in skill_scorecard.by_skill(scorecard.STATE)})
            for skill_id in skill_ids:
                for row in skill_scorecard.marginal(
                        scorecard.STATE, skill_id,
                        min_samples=Pool().cfg.get("promotion", {}).get("min_samples", 20)):
                    if not row.get("insufficient") and row.get("verdict") in ("costly", "harmful"):
                        warnings.append({"skill": skill_id, **row})
            card["skills"]["costly_or_harmful"] = sorted(
                warnings, key=lambda row: (row["verdict"] != "harmful", row["skill"]))[:3]
            if a.json:
                print(json.dumps(card, indent=1))
            else:
                status = "never run" if not last_eval else f"{last_eval.get('ran_at')} {last_eval.get('git_head')} passed={last_eval.get('suite_passed')}"
                print("Last context-eval result: " + status)
                print("role/class\tcontext+tools\treads")
                for key, value in joined.items(): print(f"{key}\t{value['context_and_tools']}\t{value['reads']}")
                print("executor/class\thandoffs")
                for key, value in handoffs_by_executor.items(): print(f"{key}\t{value}")
                print(promotion.format_report(card["promotion"]))
        elif a.handoffs:
            from . import handoff_scorecard
            card = handoff_scorecard.build(root=scorecard.STATE, cfg=Pool().cfg)
            print(json.dumps(card, indent=1) if a.json else handoff_scorecard.format_report(card))
        elif a.reads:
            from . import read_economy
            card = read_economy.summary(scorecard.STATE)
            print(json.dumps(card, indent=1) if a.json else read_economy.format_summary(card))
        elif a.context:
            from . import context_scorecard
            card = context_scorecard.build(root=scorecard.STATE)
            print(json.dumps(card, indent=1) if a.json else context_scorecard.format_report(card))
        elif a.planner_routing:
            from . import planner_scorecard
            card = planner_scorecard.build(root=scorecard.STATE)
            print(json.dumps(card, indent=1) if a.json else planner_scorecard.format(card))
        elif a.scheduling:
            from . import sched_scorecard
            card = sched_scorecard.build()
            print(json.dumps(card, indent=1) if a.json else sched_scorecard.format(card))
        elif a.strategies:
            from . import strategy
            card = strategy.scorecard()
            serializable = {"/".join(key) if isinstance(key, tuple) else key: value
                            for key, value in card.items()}
            print(json.dumps(serializable, indent=1) if a.json else strategy.format(card))
        elif a.parallelism:
            card = scorecard.parallelism(root=scorecard.STATE, goal=a.goal)
            print(json.dumps(card, indent=1) if a.json else scorecard.format_parallelism(card))
        elif a.reviews:
            card = scorecard.review_quality(root=scorecard.STATE, by=a.by or "role")
            print(json.dumps(card, indent=1) if a.json else scorecard.format_review_quality(card))
        elif a.routing:
            card = scorecard.routing_eval(root=scorecard.STATE)
            print(json.dumps(card, indent=1) if a.json else scorecard.format_routing_eval(card))
        elif a.economics:
            grouping = a.by or "executor"
            card = scorecard.executor_economics(root=scorecard.STATE, by=grouping)
            if a.json:
                serializable = {("/".join(key) if isinstance(key, tuple) else key): value
                                for key, value in card.items()}
                print(json.dumps(serializable, indent=1))
            else:
                print(scorecard.format_executor_economics(card))
        elif a.efficiency:
            card = scorecard.efficiency(root=scorecard.STATE, by=a.by)
            print(json.dumps(card, indent=1) if a.json else scorecard.format_efficiency(card))
        elif a.planner:
            from . import planner_runs
            summary = planner_runs.premium_summary(root=scorecard.STATE)
            print(json.dumps(summary, indent=1) if a.json else scorecard.format_premium_summary(summary))
        elif a.by in ("executor", "tier"):
            card = scorecard.build(by=a.by)
            sc = scorecard.scores(card)
            if a.json:
                print(json.dumps(card, indent=1))
            else:
                print("id\tmerged\tfailed\trounds_avg\twall_s\tusd\thits\tscore")
                for eid, r in sorted(card.items()):
                    print(f"{eid}\t{r['merged']}\t{r['failed']}\t{r['rounds_avg']}\t{round(r['wall_s'])}\t"
                          f"{round(r['usd'], 2)}\t{r['held_usage_limit']}\t{round(sc.get(eid, 1.0), 3)}")
        elif a.by == "task":
            card = scorecard.by_task()
            if a.json:
                print(json.dumps(card, indent=1))
            else:
                print("task\trole\ttier\tusd\ttokens\twall_s\tcalls\twaste_pct\tblocked\tturns")
                total_usd = total_tokens = total_wall = 0.0
                for tid, r in sorted(card.items(), key=lambda kv: kv[1]["usd"], reverse=True):
                    print(f"{tid}\t{r['role'] or '-'}\t{r['tier'] or '-'}\t{round(r['usd'], 2)}\t"
                          f"{r['tokens']}\t{round(r['wall_s'])}\t{r['calls']}\t{r['waste_pct']}\t"
                          f"{r['blocked']}\t{r['turns']}")
                    total_usd += r["usd"]; total_tokens += r["tokens"]; total_wall += r["wall_s"]
                totals = _scorecard_measurement_totals(card, "task")
                print(f"total\t-\t-\t{round(total_usd, 2)}\t{total_tokens}\t{round(total_wall)}\t"
                      f"{totals['calls']}\t{totals['waste_pct']}\t{totals['blocked']}\t{totals['turns']}")
        elif a.by == "goal":
            card = scorecard.by_goal()
            accepted_tokens = scorecard.tokens_per_accepted_goal(root=scorecard.STATE)
            if a.json:
                print(json.dumps({**card, "tokens_per_accepted_goal": accepted_tokens}, indent=1))
            else:
                print("goal\tusd\texecute%\treview%\tspec_review%\tscout%\tother%\tplanner_runs\ttotal_tokens\tuncached\tcache_read\toutput\tjev\tplanner\troute\tcalls\twaste_pct\tturns")
                total_usd = 0.0
                for gid, r in sorted(card.items(), key=lambda kv: kv[1]["total_usd"], reverse=True):
                    pct = scorecard.goal_percentages(r)
                    runs_cell = scorecard.format_planner_runs_cell(r)
                    route_cell = ", ".join(f"{name}={count}" for name, count in sorted(r.get("routes", {}).items())) or "-"
                    print(f"{gid}\t{round(r['total_usd'], 2)}\t{round(pct['execute'], 1)}%\t"
                          f"{round(pct['review'], 1)}%\t{round(pct['spec_review'], 1)}%\t{round(pct['scout'], 1)}%\t"
                          f"{round(pct['other'], 1)}%\t{runs_cell}\t{r['total_tokens']}\t{r['tokens_uncached']}\t"
                          f"{r['tokens_cache_read']}\t{r['tokens_output']}\t{r['jev_tokens']}\t{r['planner_tokens']}\t"
                          f"{route_cell}\t{r['calls']}\t{r['waste_pct']}\t{r['turns']}")
                    total_usd += r["total_usd"]
                totals = _scorecard_measurement_totals(card, "goal")
                print(f"total\t{round(total_usd, 2)}\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t"
                      f"-\t{totals['calls']}\t{totals['waste_pct']}\t{totals['turns']}")
                for gid, r in sorted(card.items()):
                    print(f"tokens per task ({gid}): {scorecard.format_task_tokens_cell(r)}")
                print(scorecard.planner_footer())
                accepted_usd = scorecard.usd_per_accepted_goal(root=scorecard.STATE)
                ratio = ("undefined (0 accepted goals)" if accepted_tokens['tokens'] is None
                         else str(round(accepted_tokens['tokens'])))
                print(f"tokens per accepted goal: {ratio} over {accepted_tokens['count']} goals "
                      f"(usd {round(accepted_usd['usd'], 2)})")
    elif a.cmd == "explain":
        from . import decision_log
        rows = decision_log.explain(a.task)
        print(json.dumps(rows, indent=1) if a.json else decision_log.format_explain(rows))
    elif a.cmd == "promotion":
        from . import promotion
        rows = promotion.report()
        print(json.dumps(rows, indent=1) if a.json else promotion.format_report(rows))
    elif a.cmd == "planner-runs":
        from . import planner_runs
        s = planner_runs.summary()
        print(f"decisions={s['decisions']}\tjev_scored={s['jev_scored']}\t"
              f"agreement_rate={s['agreement_rate']}\tmean_confidence={s['mean_confidence']}")
    elif a.cmd == "jev" and a.jev_cmd == "diagnose":
        _jev_diagnose(a.since, a.export, a.n)
    elif a.cmd == "bench":
        from . import bench
        if a.bench_cmd == "fetch":
            result = bench.fetch(force=a.force, by=a.by)
            if "skipped" in result:
                print(result["skipped"])
            else:
                matched = sum(1 for v in result["models"].values() if v)
                print(f"matched {matched}/{len(result['models'])}; unmatched sample: {result['unmatched_names'][:5]}")
                print(bench.FILE)
        elif a.bench_cmd == "show":
            data = bench.load()
            print("model_id\tname\tintelligence\tcoding\tspeed\tprice_in\tprice_out\tfetched_at/manual")
            for mid, rec in sorted((data.get("models") or {}).items()):
                if not rec:
                    print(f"{mid}\t-\t-\t-\t-\t-\t-\tunmatched"); continue
                stamp = "manual" if rec.get("manual") else data.get("fetched_at", "-")
                print(f"{mid}\t{rec.get('name', '-')}\t{rec.get('intelligence', '-')}\t{rec.get('coding', '-')}\t"
                      f"{rec.get('speed_tps', '-')}\t{rec.get('price_in', '-')}\t{rec.get('price_out', '-')}\t{stamp}")
        elif a.bench_cmd == "set":
            metrics = {}
            for kv in a.metrics:
                k, _, v = kv.partition("=")
                try:
                    v = float(v)
                except ValueError:
                    pass
                metrics[k] = v
            print(json.dumps(bench.set_model(a.model_id, a.by, **metrics), indent=1))


if __name__ == "__main__":
    main()
