import _harness

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from orchestrator import planner_telemetry as telemetry
from orchestrator import planner_runs, pool, schedlog, scorecard


class PlannerTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planner-telemetry-")
        self.root = Path(self.temp.name)
        (self.root / "tasks").mkdir()
        self.addCleanup(self.temp.cleanup)

    def launch(self, launch_id, goal_id="T-0001", tier="fable"):
        return telemetry.record_launch(
            launch_id=launch_id, goal_id=goal_id, event="held",
            decision_type="repair", kind="task", payload_keys=["T-0002"],
            model=f"model-{tier}", tier=tier, account="A", complexity=7,
            band="7-10", task_class="debugging", architectural=False,
            route="escalate", route_reason="held", mode="headless",
            state_version=2, packet_chars=1000, started_at=10.0, root=self.root)

    def task(self, task_id, **fields):
        task = {"id": task_id, "status": "queued", "depends_on": [],
                "created_at": 0, "role": "execute", "events": [], **fields}
        (self.root / "tasks" / f"{task_id}.json").write_text(json.dumps(task))

    def test_launch_usage_materiality_merge_by_launch_id(self):
        original_sched_dir = schedlog.SCHED_DIR
        self.launch("L1")
        telemetry.record_usage("L1", input_tokens=10, output_tokens=20,
                               cache_read_tokens=109, cache_write_tokens=5,
                               outcome="done", root=self.root)
        telemetry.record_materiality("L1", {"tasks": {}, "plan_hash": "a"},
                                     {"tasks": {"T-2": {}}, "plan_hash": "b"}, self.root)
        rows = telemetry.read_invocations(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "model-fable")
        self.assertEqual(rows[0]["total_tokens"], 40)
        self.assertTrue(rows[0]["material"])
        self.assertEqual(rows[0]["new_tasks"], ["T-2"])
        self.assertIs(schedlog.SCHED_DIR, original_sched_dir)

    def test_materiality_detects_new_task_dependency_change_and_fix_strategy(self):
        self.task("T-0001", role="triage", parent=None, status="running")
        self.task("T-0002", parent="T-0001", status="held", hold_reason="gate", result="x")
        before = telemetry.snapshot("T-0001", self.root)
        self.assertIs(before["tasks"]["T-0002"]["result_escalate"], False)
        self.task("T-0002", parent="T-0001", status="held", hold_reason="gate",
                  depends_on=["T-0099"])
        self.task("T-0003", parent="T-0001",
                  constraints={"fix_round_for": "T-0002"})
        after = telemetry.snapshot("T-0001", self.root)
        result = telemetry.materiality(before, after)
        self.assertTrue(result["material"])
        self.assertEqual(result["new_tasks"], ["T-0003"])
        self.assertEqual(result["dependencies_changed"], ["T-0002"])
        self.assertEqual(result["fix_strategy_for"], ["T-0002"])
        self.assertFalse(telemetry.materiality(after, after)["material"])
        escalated = json.loads((self.root / "tasks" / "T-0002.json").read_text())
        escalated["result"] = {"escalate": True}
        (self.root / "tasks" / "T-0002.json").write_text(json.dumps(escalated))
        self.assertTrue(telemetry.materiality(after, telemetry.snapshot("T-0001", self.root))["escalated"])

    def test_plan_hash_is_goal_scoped_and_ignores_h1(self):
        plan = self.root / "plan.md"
        plan.write_text("# T-0001 title\n## GOAL T-0001\none\n## GOAL T-0002\ntwo\n")
        original = telemetry.snapshot("T-0001", self.root)
        plan.write_text("# changed T-0001 title\n## GOAL T-0001\none\n## GOAL T-0002\nchanged\n")
        other_changed = telemetry.snapshot("T-0001", self.root)
        self.assertEqual(original["plan_hash"], other_changed["plan_hash"])
        self.assertFalse(telemetry.materiality(original, other_changed)["plan_changed"])
        plan.write_text("# title\n## GOAL T-0001\nchanged\n## GOAL T-0002\nchanged\n")
        own_changed = telemetry.snapshot("T-0001", self.root)
        self.assertTrue(telemetry.materiality(original, own_changed)["plan_changed"])
        plan.write_text("# T-0001 only\n## GOAL T-00010\nwrong\n")
        absent = telemetry.snapshot("T-0001", self.root)
        self.assertIsNone(absent["plan_hash"])
        self.assertFalse(telemetry.materiality(own_changed, absent)["plan_changed"])
        plan.write_text("## GOAL T-0069 — X (depends on T-0055)\nother goal\n")
        self.assertIsNone(telemetry.goal_plan_section("T-0055", self.root))
        self.assertIsNone(telemetry.snapshot("T-0055", self.root)["plan_hash"])

    def test_per_goal_and_accepted_goal_summary(self):
        self.launch("L1", tier="fable")
        telemetry.record_usage("L1", input_tokens=300, outcome="done", root=self.root)
        telemetry.record_materiality("L1", {"tasks": {}, "plan_hash": None},
                                     {"tasks": {"x": {}}, "plan_hash": None}, self.root)
        self.launch("L2", tier="opus")
        telemetry.record_usage("L2", input_tokens=100, outcome="done", root=self.root)
        telemetry.record_skip(goal_id="T-0001", event="held", reason="dedup", root=self.root)
        item = telemetry.per_goal("T-0001", self.root)
        self.assertEqual((item["fable_tokens"], item["opus_tokens"], item["calls"], item["skips"]),
                         (300, 100, 2, 1))
        self.assertEqual(item["tokens_per_material_decision"], 400)
        with mock.patch.object(scorecard, "accepted_goals", return_value=["T-0001", "T-0002"]):
            summary = telemetry.accepted_goal_summary(self.root)
        self.assertEqual(summary["fable_share"], 0.75)
        self.assertEqual(summary["fable_tokens_per_accepted_goal"], 150)
        with mock.patch.object(scorecard, "accepted_goals", return_value=[]):
            empty = telemetry.accepted_goal_summary(self.root)
        self.assertEqual(empty["n_goals"], 0)
        self.assertIsNone(empty["fable_share"])

    def test_interactive_by_goal_day_overlap_uses_last_done_event(self):
        day1 = datetime(2026, 1, 1, tzinfo=pool.TZ)
        day2 = datetime(2026, 1, 2, tzinfo=pool.TZ)
        now = datetime(2026, 1, 3, tzinfo=pool.TZ).timestamp()
        self.task("T-0001", role="triage", parent=None, status="done",
                  created_at=day1.timestamp(),
                  events=[{"status": "done", "ts": day1.timestamp() + 3600},
                          {"status": "done", "ts": day2.timestamp() + 43200}])
        self.task("T-0002", role="goal", parent=None, status="running",
                  created_at=day1.timestamp() + 43200)
        fake = {"day_totals": [
            {"day": "2026-01-01", "input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 0},
            {"day": "2026-01-02", "input_tokens": 240, "output_tokens": 0, "cache_read_tokens": 0},
        ]}
        cfg = {"models": {"planner": "claude-fable"}, "claude_accounts": []}
        with mock.patch.object(planner_runs, "_interactive_summary", return_value=fake) as summary:
            rows = telemetry.interactive_by_goal(self.root, cfg, now, 30)
        summary.assert_called_once_with(self.root, cfg, now - 30 * 86400, now, [])
        by_goal = {row["goal_id"]: row for row in rows}
        self.assertAlmostEqual(by_goal["T-0001"]["tokens"], 100 * 2 / 3 + 80)
        self.assertAlmostEqual(by_goal["T-0002"]["tokens"], 100 / 3 + 160)
        self.assertTrue(all(row["method"] == "day_overlap_approximate" for row in rows))
        self.assertTrue(all(row["model_source"] == "assumed_models_planner" for row in rows))
        ledger = self.root / "runs" / "sched" / "planner_invocations.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a") as stream:
            stream.write("not json\n")
        _, malformed = telemetry.read_invocations_with_malformed(self.root)
        self.assertEqual(malformed, 1)


if __name__ == "__main__":
    unittest.main()
