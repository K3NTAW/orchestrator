import _harness  # noqa: F401
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import skill_scorecard


class SkillScorecardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "runs").mkdir()
        (self.root / "tasks").mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def _rows(self, rows):
        (self.root / "runs/2026-01-01.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_use_rate_and_overhead_ratio_per_skill(self):
        self._rows([
            {"role": "execute", "input_tokens": 100, "context": {"skills_exposed": ["executor/x"], "skills_used": ["executor/x"], "skill_tokens_l0": 10, "skill_tokens_l2": 20}},
            {"role": "execute", "input_tokens": 100, "context": {"skills_exposed": ["executor/x"], "skills_used": [], "skill_tokens_l0": 10, "skill_tokens_l2": 0}},
        ])
        row = skill_scorecard.build(self.root)["by_role_skill"]["execute/executor/x"]
        self.assertEqual(row["use_rate"], .5)
        self.assertEqual(row["skill_overhead_ratio"], .2)

    def test_skill_tokens_per_accepted_task(self):
        (self.root / "tasks/T-1.json").write_text(json.dumps({"id": "T-1", "merged_into": "main"}))
        self._rows([{"task": "T-1", "lineage_root": "G-1", "role": "execute",
                     "context": {"skills_exposed": ["executor/x"], "skill_tokens_l0": 3, "skill_tokens_l2": 7}}])
        card = skill_scorecard.build(self.root)
        self.assertEqual(card["skill_tokens_per_accepted_task"]["G-1"], 10)

    def test_skill_recovery_rate_and_reduction(self):
        self._rows([
            {"role": "execute", "context": {"skills_exposed": ["executor/x", "executor/y"],
             "skills_selected": ["executor/x"], "skills_used": ["executor/y"],
             "skill_tokens_l0": 10, "skill_tokens_selected_l0": 4}},
            {"role": "execute", "context": {"skills_exposed": ["executor/x", "executor/y"],
             "skills_selected": ["executor/x"], "skills_used": ["executor/x"],
             "skill_tokens_l0": 10, "skill_tokens_selected_l0": 4}},
        ])
        row = skill_scorecard.build(self.root)["by_role"]["execute"]
        self.assertEqual(row["selected_set_size_avg"], 1)
        self.assertEqual(row["skill_reduction"], .6)
        self.assertEqual(row["skill_recovery_rate"], .5)


if __name__ == "__main__":
    unittest.main()
