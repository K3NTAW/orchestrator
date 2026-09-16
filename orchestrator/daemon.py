"""Heartbeat: requeue running tasks whose process died, notify when both accounts cool >30 min or a budget trips.
Timeouts are enforced by the spawner itself (subprocess timeout); this loop only catches crashes."""
import os, subprocess, sys, time
from . import bus
from .pool import Pool


def notify(msg):
    print(f"[notify] {msg}", file=sys.stderr)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "orchestrator"'], check=False)


def alive(pid):
    try:
        os.kill(pid, 0); return True
    except (OSError, TypeError):
        return False


def tick(pool):
    for t in bus.read(status="running"):
        if t.get("pid") and not alive(t["pid"]) and time.time() - t.get("claimed_at", 0) > 60:
            bus.update(t["id"], status="queued", pid=None, reason="process died; requeued")
    m = pool.both_cooling_minutes()
    if m > 30:
        notify(f"both Claude accounts cooling for {m:.0f} more min")
    for a in pool.accounts:
        if a.daily_budget and a.day_tokens >= a.daily_budget:
            notify(f"account {a.id} hit its daily budget; tasks held")
    if not pool.codex_available() and pool.codex.cooling():
        notify("Executor (Codex) cooling; execute tasks held, refill the pipeline")


def main(interval=30):
    pool = Pool()
    while True:
        tick(pool); time.sleep(interval)


if __name__ == "__main__":
    main()
