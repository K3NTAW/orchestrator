import _harness
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import bus, goals, handover, jev, planner_scorecard
from orchestrator import planner_runs as PR
from orchestrator import planner_telemetry as telemetry
from test_planner_runs import PlannerRunsBase


class PlannerRouting(PlannerRunsBase):
    def setUp(self):
        super().setUp()
        self.repo_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo_tmp.cleanup)
        self.repo = Path(self.repo_tmp.name)
        config = self.repo / ".orchestrator"
        (config / "prompts").mkdir(parents=True)
        (config / "prompts/planner.md").write_text("Planner system")
        (config / "pool.toml").write_text(
            '[models]\nplanner = "test-fable"\nopus = "test-opus"\n'
            '[[claude_accounts]]\nid = "A"\nconfig_dir = "/unused"\n')
        self.swap(PR, "ROOT", self.repo)
        self.account = SimpleNamespace(id="A", config_dir="/unused", daily_budget=0,
                                       day_tokens=0, planner_day_tokens=0,
                                       cooling=lambda: False, hold_reason="")
        self.pool = SimpleNamespace(
            accounts=[self.account], executors={},
            cfg={"models": {"planner": "test-fable", "opus": "test-opus"},
                 "claude_accounts": [{"id": "A", "config_dir": "/unused"}],
                 "planner": {"routes": {}, "routing": {"mode": "off", "jev_mode": "off"}},
                 "review": {}, "daemon": {"auto_fix_rounds": 2}},
            pick=lambda role: self.account)
        self.swap(PR, "Pool", lambda: self.pool)
        self.swap(handover, "write", lambda *a: None)
        self.swap(PR, "_session_attached", lambda: False)
        self.swap(PR.spawn, "render", lambda name, **kw: kw["packet"])
        self.swap(PR.failures, "touch_areas",
                  lambda *a: {area: False for area in PR.failures.DEFAULT_AREAS})
        self.swap(goals, "trust_workspace", lambda *a: None)
        self.swap(goals, "_proc_start", lambda pid: f"start-{pid}")
        self.swap(goals, "resolve_secrets", lambda *a: {})
        self.popen = self.enterContext(patch.object(goals, "Popen", return_value=SimpleNamespace(pid=8001)))
        self.ask = self.enterContext(patch.object(jev, "ask", return_value=None))
        self.notify = self.enterContext(patch.object(PR.notify, "notify_once"))

    def complex_goal(self):
        return bus.create_task("GOAL: complex", "spec", ["ok"], ["x"],
                               role="triage", complexity=9)["id"]

    def execute_child(self, goal_id, complexity=3, **fields):
        task = bus.create_task("execute", "spec", ["ok"], ["x"], role="execute",
                               parent=goal_id, complexity=complexity)
        if fields:
            bus.update(task["id"], **fields)
        return task["id"]

    def sections(self, goal):
        sections = []
        for raw in PR.decision_points():
            if raw[0] != goal:
                continue
            point = PR._point(raw)
            ctx = PR.build_ctx(point, self.pool)
            entity = bus.get(point["task_id"])
            ctx.update(complexity=entity["complexity"], state_version=PR.state_version(goal))
            route = PR.decision.route(point, ctx)
            classification = PR.planner_taxonomy.classify(
                point, ctx, goal=bus.get(goal), task=entity)
            sections.append((point, ctx, route, classification))
        return sections

    def rows(self, name="planner_invocations"):
        path = PR.STATE / "runs/sched" / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_mode_off_argv_identical_to_legacy(self):
        goal = self.goal()
        self.scout_child(goal)
        sections = self.sections(goal)
        prompt = PR.grouped_packet(sections)
        goals.launch_planner(self.repo, prompt, "A", 3, PR.STATE / "legacy.log")
        legacy = self.popen.call_args.args[0]
        self.popen.reset_mock()
        self.assertTrue(PR.run_group(sections, self.pool)["launched"])
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args.args[0], legacy)
        self.assertFalse(any(r["status"] == "held_for_fable" for r in PR._load_records()))

    def test_shadow_mode_launches_fable_then_opus_shadow_without_bus_writes(self):
        self.pool.cfg["planner"]["routing"]["mode"] = "shadow"
        goal = self.goal()
        self.scout_child(goal)
        (PR.STATE / "plan.md").write_text("unchanged plan")
        before = {p: p.read_bytes() for p in bus.TASKS.glob("*.json")}
        plan = (PR.STATE / "plan.md").read_bytes()
        PR.tick(self.pool)
        self.assertEqual(self.popen.call_count, 2)
        production, shadow = [c.args[0] for c in self.popen.call_args_list]
        self.assertEqual(production[production.index("--model") + 1], "test-fable")
        self.assertIn(".mcp.planner.json", production)
        self.assertEqual(shadow[shadow.index("--model") + 1], "test-opus")
        self.assertIn(".mcp.planner-shadow.json", shadow)
        self.assertIn("--disallowedTools", shadow)
        self.assertEqual(before, {p: p.read_bytes() for p in bus.TASKS.glob("*.json")})
        self.assertEqual(plan, (PR.STATE / "plan.md").read_bytes())
        record = PR._load_records()[0]
        self.assertTrue(record["shadow_log"])
        self.assertEqual(record["shadow_pid_start"], "start-8001")
        row = self.rows()[0]
        self.assertEqual((row["tier"], row["mode"]), ("fable", "shadow"))
        self.assertEqual(row["packet_chars"], len(production[2]))

    def test_hard_decision_holds_when_fable_unavailable(self):
        self.pool.cfg["planner"]["routing"]["mode"] = "active"
        del self.pool.cfg["models"]["planner"]
        goal = self.complex_goal()
        self.scout_child(goal)
        PR.tick(self.pool)
        self.popen.assert_not_called()
        records = PR._load_records()
        self.assertTrue(records)
        for record in records:
            self.assertEqual(record["status"], "held_for_fable")
            self.assertTrue(record["hold_reason"].startswith("fable_unavailable"))
            self.assertEqual(record["attempts"], 0)
        self.notify.assert_called_once()
        self.pool.cfg["models"]["planner"] = "test-fable"
        PR.tick(self.pool)
        self.popen.assert_called_once()
        self.assertEqual(PR._load_records()[0]["status"], "running")
        self.ask.assert_not_called()

    def test_skips_usage_materiality_and_jev_invocation(self):
        routine = self.goal()
        self.execute_child(routine, status="held", hold_reason="gate_red",
                           resume_hint={"failures": "FAILED tests/test_x.py::test_x"})
        PR.tick(self.pool)
        self.assertIn("routine", [r["reason"] for r in self.rows("planner_skips")])
        goal = self.goal()
        self.scout_child(goal)
        sections = self.sections(goal)
        self.pool.cfg["planner"]["routing"].update(mode="shadow", jev_mode="shadow")
        with patch.object(planner_scorecard, "class_evidence",
                          return_value={"n": 0, "noninferior": None, "reescalation_rate": None}):
            PR.run_group(sections, self.pool)
        self.ask.assert_called_once()
        self.assertTrue(self.rows("planner_jev")[0]["asked"])
        self.assertFalse(PR.run_group(sections, self.pool)["launched"])
        record = next(r for r in PR._load_records() if r["goal_id"] == goal)
        Path(record["log"]).write_text(json.dumps({
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 20,
                      "cache_creation_input_tokens": 3},
            "total_cost_usd": .1, "session_id": "session"}))
        # Finish production while the shadow PID is still alive, then finish shadow later.
        self.swap(goals, "identity_of", lambda pid, start: bool(start))
        records = PR._load_records()
        for r in records:
            if r["goal_id"] == goal:
                r["pid_start"] = None
        PR._save_records(records)
        new_task = self.execute_child(goal)
        PR.reconcile()
        phases = [r for r in self.rows() if r["launch_id"] == record["launch_id"]]
        self.assertEqual([r["phase"] for r in phases], ["launch", "materiality", "usage"])
        self.assertIn(new_task, phases[1]["new_tasks"])
        self.assertEqual(phases[2]["cache_write_tokens"], 3)
        self.assertFalse(self.rows("planner_shadow"))
        Path(record["shadow_log"]).write_text(json.dumps({"result": json.dumps(
            {"proposed_action": "write_specs", "tasks_proposed": [], "needs_fable": False})}))
        goals.identity_of = lambda *a: False
        PR.reconcile()
        PR.reconcile()
        self.assertEqual(len(self.rows("planner_shadow")), 1)
        self.assertTrue(next(r for r in PR._load_records() if r["goal_id"] == goal)["shadow_agreement"]["agreement"])
        # A fresh goal with an unchanged guard still emits a tick skip.
        unchanged = self.goal()
        self.scout_child(unchanged)
        guards = PR._goal_launches()
        guards[unchanged] = {"last_state_version": PR.state_version(unchanged)}
        PR._save_records(PR._load_records(), guards)
        PR.tick(self.pool)
        reasons = [r["reason"] for r in self.rows("planner_skips")]
        self.assertIn("same_state_version", reasons)
        self.assertIn("none:unchanged_state", reasons)
        hard = self.complex_goal()
        self.scout_child(hard)
        self.ask.reset_mock()
        PR.tick(self.pool)
        self.ask.assert_not_called()

    def test_reescalation_forces_fable_after_opus_and_tie_break(self):
        self.pool.cfg["planner"]["routing"]["mode"] = "active"
        goal = self.goal()
        first = self.execute_child(goal, status="held", hold_reason="manual", complexity=4)
        second = self.execute_child(goal, status="held", hold_reason="manual", complexity=6)
        third = self.execute_child(goal, status="held", hold_reason="manual", complexity=6)
        for _ in range(2):
            review = bus.create_task("review", "spec", ["ok"], ["x"], role="spec_review",
                                     parent=goal, inputs=[second], complexity=3)
            bus.update(review["id"], review_verdict="request_changes", status="done")
        telemetry._append("planner_invocations", {"launch_id": "previous", "goal_id": goal,
                          "tier": "opus", "started_at": 1}, root=PR.STATE)
        sections = list(reversed(self.sections(goal)))
        self.assertTrue(PR.run_group(sections, self.pool)["launched"])
        row = self.rows()[-1]
        self.assertEqual(row["payload_keys"], [second, third, first])
        self.assertEqual(row["complexity"], 6)
        self.assertTrue(row["reescalation"])
        self.assertEqual(row["tier"], "fable")
        argv = self.popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "test-fable")
        decisions = self.rows("decisions")
        route = next(r for r in decisions if r["kind"] == "planner_route")
        self.assertIn("repeated_spec_rejection", route["hard_constraints"])
        self.assertIn("reescalation", route["hard_constraints"])
        self.ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
