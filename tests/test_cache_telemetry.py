import _harness
import json, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from orchestrator import cache_telemetry


class CacheTelemetry(unittest.TestCase):
    def test_normalize_claude_and_codex_usage(self):
        claude = cache_telemetry.normalize("claude", {"input_tokens": 10, "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10, "output_tokens": 2})
        codex = cache_telemetry.normalize("codex", {"input_tokens": 40, "cached_input_tokens": 30,
            "output_tokens": 2})
        self.assertEqual([claude[k] for k in ("input_uncached_tokens", "cache_read_tokens",
            "cache_write_tokens", "output_tokens")], [10, 30, 10, 2])
        self.assertEqual([codex[k] for k in ("input_uncached_tokens", "cache_read_tokens",
            "cache_write_tokens", "output_tokens")], [10, 30, 0, 2])
        self.assertTrue(0 <= claude["hit_ratio"] <= 1)
        self.assertEqual(cache_telemetry.normalize("claude", {})["hit_ratio"], 0.0)

    def test_effective_cost_uses_pool_ratios(self):
        norm = {"provider": "claude", "input_uncached_tokens": 10, "cache_read_tokens": 20,
                "cache_write_tokens": 4, "output_tokens": 2}
        self.assertEqual(cache_telemetry.effective_cost(norm, {}), 27)
        cfg = {"cache": {"claude_read_ratio": .5, "claude_write_ratio": 2, "output_ratio": 3}}
        self.assertEqual(cache_telemetry.effective_cost(norm, cfg), 34)

    def test_report_groups_by_role_provider_and_prefix_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "runs").mkdir()
            rows = [
                {"ts": time.time(), "role": "execute", "provider": "codex", "goal_id": "G",
                 "input_uncached_tokens": 10, "cache_read_tokens": 10, "cache_write_tokens": 0,
                 "output_tokens": 1, "prefix_sha": "same"},
                {"ts": time.time(), "role": "execute", "provider": "codex", "goal_id": "G",
                 "input_uncached_tokens": 5, "cache_read_tokens": 15, "cache_write_tokens": 0,
                 "output_tokens": 1, "prefix_sha": "same"},
                {"ts": time.time(), "role": "review", "provider": "claude", "goal_id": "G",
                 "input_uncached_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 0,
                 "output_tokens": 1},
            ]
            (root / "runs" / "today.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            card = cache_telemetry.report(root)
        self.assertEqual(card["by_role"]["execute"]["runs"], 2)
        self.assertEqual(card["by_provider"]["codex"]["reuse_rate"], .5)
        self.assertEqual(card["by_goal"]["G"]["unknown_prefix_runs"], 1)

    def test_annotate_reuses_existing_row_fields_and_adds_only_ratio_cost_prefix(self):
        row = {"provider": "claude", "input_uncached_tokens": 10, "cache_read_tokens": 10,
               "cache_write_tokens": 0, "output_tokens": 1,
               "packet_meta": {"prefix_sha": "abc"}}
        before = dict(row)
        cache_telemetry.annotate(row, {})
        self.assertEqual({key: row[key] for key in before}, before)
        self.assertEqual(set(row) - set(before), {"hit_ratio", "effective_tokens", "prefix_sha"})
        self.assertEqual(row["prefix_sha"], "abc")


if __name__ == "__main__":
    unittest.main()
