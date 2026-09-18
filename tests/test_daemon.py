"""orchestrator.daemon pipeline: dependency-gated dispatch, spec review for complexity >=6, the tests-green gate,
review routing and the serial merge — all driven by `tick()` with the four side-effecting calls (executor.start,
spawn.run_worker, merge.merge, subprocess.run) monkeypatched to record instead of act.

Each test gets its own bus directory (bus.STATE/TASKS/RUNS swapped) because bus.read() is global: without the swap
these ticks would pick up every execute task any other test file left queued in the shared TMP root."""
import http.server, os, subprocess, sys, tempfile, threading, time, unittest
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

    def test_saturated_codex_returns_zero_not_fallback(self):
        """§T-0102 acceptance: with the committed pool.toml, four healthy Codex rows at running == max_parallel
        (saturated, never cooling) must return 0 -- not fall into the Claude fallback branch, which is for a
        pool where every row is cooling, not merely busy."""
        pool = P.Pool()
        for ex in pool.executors.values():
            if ex.enabled and "execute" in ex.roles:
                ex.running = ex.max_parallel
        self.assertEqual(daemon.free_slots(pool), 0)

    def test_fallback_counts_running_claude_roles_and_inflight_dispatches(self):
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)                       # cools the whole "chatgpt" quota group
        scout = self.task("scout running", role="scout", complexity=2)
        bus.update(scout, status="running", assigned_to="claude:A")
        dispatched = self.task("dispatched not claimed", complexity=3)
        bus.update(dispatched, pipeline={"dispatched_at": time.time()})   # spawn_async window before bus.claim
        want = pool.cfg["limits"]["max_parallel_claude_workers"] - 2
        self.assertEqual(daemon.free_slots(pool), want)

    def test_fallback_skips_complexity_without_tier(self):
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)
        c = self.task("complexity9", complexity=9)
        bus.update(c, spec_review_verdict="approve")
        daemon.tick(pool)
        t = bus.get(c)
        self.assertEqual(t["status"], "queued")
        self.assertFalse((t.get("pipeline") or {}).get("dispatched_at"))

    def test_free_slots_survives_missing_cfg_tables(self):
        pool = P.Pool()
        pool.cooldown_executor("astra", 600)                       # no executor available
        cfg_no_codex = {k: v for k, v in pool.cfg.items() if k != "codex"}
        pool.cfg = cfg_no_codex
        self.assertEqual(daemon.free_slots(pool), 0)                # missing [codex] defaults on_exhausted to "hold"

        cfg_fallback_no_limits = {k: v for k, v in cfg_no_codex.items() if k != "limits"}
        cfg_fallback_no_limits["codex"] = {"on_exhausted": "fallback_claude"}
        pool.cfg = cfg_fallback_no_limits
        self.assertEqual(daemon.free_slots(pool), 4)                # missing [limits] defaults max_parallel_claude_workers to 4

    def test_defaults_when_review_table_missing(self):
        """A pool.toml without a [review] table must not crash tick() -- and must leave the four review-policy
        module attributes at their documented defaults, the same ones the committed pool.toml's [review] table
        happens to also carry."""
        pool = P.Pool()
        pool.cfg = {k: v for k, v in pool.cfg.items() if k != "review"}
        daemon.tick(pool)
        self.assertEqual(daemon.SPEC_REVIEW_MIN, 6)
        self.assertEqual(daemon.DIRECT_MERGE_MAX, 3)
        self.assertEqual(daemon.TWO_REVIEWS_FROM, 7)
        self.assertEqual(daemon.SPEC_REVIEW_TIER, "sonnet")

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

    def test_spec_review_threshold_from_cfg(self):
        """With the committed pool.toml ([review].spec_review_min = 6), a complexity-5 task dispatches straight
        to the executor while a complexity-6 task is held for a spec review on spec_review_tier."""
        a = self.task("A", complexity=5)
        b = self.task("B", complexity=6)
        daemon.tick()
        self.assertEqual(self.settle_started(1), [a])
        self.assertFalse((bus.get(a).get("pipeline") or {}).get("spec_review_at"))
        reviews = bus.read(role="spec_review")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["inputs"], [b])
        self.assertEqual(reviews[0]["tier"], "sonnet")

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

    def test_gate_never_merges_orphaned_result_even_when_cheap(self):
        """A result with orphaned=true came from reconcile_dead re-gating a dead worker's last commit, not from
        an executor that actually finished. gate() must never route it straight to merge.merge just because the
        task's complexity is at or below DIRECT_MERGE_MAX -- it must always go to review instead, with the
        orphaned warning leading the review task's spec."""
        t = self.task("orphaned cheap", complexity=2)
        bus.update(t, status="done", worktree=str(TMP), result={"orphaned": True, "commit": "deadbee"})
        daemon.tick()
        self.assertEqual(self.merged, [])
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["inputs"], [t])
        self.assertEqual(reviews[0]["tier"], "sonnet")
        self.assertTrue(reviews[0]["spec"].startswith(
            "orphaned executor: verify the acceptance criteria are fully met, the worker may have died mid-task"))

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

    def test_one_review_only_for_mid_complexity(self):
        """complexity 5 sits between direct_merge_max (3) and two_reviews_from (7): exactly one review task, and
        a second gate pass (the task is already gated_at) must not spawn a second one."""
        t = self.task("mid", complexity=5)
        bus.update(t, status="done", worktree=str(TMP))
        daemon.tick()
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["inputs"], [t])

    def test_two_reviews_for_high_complexity_merge_waits_for_both(self):
        """complexity 7 (>= two_reviews_from), Codex-executed: two review tasks split across the two different
        Claude tiers, and merge_reviewed() must not merge until both have approved -- one approve alone must not
        be enough."""
        t = self.task("big", complexity=7)
        bus.update(t, status="done", worktree=str(TMP), executor="astra")

        def fake_merge(tid, target=None):
            # unlike the shared setUp() stub, this mirrors merge.merge's real side effect of marking the task
            # merged in the bus -- needed so already_merged() short-circuits the loop's second review the same
            # way it would in production once the first review's merge call lands.
            self.merged.append(tid)
            bus.update(tid, status="done", merged_into="goal/G", sha="abc12345")
            return {"status": "merged", "target": "goal/G", "sha": "abc12345"}
        self.swap(merge, "merge", fake_merge)

        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(sorted(r["tier"] for r in reviews), ["opus", "sonnet"])  # split across the two tiers

        bus.update(reviews[0]["id"], status="done", review_verdict="approve")
        daemon.tick()
        self.assertEqual(self.merged, [])                          # only one of two reviews approved so far

        bus.update(reviews[1]["id"], status="done", review_verdict="approve")
        daemon.tick()
        self.assertEqual(self.merged, [t])

    def test_two_reviews_claude_executor_same_non_executing_tier(self):
        """T-0150 review item 3: for a Claude-executed complexity-7 task, both reviews must land on the
        non-executing tier (never the tier that executed), not split across the two tiers -- and the second
        carries constraints.second_review so a later spawn change can steer routing off it. Both reviews may
        land on the same account; only the model differs from the executor (T-0159 review item 2)."""
        t = self.task("big", complexity=7)
        bus.update(t, status="done", worktree=str(TMP), executor="claude:opus")
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 2)
        self.assertEqual([r["tier"] for r in reviews], ["sonnet", "sonnet"])
        self.assertTrue(reviews[1]["constraints"]["second_review"])
        self.assertNotIn("avoid_account", reviews[1]["constraints"])

    def test_orphaned_high_complexity_merges_on_single_approve(self):
        """T-0150 review item 1: reviews_expected() must come from the orphaned flag, not complexity alone -- an
        orphaned complexity-7 task only ever gets one review task, and merge_reviewed() must merge it on that
        review's single approve instead of waiting for a second review gate() never opens."""
        t = self.task("orphaned big", complexity=7)
        bus.update(t, status="done", worktree=str(TMP), result={"orphaned": True, "commit": "deadbee"})
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 1)
        bus.update(reviews[0]["id"], status="done", review_verdict="approve")
        daemon.tick()
        self.assertEqual(self.merged, [t])

    def test_sibling_review_failed_holds_source_without_waiting(self):
        """T-0150 review item 2: one sibling review approved, the other failed -- merge_reviewed() must hold the
        source task naming the failed sibling instead of waiting on a review that can never finish."""
        t = self.task("big", complexity=7)
        bus.update(t, status="done", worktree=str(TMP), executor="astra")
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 2)
        bus.update(reviews[0]["id"], status="done", review_verdict="approve")
        bus.update(reviews[1]["id"], status="failed")
        daemon.tick()
        held = bus.get(t)
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["hold_reason"], f"review failed: {reviews[1]['id']}")
        self.assertEqual(self.merged, [])

    def test_sibling_review_held_holds_source_without_waiting(self):
        """T-0150 review item 2, held variant: a sibling review itself held (e.g. no account headroom) must not
        be waited on forever either."""
        t = self.task("big", complexity=7)
        bus.update(t, status="done", worktree=str(TMP), executor="astra")
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 2)
        bus.update(reviews[0]["id"], status="done", review_verdict="approve")
        bus.update(reviews[1]["id"], status="held", hold_reason="no account with headroom")
        daemon.tick()
        held = bus.get(t)
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["hold_reason"], f"review held: {reviews[1]['id']}")
        self.assertEqual(self.merged, [])

    def test_all_reviews_failed_holds_without_merge(self):
        """T-0159 review item 1 (a): both reviews of a complexity-7 task fail -- neither ever reaches done, so
        the old review-centric loop (`for r in bus.read(status="done", role="review")`) never even saw this
        task. merge_reviewed must still hold it, naming both failed review ids, instead of leaving it stuck
        done+gated forever."""
        t = self.task("big", complexity=7)
        bus.update(t, status="done", worktree=str(TMP), executor="astra")
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 2)
        bus.update(reviews[0]["id"], status="failed")
        bus.update(reviews[1]["id"], status="failed")
        daemon.tick()
        held = bus.get(t)
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["hold_reason"], f"reviews failed: {', '.join(sorted(r['id'] for r in reviews))}")
        self.assertEqual(self.merged, [])

    def test_single_review_failed_holds_mid_complexity(self):
        """T-0159 review item 1 (b): a complexity-4 task's one and only review fails -- same escape-hatch gap as
        above, but with reviews_expected() == 1 instead of 2."""
        t = self.task("mid", complexity=4)
        bus.update(t, status="done", worktree=str(TMP))
        daemon.tick()
        reviews = bus.read(role="review")
        self.assertEqual(len(reviews), 1)
        bus.update(reviews[0]["id"], status="failed")
        daemon.tick()
        held = bus.get(t)
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["hold_reason"], f"reviews failed: {reviews[0]['id']}")
        self.assertEqual(self.merged, [])

    def test_merged_at_stamped_on_source_task(self):
        """T-0150 review item 5: the merge dedup stamp lives on the source execute task, not the review task, so
        a conflict is attempted once per task rather than once per review."""
        ok = self.gated_execute("stamp-on-source")
        r_ok = self.task("review stamp-on-source", complexity=5, role="review", inputs=[ok])
        bus.update(r_ok, status="done", review_verdict="approve")
        daemon.tick()
        self.assertEqual(self.merged, [ok])
        self.assertTrue(bus.get(ok)["pipeline"].get("merged_at"))
        self.assertFalse((bus.get(r_ok).get("pipeline") or {}).get("merged_at"))

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

    def test_merge_reviewed_falls_back_to_result_verdict(self):
        """T-0139/T-0141: when review_verdict never landed on the review task or its source (spawn.run_worker's
        parse failed before it could set them), merge_reviewed still finds the verdict inside the review's own
        posted result."""
        ok = self.gated_execute("approved-fallback")
        r_ok = self.task("review approved-fallback", complexity=5, role="review", inputs=[ok])
        bus.update(r_ok, status="done", result={"verdict": "approve", "confidence": 1.0, "provenance": ["repo"]})
        daemon.tick()
        self.assertEqual(self.merged, [ok])

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

    def dead_pid(self):
        """A pid guaranteed not alive: spawn a trivial child and wait for it to exit."""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_reconcile_dead_regates_clean_worktree_ahead_of_base(self):
        """An execute task's worker died, but it had already committed and left the worktree clean: tick() must
        post that commit as a done result (orphaned=true) instead of requeuing and redoing the work."""
        scratch_repo(TMP)
        self.addCleanup(lambda: (TMP / "orphan_clean.txt").unlink(missing_ok=True))
        self.addCleanup(g, "branch", "-D", "task/orphan-clean")
        self.addCleanup(g, "branch", "-D", "goal/T-0043")
        self.addCleanup(g, "checkout", "main")
        g("checkout", "-b", "goal/T-0043")
        g("checkout", "-b", "task/orphan-clean")
        (TMP / "orphan_clean.txt").write_text("x")
        g("add", "-A")
        g("commit", "-qm", "finished work")
        sha = g("rev-parse", "HEAD").stdout.strip()

        t = self.task("orphan clean")
        bus.update(t, status="running", executor="astra", pid=self.dead_pid(), claimed_at=time.time() - 61, worktree=str(TMP))
        daemon.tick()

        task = bus.get(t)
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["result"]["orphaned"])
        self.assertEqual(task["result"]["commit"], sha)

    def test_reconcile_dead_holds_dirty_worktree_ahead_of_base(self):
        """Same as above but the worktree has an uncommitted change on top of the commit: too risky to auto-gate,
        so tick() must hold the task for the Planner instead, naming the commit in the resume hint."""
        scratch_repo(TMP)
        self.addCleanup(lambda: (TMP / "orphan_dirty.txt").unlink(missing_ok=True))
        self.addCleanup(lambda: (TMP / "uncommitted.txt").unlink(missing_ok=True))
        self.addCleanup(g, "branch", "-D", "task/orphan-dirty")
        self.addCleanup(g, "branch", "-D", "goal/T-0043")
        self.addCleanup(g, "checkout", "main")
        g("checkout", "-b", "goal/T-0043")
        g("checkout", "-b", "task/orphan-dirty")
        (TMP / "orphan_dirty.txt").write_text("x")
        g("add", "-A")
        g("commit", "-qm", "finished work")
        sha = g("rev-parse", "HEAD").stdout.strip()
        (TMP / "uncommitted.txt").write_text("dirty")   # untracked change: worktree is no longer clean

        t = self.task("orphan dirty")
        bus.update(t, status="running", executor="astra", pid=self.dead_pid(), claimed_at=time.time() - 61, worktree=str(TMP))
        daemon.tick()

        task = bus.get(t)
        self.assertEqual((task["status"], task["hold_reason"]), ("held", "orphaned_dirty_worktree"))
        self.assertEqual(task["resume_hint"]["commit"], sha)

    def test_reconcile_dead_regates_using_main_when_no_goal_branch(self):
        """No goal/<parent> branch exists to merge-base against (a parentless task, or the first execute task of
        a goal that hasn't cut its branch yet): reconcile_dead must fall back to merge-base against origin/main,
        then main, instead of requeuing a worktree that actually has finished work sitting in it."""
        scratch_repo(TMP)
        t = bus.create_task("no goal branch", "spec", ["works"], ["x.py"], role="execute", complexity=2)["id"]
        self.addCleanup(lambda: (TMP / "no_goal.txt").unlink(missing_ok=True))
        self.addCleanup(g, "branch", "-D", f"task/{t}")
        self.addCleanup(g, "checkout", "main")
        g("checkout", "-b", f"task/{t}")
        (TMP / "no_goal.txt").write_text("x")
        g("add", "-A")
        g("commit", "-qm", "finished work")
        sha = g("rev-parse", "HEAD").stdout.strip()

        bus.update(t, status="running", executor="astra", pid=self.dead_pid(), claimed_at=time.time() - 61,
                   worktree=str(TMP))
        daemon.tick()

        task = bus.get(t)
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["result"]["orphaned"])
        self.assertEqual(task["result"]["commit"], sha)

    def test_reconcile_dead_requeues_and_retriggers_dispatch_when_no_commits_ahead(self):
        """The worker died before committing anything: no ahead commits, so tick() must fall back to the plain
        requeue instead of treating an unchanged worktree as orphaned work. The requeue must also drop
        pipeline.dispatched_at so the very next tick dispatches the task again -- otherwise dispatch()'s stamp()
        sees the stale stamp, thinks a worker is already out, and leaves the task queued forever."""
        scratch_repo(TMP)
        self.addCleanup(g, "branch", "-D", "goal/T-0043")
        self.addCleanup(g, "checkout", "main")
        g("checkout", "-b", "goal/T-0043")   # HEAD == base: nothing ahead

        t = self.task("orphan none")
        old_stamp = time.time() - 120
        bus.update(t, status="running", executor="astra", pid=self.dead_pid(), claimed_at=time.time() - 61,
                   worktree=str(TMP), pipeline={"dispatched_at": old_stamp})
        daemon.tick()   # reconcile_dead requeues it, then dispatch() -- later in this same tick -- sees it
                        # queued again with dispatched_at cleared and dispatches it right away

        self.assertEqual(self.settle_started(1), [t])
        task = bus.get(t)
        self.assertEqual(task["reason"], "process died; requeued")
        self.assertGreater(task["pipeline"]["dispatched_at"], old_stamp)

    def test_reconcile_dead_requeues_non_execute_role_unchanged(self):
        """A scout task's worker died: this is the pre-existing path and must be untouched by the orphaned-work
        check, which only ever applies to execute tasks."""
        t = self.task("scout dead", role="scout", complexity=2)
        bus.update(t, status="running", pid=self.dead_pid(), claimed_at=time.time() - 61, worktree=str(TMP))
        daemon.tick()

        task = bus.get(t)
        self.assertEqual((task["status"], task["reason"]), ("queued", "process died; requeued"))

    def test_reconcile_dead_return_values(self):
        """reconcile_dead(task) itself returns the tri-state result the daemon acted on, so this is testable
        directly without going through tick()'s live-pid check."""
        scratch_repo(TMP)
        self.addCleanup(lambda: (TMP / "rv.txt").unlink(missing_ok=True))
        self.addCleanup(g, "branch", "-D", "task/rv")
        self.addCleanup(g, "branch", "-D", "goal/T-0043")
        self.addCleanup(g, "checkout", "main")
        g("checkout", "-b", "goal/T-0043")
        g("checkout", "-b", "task/rv")
        (TMP / "rv.txt").write_text("x")
        g("add", "-A")
        g("commit", "-qm", "work")

        t = self.task("rv clean")
        task = bus.get(t)
        task["worktree"] = str(TMP)
        self.assertEqual(daemon.reconcile_dead(task), "regated")

        (TMP / "rv.txt").write_text("y")   # dirty it for the held case
        t2 = self.task("rv dirty")
        task2 = bus.get(t2)
        task2["worktree"] = str(TMP)
        self.assertEqual(daemon.reconcile_dead(task2), "held")

        g("reset", "--hard", "goal/T-0043")   # discard the dirty change and land back on base: nothing ahead
        t3 = self.task("rv none")
        task3 = bus.get(t3)
        task3["worktree"] = str(TMP)
        self.assertEqual(daemon.reconcile_dead(task3), "requeued")

        t4 = self.task("rv scout", role="scout", complexity=2)
        task4 = bus.get(t4)
        self.assertEqual(daemon.reconcile_dead(task4), "requeued")

    def test_tick_survives_reconcile_dead_exception(self):
        """A vanished worktree or a git call inside reconcile_dead that raises for one dead task must not abort
        tick()'s stage loop: dispatch/gate/merge_reviewed must still run for every other task this tick."""
        dead = self.task("dead git", complexity=2)
        bus.update(dead, status="running", pid=self.dead_pid(), claimed_at=time.time() - 61, worktree=str(TMP))
        self.swap(daemon, "_git_in", raiser(RuntimeError("git blew up")))
        other = self.task("other queued", complexity=3)

        daemon.tick()

        self.assertEqual(self.settle_started(1), [other])   # dispatch() still ran despite the reconcile blow-up
        self.assertEqual(bus.get(dead)["status"], "running")   # left alone, not requeued or crashed on

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
