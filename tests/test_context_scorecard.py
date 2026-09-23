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

    def test_recovery_rate_from_evidence_reuse_rows(self):
        from orchestrator import decision_log, promotion
        root = self.root_with([])
        for role, hidden, reads in (("execute", 4, 2), ("review", 2, 1)):
            decision_log.record(kind="context_selection", subject=role,
                candidates=[f"{i}:HIDE" for i in range(hidden)] + ["short:SHORT"],
                hard_constraints=[], deterministic={"role": role}, selected="routed v1", reason="selection",
                mode="active", root=root)
            for _ in range(reads):
                decision_log.record(kind="evidence_reuse", subject=role, candidates=["0"],
                    hard_constraints=[], deterministic={"role": role}, selected="0", reason="recovery_read",
                    mode="active", root=root)
        decision_log.record(kind="evidence_reuse", subject="execute", candidates=["0"],
            hard_constraints=[], deterministic={}, selected="0", reason="recovery_read", mode="shadow", root=root)
        card = context_scorecard.build(root)
        for role in ("execute", "review"):
            self.assertEqual(card["roles"][role]["recovery_rate"], .5)
        self.assertIn("recovery_rate", context_scorecard.format_report(card))
        result = promotion.collect("context_router", root)
        self.assertEqual(result["context_recovery_rate"], .5)
        self.assertEqual(result["context_recovery_n"], 6)

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

    def test_instruction_columns_keyed_like_bus_context(self):
        rows = [
            {"role": "execute", "context": {"instruction_tokens": 100,
                                             "instruction_tokens_modular": 60}},
            {"role": "execute", "context": {"instruction_tokens": 80}},
            {"role": "review", "context": {"instruction_tokens": 40}},
        ]
        roles = context_scorecard.build(self.root_with(rows))["roles"]
        execute = roles["execute"]
        self.assertEqual(execute["instruction_tokens"], 90)
        self.assertEqual(execute["instruction_tokens_modular"], 60)
        self.assertEqual(execute["instruction_reduction"], .6)
        self.assertEqual(execute["instruction_unmeasured"], 1)
        self.assertEqual((execute["measured"], execute["unmeasured"]), (2, 0))
        review = roles["review"]
        self.assertEqual(review["instruction_tokens"], 40)
        self.assertIsNone(review["instruction_tokens_modular"])
        self.assertIsNone(review["instruction_reduction"])
        self.assertEqual(review["instruction_unmeasured"], 1)

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

    def test_hidden_tool_recovery_rate_in_by_role_task_class(self):
        from orchestrator import decision_log
        root = self.root_with([])
        (root / "tasks").mkdir()
        (root / "tasks" / "T-1.json").write_text(json.dumps(
            {"id": "T-1", "role": "review", "scope": ["x.py"], "constraints": {"task_class": "feature"}}))
        for reason in ("review/feature: categories bus,git,read,search", "hidden_tool_requested"):
            decision_log.record(kind="tool_disclosure", subject="T-1", candidates=["Read"],
                hard_constraints=["Read"], deterministic={"role": "review"}, selected=["Read"],
                reason=reason, mode="active", root=root)
        row = context_scorecard.build(root)["by_role_task_class"][("review", "feature")]
        self.assertEqual(row["hidden_tool_recovery_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
