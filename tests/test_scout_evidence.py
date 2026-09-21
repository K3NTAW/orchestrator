"""Acceptance coverage for scout reuse, structured findings, and telemetry."""
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


def test_fresh_duplicate_evidence_marks_scout_unnecessary():
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
    assert result["sufficient"] is True
    assert result["objective"] == "implementation-map"
    assert len(result["hits"]) == 2
    assert all(hit["fresh"] and hit["age_days"] == 0 for hit in result["hits"])
    assert all(hit["id"] in result["reason"] for hit in hits)
    assert recall.call_args.kwargs["root"] == TMP
    assert recall.call_args.kwargs["layers"] == ("notes", "bus", "graph")


def test_orthogonal_objectives_allowed_and_duplicates_flagged():
    assert scout_evidence.orthogonal(["implementation-map", "test-surface"]) == {
        "ok": True, "duplicates": [],
    }
    result = scout_evidence.orthogonal([
        "implementation-map", "test-surface", "Implementation_Map", "implementation-map",
    ])
    assert result == {"ok": False, "duplicates": ["implementation-map"]}


def test_normalize_findings_produces_structured_entries_and_flags_low_confidence():
    result = scout_evidence.normalize_findings({
        "findings": [
            {"claim": "Scout workers use the shared packet builder",
             "evidence": ["orchestrator/spawn.py:513"], "confidence": 0.5,
             "provenance": ["repo"]},
            {"claim": "Unsupported claim", "confidence": 0.9},
        ],
        "open_questions": ["Does the packet cover the complete test surface?"],
    })
    assert result == [{
        "finding": "Scout workers use the shared packet builder",
        "source": "orchestrator/spawn.py:513", "confidence": 0.5,
        "relevance": 1.0, "unresolved": True, "needs_challenge": True,
    }]
    assert scout_evidence.normalize_findings({"findings": result}) == result
    assert scout_evidence.normalize_findings(None) == []
    assert scout_evidence.normalize_findings({"findings": "malformed"}) == []


def test_stale_bus_evidence_does_not_suppress_scouting():
    hit = {"id": "T-scout", "title": "Scout packet implementation",
           "date": datetime.date.today().isoformat(), "layer": "bus", "relevant": True}
    task = {"id": hit["id"], "role": "scout", "scope": ["orchestrator/spawn.py"],
            "packet_meta": {"base": "packet-base"},
            "result": {"findings": [{"claim": "Earlier implementation"}]}}
    git = mock.Mock(side_effect=[FakeProc("merge-base\n"), FakeProc("orchestrator/spawn.py\0")])
    with mock.patch.object(scout_evidence, "memory_recall", return_value={"hits": [hit]}), \
            mock.patch.object(bus, "get", return_value=task):
        result = scout_evidence.reuse_check(
            "How are scout packets built?", root=TMP / ".orchestrator",
            min_hits=1, head_sha="current-head", git=git,
        )
    assert result["sufficient"] is False
    assert result["hits"][0]["fresh"] is False
    assert "orchestrator/spawn.py" in result["hits"][0]["reason"]
    assert git.call_args_list == [
        mock.call(TMP, "merge-base", "packet-base", "current-head"),
        mock.call(TMP, "diff", "--name-only", "-z", "merge-base", "current-head"),
    ]


def test_record_decision_logs_considered_and_skipped():
    with tempfile.TemporaryDirectory(prefix="scout-runs-") as directory:
        runs = Path(directory)
        with mock.patch.object(bus, "RUNS", runs):
            scout_evidence.record_decision(
                question="Where are scout packets built?", objective="implementation-map",
                considered=True, launched=False, skipped_reason="two fresh note hits",
                hits=[{"id": "note-routing"}, {"id": "note-tests"}],
            )
        rows = [json.loads(line) for path in runs.glob("*.jsonl")
                for line in path.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "scout_decision"
    assert row["considered"] is True
    assert row["launched"] is False
    assert row["skipped_reason"] == "two fresh note hits"
    assert row["objective"] == "implementation-map"
    assert row["n_hits"] == 2
    assert row["question"] == "Where are scout packets built?"


def load_tests(loader, tests, pattern):
    """Keep these exact pytest acceptance IDs runnable by the unittest fallback."""
    return unittest.TestSuite(unittest.FunctionTestCase(test) for name, test in globals().items()
                              if name.startswith("test_") and callable(test))
