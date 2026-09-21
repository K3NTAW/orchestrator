import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _harness  # noqa: F401 - establishes isolated ORCH_ROOT before orchestrator imports
from orchestrator import strategy


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo" / ".orchestrator"
        (self.root / "tasks").mkdir(parents=True)
        (self.root / "runs").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, task):
        (self.root / "tasks" / f'{task["id"]}.json').write_text(json.dumps(task))

    def test_derive_attributes_strategy_steps_and_components(self):
        root = {"id": "T-root", "role": "execute", "parent": "G", "created_at": 2,
                "pipeline": {"review_reason": "diff_unavailable"}}
        rows = [root,
                {"id": "T-s", "role": "scout", "parent": "G", "created_at": 1,
                 "constraints": {"objective": "dependency-map"}},
                {"id": "T-spec", "role": "spec_review", "inputs": ["T-root"]},
                {"id": "T-a", "role": "review", "inputs": ["T-root"],
                 "constraints": {"reviewer_role": "acceptance"}},
                {"id": "T-b", "role": "review", "inputs": ["T-root"],
                 "constraints": {"reviewer_role": "adversarial"}},
                {"id": "T-fix", "role": "execute", "constraints": {"fix_round_for": "T-root"}}]
        got = strategy.derive(root, {row["id"]: row for row in rows}, [{"wave": ["T-root", "T-x"]}])
        self.assertEqual(got["strategy"], "dependency_scout_execute+execute_specialist_review+security_context_strong_execute_security_review+parallel_wave")
        self.assertEqual(got["steps"], ["scout", "spec_review", "execute", "review", "security_review", "fix_round"])
        self.assertEqual(got["components"], got["strategy"].split("+"))

    def test_observations_use_real_bus_fields_and_lineage_sums(self):
        self.write({"id": "T-root", "role": "execute", "parent": "G", "complexity": 5,
                    "constraints": {"task_class": "unfamiliar"}, "executor": "codex", "created_at": 10,
                    "merged_into": "goal/G", "pipeline": {"merged_at": 25}})
        self.write({"id": "T-fix", "role": "execute", "constraints": {"fix_round_for": "T-root"},
                    "pipeline": {"gate_reds": 1}})
        self.write({"id": "T-r", "role": "review", "inputs": ["T-fix"], "tier": "strong",
                    "result": {"verdict": "request_changes"}})
        metrics = {"tasks": {"T-root": {"usd": 2, "tokens": 30, "fix_rounds": 1,
                                          "first_pass": False, "goal_id": "G"}}}
        with patch.object(strategy.scorecard_api, "efficiency", return_value=metrics):
            row, = strategy.observations(self.root)
        self.assertTrue(row["accepted"])
        self.assertEqual((row["time_s"], row["gate_reds"], row["review_request_changes"]), (15, 1, 1))
        self.assertEqual((row["task_class"], row["band"]), ("unfamiliar", "4-6"))

    def test_observations_aggregate_outcomes_per_strategy(self):
        rows = [{"strategy": "direct_execute", "task_class": "unfamiliar", "band": "4-6", "accepted": True,
                 "first_pass": True, "usd": 1, "time_s": 3, "fix_rounds": 0},
                {"strategy": "direct_execute", "task_class": "unfamiliar", "band": "4-6", "accepted": False,
                 "first_pass": False, "usd": 3, "time_s": None, "fix_rounds": 1},
                {"strategy": "scout_execute", "task_class": "unfamiliar", "band": "4-6", "accepted": True,
                 "first_pass": False, "usd": 2, "time_s": 4, "fix_rounds": 1}]
        with patch.object(strategy, "observations", return_value=rows):
            card = strategy.scorecard(self.root)
        self.assertEqual(card[("direct_execute", "unfamiliar", "4-6")]["n"], 2)
        self.assertEqual(card[("direct_execute", "unfamiliar", "4-6")]["accepted_rate"], .5)
        self.assertEqual(card[("direct_execute", "unfamiliar", "4-6")]["first_pass_rate"], 1)
        self.assertEqual(card[("direct_execute", "unfamiliar", "4-6")]["median_usd"], 2)

    def test_cold_start_and_insufficient_evidence_return_no_strategy(self):
        with patch.object(strategy, "observations", return_value=[]):
            cold = strategy.recommend("unfamiliar", "4-6", root=self.root)
        with patch.object(strategy, "observations", return_value=[self.sample("direct_execute", True)]):
            small = strategy.recommend("unfamiliar", "4-6", root=self.root)
        self.assertEqual((cold["strategy"], small["strategy"]), (None, None))
        self.assertEqual(cold["reason"], "insufficient_evidence")
        self.assertTrue(cold["required"])

    def sample(self, name, accepted, repo="repo", task_class="unfamiliar"):
        return {"strategy": name, "task_class": task_class, "band": "4-6", "repo": repo,
                "accepted": accepted, "first_pass": accepted, "usd": 1 if accepted else 2}

    def test_shadow_recommendation_prefers_evidence_without_changing_policy(self):
        rows = ([self.sample("direct_execute", i < 8) for i in range(12)]
                + [self.sample("scout_execute", i < 11) for i in range(12)])
        with patch.object(strategy, "observations", return_value=rows):
            shadow = strategy.recommend("unfamiliar", "4-6", repo="repo", root=self.root)
            off = strategy.recommend("unfamiliar", "4-6", repo="repo", root=self.root,
                                     cfg={"strategy": {"mode": "off"}})
        self.assertEqual((shadow["mode"], shadow["strategy"]), ("shadow", "scout_execute"))
        self.assertIsNone(off["strategy"])

    def test_repository_and_task_class_are_separated(self):
        rows = ([self.sample("direct_execute", True) for _ in range(10)]
                + [self.sample("scout_execute", True, repo="elsewhere") for _ in range(12)]
                + [self.sample("parallel_wave", True, task_class="security") for _ in range(12)])
        with patch.object(strategy, "observations", return_value=rows):
            got = strategy.recommend("unfamiliar", "4-6", repo="repo", root=self.root)
        self.assertEqual(got["strategy"], "direct_execute")
        self.assertNotIn("scout_execute", got["evidence"])
        self.assertNotIn("parallel_wave", got["evidence"])


if __name__ == "__main__":
    unittest.main()
