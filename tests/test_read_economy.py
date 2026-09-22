import _harness  # noqa: F401 - share the suite's single isolated ORCH_ROOT
import json
import os
import tempfile
import unittest
from pathlib import Path

from orchestrator import evidence, read_economy


class ReadEconomyTests(unittest.TestCase):
    def test_repeated_read_unchanged_detected_by_mtime_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text("x" * 400)
            call = {"name": "Read", "input": {"file_path": str(path)}}
            first = read_economy.classify(call, [])
            old = {**call, "mtime": path.stat().st_mtime_ns, "size": path.stat().st_size}
            repeated = read_economy.classify(call, [old])
            self.assertEqual(first["kind"], "first_read")
            self.assertEqual(repeated, {"kind": "repeated_read_unchanged",
                                        "tokens_estimate": 100, "would_suppress": True})
            stat = path.stat()
            path.write_text("y" * 400)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            self.assertEqual(read_economy.classify(call, [old])["kind"], "repeated_read_changed")

    def test_repeated_search_and_narrower_search(self):
        old = {"name": "Grep", "input": {"path": "src", "pattern": "needle", "-i": True},
               "result_size": 23}
        exact = read_economy.classify(dict(old), [old])
        narrower = read_economy.classify(
            {"name": "Grep", "input": {"path": "src", "pattern": "needle.+thread", "-i": True}}, [old])
        self.assertEqual((exact["kind"], exact["tokens_estimate"], exact["would_suppress"]),
                         ("repeated_search", 23, True))
        self.assertEqual(narrower["kind"], "narrower_search")
        self.assertFalse(narrower["would_suppress"])

    def test_evidence_available_from_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            original = evidence.STATE
            evidence.STATE = Path(directory)
            try:
                pool = evidence.EvidencePool("G-1")
                pool.add(evidence.make("source_chunk", "src/a.py:1-4", "data", commit="abc",
                                       provenance="repo"))
                result = read_economy.classify(
                    {"name": "Read", "input": {"file_path": "src/a.py"}}, [],
                    evidence=pool, head_sha="abc")
                stale = read_economy.classify(
                    {"name": "Read", "input": {"file_path": "src/a.py"}}, [],
                    evidence=pool, head_sha="def")
            finally:
                evidence.STATE = original
        self.assertEqual(result["kind"], "evidence_available")
        self.assertEqual(stale["kind"], "first_read")

    def test_summary_counts_tokens_avoidable_and_false_suppression_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs/jev").mkdir(parents=True)
            (root / "tasks").mkdir()
            (root / "tasks/T-1.json").write_text(json.dumps(
                {"id": "T-1", "role": "execute", "constraints": {"task_class": "feature"}}))
            rows = [
                {"task": "T-1", "session": "s", "tool": "Read", "tool_target": "a.py",
                 "read_kind": "first_read", "sampled": False},
                {"task": "T-1", "session": "s", "tool": "Read", "tool_target": "a.py",
                 "read_kind": "repeated_read_unchanged", "would_suppress": True,
                 "tokens_estimate": 50},
                {"task": "T-1", "session": "s", "tool": "Edit", "tool_target": "a.py"},
                {"task": "T-1", "session": "s", "tool": "Grep", "tool_target": "src",
                 "read_kind": "repeated_search", "would_suppress": True, "tokens_estimate": 20},
            ]
            (root / "runs/jev/gate.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = read_economy.summary(root)["by_role"]["execute"]
        self.assertEqual(result["reads"], 2)
        self.assertEqual(result["tokens_avoidable"], 70)
        self.assertEqual(result["jev_calls_avoided"], 1)
        self.assertEqual(result["false_suppression_proxy"], 1)


if __name__ == "__main__":
    unittest.main()
