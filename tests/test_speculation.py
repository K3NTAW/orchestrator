"""Selective speculative execution policy."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO  # noqa: F401
from orchestrator import speculation


def snap(*ids):
    return {"executors": {executor: {"free": 1, "roles": ["execute"], "complexity_min": 1,
                                      "complexity_max": 10, "enabled": True,
                                      "day_tasks_left": 1, "cooldown_until": 0}
                          for executor in ids}}


def inputs(**changes):
    task = {"id": "T", "executor": "a", "complexity": 4, "scope": ["app.py"]}
    task.update(changes.pop("task", {}))
    values = {"priority": {"critical_path_s": 20},
              "ready_priorities": {"T": {"critical_path_s": 20}, "U": {"critical_path_s": 10}},
              "evidence": {"a": {"fix_round_p": .7, "n": 8,
                                   "cost_to_accepted_usd": 3.0}},
              "snapshot": snap("a", "b"), "budget_left_usd": 10, "cfg": {}}
    values.update(changes)
    return task, values


class Speculation(unittest.TestCase):
    def test_default_mode_off_never_speculates(self):
        task, kwargs = inputs()
        result = speculation.plan(task, **kwargs)
        self.assertFalse(result["speculate"])
        self.assertEqual(result["reason"], "mode_off")
        self.assertTrue(result["eligibility"]["eligible"])

    def test_root_config_does_not_enable_speculation(self):
        for cfg in ({"mode": "active"}, {"mode": "active", "speculation": None}):
            with self.subTest(cfg=cfg):
                task, kwargs = inputs(cfg=cfg)
                self.assertEqual(speculation.plan(task, **kwargs)["reason"], "mode_off")

    def test_ordinary_task_is_ineligible_with_reasons(self):
        task, kwargs = inputs(priority={"critical_path_s": 5})
        kwargs["evidence"]["a"]["fix_round_p"] = .2
        result = speculation.eligible(task, **kwargs)
        self.assertIn("not_critical", result["reasons"])
        self.assertIn("low_fix_round_p", result["reasons"])

    def test_critical_path_task_with_high_rework_and_capacity_is_eligible(self):
        task, kwargs = inputs(cfg={"speculation": {"mode": "active"}})
        result = speculation.plan(task, **kwargs)
        self.assertTrue(result["speculate"])
        self.assertEqual(len(set(result["eligibility"]["executors"])), 2)
        kwargs["evidence"] = {"a": {"n_tasks": 8, "fix_round_probability": .7,
                                     "cost_to_accepted": 3.0}}
        self.assertTrue(speculation.plan(task, **kwargs)["speculate"])

    def test_explicit_retry_cost_and_sample_count_take_precedence(self):
        task, kwargs = inputs()
        kwargs["evidence"]["a"].update(fix_round_cost_usd=0, n=0, n_tasks=8)
        reasons = speculation.eligible(task, **kwargs)["reasons"]
        self.assertIn("low_retry_cost", reasons)
        self.assertIn("insufficient_samples", reasons)

    def test_insufficient_budget_or_no_spare_capacity_blocks(self):
        task, kwargs = inputs(budget_left_usd=5)
        self.assertIn("insufficient_budget", speculation.eligible(task, **kwargs)["reasons"])
        kwargs.update(budget_left_usd=10, snapshot=snap("a"))
        self.assertIn("no_spare_capacity", speculation.eligible(task, **kwargs)["reasons"])

    def test_select_result_picks_exactly_one_and_estimator_summary(self):
        candidates = [
            {"executor": "a", "tests_green": True, "gate_reds": 1, "usd": 1, "duration_s": 2, "sha": "a"},
            {"executor": "b", "tests_green": True, "gate_reds": 0, "usd": 2, "duration_s": 3, "sha": "b"},
        ]
        selected = speculation.select_result(candidates)
        self.assertEqual(selected["selected"]["executor"], "b")
        self.assertEqual(len(selected["discarded"]), 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tasks").mkdir()
            (root / "runs").mkdir()
            initial = {"id": "T", "executor": "a", "updated_at": 10}
            fix = {"id": "F", "executor": "a", "constraints": {"fix_round_for": "T"}, "updated_at": 20}
            (root / "tasks" / "T.json").write_text(json.dumps(initial))
            (root / "tasks" / "F.json").write_text(json.dumps(fix))
            (root / "runs" / "r.jsonl").write_text("\n".join((
                json.dumps({"task": "T", "role": "execute", "usd": 1}),
                json.dumps({"task": "F", "role": "execute", "usd": 3}))))
            evidence = {"a": {"first_pass_green_rate": .8, "n_tasks": 8,
                               "fix_round_probability": .7, "cost_to_accepted": 3.0}}
            rows = speculation.shadow_estimate(root, evidence)
            self.assertAlmostEqual(rows[0]["fix_round_usd"], 2.1)
            self.assertAlmostEqual(rows[0]["expected_saved_usd"], .68)
            evidence["a"]["fix_round_cost_usd"] = 0
            explicit = speculation.shadow_estimate(root, evidence)
            self.assertEqual(explicit[0]["fix_round_usd"], 0)
            self.assertFalse(explicit[0]["would_have_paid"])
        self.assertTrue(rows[0]["would_have_paid"])
        self.assertEqual(speculation.summary(rows)["paid_rate"], 1)


if __name__ == "__main__":
    unittest.main()
