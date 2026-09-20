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
    dm = sub.add_parser("daemon"); dm.add_argument("--once", action="store_true", help="run one pipeline tick and exit")
    ho = sub.add_parser("handover"); ho.add_argument("--reason", default="manual")
    m = sub.add_parser("merge"); m.add_argument("task"); m.add_argument("--target")
    rm = sub.add_parser("repomap"); rm.add_argument("--budget", type=int, default=4000)
    rm.add_argument("--stdout", action="store_true")
    ins = sub.add_parser("install"); ins.add_argument("target")
    p = sub.add_parser("post"); p.add_argument("task"); p.add_argument("--summary", required=True); p.add_argument("--status", default="done")
    sc = sub.add_parser("scorecard")
    sc.add_argument("--by", choices=["executor", "tier", "task", "goal", "band", "class", "role"])
    sc.add_argument("--efficiency", action="store_true")
    sc.add_argument("--economics", action="store_true")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--planner", action="store_true")
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
    a = ap.parse_args()
    if a.cmd == "scorecard":
        if a.economics and a.by not in (None, "executor", "band", "class"):
            ap.error("--economics supports --by executor|band|class")
        if a.efficiency and a.by in ("tier", "task"):
            ap.error("--efficiency supports --by goal|executor|band|class|role")
        if not a.efficiency and not a.economics:
            a.by = a.by or "executor"
            if a.by in ("band", "class", "role"):
                ap.error("this --by requires --efficiency")
    if a.cmd == "status":
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
    elif a.cmd == "scorecard":
        if a.economics:
            grouping = a.by or "executor"
            if grouping not in ("executor", "band", "class"):
                ap.error("--economics --by must be executor, band, or class")
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
