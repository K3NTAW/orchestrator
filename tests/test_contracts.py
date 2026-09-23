import _harness
import copy
import unittest
from unittest import mock
from orchestrator import contracts


FIXTURES = {
    "review": {"verdict": "approve", "comments": [{"path": "a.py", "line": 0,
                  "issue": "example", "severity": "med"}]},
    "spec_review": {"verdict": "request_changes", "risks": [], "suggested_spec_changes": []},
    "challenge": {"verdict": "uncertain", "evidence": ["a.py:1"], "note": "insufficient evidence"},
    "scout": {"findings": [{"claim": "example", "evidence": ["a.py:1"], "confidence": .5,
                            "provenance": ["repo"]}], "open_questions": [], "suggested_next": [], "blocked": None},
    "execute": {"summary": "done", "commit": "", "executed_by": "codex:example",
                "usage": None, "thread": None, "rounds": 1},
}


class Contracts(unittest.TestCase):
    def test_schemas_accept_valid_results_for_every_role(self):
        for role, result in FIXTURES.items():
            with self.subTest(role=role):
                self.assertTrue(contracts.validate(role, result)["ok"])

    def test_schemas_match_prompt_shapes(self):
        self.assertEqual(set(FIXTURES), set(contracts.SCHEMAS))
        for role, result in FIXTURES.items():
            checked = contracts.validate(role, result)
            self.assertEqual(checked["errors"], [])
            self.assertEqual(checked["repaired"], {**result, "confidence": 0.0, "provenance": ["repo"]})

    def test_deterministic_repair_aliases_and_coercions(self):
        cases = [("review", {**FIXTURES["review"], "verdict": "LGTM", "confidence": "0.7", "provenance": "repo"}),
                 ("challenge", {**FIXTURES["challenge"], "verdict": "disagree", "evidence": "a.py:1"}),
                 ("execute", {**FIXTURES["execute"], "tests_run": "unittest", "files_changed": "a.py"})]
        for role, value in cases:
            before = copy.deepcopy(value)
            repaired = contracts.repair(role, value)
            self.assertTrue(contracts.validate(role, value)["ok"])
            self.assertEqual(contracts.repair(role, repaired), repaired)
            self.assertEqual(value, before)
        self.assertEqual(contracts.repair(*cases[0])["verdict"], "approve")
        self.assertEqual(contracts.repair(*cases[1])["verdict"], "refuted")
        legacy = {"verdict": "approve", "findings": []}
        self.assertFalse(contracts.validate("review", legacy)["ok"])
        self.assertIn("findings", contracts.repair("review", legacy))

    def test_invalid_result_lists_errors(self):
        invalid = {"verdict": "unknown", "comments": [{"line": "one"}], "confidence": "nan"}
        checked = contracts.validate("review", invalid)
        self.assertFalse(checked["ok"])
        self.assertIsNone(checked["repaired"])
        self.assertGreater(len(checked["errors"]), 3)
        self.assertFalse(contracts.validate("review", ["invalid"])["ok"])
        self.assertFalse(contracts.validate("missing", {})["ok"])

    def test_recovery_is_bounded_and_preserves_invalid_original(self):
        original = {"summary": "raw"}
        adapter = mock.Mock(return_value={"result": {}, "tokens": {"output_tokens": 3}})
        out = contracts.recover("review", original, session=adapter, budget_usd=.2)
        self.assertIs(out["result"], original)
        self.assertEqual(out["tokens"], {"output_tokens": 3})
        adapter.assert_called_once()
        adapter.reset_mock()
        contracts.recover("review", original, session=adapter, budget_usd=0)
        adapter.assert_not_called()
        adapter.return_value = {"result": FIXTURES["review"], "usd": 1}
        self.assertIs(contracts.recover("review", original, session=adapter, budget_usd=.2)["result"], original)
