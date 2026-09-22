import _harness  # noqa: F401 - share the suite's single isolated ORCH_ROOT
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import decision_log


class DecisionLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _record(self, **overrides):
        fields = {
            "candidates": ["fast", "careful", "local"],
            "hard_constraints": {"available": True},
            "deterministic": {"task_class": "mechanical"},
            "selected": "fast",
            "reason": "Lowest latency among eligible executors",
        }
        fields.update(overrides)
        return decision_log.record("routing", "T-0570", root=self.tmp, **fields)

    def test_record_writes_structured_row_with_required_fields(self):
        row = self._record()

        persisted = json.loads((self.tmp / "runs/sched/decisions.jsonl").read_text())
        self.assertEqual(persisted, row)
        self.assertTrue({
            "ts", "kind", "subject", "candidates", "hard_constraints",
            "deterministic", "selected", "rejected", "reason",
        } <= row.keys())
        self.assertIsInstance(row["ts"], (int, float))
        self.assertEqual(row["kind"], "routing")
        self.assertEqual(row["subject"], "T-0570")
        self.assertEqual(row["rejected"], ["careful", "local"])
        self.assertNotIn("chain_of_thought", row)
        self.assertNotIn("chain-of-thought", row)
        self.assertEqual(decision_log.read_all(root=self.tmp), [row])

    def test_explain_pairs_decisions_with_later_outcomes(self):
        decision_log.outcome("T-0570", "routing", root=self.tmp, merged=False)
        decision_a = self._record()
        decision_b = self._record(selected="careful", reason="Higher review complexity")
        result = decision_log.outcome(
            "T-0570", "routing", root=self.tmp,
            merged=True, conflict=False, fix_rounds=0, actual_duration_s=12, first_pass=True,
        )
        decision_log.outcome("T-other", "routing", root=self.tmp, merged=True)
        decision_log.outcome("T-0570", "wave", root=self.tmp, merged=True)

        rows = decision_log.explain("T-0570", root=self.tmp)
        self.assertEqual(rows, [
            {**decision_b, "outcomes": [result]},
            {**decision_a, "outcomes": []},
        ])
        self.assertEqual(decision_log.explain("T-0570", kinds=["wave"], root=self.tmp), [])

        # Timestamp order, rather than file order, determines the predecessor.
        decision_a = {**decision_a, "ts": 10}
        decision_b = {**decision_b, "ts": 30}
        result = {**result, "ts": 20}
        with patch.object(decision_log, "_read", return_value=(
            [decision_b, result, decision_a], 0,
        )):
            self.assertEqual(decision_log.explain("T-0570", root=self.tmp), [
                {**decision_b, "outcomes": []},
                {**decision_a, "outcomes": [result]},
            ])

    def test_invalid_kind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid decision kind"):
            decision_log.record(
                "guess", "T-0570", candidates=["fast"], hard_constraints={},
                deterministic={}, selected="fast", reason="guessing", root=self.tmp,
            )

        self.assertFalse((self.tmp / "runs/sched/decisions.jsonl").exists())
        self.assertEqual(decision_log.read_all(root=self.tmp), [])

    def test_context_program_kinds_record(self):
        legacy_kinds = set(decision_log.KINDS) - set(decision_log.CONTEXT_KINDS)
        self.assertTrue({"routing", "wave", "jev_sched", "strategy", "planner_route"} <= legacy_kinds)
        for kind in decision_log.CONTEXT_KINDS:
            with self.subTest(kind=kind):
                row = decision_log.record(
                    kind, "T-context", candidates=["candidate"], hard_constraints={},
                    deterministic={}, selected="candidate", reason="context decision",
                    root=self.tmp,
                )
                self.assertEqual(row["kind"], kind)
        for kind in legacy_kinds:
            with self.subTest(legacy_kind=kind):
                self.assertIn(kind, decision_log.KINDS)
        with self.assertRaisesRegex(ValueError, "invalid decision kind"):
            decision_log.record(
                "unknown_context_kind", "T-context", candidates=[], hard_constraints={},
                deterministic={}, selected=None, reason="invalid", root=self.tmp,
            )

    def test_malformed_lines_are_tolerated_and_counted(self):
        first = self._record()
        with (self.tmp / "runs/sched/decisions.jsonl").open("a") as stream:
            stream.write('\nnot json\n[]\n{"partial":\n')
        second = self._record(selected="careful", reason="Higher review complexity")

        rows = decision_log.explain("T-0570", root=self.tmp)
        self.assertEqual(rows, [{**second, "outcomes": []}, {**first, "outcomes": []}])
        self.assertEqual(rows.malformed_count, 3)

    def test_format_explain_is_readable(self):
        self._record(
            rejected={"careful": "Higher latency", "local": "Unavailable"},
            confidence=0.9, n=10,
        )
        decision_log.outcome("T-0570", "routing", root=self.tmp, merged=True)

        lines = decision_log.format_explain(
            decision_log.explain("T-0570", root=self.tmp)
        ).splitlines()
        self.assertIn("routing", lines[0])
        self.assertIn("selected: fast", lines)
        self.assertIn("reason: Lowest latency among eligible executors", lines)
        self.assertIn("rejected: careful: Higher latency, local: Unavailable", lines)
        self.assertIn("confidence/n: 0.9/10", lines)
        self.assertIn("outcome: merged=True", lines)
