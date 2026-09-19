"""orchestrator.handover: the "## Auto-handover" section written/replaced at the end of plan.md, and
daemon.maybe_handover()'s 15-minute throttle.

Each test gets its own sandbox for bus.STATE/TASKS/RUNS and handover.STATE/ROOT (handover.write() looks both up
at call time, not import time, precisely so a test can swap them) so plan.md and wt/ never touch the real repo."""
import json, sys, tempfile, threading, time, unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_handover.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import bus, daemon, handover
from orchestrator import pool as P


class Handover(unittest.TestCase):
    def setUp(self):
        sandbox = Path(tempfile.mkdtemp(prefix="orch-handover-"))
        for mod, name, value in (
            (bus, "STATE", sandbox), (bus, "TASKS", sandbox / "tasks"), (bus, "RUNS", sandbox / "runs"),
            (handover, "STATE", sandbox), (handover, "ROOT", sandbox),
        ):
            self.swap(mod, name, value)
        P.PERSIST.unlink(missing_ok=True)
        self.addCleanup(P.PERSIST.unlink, True)
        # daemon.HANDOVER_STATE is bound at import time to the real .orchestrator dir, same as pool.PERSIST --
        # not swapped above, so clean it up directly rather than leaving a throttle timestamp behind for the
        # next test (or the real daemon) to trip over.
        daemon.HANDOVER_STATE.unlink(missing_ok=True)
        self.addCleanup(daemon.HANDOVER_STATE.unlink, True)

    def swap(self, mod, name, value):
        orig = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, orig)

    def goal(self, title="Ship the thing"):
        t = bus.create_task(f"GOAL: {title}", title, ["Planner closes the goal with a PR"], ["**"], role="triage",
                            complexity=5)
        return t["id"]

    def child(self, goal_id, title, **fields):
        t = bus.create_task(title, "spec", ["works"], ["x.py"], role="execute", complexity=2, parent=goal_id,
                            **fields)
        return t["id"]

    def section(self, text):
        i = text.index("## Auto-handover ")
        return text[:i], text[i:]

    def test_writes_section_with_status_groups(self):
        g = self.goal("Ship indexing")
        queued = self.child(g, "Add index", depends_on=[])
        running = self.child(g, "Wire cache")
        bus.claim(running, "claude:B", "wt/T-run")
        bus.update(running, executor="astra")
        held = self.child(g, "Fix gate")
        bus.update(held, status="held", hold_reason="gate_red", resume_hint={"failures": "boom"})
        done = self.child(g, "Docs pass")
        bus.post_result(done, {"summary": "ok"}, "done")
        merged = self.child(g, "Landed fix")
        bus.update(merged, status="done", merged_into="goal/G", sha="abc12345")

        plan = handover.write("test")
        text = plan.read_text()

        self.assertEqual(text.count("## Auto-handover "), 1)
        _, section = self.section(text)
        self.assertIn(f"### {g} GOAL: Ship indexing", section)
        self.assertIn(f"- queued: {queued}", section)
        self.assertIn("depends_on=[]", section)
        self.assertIn(f"- running: {running}", section)
        self.assertIn("executor=astra", section)
        self.assertIn(f"- held: {held}", section)
        self.assertIn("hold_reason=gate_red", section)
        self.assertIn("resume_hint_keys=['failures']", section)
        self.assertIn(f"- done (not merged): {done}", section)
        self.assertIn(f"- merged: {merged}", section)
        self.assertIn("sha=abc12345", section)
        self.assertIn(handover.RESUME_SENTENCE, section)
        self.assertIn("test", section.splitlines()[0])

    def test_failed_group_and_other_bucket(self):
        g = self.goal("Ship migrations")
        cancelled = self.child(g, "Weird status")
        bus.update(cancelled, status="cancelled")

        risky = self.goal("Ship the risky bit")
        failed = self.child(risky, "Migrate schema")
        bus.update(failed, status="failed", reason="rebase_conflict", resume_hint={"conflict_files": ["a.py"]})

        plan = handover.write("test")
        _, section = self.section(plan.read_text())

        self.assertIn(f"### {risky} GOAL: Ship the risky bit", section)
        self.assertIn(f"- failed: {failed}", section)
        self.assertIn("reason=rebase_conflict", section)
        self.assertIn(f"- other (cancelled): {cancelled}", section)
        self.assertNotIn("no child tasks", section)   # a goal with only a failed (or only an unknown-status) child must still show it

    def test_worktree_names_excludes_merged_and_state(self):
        g = self.goal("Ship worktrees")
        merged = self.child(g, "Landed")
        bus.update(merged, status="done", merged_into="goal/G")
        kept = self.child(g, "Still open")
        wt = handover.ROOT / "wt"
        (wt / merged).mkdir(parents=True)
        (wt / kept).mkdir(parents=True)
        (wt / "_state").mkdir(parents=True)

        plan = handover.write("test")
        _, section = self.section(plan.read_text())

        self.assertIn(f"wt/{kept}", section)
        self.assertNotIn(f"wt/{merged}", section)
        self.assertNotIn("wt/_state", section)

    def test_replaces_last_heading_only(self):
        plan = handover.STATE / "plan.md"
        handover.STATE.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            "# plan.md\n\n"
            "Note: the auto-handover section starts with a heading like:\n"
            "```\n## Auto-handover 2026-01-01T00:00:00+01:00 — example\nold quoted body\n```\n\n"
            "## Auto-handover 2026-01-01T00:00:00+01:00 — first pass\nold real body\n"
        )

        plan2 = handover.write("second pass")
        text = plan2.read_text()

        self.assertIn("```\n## Auto-handover 2026-01-01T00:00:00+01:00 — example\nold quoted body\n```", text)
        self.assertNotIn("old real body", text)
        self.assertEqual(text.count("## Auto-handover "), 2)  # the quoted one survives, plus the real section

    def test_write_failure_leaves_plan_unchanged(self):
        plan = handover.STATE / "plan.md"
        handover.STATE.mkdir(parents=True, exist_ok=True)
        plan.write_text("before\n")

        with mock.patch("orchestrator.handover.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                handover.write("test")

        self.assertEqual(plan.read_text(), "before\n")

    def test_truncates_long_lists(self):
        g = self.goal("Big backlog")
        for i in range(20):
            self.child(g, f"Task {i}", depends_on=[])

        plan = handover.write("test")
        _, section = self.section(plan.read_text())
        lines = section.splitlines()

        self.assertLessEqual(len(lines), 120)
        self.assertIn("… and 12 more", section)   # 20 queued items, MAX_ITEMS=8 shown

    def test_replaces_section_idempotently_and_preserves_rest(self):
        plan = handover.STATE / "plan.md"
        handover.STATE.mkdir(parents=True, exist_ok=True)
        plan.write_text("# plan.md\n\n## Read first\n1. do the thing\n")

        p1 = handover.write("first pass")
        text1 = p1.read_text()
        before1, section1 = self.section(text1)
        self.assertEqual(text1.count("## Auto-handover "), 1)

        p2 = handover.write("second pass")
        text2 = p2.read_text()
        before2, section2 = self.section(text2)

        self.assertEqual(text2.count("## Auto-handover "), 1)
        self.assertEqual(before1, before2)                 # everything above the section is byte-identical
        self.assertIn("first pass", section1.splitlines()[0])
        self.assertIn("second pass", section2.splitlines()[0])
        self.assertNotIn("first pass", text2)

    def test_daemon_throttles_to_15_min(self):
        calls = []
        self.swap(handover, "write", lambda reason: calls.append(reason))
        now = time.time()

        self.assertTrue(daemon.maybe_handover("daemon tick", now=now))
        self.assertFalse(daemon.maybe_handover("daemon tick", now=now + 60))     # second tick, same 15-min window
        self.assertEqual(len(calls), 1)

        self.assertTrue(daemon.maybe_handover("daemon tick", now=now + 16 * 60))  # past the throttle window
        self.assertEqual(len(calls), 2)

    def test_daemon_throttle_timestamp_lives_in_handover_state_not_pool_state(self):
        self.swap(handover, "write", lambda reason: None)
        P.PERSIST.unlink(missing_ok=True)
        now = time.time()

        self.assertTrue(daemon.maybe_handover("daemon tick", now=now))

        self.assertTrue(daemon.HANDOVER_STATE.exists())
        self.assertEqual(json.loads(daemon.HANDOVER_STATE.read_text())["handover_last_at"], now)
        self.assertFalse(P.PERSIST.exists())   # pool_state.json is untouched by the handover throttle

    def test_throttle_under_lock(self):
        """handover.maybe_write() checks and updates its last-written timestamp inside the same bus.locked()
        acquisition used for the plan.md write itself (not a separate flock taken after the fact), so a second
        caller racing the first blocks on that lock rather than also passing the throttle check and writing
        plan.md a second time within the interval."""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_write(reason):
            entered.set()
            release.wait(2)
            calls.append(reason)

        self.swap(handover, "write", slow_write)
        now = time.time()
        results = []

        def call():
            results.append(handover.maybe_write("tick", now=now))

        t1 = threading.Thread(target=call)
        t1.start()
        self.assertTrue(entered.wait(2))  # t1 is inside write(), holding the bus lock

        t2 = threading.Thread(target=call)
        t2.start()
        time.sleep(0.05)  # give t2 a chance to block on the lock rather than race the entered check
        release.set()
        t1.join(2)
        t2.join(2)

        self.assertEqual(len(calls), 1)              # only one real write happened
        self.assertEqual(sorted(results), [False, True])  # the blocked caller saw the fresh timestamp and skipped

    def test_truncation_tail(self):
        items = [f"item{i}" for i in range(11)]

        text = handover._join_truncated(items)

        self.assertEqual(text, ", ".join(items[:handover.MAX_ITEMS]) + ", … and 3 more")

    def test_identical_section_does_not_rewrite(self):
        fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=handover.TZ)
        with mock.patch("orchestrator.handover.datetime") as dt:
            dt.now.return_value = fixed

            p1 = handover.write("test")
            text1 = p1.read_text()

            with mock.patch("orchestrator.handover.os.replace") as replace:
                p2 = handover.write("test")
                replace.assert_not_called()

        self.assertEqual(p2.read_text(), text1)


if __name__ == "__main__":
    unittest.main()
