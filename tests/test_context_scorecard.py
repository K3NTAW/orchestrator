import _harness
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import context_scorecard


class ContextScorecard(unittest.TestCase):
    def root_with(self, rows):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "runs").mkdir()
        (root / "runs" / "day.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        return root

    def test_amplification_by_section_groups_by_goal(self):
        section = {"est_tokens": 10, "sha256": "same"}
        rows = [{"goal_id": "G", "role": role, "context": {"sections": {"spec": section}}}
                for role in ("execute", "review", "scout")]
        goal = context_scorecard.build(self.root_with(rows))["goals"]["G"]
        self.assertEqual(goal["amplification_by_section"]["spec"], 3.0)
        self.assertEqual(goal["repeated_sections"], ["spec"])

    def test_rows_per_role_and_unmeasured_count(self):
        rows = [{"role": "execute", "context": {"sections": {}, "presented_tokens": 4}},
                {"role": "execute", "context": None}, {"role": "review", "context": None}]
        roles = context_scorecard.build(self.root_with(rows))["roles"]
        self.assertEqual((roles["execute"]["runs"], roles["execute"]["unmeasured"]), (2, 1))
        self.assertEqual(roles["review"]["unmeasured"], 1)

    def test_shadow_columns_present_and_unmeasured_rows_counted(self):
        routed = {"sections": {}, "routed_tokens": 30, "routed_reduction_ratio": .5,
                  "routed_hidden": 2, "routed_ambiguous": 1, "evidence_ids": ["a", "b", "c", "d"]}
        rows = [{"role": "execute", "goal_id": "G", "context": routed},
                {"role": "execute", "goal_id": "G", "context": {"sections": {}}},
                {"role": "execute", "goal_id": "G", "context": None}]
        card = context_scorecard.build(self.root_with(rows))
        for row in (card["roles"]["execute"], card["goals"]["G"]):
            self.assertEqual(row["shadow_routed_tokens"], 30)
            self.assertEqual(row["shadow_reduction"], .5)
            self.assertEqual(row["shadow_hidden"], 2)
            self.assertEqual(row["shadow_ambiguous_rate"], .25)
            self.assertEqual(row["shadow_unmeasured"], 2)
        report = context_scorecard.format_report(card)
        self.assertIn("shadow routed", report)


if __name__ == "__main__":
    unittest.main()
