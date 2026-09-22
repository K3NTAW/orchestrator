import _harness  # noqa: F401 - share the suite's single isolated ORCH_ROOT
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import handoff_scorecard, planner_telemetry


class TestHandoffScorecard(unittest.TestCase):
    def task(self, root, task_id, **fields):
        row = {"id": task_id, "role": "execute", "complexity": 4,
               "constraints": {}, "status": "done", **fields}
        path = Path(root) / "tasks" / f"{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf-8")
        return row

    def add_run(self, root, **fields):
        path = Path(root) / "runs" / "2026-09-22.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"role": "execute", **fields}) + "\n")

    def test_lineage_rows_orders_rounds_and_counts_resume_vs_fresh(self):
        with tempfile.TemporaryDirectory() as root:
            self.task(root, "T-root", merged_into="goal/G", executor="cheap")
            self.task(root, "T-fix1", executor="cheap", hold_reason="gate_red",
                      constraints={"fix_round_for": "T-root"})
            self.task(root, "T-fix2", executor="strong", hold_reason="review request_changes: tests",
                      constraints={"fix_round_for": "T-fix1"})
            self.add_run(root, task="T-fix2", executor="strong", input_tokens=40,
                     cache_read_input_tokens=10, resume_mode="fresh")
            self.add_run(root, task="T-root", executor="cheap", input_tokens=10)
            self.add_run(root, task="T-fix1", executor="cheap", input_tokens=20, resume_mode="resume")
            row = handoff_scorecard.lineage_rows(root)[0]
            self.assertEqual(row["executors_by_round"], ["cheap", "cheap", "strong"])
            self.assertEqual((row["rounds"], row["resume_rounds"], row["fresh_rounds"]), (3, 1, 1))
            self.assertEqual(row["reconstruction_tokens"], 30)
            self.assertTrue(row["estimated"])
            self.assertEqual(row["handoff_pairs"][1][2], "review")

    def test_expected_route_cost_insufficient_below_min_samples(self):
        with tempfile.TemporaryDirectory() as root:
            self.task(root, "T-one", executor="cheap", merged_into="main")
            self.add_run(root, task="T-one", executor="cheap", input_tokens=10)
            self.assertEqual(handoff_scorecard.expected_route_cost(
                "unfamiliar", "cheap", root, {"promotion": {"min_samples": 2}}),
                {"insufficient": True, "n": 1})

    def test_start_strong_flag_when_cheap_first_costs_more_overall(self):
        with tempfile.TemporaryDirectory() as root:
            for executor, first, fix in (("cheap", 2, 100), ("strong", 10, 0)):
                for index in range(2):
                    tid = f"T-{executor}-{index}"
                    self.task(root, tid, executor=executor, merged_into="main")
                    self.add_run(root, task=tid, executor=executor, input_tokens=first)
                    if fix:
                        fid = tid + "-fix"
                        self.task(root, fid, executor=executor, constraints={"fix_round_for": tid})
                        self.add_run(root, task=fid, executor=executor, input_tokens=fix, resume_mode="resume")
            result = handoff_scorecard.recommendation(
                "unfamiliar", root, {"promotion": {"min_samples": 2}})
            self.assertEqual(result["executor"], "strong")
            self.assertEqual(result["cheapest_first"], "cheap")
            self.assertTrue(result["start_strong"])

    def test_planner_handoffs_reports_unmeasured_fields(self):
        with tempfile.TemporaryDirectory() as root:
            planner_telemetry.record_launch(
                launch_id="L-1", goal_id="G-1", event="tick", decision_type="close",
                kind="planner", payload_keys=[], model="m", tier="fable", account="A",
                complexity=3, band="small", task_class="code", architectural=False,
                route="escalate", route_reason="shadow", mode="shadow", state_version="1",
                packet_chars=None, started_at=1, root=root)
            planner_telemetry.record_usage("L-1", input_tokens=11, output_tokens=4,
                                           outcome="done", root=root)
            row = handoff_scorecard.planner_handoffs(root)["rows"][0]
            self.assertEqual(row["before_tokens"], "unmeasured")
            self.assertEqual(row["escalation_packet_tokens"], "unmeasured")
            self.assertEqual(row["after_tokens"], 15)
