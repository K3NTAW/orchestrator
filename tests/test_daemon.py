"""orchestrator.daemon pipeline: dependency-gated dispatch, spec review for complexity >=5, the tests-green gate,
review routing and the serial merge — all driven by `tick()` with the four side-effecting calls (executor.start,
spawn.run_worker, merge.merge, subprocess.run) monkeypatched to record instead of act.

Each test gets its own bus directory (bus.STATE/TASKS/RUNS swapped) because bus.read() is global: without the swap
these ticks would pick up every execute task any other test file left queued in the shared TMP root."""
import http.server, os, sys, tempfile, threading, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_daemon.py` doesn't add this dir itself
from _harness import REPO, TMP, FakeProc, g, scratch_repo  # noqa: F401
from orchestrator import bus, daemon, executor, merge, pool as P, spawn

REAL_RUN = daemon.subprocess.run  # captured before any test's gate_green() fakes the shared subprocess module


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
        # README now tells operators to export ORCH_NOTIFY_URL/ORCH_NOTIFY_DESKTOP ambiently; without clearing
        # them here every notify() call in this suite would fire a real webhook POST or osascript popup.
        self.clear_env("ORCH_NOTIFY_URL")
        self.clear_env("ORCH_NOTIFY_DESKTOP")
        self.gate_green(True)

    def swap(self, mod, name, value):
        orig = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, orig)

    def clear_env(self, name):
        had = name in os.environ
        orig = os.environ.pop(name, None)
        self.addCleanup(lambda: os.environ.__setitem__(name, orig) if had else os.environ.pop(name, None))

    def set_env(self, name, value):
        had = name in os.environ
        orig = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.__setitem__(name, orig) if had else os.environ.pop(name, None))

    def gate_green(self, green):
        """daemon.subprocess.run covers the tests-green gate, notify()'s osascript, and already_merged()'s git
        ancestry check (daemon.subprocess IS the stdlib subprocess module, shared with spawn.git); the first two
        never need a real run, but the git check does, so only those two are faked and everything else -- git
        calls -- passes through to the real subprocess.run."""
        def fake(*a, **k):
            argv = a[0]
            if argv[:1] == [str(merge.TESTS_GREEN)]:
                return FakeProc("", 0 if green else 1)
            if argv[:1] == ["osascript"]:
                return FakeProc("", 0)
            return REAL_RUN(*a, **k)
        self.swap(daemon.subprocess, "run", fake)

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

    def test_free_slots_uses_claude_capacity_under_fallback(self):
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)                        # cools the whole "chatgpt" quota group
        self.assertEqual(pool.cfg["codex"]["on_exhausted"], "fallback_claude")
        self.assertEqual(daemon.free_slots(pool), pool.cfg["limits"]["max_parallel_claude_workers"])

    def test_free_slots_zero_under_hold_policy(self):
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)
        pool.cfg["codex"]["on_exhausted"] = "hold"
        self.assertEqual(daemon.free_slots(pool), 0)

    def test_free_slots_subtracts_running_claude_executes(self):
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)
        a = self.task("running A", complexity=3)
        bus.update(a, status="running", executor="claude:sonnet")
        b = self.task("running B", complexity=3)
        bus.update(b, status="running", executor="claude:opus")
        want = pool.cfg["limits"]["max_parallel_claude_workers"] - 2
        self.assertEqual(daemon.free_slots(pool), want)

    def test_daemon_once_dispatches_under_fallback_when_codex_cooling(self):
        """§T-0083 acceptance: with pool.toml as committed and every Codex executor cooling, a single tick still
        dispatches a queued low-complexity execute task instead of stalling at free_slots()==0 (gotchas.md
        2026-09-18: T-0070 needed a hand dispatch before this fix)."""
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)
        a = self.task("A", complexity=3)
        daemon.tick(pool)
        self.assertTrue(bus.get(a)["pipeline"].get("dispatched_at"))

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

    def test_gate_review_tier_opus_for_sonnet_executor(self):
        t = self.task("big", complexity=5)
        bus.update(t, status="done", worktree=str(TMP), executor="claude:sonnet")
        daemon.tick()
        self.assertEqual(bus.read(role="review")[0]["tier"], "opus")

    def test_gate_review_tier_sonnet_for_opus_executor(self):
        t = self.task("big", complexity=5)
        bus.update(t, status="done", worktree=str(TMP), executor="claude:opus")
        daemon.tick()
        self.assertEqual(bus.read(role="review")[0]["tier"], "sonnet")

    def test_gate_review_tier_default_for_codex(self):
        t = self.task("big", complexity=5)
        bus.update(t, status="done", worktree=str(TMP), executor="astra")
        daemon.tick()
        self.assertEqual(bus.read(role="review")[0]["tier"], "sonnet")

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

    def test_ancestor_merged_task_skips_gate_and_review(self):
        """A fix-round merge stamps merged_into only on the fix-round task; the original stays done with
        merged_into unset even though its commit already landed in goal/G. The gate must detect that by
        ancestry and never re-gate or re-review it, while a task whose branch truly hasn't landed still is."""
        # TMP is a module-wide sandbox shared with every other test in this file, but bus task ids restart at
        # T-0001 per test (fresh sandbox in setUp), so the task/T-000N branches created here must be deleted
        # again in cleanup -- otherwise a later test's identically-numbered task would find them still on disk
        # and see itself as already merged. setUp()'s gate_green(True) passes git calls through to the real
        # subprocess.run (see gate_green's docstring), so the checkout/commit/merge below run for real.
        scratch_repo(TMP)
        self.addCleanup(lambda: (TMP / "landed.txt").unlink(missing_ok=True))
        self.addCleanup(lambda: (TMP / "pending.txt").unlink(missing_ok=True))
        self.addCleanup(g, "checkout", "main")
        self.addCleanup(g, "branch", "-D", "goal/G")

        merged_id = self.task("already landed", complexity=5)
        bus.update(merged_id, status="done", worktree=str(TMP), parent="G")
        self.addCleanup(g, "branch", "-D", f"task/{merged_id}")
        g("checkout", "-b", "goal/G")
        g("checkout", "-b", f"task/{merged_id}")
        (TMP / "landed.txt").write_text("x")
        g("add", "-A")
        g("commit", "-qm", "task work")
        g("checkout", "goal/G")
        g("merge", "--ff-only", f"task/{merged_id}")
        g("checkout", "main")

        pending_id = self.task("not landed yet", complexity=2)
        bus.update(pending_id, status="done", worktree=str(TMP), parent="G")
        self.addCleanup(g, "branch", "-D", f"task/{pending_id}")
        g("checkout", "-b", f"task/{pending_id}")
        (TMP / "pending.txt").write_text("y")
        g("add", "-A")
        g("commit", "-qm", "unrelated work")
        g("checkout", "main")

        tests_green_calls = []
        already_faked = daemon.subprocess.run          # gate_green(True)'s fake, installed in setUp
        def counting_run(*a, **k):
            if a[0][:1] == [str(merge.TESTS_GREEN)]:
                tests_green_calls.append(a)
            return already_faked(*a, **k)
        self.swap(daemon.subprocess, "run", counting_run)

        daemon.tick()

        merged = bus.get(merged_id)
        self.assertEqual(merged["merged_into"], "goal/G")
        self.assertEqual(merged["merged_via"], "ancestor")
        self.assertFalse((merged.get("pipeline") or {}).get("gated_at"))
        self.assertEqual(self.workers, [])                 # no review spawned for either task
        self.assertEqual(len(tests_green_calls), 1)         # tests-green ran once, for pending_id only

        pending = bus.get(pending_id)
        self.assertEqual(self.merged, [pending_id])         # non-ancestor task still gated and merged as before
        self.assertTrue(pending["pipeline"]["gated_at"])

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

    def test_webhook_posts_body(self):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received["method"] = self.command
                received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_port}/"
        self.set_env("ORCH_NOTIFY_URL", url)
        self.swap(daemon.subprocess, "run", lambda *a, **k: FakeProc("", 0))

        daemon.notify("hello webhook")
        thread.join(timeout=5)

        self.assertEqual(received.get("method"), "POST")
        self.assertEqual(received.get("body"), b"hello webhook")

    def test_webhook_body_capped_at_200_chars(self):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_port}/"
        self.set_env("ORCH_NOTIFY_URL", url)
        self.swap(daemon.subprocess, "run", lambda *a, **k: FakeProc("", 0))

        msg = "x" * 1000                                            # e.g. unbounded merge stderr (daemon.py:188)
        daemon.notify(msg)
        thread.join(timeout=5)

        self.assertEqual(received.get("body"), msg[:200].encode())

    def test_webhook_failure_is_swallowed(self):
        self.set_env("ORCH_NOTIFY_URL", "http://127.0.0.1:1/")
        self.swap(daemon.subprocess, "run", lambda *a, **k: FakeProc("", 0))
        daemon.notify("unreachable")                               # must not raise

    def test_no_osascript_off_darwin(self):
        calls = []
        self.swap(daemon.subprocess, "run", lambda *a, **k: calls.append(a[0]) or FakeProc("", 0))
        self.swap(daemon.sys, "platform", "linux")
        daemon.notify("linux box")
        self.assertEqual(calls, [])

    def test_desktop_alert_suppressed_by_env(self):
        calls = []
        self.swap(daemon.subprocess, "run", lambda *a, **k: calls.append(a[0]) or FakeProc("", 0))
        self.swap(daemon.sys, "platform", "darwin")
        self.set_env("ORCH_NOTIFY_DESKTOP", "0")
        daemon.notify("suppressed")
        self.assertEqual(calls, [])

    def test_ambient_notify_url_does_not_leak_into_other_tests(self):
        """Stands in for a shell that exported ORCH_NOTIFY_URL before this process's setUp ran: re-applying
        clear_env's own pop-then-restore here proves that pattern -- not just avoiding the var in test bodies
        -- is what keeps an internal notify() call (gate()'s worktree-missing path, exercised for real by
        test_gate_missing_worktree_holds) from ever reaching the webhook."""
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 0.3
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_port}/"

        os.environ["ORCH_NOTIFY_URL"] = url   # simulate the stale ambient value setUp would normally clear
        self.clear_env("ORCH_NOTIFY_URL")

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        t = self.task("nowt", complexity=2)
        bus.update(t, status="done", worktree=str(TMP / "does-not-exist"))
        daemon.tick()                          # gate()'s worktree-missing branch calls notify() internally

        thread.join(timeout=2)
        self.assertEqual(received, [])


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
