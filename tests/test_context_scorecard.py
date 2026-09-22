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


if __name__ == "__main__":
    unittest.main()
