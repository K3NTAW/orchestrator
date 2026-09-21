"""Scout evidence acceptance tests, discovered by unittest."""
import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import FakeProc, TMP
from orchestrator import bus, scout_evidence


class ScoutEvidenceTests(unittest.TestCase):
    def test_fresh_duplicate_evidence_marks_scout_unnecessary(self):
        today = datetime.date.today().isoformat()
        hits = [
            {"id": "note-routing", "title": "Scout routing implementation",
             "date": today, "layer": "notes", "relevant": True},
            {"id": "note-tests", "title": "Scout routing test surface",
             "date": today, "layer": "notes", "relevant": True},
        ]
        with mock.patch.object(scout_evidence, "memory_recall", return_value={"hits": hits}) as recall:
            result = scout_evidence.reuse_check(
                "Where is scout routing implemented and tested?",
                objective="Implementation_Map", root=TMP / ".orchestrator",
            )
        self.assertIs(result["sufficient"], True)
        self.assertEqual(result["objective"], "implementation-map")
        self.assertEqual(len(result["hits"]), 2)
        for hit in result["hits"]:
            self.assertIs(hit["fresh"], True)
            self.assertEqual(hit["age_days"], 0)
            self.assertIn(hit["id"], result["reason"])
        self.assertEqual(recall.call_args.kwargs["root"], TMP)
        self.assertEqual(recall.call_args.kwargs["layers"], ("notes", "bus", "graph"))

    def test_orthogonal_objectives_allowed_and_duplicates_flagged(self):
        self.assertEqual(scout_evidence.orthogonal(["implementation-map", "test-surface"]), {
            "ok": True, "duplicates": [],
        })
        result = scout_evidence.orthogonal([
            "implementation-map", "test-surface", "Implementation_Map", "implementation-map",
        ])
        self.assertEqual(result, {"ok": False, "duplicates": ["implementation-map"]})

    def test_normalize_findings_produces_structured_entries_and_flags_low_confidence(self):
        result = scout_evidence.normalize_findings({
            "findings": [
                {"claim": "Scout workers use the shared packet builder",
                 "evidence": ["orchestrator/spawn.py:513"], "confidence": 0.5,
                 "provenance": ["repo"]},
                {"claim": "Unsupported claim", "confidence": 0.9},
            ],
            "open_questions": ["Does the packet cover the complete test surface?"],
        })
        self.assertEqual(result, [{
            "finding": "Scout workers use the shared packet builder",
            "source": "orchestrator/spawn.py:513", "confidence": 0.5,
            "relevance": 1.0, "unresolved": True, "needs_challenge": True,
        }])
        self.assertEqual(scout_evidence.normalize_findings({"findings": result}), result)
        self.assertEqual(scout_evidence.normalize_findings(None), [])
        self.assertEqual(scout_evidence.normalize_findings({"findings": "malformed"}), [])

    def test_stale_bus_evidence_does_not_suppress_scouting(self):
        hit = {"id": "T-scout", "title": "Scout packet implementation",
               "date": datetime.date.today().isoformat(), "layer": "bus", "relevant": True}
        task = {"id": hit["id"], "role": "scout", "scope": ["orchestrator/spawn.py"],
                "packet_meta": {"base": "packet-base"},
                "result": {"findings": [{"claim": "Earlier implementation"}]}}
        with mock.patch.object(scout_evidence, "memory_recall", return_value={"hits": [hit]}), \
                mock.patch.object(bus, "get", return_value=task), \
                mock.patch.object(scout_evidence.gitutil, "_git_in", side_effect=[
                    FakeProc("merge-base\n"), FakeProc("orchestrator/spawn.py\0"),
                ]) as git:
            result = scout_evidence.reuse_check(
                "How are scout packets built?", root=TMP / ".orchestrator",
                min_hits=1, head_sha="current-head", git=git,
            )
        self.assertIs(result["sufficient"], False)
        self.assertIs(result["hits"][0]["fresh"], False)
        self.assertIn("orchestrator/spawn.py", result["hits"][0]["reason"])
        self.assertEqual(git.call_args_list, [
            mock.call(TMP, "merge-base", "packet-base", "current-head"),
            mock.call(TMP, "diff", "--name-only", "-z", "merge-base", "current-head"),
        ])

    def test_record_decision_logs_considered_and_skipped(self):
        with tempfile.TemporaryDirectory(prefix="scout-runs-") as directory:
            runs = Path(directory)
            with mock.patch.object(bus, "RUNS", runs), \
                    mock.patch.object(bus, "log_run", wraps=bus.log_run) as log_run:
                scout_evidence.record_decision(
                    question="Where are scout packets built?", objective="implementation-map",
                    considered=True, launched=False, skipped_reason="two fresh note hits",
                    hits=[{"id": "note-routing"}, {"id": "note-tests"}],
                )
            log_run.assert_called_once_with(
                task=None, role="scout_decision", outcome="recorded",
                question="Where are scout packets built?", objective="implementation-map",
                considered=True, launched=False, skipped_reason="two fresh note hits", n_hits=2,
            )
            rows = [json.loads(line) for path in runs.glob("*.jsonl")
                    for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["role"], "scout_decision")
        self.assertIs(row["considered"], True)
        self.assertIs(row["launched"], False)
        self.assertEqual(row["skipped_reason"], "two fresh note hits")
        self.assertEqual(row["objective"], "implementation-map")
        self.assertEqual(row["n_hits"], 2)
        self.assertEqual(row["question"], "Where are scout packets built?")
