import _harness

import unittest

from orchestrator.planner_taxonomy import DECISION_TYPES, classify, explain


def _ctx(**overrides):
    values = {
        "scout_count": 0,
        "signature_repeated": False,
        "spec_review_request_changes": 0,
        "auto_fix_rounds_used": 0,
        "auto_fix_rounds": 2,
        "failing_ids": [],
        "in_scope_review_comments": [],
        "ambiguous": False,
        "touches_interfaces": False,
        "touches_migrations": False,
        "touches_auth": False,
        "touches_data_deletion": False,
        "security_trigger": False,
        "semantic_trigger": False,
    }
    values.update(overrides)
    return values


class PlannerTaxonomyTests(unittest.TestCase):
    def test_held_with_failing_tests_is_fix_strategy(self):
        result = classify(
            {"kind": "held"},
            _ctx(failing_ids=["test_failure"]),
            task={"complexity": 3},
        )
        self.assertEqual(result["decision_type"], "fix_strategy")
        self.assertFalse(result["architectural"])
        self.assertEqual(result["risk_class"], "none")

    def test_repeated_signature_is_architectural_replan(self):
        result = classify(
            {"kind": "held"},
            _ctx(signature_repeated=True),
            task={"complexity": 3},
        )
        self.assertEqual(result["decision_type"], "architectural_replan")
        self.assertTrue(result["architectural"])

    def test_scouts_done_without_scouts_is_initial_goal_plan(self):
        no_scouts = classify({"kind": "scouts_done"}, _ctx(scout_count=0))
        with_scouts = classify({"kind": "scouts_done"}, _ctx(scout_count=2))
        self.assertEqual(no_scouts["decision_type"], "initial_goal_plan")
        self.assertEqual(with_scouts["decision_type"], "scout_results")

    def test_ambiguity_markers_override_to_ambiguous_requirement(self):
        ambiguous = classify(
            {"kind": "scouts_done"},
            _ctx(),
            goal={"spec": "use redis or postgres, unclear which?", "complexity": 6},
        )
        plain = classify(
            {"kind": "scouts_done"},
            _ctx(),
            goal={"spec": "use redis for storage", "complexity": 6},
        )
        self.assertEqual(ambiguous["decision_type"], "ambiguous_requirement")
        self.assertIn("ambiguity:or", ambiguous["signals"])
        self.assertIn("ambiguity:unclear", ambiguous["signals"])
        self.assertIn("ambiguity:?", ambiguous["signals"])
        self.assertEqual(plain["decision_type"], "initial_goal_plan")

    def test_risk_and_missing_fields_never_raise(self):
        auth = classify({"kind": "held"}, _ctx(touches_auth=True), task={"complexity": 3})
        missing = classify({"kind": "held"}, {}, task={"complexity": 3})
        closable = classify(
            {"kind": "closable"}, _ctx(), goal={"complexity": 8}
        )
        self.assertEqual(auth["risk_class"], "high")
        self.assertTrue(any(signal.startswith("unknown:") for signal in missing["signals"]))
        self.assertEqual(closable["decision_type"], "closable_goal")
        self.assertTrue(closable["architectural"])
        self.assertEqual(len(DECISION_TYPES), 9)
        self.assertIn("type=closable_goal", explain(closable))


if __name__ == "__main__":
    unittest.main()
