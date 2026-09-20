"""orchestrator status | cost [--by role|tier|account|task] | hold A [--minutes] | resume A | pick planner|scout|review|execute | daemon [--once] | merge T-0001 | install /path/to/target | post T-0001 --summary ..."""
import argparse, json, os, sys
from collections import defaultdict
from . import bus, scorecard
from .bus import RUNS
from .pool import Pool


def _format_goal_line(e):
    counts = ",".join(f"{status}={len(items)}" for status, items in sorted(e["children"].items())) or "-"
    return (f"{e['goal_id']}\tstatus={e['record_status']}\tplanner_alive={e['planner_alive']}\t"
            f"children=[{counts}]\tpr_url={e['pr_url'] or '-'}")


def cost(by):
    agg = defaultdict(lambda: defaultdict(int))
    for f in sorted(RUNS.glob("*.jsonl")) if RUNS.exists() else []:
        for line in f.read_text().splitlines():
            e = json.loads(line); k = e.get(by, "?")
            for m in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                agg[k][m] += e.get(m, 0)
            agg[k]["runs"] += 1
    return dict(agg)


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
    ins = sub.add_parser("install"); ins.add_argument("target")
    p = sub.add_parser("post"); p.add_argument("task"); p.add_argument("--summary", required=True); p.add_argument("--status", default="done")
    sc = sub.add_parser("scorecard"); sc.add_argument("--by", default="executor", choices=["executor", "tier"])
    sc.add_argument("--json", action="store_true")
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
    a = ap.parse_args()
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
        card = scorecard.build(by=a.by)
        sc = scorecard.scores(card)
        if a.json:
            print(json.dumps(card, indent=1))
        else:
            print("id\tmerged\tfailed\trounds_avg\twall_s\tusd\thits\tscore")
            for eid, r in sorted(card.items()):
                print(f"{eid}\t{r['merged']}\t{r['failed']}\t{r['rounds_avg']}\t{round(r['wall_s'])}\t"
                      f"{round(r['usd'], 2)}\t{r['held_usage_limit']}\t{round(sc.get(eid, 1.0), 3)}")
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
