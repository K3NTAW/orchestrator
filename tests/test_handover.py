"""orchestrator.handover: the "## Auto-handover" section written/replaced at the end of plan.md, and
daemon.maybe_handover()'s 15-minute throttle.

Each test gets its own sandbox for bus.STATE/TASKS/RUNS and handover.STATE/ROOT (handover.write() looks both up
at call time, not import time, precisely so a test can swap them) so plan.md and wt/ never touch the real repo."""
import sys, tempfile, time, unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
