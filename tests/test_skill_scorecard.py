import _harness  # noqa: F401
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from orchestrator import cli, scorecard, skill_scorecard


class SkillScorecardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "runs").mkdir()
        (self.root / "tasks").mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def _rows(self, rows):
        (self.root / "runs/2026-01-01.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_use_rate_and_overhead_ratio_per_skill(self):
        self._rows([
            {"role": "execute", "input_tokens": 100, "context": {"skills_exposed": ["executor/x"], "skills_used": ["executor/x"], "skill_tokens_l0": 10, "skill_tokens_l2": 20}},
            {"role": "execute", "input_tokens": 100, "context": {"skills_exposed": ["executor/x"], "skills_used": [], "skill_tokens_l0": 10, "skill_tokens_l2": 0}},
        ])
        row = skill_scorecard.build(self.root)["by_role_skill"]["execute/executor/x"]
        self.assertEqual(row["use_rate"], .5)
        self.assertEqual(row["skill_overhead_ratio"], .2)

    def test_skill_tokens_per_accepted_task(self):
        (self.root / "tasks/T-1.json").write_text(json.dumps({"id": "T-1", "merged_into": "main"}))
        self._rows([{"task": "T-1", "lineage_root": "G-1", "role": "execute",
                     "context": {"skills_exposed": ["executor/x"], "skill_tokens_l0": 3, "skill_tokens_l2": 7}}])
        card = skill_scorecard.build(self.root)
        self.assertEqual(card["skill_tokens_per_accepted_task"]["G-1"], 10)

    def test_skill_recovery_rate_and_reduction(self):
        self._rows([
            {"role": "execute", "context": {"skills_exposed": ["executor/x", "executor/y"],
             "skills_selected": ["executor/x"], "skills_used": ["executor/y"],
             "skill_tokens_l0": 10, "skill_tokens_selected_l0": 4}},
            {"role": "execute", "context": {"skills_exposed": ["executor/x", "executor/y"],
             "skills_selected": ["executor/x"], "skills_used": ["executor/x"],
             "skill_tokens_l0": 10, "skill_tokens_selected_l0": 4}},
        ])
        row = skill_scorecard.build(self.root)["by_role"]["execute"]
        self.assertEqual(row["selected_set_size_avg"], 1)
        self.assertEqual(row["skill_reduction"], .6)
        self.assertEqual(row["skill_recovery_rate"], .5)

    def test_usage_rows_join_lineage_outcomes_and_dimensions(self):
        (self.root / "tasks/T-1.json").write_text(json.dumps({
            "id": "T-1", "role": "execute", "complexity": 5, "merged_into": "main",
            "constraints": {"task_class": "feature"},
            "pipeline": {"gate_reds": 0, "lineage_fix_rounds": 0}}))
        (self.root / "tasks/T-2.json").write_text(json.dumps({
            "id": "T-2", "role": "review", "inputs": ["T-1"],
            "result": {"verdict": "request_changes"}}))
        self._rows([
            {"task": "T-1", "role": "execute", "model": "m", "input_tokens": 10,
             "usd": 1, "duration_s": 2, "context": {"skills_used": ["s"]}},
            {"task": "T-2", "role": "review", "model": "m", "input_tokens": 5,
             "usd": .5, "duration_s": 1, "context": {"skills_used": ["r"]}},
        ])
        rows = skill_scorecard.usage_rows(self.root)
        self.assertEqual(rows[1]["lineage_root"], "T-1")
        self.assertEqual(rows[1]["accepted_tokens"], 15)
        self.assertEqual(rows[1]["review_request_changes"], 1)
        self.assertEqual((rows[1]["task_class"], rows[1]["band"], rows[1]["model"]),
                         ("feature", "4-6", "m"))

    def test_by_skill_groups_and_none_for_unknown(self):
        self._rows([{"role": "execute", "context": {"skills_used": ["s"]}}])
        row = skill_scorecard.by_skill(self.root)[0]
        self.assertEqual(row["n"], 1)
        self.assertIsNone(row["first_pass_rate"])
        self.assertIsNone(row["accepted_tokens"])

    def test_marginal_insufficient_below_min_samples(self):
        rows = [{"role": "execute", "task_class": "feature", "skills_used": ["s"],
                 "skills_selected": ["s"]}]
        with mock.patch.object(skill_scorecard, "usage_rows", return_value=rows):
            result = skill_scorecard.marginal(self.root, "s", min_samples=2)
        self.assertEqual(result[0]["n_with"], 1)
        self.assertTrue(result[0]["insufficient"])

    def test_marginal_verdicts_valuable_costly_harmful(self):
        def outcome(role, used, first_pass, tokens):
            return {"role": role, "task_class": "feature", "skills_used": used,
                    "skills_selected": used, "first_pass": first_pass, "fix_rounds": 0,
                    "accepted_tokens": tokens, "accepted_usd": tokens / 100,
                    "latency_s": tokens, "skill_tokens_l2": 3}
        rows = []
        for role, quality, cost in (("valuable", 1, 90), ("costly", 1, 110), ("harmful", 0, 90)):
            rows += [outcome(role, ["s"], quality, cost), outcome(role, [], 1, 100)]
        with mock.patch.object(skill_scorecard, "usage_rows", return_value=rows):
            result = skill_scorecard.marginal(self.root, "s", group_by=("role",), min_samples=1)
        self.assertEqual({row["role"]: row["verdict"] for row in result},
                         {"valuable": "valuable", "costly": "costly", "harmful": "harmful"})

    def test_redundancy_pairs_and_script_overlap(self):
        self._rows([
            {"task": "T-a", "context": {"skills_used": ["a"]}},
            {"task": "T-b", "context": {"skills_used": ["b"]}},
            {"task": "T-ab", "context": {"skills_used": ["a", "b"]}},
        ])
        gate = self.root / "runs/jev"
        gate.mkdir()
        (gate / "gate.jsonl").write_text("".join(json.dumps(row) + "\n" for row in [
            {"task": "T-a", "tool": "read", "tool_target": "one"},
            {"task": "T-a", "tool": "read", "tool_target": "shared"},
            {"task": "T-b", "tool": "read", "tool_target": "shared"},
        ]))
        registry = self.root / "skills"
        registry.mkdir()
        (registry / "registry.json").write_text(json.dumps({"skills": {
            "a": {"tools": ["script:same.py"]}, "b": {"tools": ["script:same.py"]}}}))
        row = skill_scorecard.redundancy(self.root)[0]
        self.assertEqual((row["n"], row["overlap"]), (1, .5))
        self.assertEqual(row["duplicated_script_invocations"], ["script:same.py"])

    def test_redundancy_reads_real_gate_row_tool_names(self):
        self._rows([
            {"task": "T-a", "context": {"skills_used": ["a"]}},
            {"task": "T-b", "context": {"skills_used": ["b"]}},
            {"task": "T-ab", "context": {"skills_used": ["a", "b"]}},
        ])
        gate = self.root / "runs/jev"
        gate.mkdir()
        for tool in ("Read", "Grep", "Glob"):
            with self.subTest(tool=tool):
                rows = [
                    {"task": "T-a", "session": "a", "tool": tool, "tool_target": "src/shared.py"},
                    {"task": "T-a", "session": "a", "tool": tool, "tool_target": "src/only_a.py"},
                    {"task": "T-b", "session": "b", "tool": tool, "tool_target": "src/shared.py"},
                    {"task": "T-ab", "session": "ab", "tool": "Read", "tool_target": "src/co_used.py"},
                    {"task": "T-a", "session": "a", "tool": "Read", "tool_target": ""},
                ]
                rows.extend({"task": "T-b", "session": "b", "tool": excluded,
                             "tool_target": "src/only_a.py"}
                            for excluded in ("Edit", "Write", "Bash"))
                (gate / "gate.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows))
                pair = skill_scorecard.redundancy(self.root)[0]
                self.assertEqual(pair["n"], 1)
                self.assertEqual(pair["overlap_proxy"], .5)
                self.assertNotEqual(pair["note"], "no single-skill runs")

    def test_cli_by_model_and_strategy(self):
        with mock.patch.object(scorecard, "STATE", self.root), \
             mock.patch.object(skill_scorecard, "by_skill", return_value=[]) as grouped, \
             mock.patch("sys.argv", ["orchestrator", "scorecard", "--skills", "--group-by", "model,strategy", "--json"]), \
             redirect_stdout(StringIO()):
            cli.main()
        grouped.assert_called_once_with(self.root, ("model", "strategy"))

    def test_cli_rejects_skill_flags_without_skills_mode(self):
        for flag in (["--group-by", "model"], ["--marginal", "s"], ["--redundancy"]):
            with self.subTest(flag=flag), mock.patch("sys.argv", ["orchestrator", "scorecard", *flag]), \
                 redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                cli.main()

    def test_selection_rows_by_role_and_window(self):
        rows = [{"kind": "skill_selection", "role": "execute", "mode": "shadow"},
                {"kind": "skill_selection", "role": "review", "mode": "shadow"}]
        with mock.patch.object(skill_scorecard.decision_log, "read_all", return_value=rows):
            self.assertEqual(skill_scorecard.selection_rows(self.root, "execute", 60), 1)

    def test_selection_rows_read_top_level_role_with_fallback(self):
        log = skill_scorecard.decision_log
        with mock.patch.object(log.time, "time", return_value=1000):
            for fields in (
                {"role": "execute", "deterministic": {"role": "review"}},
                {"deterministic": {"role": "execute"}},
                {"extra": {"role": "execute"}},
                {"role": "review", "extra": {"role": "execute"}},
                {},
            ):
                log.record("skill_selection", "T-role", root=self.root,
                           candidates=[], hard_constraints=[], selected=[], reason="test",
                           mode="shadow", **({"deterministic": {}} | fields))
        with mock.patch.object(log.time, "time", return_value=1010):
            self.assertEqual(skill_scorecard.selection_rows(self.root, "execute", 60), 3)
            self.assertEqual(skill_scorecard.selection_rows(self.root, "review", 60), 1)
            self.assertEqual(skill_scorecard.selection_rows(self.root, "execute", 5), 0)

    def test_recovery_rate_windowed(self):
        rows = [{"kind": "skill_selection", "subject": "T-1", "role": "execute", "mode": "active"},
                {"kind": "skill_selection", "subject": "T-2", "role": "execute", "mode": "shadow"},
                {"kind": "outcome", "decision_kind": "skill_selection", "subject": "T-1",
                 "skill_recovery": ["executor/x"]}]
        with mock.patch.object(skill_scorecard.decision_log, "read_all", return_value=rows):
            self.assertEqual(skill_scorecard.recovery_rate(self.root, "execute", 60), .5)

    def test_presented_tokens_and_recovery_from_active_rows(self):
        self._rows([{"role": "execute", "input_tokens": 100, "context": {
            "skills_exposed": ["executor/x"], "skills_selected": ["executor/x"],
            "skills_used": ["executor/y"], "skill_tokens_l0": 4,
            "skill_tokens_presented_l2": 12}}])
        card = skill_scorecard.build(self.root)
        self.assertEqual(card["by_role_skill"]["execute/executor/x"]["skill_tokens_l2"], 12)
        self.assertEqual(card["by_role"]["execute"]["skill_recovery_rate"], 1)


if __name__ == "__main__":
    unittest.main()
