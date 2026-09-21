import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import decision_log


def _record(root, **overrides):
    fields = {
        "candidates": ["fast", "careful", "local"],
        "hard_constraints": {"available": True},
        "deterministic": {"task_class": "mechanical"},
        "selected": "fast",
        "reason": "Lowest latency among eligible executors",
    }
    fields.update(overrides)
    return decision_log.record("routing", "T-0570", root=root, **fields)


def test_record_writes_structured_row_with_required_fields(tmp_path):
    row = _record(tmp_path)

    persisted = json.loads((tmp_path / "runs/sched/decisions.jsonl").read_text())
    assert persisted == row
    assert {
        "ts", "kind", "subject", "candidates", "hard_constraints",
        "deterministic", "selected", "rejected", "reason",
    } <= row.keys()
    assert isinstance(row["ts"], (int, float))
    assert row["kind"] == "routing"
    assert row["subject"] == "T-0570"
    assert row["rejected"] == ["careful", "local"]
    assert "chain_of_thought" not in row
    assert "chain-of-thought" not in row
    assert decision_log.read_all(root=tmp_path) == [row]


def test_explain_pairs_decisions_with_later_outcomes(tmp_path):
    decision_log.outcome("T-0570", "routing", root=tmp_path, merged=False)
    decision = _record(tmp_path)
    result = decision_log.outcome(
        "T-0570", "routing", root=tmp_path,
        merged=True, conflict=False, fix_rounds=0, actual_duration_s=12, first_pass=True,
    )
    decision_log.outcome("T-other", "routing", root=tmp_path, merged=True)
    decision_log.outcome("T-0570", "wave", root=tmp_path, merged=True)

    rows = decision_log.explain("T-0570", root=tmp_path)
    assert rows == [{**decision, "outcomes": [result]}]
    assert decision_log.explain("T-0570", kinds=["wave"], root=tmp_path) == []


def test_invalid_kind_is_rejected(tmp_path):
    with unittest.TestCase().assertRaisesRegex(ValueError, "invalid decision kind"):
        decision_log.record(
            "guess", "T-0570", candidates=["fast"], hard_constraints={},
            deterministic={}, selected="fast", reason="guessing", root=tmp_path,
        )

    assert not (tmp_path / "runs/sched/decisions.jsonl").exists()
    assert decision_log.read_all(root=tmp_path) == []


def test_malformed_lines_are_tolerated_and_counted(tmp_path):
    first = _record(tmp_path)
    with (tmp_path / "runs/sched/decisions.jsonl").open("a") as stream:
        stream.write('\nnot json\n[]\n{"partial":\n')
    second = _record(tmp_path, selected="careful", reason="Higher review complexity")

    rows = decision_log.explain("T-0570", root=tmp_path)
    assert rows == [{**second, "outcomes": []}, {**first, "outcomes": []}]
    assert rows.malformed_count == 3


def test_format_explain_is_readable(tmp_path):
    _record(
        tmp_path, rejected={"careful": "Higher latency", "local": "Unavailable"},
        confidence=0.9, n=10,
    )
    decision_log.outcome("T-0570", "routing", root=tmp_path, merged=True)

    lines = decision_log.format_explain(
        decision_log.explain("T-0570", root=tmp_path)
    ).splitlines()
    assert "routing" in lines[0]
    assert "selected: fast" in lines
    assert "reason: Lowest latency among eligible executors" in lines
    assert "rejected: careful: Higher latency, local: Unavailable" in lines
    assert "confidence/n: 0.9/10" in lines
    assert "outcome: merged=True" in lines


def load_tests(loader, tests, pattern):
    """Expose the named acceptance functions to the standard-library runner."""
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            def run(function=function):
                with tempfile.TemporaryDirectory() as directory:
                    function(Path(directory))

            suite.addTest(unittest.FunctionTestCase(run, description=name))
    return suite
