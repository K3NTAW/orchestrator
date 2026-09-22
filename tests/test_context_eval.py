import _harness
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import context_eval
from orchestrator import cli


class ContextEval(unittest.TestCase):
    def result(self, category):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return context_eval.run(category, root)

    def test_localized_fix_reduces_and_keeps_scope_full(self):
        row = self.result("localized_fix")
        self.assertGreaterEqual(row["reduction"], .4)
        self.assertTrue(row["kept"])
        self.assertGreater(row["active"], 0)
        self.assertIn("active", context_eval.format_report([row]))

    def test_security_fixture_keeps_security_evidence_full(self):
        self.assertTrue(self.result("security")["kept"])

    def test_test_failure_fixture_keeps_failing_output_full(self):
        self.assertTrue(self.result("test_failure")["kept"])

    def test_documentation_fixture_has_no_full_source_chunks(self):
        self.assertTrue(self.result("documentation")["kept"])

    def test_repeated_fix_round_reuses_evidence(self):
        self.assertTrue(self.result("repeated_fix_round")["kept"])

    def test_cli_exit_code_reflects_expectations(self):
        with mock.patch.object(context_eval, "run_all", return_value=[{"within_expectation": False, "kept": True}]), \
             mock.patch("sys.argv", ["orchestrator", "context-eval", "--json"]), self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 1)

    def test_eval_writes_nothing_into_production_state(self):
        sentinel = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        from orchestrator import spawn
        with mock.patch.object(spawn, "STATE", sentinel):
            context_eval.run("localized_fix", root)
        self.assertEqual(list(sentinel.rglob("*")), [])

    def test_cli_writes_context_eval_json(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        result = {"category": "x", "within_expectation": True, "kept": True}
        with mock.patch.object(cli, "ROOT", root), mock.patch.object(context_eval, "run_all", return_value=[result]), \
             mock.patch("sys.argv", ["orchestrator", "context-eval", "--json"]):
            cli.main()
        saved = json.loads((root / ".orchestrator" / "context_eval.json").read_text())
        self.assertTrue(saved["suite_passed"])

    def test_economy_join_keys_align(self):
        context = {"by_role_task_class": {("execute", "localized_fix"): {"n": 1}}}
        handoffs = {("codex", "localized_fix"): {"n": 2}}
        reads = {"by_role": {"execute": {"n": 3}},
                 "by_task_class": {"localized_fix": {"n": 4}}}
        with mock.patch("orchestrator.context_scorecard.build", return_value=context), \
             mock.patch("orchestrator.handoff_scorecard.by_start", return_value=handoffs), \
             mock.patch("orchestrator.read_economy.summary", return_value=reads), \
             mock.patch("orchestrator.promotion.collect", return_value={"n": 0}), \
             mock.patch("sys.argv", ["orchestrator", "scorecard", "--economy", "--json"]), \
             mock.patch("builtins.print") as output:
            cli.main()
        card = json.loads(output.call_args.args[0])
        joined = card["by_role_task_class"]["execute/localized_fix"]
        self.assertEqual(joined["context_and_tools"], {"n": 1})
        self.assertEqual(joined["reads"], {"role": {"n": 3}, "task_class": {"n": 4}})
        self.assertEqual(card["handoffs_by_executor_task_class"]["codex/localized_fix"], {"n": 2})
