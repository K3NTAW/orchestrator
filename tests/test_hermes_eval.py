import _harness
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from orchestrator import hermes_eval


class HermesEval(unittest.TestCase):
    def test_all_cases_pass_on_fixtures(self):
        expected = {
            "memory_current_decision_in_hot", "memory_older_decision_in_warm_search",
            "memory_historical_provenance_in_cold_search", "memory_irrelevant_record_excluded",
            "memory_compaction_preserves_knowledge", "cache_stable_prefix", "cache_dynamic_suffix",
            "cache_normalize_effective_cost", "cache_stable_tool_catalog", "worker_inspect_running",
            "worker_steer_records_before_delivery", "worker_cancel_releases_and_retains_partial",
            "worker_replacement_prior_worker", "security_external_skill_high_risk",
            "security_malicious_agents_blocked", "security_fake_authority_blocked",
            "security_secret_exfiltration_blocked", "security_harmless_document_allowed",
            "fast_path_trivial_skip_list", "fast_path_security_minimum_four",
            "fast_path_architectural_four", "fast_path_two_fix_rounds_demote"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 23, 12, tzinfo=hermes_eval.ZONE)
            result = hermes_eval.run(root, now)
            self.assertTrue(result["passed"], result)
            self.assertEqual({c["name"] for c in result["cases"]}, expected)
            self.assertEqual(json.loads((root / ".orchestrator/hermes_eval.json").read_text()), result)
            self.assertEqual(result["ran_at"], now.isoformat())
            self.assertEqual(list((root / ".orchestrator").iterdir()), [root / ".orchestrator/hermes_eval.json"])

    def test_freshness_uses_injected_now_and_failed_or_malformed_file_never_passes(self):
        now = datetime(2026, 9, 23, 12, tzinfo=hermes_eval.ZONE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".orchestrator"
            state.mkdir()
            path = state / "hermes_eval.json"
            self.assertFalse(hermes_eval.fresh(root, now=now))
            for age, passed, expected in ((0, True, True), (7, True, True), (8, True, False),
                                           (-1, True, False), (0, False, False)):
                path.write_text(json.dumps({"ran_at": (now - timedelta(days=age)).isoformat(),
                    "passed": passed, "cases": [{"name": "fixture", "passed": True, "detail": "ok"}]}))
                self.assertEqual(hermes_eval.fresh(root, now=now), expected)
                self.assertEqual(hermes_eval.fresh(state, now=now), expected)
            for malformed in ("{", "null", "[]", "{}", '{"ran_at":42,"passed":true}',
                              '{"ran_at":"2026-09-23T12:00:00","passed":true,"cases":[]}'):
                path.write_text(malformed)
                self.assertFalse(hermes_eval.fresh(root, now=now))
