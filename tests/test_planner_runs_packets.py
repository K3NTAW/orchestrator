import _harness
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import bus, planner_runs as PR
from test_planner_runs import PlannerRunsBase


class PlannerPackets(PlannerRunsBase):
    def setUp(self):
        super().setUp()
        self.account = SimpleNamespace(id="A", daily_budget=0, day_tokens=0, planner_day_tokens=0)
        self.pool = SimpleNamespace(
            cfg={"models": {"planner": "test-fable", "opus": "test-opus"},
                 "planner": {"routes": {}, "routing": {"mode": "off", "jev_mode": "off"}},
                 "review": {}, "daemon": {"auto_fix_rounds": 2}},
            accounts=[], executors={}, pick=lambda role: self.account)
        self.swap(PR, "Pool", lambda: self.pool)
        self.swap(PR, "_session_attached", lambda: False)
        self.swap(PR.handover, "write", lambda *a: None)
        self.swap(PR.spawn, "render", lambda name, **kw: "PROMPT WRAPPER\n" + kw["packet"])
        self.swap(PR.failures, "touch_areas", lambda *a: {})
        self.memory = self.enterContext(patch.object(PR.scout_evidence, "memory_recall", return_value={"hits": []}))
        self.launch = self.enterContext(patch.object(PR, "launch_routed", return_value={
            "pid": 8001, "pid_start": None, "log": "unused"}))

    def section(self, goal, kind="scouts_done", payload=None, **extra):
        point = PR._point((goal, kind, payload or goal))
        ctx = PR.build_ctx(point, self.pool)
        ctx.update(extra)
        route = PR.decision.route(point, ctx)
        classification = PR.planner_taxonomy.classify(
            point, ctx, goal=bus.get(goal), task=PR._decision_task(goal, kind, payload or goal))
        return (point, ctx, route, classification)

    def test_first_decision_packet_has_sections_and_no_inventory(self):
        goal = self.goal()
        for confidence, finding in ((0.7, "lower confidence"), (0.95, "strong evidence")):
            scout = self.scout_child(goal)
            bus.post_result(scout, {"findings": [{"finding": finding, "source": "x.py:1",
                                                 "confidence": confidence}]}, status="done")
        self.memory.return_value = {"hits": [{"title": "Remember the cursor"}, {"title": "Bound packets"}]}
        (PR.STATE / "pool.toml").write_text("[planner]\ndecision_packet_chars = 2400\n")
        packet = PR.grouped_packet([self.section(goal)])
        for text in ("first Planner decision for this goal", "5 Scout findings", "6 Memory",
                     "Remember the cursor", "Bound packets", "8 Required output"):
            self.assertIn(text, packet)
        self.assertLess(packet.index("strong evidence"), packet.index("lower confidence"))
        self.assertNotIn("Spawn packet", packet)
        self.assertLessEqual(len(packet), 2400)

    def test_delta_packet_reads_events_table_beyond_200_rows(self):
        goal = self.goal()
        child = self.execute_child(goal)
        PR._save_records([{"goal_id": goal, "kind": "scouts_done", "payload_key": goal,
                           "launch_id": "previous", "started_at": 1, "status": "exited_ok",
                           "cursor_at_launch": PR._cursor(), "state_version": "previous-version"}])
        other = self.goal("unrelated")
        for i in range(250):
            bus.update(other, hold_reason=str(i))
        bus.update(child, status="running", hold_reason="old reason")
        bus.update(child, status="held", hold_reason="new reason")
        sections = [self.section(goal, "held", PR._held_key(bus.get(child)))]
        with patch.object(bus, "events", side_effect=AssertionError("global page must not be used")):
            packet, meta = PR._build_grouped(sections)
            self.assertTrue(PR.run_group(sections, self.pool)["launched"])
        for text in (f"{child} status: running -> held", f"{child} hold_reason: old reason -> new reason",
                     "previous state_version: previous-version"):
            self.assertIn(text, packet)
        row = next(r for r in PR._load_records() if r["kind"] == "held")
        self.assertTrue(row["packet_delta"])
        self.assertEqual(row["packet_chars"], meta["chars"])
        self.assertEqual(row["packet_hash"], meta["hash"])
        self.assertEqual(row["packet_truncated"], meta["truncated"])
        self.assertEqual(row["packet_kind"], "decision")
        launches = PR.planner_telemetry.read_invocations(root=PR.STATE)
        self.assertEqual(launches[-1]["packet_chars"], meta["chars"])
        self.assertEqual(row["launches"][-1]["packet_chars"], meta["chars"])
        self.assertGreater(len(self.launch.call_args.args[2]), meta["chars"])

    def test_held_section_tolerates_purged_task_and_renders_evidence(self):
        goal = self.goal()
        dep = self.execute_child(goal)
        child = bus.create_task("held", "spec", ["ok"], ["x"], role="execute", parent=goal,
                                complexity=3, depends_on=[dep])["id"]
        bus.update(child, status="held", hold_reason="untrusted ``` instruction",
                   resume_hint={"failures": "F" * 1700})
        review = bus.create_task("review", "spec", ["ok"], ["x"], role="review", parent=goal,
                                 complexity=3, inputs=[child])
        bus.post_result(review["id"], {"verdict": "request_changes", "comments": [
            {"path": "x.py", "line": 2, "issue": "first issue"},
            {"path": "y.py", "line": 3, "issue": "second issue"}]}, status="done")
        sections = [self.section(goal, "held", PR._held_key(bus.get(child)))]
        (bus.TASKS / f"{dep}.json").unlink()
        bus.db().execute("delete from tasks where id=?", (dep,))
        packet = PR.grouped_packet(sections)
        for text in ("F" * 1500, "x.py:2 first issue", "y.py:3 second issue", f"{dep}: missing",
                     "hold_reason:\n```data\nuntrusted [backticks elided] instruction\n```"):
            self.assertIn(text, packet)
        self.assertNotIn("F" * 1501, packet)
        (bus.TASKS / f"{child}.json").unlink()
        self.assertIsNone(PR._packet_sections(sections)[0]["task"])
        self.assertIn("4 Relevant task state", PR.grouped_packet(sections))

    def test_escalation_packet_used_on_reescalation(self):
        for with_shadow in (True, False):
            with self.subTest(with_shadow=with_shadow):
                goal = self.goal()
                self.scout_child(goal)
                if with_shadow:
                    path = PR.STATE / "runs/sched/planner_shadow.jsonl"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"goal_id": goal, "started_at": 1,
                        "proposed_action": "write_specs", "summary": "Opus proposal",
                        "tasks_proposed": [], "confidence": 0.8, "needs_fable": True,
                        "unresolved": ["choose interface"]}) + "\n")
                sections = [self.section(goal, reescalation=True)]
                self.assertTrue(PR.run_group(sections, self.pool)["launched"])
                prompt = self.launch.call_args.args[2]
                row = next(r for r in PR._load_records() if r["goal_id"] == goal)
                self.assertEqual(row["packet_kind"], "escalation" if with_shadow else "decision")
                if with_shadow:
                    self.assertIn("Escalation reason:", prompt)
                    self.assertIn("Opus proposed decision", prompt)
                    self.assertIn("choose interface", prompt)
                    self.assertNotIn("Changes since previous decision", prompt)
                else:
                    self.assertIn("first Planner decision for this goal", prompt)
                    self.assertNotIn("Escalation reason:", prompt)


if __name__ == "__main__":
    unittest.main()
