import unittest
from unittest import mock

from tests import _harness  # noqa: F401
from orchestrator import allocation


class AllocationTest(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "T-1", "complexity": 5}
        self.priority = {"critical_path_s": 1000}
        self.evidence = {
            "cheap": {"cost_to_accepted_usd": 0.5, "first_pass_p": 0.8, "n": 8},
            "strong": {"cost_to_accepted_usd": 1.5, "first_pass_p": 0.8, "n": 8},
        }

    def choose(self, ids=("cheap", "strong"), baseline="cheap", **kwargs):
        params = {"priority": self.priority, "ready_priorities": [1000],
                  "evidence": self.evidence, "durations": {key: 600 for key in ids},
                  "cfg": {"mode": "active"}}
        params.update(kwargs)
        return allocation.choose(self.task, ids, baseline=baseline, **params)

    def test_evidence_for_is_scoped_to_the_task_class(self):
        economics = {
            ("a", "known"): {"cost_to_accepted": 2.0, "fix_round_probability": .25, "n_tasks": 8},
            ("a", "novel"): {"cost_to_accepted": 9.0, "fix_round_probability": .5, "n_tasks": 3},
            ("b", "novel"): {"cost_to_accepted": 1.0, "fix_round_probability": 0, "n_tasks": 7},
        }
        result = allocation.evidence_for(["a", "b"], "known", economics=economics)
        self.assertEqual(result["a"], {"cost_to_accepted_usd": 2.0, "first_pass_p": .75,
                                        "expected_rework_usd": .5, "n": 8})
        self.assertEqual(result["b"], {"cost_to_accepted_usd": None, "first_pass_p": None,
                                        "expected_rework_usd": None, "n": 0})

    def test_cheap_executor_preferred_when_outcomes_equivalent(self):
        self.assertEqual(self.choose()["executor"], "cheap")

    def test_stronger_executor_only_when_downstream_value_justifies(self):
        evidence = {
            "cheap": {"cost_to_accepted_usd": .8, "first_pass_p": .8, "n": 5},
            "strong": {"cost_to_accepted_usd": 1.2, "first_pass_p": .8, "n": 5},
        }
        durations = {"cheap": 2400, "strong": 600}
        critical = self.choose(evidence=evidence, durations=durations)
        self.assertEqual(critical["executor"], "strong")
        noncritical = self.choose(evidence=evidence, durations=durations,
                                  ready_priorities=[2000],
                                  cfg={"mode": "active", "non_critical_factor": .25})
        self.assertEqual(noncritical["executor"], "cheap")

    def test_hard_eligibility_always_wins(self):
        result = self.choose(ids=("strong",), baseline="strong",
                             evidence={"strong": self.evidence["strong"]})
        self.assertEqual(result["executor"], "strong")
        empty = self.choose(ids=(), baseline="cheap", evidence={})
        self.assertIsNone(empty["executor"])
        self.assertEqual(empty["reason"], "no_eligible")

    def test_sparse_evidence_falls_back_to_baseline(self):
        evidence = {**self.evidence, "strong": {**self.evidence["strong"], "n": 1}}
        result = self.choose(baseline="strong", evidence=evidence)
        self.assertEqual((result["executor"], result["reason"]), ("strong", "sparse_evidence"))

    def test_modes_and_decision_row(self):
        off = self.choose(cfg={"mode": "off"})
        self.assertEqual((off["executor"], off["reason"], off["would_pick"]),
                         ("cheap", "mode_off", None))
        shadow = self.choose(baseline="strong", cfg={"mode": "shadow"})
        self.assertEqual((shadow["executor"], shadow["would_pick"]), ("strong", "cheap"))
        with mock.patch.object(allocation.decision_log, "record", return_value={}) as write:
            allocation.record("T-1", shadow)
        fields = write.call_args.kwargs
        self.assertEqual(fields["kind"], "allocation")
        self.assertEqual(fields["candidates"], ["cheap", "strong"])
        self.assertEqual(fields["selected"], "strong")
        self.assertIn("strong", fields["rejected"])


if __name__ == "__main__":
    unittest.main()
