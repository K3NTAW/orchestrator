import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import sched_scorecard


class SchedScorecard(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "tasks").mkdir()
        (self.root / "runs" / "sched").mkdir(parents=True)

    def task(self, task_id, **values):
        task = {"id": task_id, "role": "execute", "scope": [f"src/{task_id}.py"], **values}
        (self.root / "tasks" / f"{task_id}.json").write_text(json.dumps(task))

    def rows(self, name, *rows):
        (self.root / "runs" / "sched" / f"{name}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows))

    def test_predicted_hard_pair_that_conflicted_counts_toward_precision(self):
        self.task("A", pipeline={"dispatched_at": 1, "gated_at": 5})
        self.task("B", reason="rebase_conflict", pipeline={"dispatched_at": 2, "gated_at": 6})
        self.rows("waves", {"ts": 1, "predicted": [{"a": "A", "b": "B", "level": "hard", "reasons": ["same_file"]}]})
        card = sched_scorecard.build(self.root)
        self.assertEqual(card["hard_conflict_precision"], 1.0)
        self.assertEqual(card["hard_conflict_precision_n"], 1)

    def test_stale_outcomes_are_attributed_to_pairs_and_rate(self):
        self.task("A")
        self.task("B")
        self.rows("waves", {"predicted": [{"a": "A", "b": "B", "level": "soft", "reasons": []}]})
        self.rows("stale", {"task": "B", "risk": "high"})
        self.assertTrue(sched_scorecard.pair_outcomes(self.root)[0]["stale_event"])
        self.assertEqual(sched_scorecard.build(self.root)["stale_work_rate"], 1.0)

    def test_duration_prediction_error_uses_priority_estimate_and_actual(self):
        self.task("A", pipeline={"claimed_at": 100, "gated_at": 700})
        self.rows("waves", {"priority": {"A": {"est_duration_s": 1200}}})
        outcome = sched_scorecard.task_outcomes(self.root)[0]
        self.assertEqual(outcome["error"]["ratio"], 2.0)
        card = sched_scorecard.build(self.root)
        self.assertEqual(card["duration_mape"], 1.0)
        self.assertEqual(card["duration_mape_n"], 1)

    def test_malformed_telemetry_is_tolerated_and_counted(self):
        (self.root / "runs" / "sched" / "waves.jsonl").write_text("broken\n")
        (self.root / "runs" / "sched" / "stale.jsonl").write_text("[1]\n")
        card = sched_scorecard.build(self.root)
        self.assertEqual(card["malformed"], 2)
        self.assertIsNone(card["hard_conflict_precision"])
        self.assertIsNone(card["duration_mape"])

    def test_unnecessary_serialization_detects_disjoint_deferred_pairs(self):
        self.task("A", changed_files=["one.py"])
        self.task("B", changed_files=["two.py"])
        self.rows("waves", {"predicted": [{"a": "A", "b": "B", "level": "soft", "reasons": []}],
                            "deferred": [{"a": "A", "b": "B", "reason": "soft:same_dir"}]})
        card = sched_scorecard.build(self.root)
        self.assertEqual(card["unnecessary_serialization"], 1.0)
        self.assertEqual(card["unnecessary_serialization_n"], 1)

    def test_unnecessary_serialization_ignores_pairs_without_literal_files(self):
        self.task("A", changed_files=["one.py"])
        self.task("glob", scope=["src/*.py"])
        self.task("empty", changed_files=[])
        unknown = ["glob", "empty", "missing"]

        def wave(partners):
            self.rows("waves", {
                "predicted": [{"a": "A", "b": partner, "level": "soft"}
                              for partner in partners],
                "deferred": [{"a": "A", "b": partner, "reason": "soft:same_dir"}
                             for partner in partners],
            })

        wave(unknown)
        card = sched_scorecard.build(self.root)
        self.assertIsNone(card["unnecessary_serialization"])
        self.assertEqual(card["unnecessary_serialization_n"], 0)
        self.assertEqual(card["serialization_undetermined"], 3)

        self.task("disjoint", changed_files=["two.py"])
        self.task("overlap", changed_files=["one.py"])
        wave(unknown + ["disjoint", "overlap"])
        card = sched_scorecard.build(self.root)
        self.assertEqual(card["unnecessary_serialization"], 0.5)
        self.assertEqual(card["unnecessary_serialization_n"], 2)
        self.assertEqual(card["serialization_undetermined"], 3)
        self.assertIn("serialization_undetermined", sched_scorecard.format(card))

    def test_decisions_stream_is_not_read(self):
        from unittest.mock import patch

        with patch.object(sched_scorecard.schedlog, "read_with_malformed",
                          return_value=([], 0)) as read:
            sched_scorecard.build(self.root)
        self.assertEqual({call.args[0] for call in read.call_args_list},
                         {"waves", "stale", "dispatch"})


if __name__ == "__main__":
    unittest.main()
