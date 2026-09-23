"""Orchestration overhead measurements."""
import _harness
import json, tempfile, unittest
from pathlib import Path
from unittest import mock

from orchestrator import overhead, scorecard


class Overhead(unittest.TestCase):
    def _root(self, directory):
        root = Path(directory) / ".orchestrator"
        (root / "runs").mkdir(parents=True)
        return root

    def test_for_goal_splits_roles_and_computes_ratios(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            rows = [
                {"ts": 10, "goal_id": "T-1", "role": "execute", "duration_s": 5, "usd": 3,
                 "provider": "claude", "usage": {"input_tokens": 80, "output_tokens": 20}},
                {"ts": 30, "goal_id": "T-1", "role": "review", "duration_s": 4, "usd": 1,
                 "provider": "claude", "usage": {"input_tokens": 40, "output_tokens": 10}},
                {"ts": 40, "role": "memory", "est_tokens": 7},
            ]
            (root / "runs/2026-01-01.jsonl").write_text("\n".join(map(json.dumps, rows)))
            result = overhead.for_goal("T-1", root)
            self.assertEqual((result["orchestration_tokens"], result["execution_tokens"]), (50, 100))
            self.assertEqual(result["amplification"], .5)
            self.assertEqual(result["cost_share"], .25)
            self.assertEqual(result["latency_share"], .2)
            self.assertEqual(result["unattributed_tokens"], 7)
            self.assertEqual(result["roles"]["review"], {"tokens": 50, "runs": 1})

    def test_amplification_none_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            (root / "runs/a.jsonl").write_text(json.dumps(
                {"ts": 1, "goal_id": "T-1", "role": "scout", "est_tokens": 10}) + "\n")
            self.assertIsNone(overhead.for_goal("T-1", root)["amplification"])

    def test_report_over_real_run_rows(self):
        repository = Path(__file__).resolve().parents[3]
        real = repository / ".orchestrator"
        rows = overhead.report(real)
        self.assertTrue(any(row["goal_id"].startswith("T-") and row["goal_id"] not in ("total", "median")
                            for row in rows))
