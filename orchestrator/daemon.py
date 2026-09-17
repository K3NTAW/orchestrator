"""Heartbeat and pipeline driver: requeue running tasks whose process died, notify when both accounts cool >30 min
or a budget trips, and walk every task one stage forward — dispatch -> gate -> review -> merge — so a goal advances
without the Planner in the loop. Timeouts are enforced by the spawner itself (subprocess timeout); this loop only
catches crashes. Every stage stamps `pipeline.<stage>_at` on the task json under the bus lock before it acts, so a
stage runs at most once no matter how often tick() runs."""
import os, subprocess, sys, threading, time
from . import bus, executor, merge, spawn
from .pool import Pool

SPEC_REVIEW_MIN = 5   # complexity at which a spec must be reviewed before an executor sees it
DIRECT_MERGE_MAX = 3  # complexity at or below which hooks are the whole review (CLAUDE.md step 7)


def notify(msg):
    print(f"[notify] {msg}", file=sys.stderr)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "orchestrator"'], check=False)


def alive(pid):
    try:
        os.kill(pid, 0); return True
    except (OSError, TypeError):
        return False


def stamp(tid, stage, **fields):
    """Claim one pipeline stage for one task. Returns False when another tick already claimed it. The read of the
    existing stamp and the write of the new one happen under the same bus lock, so two ticks cannot both win."""
    with bus.locked():
        t = bus.get(tid)
        pipeline = dict(t.get("pipeline") or {})
        if pipeline.get(stage):
            return False
        pipeline[stage] = time.time()
        bus.update(tid, pipeline=pipeline, **fields)
    return True


def free_slots(pool):
    """How many execute dispatches this tick may make: the executor pool's idle parallelism. Bounds tick()'s work
    so a queue of forty ready tasks does not fork forty subprocesses at once."""
    return sum(max(0, ex.max_parallel - ex.running) for ex in pool.executors.values()
               if ex.enabled and "execute" in ex.roles and not ex.cooling())


def spawn_async(task_id):
    threading.Thread(target=spawn.run_worker, args=(task_id,), daemon=True).start()


def dispatch(pool):
    """queued execute tasks whose dependencies are merged: hand to the executor, or route through spec review first."""
    slots = free_slots(pool)
    for t in bus.read(status="queued", role="execute"):
        if not bus.ready(t):
            continue
        verdict = t.get("spec_review_verdict")
        if t["complexity"] < SPEC_REVIEW_MIN or verdict == "approve":
            if slots <= 0:
                break
            if stamp(t["id"], "dispatched_at"):
                slots -= 1
                prompt = spawn.render("execute", spec=t["spec"], acceptance=t["acceptance"], scope=t["scope"])
                executor.start(t["id"], prompt)
        elif verdict == "request_changes":
            if stamp(t["id"], "spec_review_held_at", status="held", hold_reason="spec_review request_changes"):
                notify(f"{t['id']}: spec review asked for changes; re-spec it")
        elif not any(r["inputs"][:1] == [t["id"]] for r in bus.read(role="spec_review")):
            if stamp(t["id"], "spec_review_at"):
                sr = bus.create_task(f"spec review: {t['title']}", t["spec"], t["acceptance"], t["scope"],
                                     role="spec_review", inputs=[t["id"]], parent=t.get("parent"),
                                     complexity=t["complexity"])
                spawn_async(sr["id"])


def gate(pool):
    """done execute tasks that have not been gated: run tests-green on the worktree, then merge (cheap tasks) or
    open a review task (everything else)."""
    for t in bus.read(status="done", role="execute"):
        if t.get("merged_into") or (t.get("pipeline") or {}).get("gated_at") or not t.get("worktree"):
            continue
        tg = subprocess.run([str(merge.TESTS_GREEN), t["worktree"]], capture_output=True, text=True, input="{}")
        if tg.returncode:
            if stamp(t["id"], "gated_at", status="held", hold_reason="gate_red",
                     resume_hint={"failures": tg.stderr[-4000:]}):
                notify(f"{t['id']}: tests red at the gate; held")
            continue
        if not stamp(t["id"], "gated_at"):
            continue
        if t["complexity"] <= DIRECT_MERGE_MAX:
            report_merge(t["id"], merge.merge(t["id"]))
        else:
            r = bus.create_task(f"review: {t['title']}", t["spec"], t["acceptance"], t["scope"], role="review",
                                inputs=[t["id"]], parent=t.get("parent"), complexity=t["complexity"])
            spawn_async(r["id"])


def report_merge(task_id, r):
    if r.get("status") == "merged":
        notify(f"{task_id} merged into {r['target']} ({r['sha'][:8]})")
    else:                                  # merge.merge already set the task failed with a resume_hint
        notify(f"{task_id} merge failed: {r.get('status')} {r.get('reason', '')}".strip())
    return r


def merge_reviewed(pool):
    """done review tasks: approve -> serial merge of the reviewed task; request_changes -> hold it for the Planner,
    which writes the fix-round spec (a daemon must not invent a spec)."""
    for r in bus.read(status="done", role="review"):
        if not (r.get("inputs") and isinstance(r["inputs"][0], str)):
            continue
        try:
            src = bus.get(r["inputs"][0])
        except KeyError:
            continue
        if src.get("merged_into"):
            continue
        verdict = src.get("review_verdict") or r.get("review_verdict")
        if verdict == "approve":
            # stamped before the merge, not after: a conflict leaves merged_into unset, and retrying it every tick
            # would just rebuild the same conflict
            if stamp(r["id"], "merged_at"):
                report_merge(src["id"], merge.merge(src["id"]))
        elif verdict == "request_changes":
            if stamp(src["id"], "review_held_at", status="held", hold_reason="review request_changes"):
                notify(f"{src['id']}: review asked for changes; Planner writes the fix round")


def tick(pool=None):
    pool = pool or Pool()
    for t in bus.read(status="running"):
        if t.get("pid") and not alive(t["pid"]) and time.time() - t.get("claimed_at", 0) > 60:
            bus.update(t["id"], status="queued", pid=None, reason="process died; requeued")
    dispatch(pool)
    gate(pool)
    merge_reviewed(pool)
    m = pool.both_cooling_minutes()
    if m > 30:
        notify(f"both Claude accounts cooling for {m:.0f} more min")
    for a in pool.accounts:
        if a.daily_budget and a.day_tokens >= a.daily_budget:
            notify(f"account {a.id} hit its daily budget; tasks held")
    if not pool.codex_available() and pool.codex.cooling():
        notify("Executor (Codex) cooling; execute tasks held, refill the pipeline")


def main(interval=30, once=False):
    """A fresh Pool() per tick: cooldowns and running counts are written by the spawned workers, so a long-lived
    Pool would dispatch against minutes-old state."""
    while True:
        tick(Pool())
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
