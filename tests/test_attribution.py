import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import TMP
from orchestrator import attribution, bus, scorecard


class AttributionTests(unittest.TestCase):
    def test_review_facts_backfills_packet_version_from_multiline_runs_file(self):
        rows = [
            {"task": "other-first", "packet_meta": {"version": "wrong-first"}},
            {"task": "historical-review", "packet_meta": {"version": "review-packet-hash"}},
            {"task": "other-last", "packet_meta": {"version": "wrong-last"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            (runs / "2026-09-20.jsonl").write_text(
                json.dumps(rows[0]) + "\n\n{malformed\n" +
                "\n".join(json.dumps(row) for row in rows[1:]) + "\n")
            with patch.object(bus, "RUNS", runs):
                facts = attribution.review_facts({"id": "historical-review", "role": "review"})
        self.assertEqual(facts["packet_version"], "review-packet-hash")

    def test_review_facts_normalises_severities_and_counts(self):
        task = {"id": "R", "role": "review", "complexity": 7,
                "constraints": {"reviewer_role": "security", "reviewed_sha": "abc"},
                "result": {"verdict": "request_changes", "comments": [
                    {"severity": "critical"}, {"severity": "medium"}, {"severity": "nit"},
                    {"severity": "unexpected"}]}}
        facts = attribution.review_facts(task)
        self.assertEqual(facts["findings_count"], 4)
        self.assertEqual(facts["findings_by_severity"], {"high": 1, "med": 1, "low": 1, "other": 1})
        self.assertTrue(facts["checklist_used"])

    def test_review_facts_handles_missing_fields_on_old_reviews(self):
        facts = attribution.review_facts({"id": "old", "role": "review"})
        self.assertEqual(facts["verdict"], None)
        self.assertEqual(facts["findings_count"], 0)
        self.assertEqual(facts["reviewer_role"], "general")
        self.assertIsNone(facts["packet_version"])

    def test_review_facts_pass_index_orders_sibling_reviews(self):
        reviewed = bus.create_task("attribution target", "s", ["a"], ["x.py"], role="execute")
        bus.update(reviewed["id"], pipeline={"reviews_expected": 2})
        first = bus.create_task("first review", "s", ["a"], ["x.py"], role="review", inputs=[reviewed["id"]])
        second = bus.create_task("second review", "s", ["a"], ["x.py"], role="review", inputs=[reviewed["id"]])
        self.assertEqual(attribution.review_facts(first)["review_pass_index"], 1)
        self.assertEqual(attribution.review_facts(second)["review_pass_index"], 2)
        self.assertEqual(attribution.review_facts(second)["reviews_expected"], 2)

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
