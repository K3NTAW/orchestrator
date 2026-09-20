import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import TMP
from orchestrator import attribution, bus, scorecard


class AttributionTests(unittest.TestCase):
    def test_bucket_of_every_role(self):
        expected = {"planner_decision": "planner", "planner": "planner", "scout": "scout",
                    "triage": "scout", "execute": "execute", "spec_review": "spec_review",
                    "review": "review", "challenge": "challenge", "jev": "jev", "memory": "memory",
                    "unknown": "other", None: "other"}
        for role, bucket in expected.items():
            self.assertEqual(attribution.bucket_of(role, {}), bucket)

    def test_fix_round_bucket_and_lineage_root_follow_chain(self):
        root = {"id": "T-1"}
        first = {"id": "T-2", "constraints": {"fix_round_for": "T-1"}}
        second = {"id": "T-3", "constraints": {"fix_round_for": "T-2"}}
        with patch.object(bus, "get", side_effect={"T-1": root, "T-2": first}.__getitem__):
            self.assertEqual(attribution.lineage(second), {"root": "T-1", "round_index": 2})
            self.assertEqual(attribution.lineage(root), {"root": "T-1", "round_index": 0})
        self.assertEqual(attribution.bucket_of("execute", second), "fix_round")
        self.assertEqual(attribution.bucket_of("execute", {"constraints": {"auto_round": True}}), "fix_round")
        self.assertEqual(attribution.bucket_of("review", second), "review")

    def test_band_matches_scorecard(self):
        for value, expected in [(None, None), (1, "1-3"), (3, "1-3"), (4, "4-6"), (6, "4-6"), (7, "7-10"), (10, "7-10")]:
            self.assertEqual(attribution.band(value), expected)
            self.assertEqual(attribution.band(value), scorecard._band(value))

    def test_model_of_codex_and_claude(self):
        cfg = {"executors": [{"id": "astra", "model": "gpt-6-astra"}], "models": {"sonnet": "claude-sonnet"}}
        self.assertEqual(attribution.model_of("astra", "astra", cfg), "gpt-6-astra")
        self.assertEqual(attribution.model_of("claude:sonnet", "sonnet", cfg), "claude-sonnet")
        self.assertIsNone(attribution.model_of("unknown", "unknown", cfg))
