"""orchestrator.daemon pipeline: dependency-gated dispatch, spec review for complexity >=5, the tests-green gate,
review routing and the serial merge — all driven by `tick()` with the four side-effecting calls (executor.start,
spawn.run_worker, merge.merge, subprocess.run) monkeypatched to record instead of act.

Each test gets its own bus directory (bus.STATE/TASKS/RUNS swapped) because bus.read() is global: without the swap
these ticks would pick up every execute task any other test file left queued in the shared TMP root."""
import sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_daemon.py` doesn't add this dir itself
from _harness import REPO, TMP, FakeProc  # noqa: F401
from orchestrator import bus, daemon, executor, merge, pool as P, spawn


def raiser(exc):
    def f(*a, **k):
        raise exc
    return f


class Daemon(unittest.TestCase):
    def setUp(self):
        sandbox = Path(tempfile.mkdtemp(prefix="orch-daemon-"))
        for name, value in (("STATE", sandbox), ("TASKS", sandbox / "tasks"), ("RUNS", sandbox / "runs")):
            self.swap(bus, name, value)
        P.PERSIST.unlink(missing_ok=True)                 # a cooldown another test persisted would zero free_slots
        self.addCleanup(P.PERSIST.unlink, True)
        self.started, self.workers, self.merged = [], [], []
        self.swap(executor, "start", lambda tid, prompt: self.started.append(tid))
        self.swap(spawn, "run_worker", lambda tid: self.workers.append(tid))
        self.swap(merge, "merge", lambda tid, target=None: (self.merged.append(tid),
                                                            {"status": "merged", "target": "goal/G", "sha": "abc12345"})[1])
        self.gate_green(True)

    def swap(self, mod, name, value):
        orig = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, orig)

    def gate_green(self, green):
        """daemon.subprocess.run covers both the tests-green gate and notify()'s osascript; neither needs a real run."""
        self.swap(daemon.subprocess, "run", lambda *a, **k: FakeProc("", 0 if green else 1))

    def task(self, title, complexity=2, role="execute", **fields):
        t = bus.create_task(title, "spec", ["works"], ["x.py"], role=role, complexity=complexity,
                            parent="T-0043", **fields)
        return t["id"]

    def settle(self, want, seconds=5):
        """spawn_async hands the worker to a thread; wait for the recorder rather than assume it already ran."""
        deadline = time.time() + seconds
        while len(self.workers) < want and time.time() < deadline:
            time.sleep(0.01)
        return self.workers

    def settle_started(self, want, seconds=5):
        """dispatch() now runs executor.start on a background thread too; wait for it the same way."""
        deadline = time.time() + seconds
        while len(self.started) < want and time.time() < deadline:
            time.sleep(0.01)
        return self.started

    def test_depends_on_gates_dispatch(self):
        a = self.task("A")
        b = self.task("B", depends_on=[a])
        daemon.tick()
        self.assertEqual(self.settle_started(1), [a])              # B's dependency has not merged
        bus.update(a, merged_into="goal/G", sha="deadbee")
        daemon.tick()
        self.assertEqual(self.settle_started(2), [a, b])           # A is not dispatched twice: dispatched_at is stamped

    def test_high_complexity_waits_for_spec_review(self):
        c = self.task("C", complexity=6)
        daemon.tick()
        daemon.tick()
        self.assertEqual(self.started, [])
        reviews = bus.read(role="spec_review")
        self.assertEqual(len(reviews), 1)                         # created once across two ticks
        self.assertEqual(reviews[0]["inputs"], [c])
        self.assertEqual(self.settle(1), [reviews[0]["id"]])
        self.assertTrue(bus.get(c)["pipeline"]["spec_review_at"])
        bus.update(c, spec_review_verdict="approve")
        daemon.tick()
        self.assertEqual(self.settle_started(1), [c])

    def test_spec_review_request_changes_holds(self):
        c = self.task("C", complexity=7)
        bus.update(c, spec_review_verdict="request_changes")
        daemon.tick()
        self.assertEqual(self.started, [])
        held = bus.get(c)
        self.assertEqual((held["status"], held["hold_reason"]), ("held", "spec_review request_changes"))

    def test_green_gate_merges_cheap_task_and_reviews_the_rest(self):
        cheap = self.task("cheap", complexity=2)
        bus.update(cheap, status="done", worktree=str(TMP))
        big = self.task("big", complexity=5)
        bus.update(big, status="done", worktree=str(TMP))
        daemon.tick()
        daemon.tick()
        self.assertEqual(self.merged, [cheap])                    # complexity <=3: hooks are the whole review
        reviews = bus.read(role="review")
        self.assertEqual([(r["inputs"], r["complexity"]) for r in reviews], [([big], 5)])  # created once, not twice
        self.assertEqual(self.settle(1), [reviews[0]["id"]])
        self.assertTrue(bus.get(big)["pipeline"]["gated_at"])

    def test_red_gate_holds_without_review(self):
        t = self.task("red", complexity=5)
        bus.update(t, status="done", worktree=str(TMP))
        self.gate_green(False)
        daemon.tick()
        held = bus.get(t)
        self.assertEqual((held["status"], held["hold_reason"]), ("held", "gate_red"))
        self.assertEqual(bus.read(role="review"), [])
        self.assertEqual(self.merged, [])

    def test_review_verdict_drives_merge_or_hold(self):
        ok = self.gated_execute("approved")
        r_ok = self.task("review ok", complexity=5, role="review", inputs=[ok])
        bus.update(ok, review_verdict="approve"); bus.update(r_ok, status="done", review_verdict="approve")
        bad = self.gated_execute("rejected")
        r_bad = self.task("review bad", complexity=5, role="review", inputs=[bad])
        bus.update(bad, review_verdict="request_changes"); bus.update(r_bad, status="done", review_verdict="request_changes")
        daemon.tick()
        daemon.tick()
        self.assertEqual(self.merged, [ok])                       # merged once, not once per tick
        held = bus.get(bad)
        self.assertEqual((held["status"], held["hold_reason"]), ("held", "review request_changes"))

    def gated_execute(self, title):
        """A done execute task that already cleared the gate, so gate() leaves it to merge_reviewed()."""
        t = self.task(title, complexity=5)
        bus.update(t, status="done", worktree=str(TMP), pipeline={"gated_at": time.time()})
        return t

    def test_dispatch_does_not_block(self):
        a = self.task("A")
        self.swap(executor, "start", lambda tid, prompt: (time.sleep(2), self.started.append(tid)))
        t0 = time.time()
        daemon.tick()
        self.assertLess(time.time() - t0, 0.5)                     # tick() returned before the sleep(2) finished
        stamp1 = bus.get(a)["pipeline"]["dispatched_at"]
        daemon.tick()
        self.assertLess(time.time() - t0, 1.0)
        stamp2 = bus.get(a)["pipeline"]["dispatched_at"]
        self.assertEqual(stamp1, stamp2)                           # not dispatched a second time
        self.assertEqual(self.settle_started(1), [a])

    def test_gate_side_effect_failure_holds(self):
        t = self.task("cheap", complexity=2)
        bus.update(t, status="done", worktree=str(TMP))
        self.swap(merge, "merge", raiser(RuntimeError("merge blew up")))
        daemon.tick()                                              # must return normally, not raise
        held = bus.get(t)
        self.assertEqual(held["status"], "held")
        self.assertTrue(held["hold_reason"].startswith("gate failed"), held["hold_reason"])
        self.assertIn("merge blew up", held["pipeline"]["gated_error"])

    def test_gate_missing_worktree_holds(self):
        t = self.task("nowt", complexity=2)
        bus.update(t, status="done", worktree=str(TMP / "does-not-exist"))
        daemon.tick()
        held = bus.get(t)
        self.assertEqual((held["status"], held["hold_reason"]), ("held", "worktree missing"))

    def test_tick_skips_execute_task_of_closed_goal(self):
        closed_goal = bus.create_task("goal closed", "s", ["ok"], ["x.py"], role="scout")["id"]
        bus.update(closed_goal, status="done")
        open_goal = bus.create_task("goal open", "s", ["ok"], ["x.py"], role="scout")["id"]
        stale_child = bus.create_task("stale child", "spec", ["works"], ["x.py"], role="execute",
                                      complexity=2, parent=closed_goal)["id"]
        fresh_child = bus.create_task("fresh child", "spec", ["works"], ["x.py"], role="execute",
                                      complexity=2, parent=open_goal)["id"]
        daemon.tick()
        self.assertEqual(self.settle_started(1), [fresh_child])   # stale_child's goal is done; never touched
        self.assertFalse(bus.get(stale_child).get("pipeline"))

    def test_notify_argv_safe(self):
        calls = []
        self.swap(daemon.subprocess, "run", lambda *a, **k: calls.append(a[0]) or FakeProc("", 0))
        msg = 'hi" & do shell script "echo pwned'
        daemon.notify(msg)
        cmd = calls[-1]
        self.assertEqual(cmd[-1], msg[:200])                       # untrusted text only ever lands in argv
        self.assertTrue(all(msg not in part for part in cmd[:-1]))


class BusLock(unittest.TestCase):
    def test_writes_take_the_flock(self):
        taken = []
        orig = bus.locked
        def counting():
            taken.append(1)
            return orig()
        bus.locked = counting
        self.addCleanup(setattr, bus, "locked", orig)
        t = bus.create_task("locked", "s", ["a"], ["x.py"])
        n = len(taken)
        self.assertGreaterEqual(n, 1)                             # create_task -> _save
        bus.update(t["id"], status="held")
        self.assertGreater(len(taken), n)                         # update -> its own lock, reentrant into _save
        self.assertTrue(bus.LOCK.exists())
        self.assertEqual(bus.LOCK.name, "bus.lock")


class Background(unittest.TestCase):
    """daemon.start_background(): the autostart the orchestrator MCP server calls on import. Uses the real
    STATE/daemon.lock (not the per-test bus sandbox above — the lock is process-wide by design), so every test
    that gets a Thread back must release it via stop_background in cleanup or the next test finds the lock held."""

    def test_autostart_false_returns_none(self):
        self.assertIsNone(daemon.start_background({"daemon": {"autostart": False}}))

    def test_no_daemon_section_returns_none(self):
        self.assertIsNone(daemon.start_background({}))

    def test_env_override_returns_none(self):
        thread = daemon.start_background({"daemon": {"autostart": True}}, env={"ORCH_DAEMON": "0"})
        self.assertIsNone(thread)

    def test_autostart_true_starts_thread_and_holds_the_lock(self):
        t1 = daemon.start_background({"daemon": {"autostart": True, "interval_s": 60}})
        self.addCleanup(daemon.stop_background, t1)
        self.assertIsNotNone(t1)
        self.assertTrue(t1.is_alive())
        t2 = daemon.start_background({"daemon": {"autostart": True, "interval_s": 60}})
        self.assertIsNone(t2)                                      # lock already held by t1
        daemon.stop_background(t1)
        self.assertFalse(t1.is_alive())
        t3 = daemon.start_background({"daemon": {"autostart": True, "interval_s": 60}})
        self.addCleanup(daemon.stop_background, t3)
        self.assertIsNotNone(t3)                                   # lock released, a fresh start_background works


if __name__ == "__main__":
    unittest.main()
