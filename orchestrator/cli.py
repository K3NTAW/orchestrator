"""orchestrator status | cost [--by role|tier|account|task] | hold A [--minutes] | resume A | daemon | merge T-0001 | post T-0001 --summary ..."""
import argparse, json
from collections import defaultdict
from . import bus
from .bus import RUNS
from .pool import Pool


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
    sub.add_parser("daemon")
    m = sub.add_parser("merge"); m.add_argument("task"); m.add_argument("--target")
    p = sub.add_parser("post"); p.add_argument("task"); p.add_argument("--summary", required=True); p.add_argument("--status", default="done")
    a = ap.parse_args()
    if a.cmd == "status":
        if a.plain:
            s = Pool().status()
            for acc in s["accounts"]:
                print(f"{acc['id']}\tutil={acc['utilization']:.3f}\tcooling={acc['cooling_s']}s\treason={acc['reason'] or '-'}")
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
    elif a.cmd == "daemon":
        from .daemon import main as d; d()
    elif a.cmd == "merge":
        from .merge import merge; print(json.dumps(merge(a.task, a.target), indent=1))
    elif a.cmd == "post":
        print(json.dumps(bus.post_result(a.task, {"summary": a.summary}, a.status)["result"]))


if __name__ == "__main__":
    main()
