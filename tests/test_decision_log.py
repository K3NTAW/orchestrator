import _harness  # noqa: F401 - share the suite's single isolated ORCH_ROOT
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator import decision_log


class DecisionLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _record(self, **overrides):
        fields = {
            "candidates": ["fast", "careful", "local"],
            "hard_constraints": {"available": True},
            "deterministic": {"task_class": "mechanical"},
            "selected": "fast",
            "reason": "Lowest latency among eligible executors",
        }
        fields.update(overrides)
        return decision_log.record("routing", "T-0570", root=self.tmp, **fields)

    def test_record_writes_structured_row_with_required_fields(self):
        row = self._record()

        persisted = json.loads((self.tmp / "runs/sched/decisions.jsonl").read_text())
        self.assertEqual(persisted, row)
        self.assertTrue({
            "ts", "kind", "subject", "candidates", "hard_constraints",
            "deterministic", "selected", "rejected", "reason",
        } <= row.keys())
        self.assertIsInstance(row["ts"], (int, float))
        self.assertEqual(row["kind"], "routing")
        self.assertEqual(row["subject"], "T-0570")
        self.assertEqual(row["rejected"], ["careful", "local"])
        self.assertNotIn("chain_of_thought", row)
        self.assertNotIn("chain-of-thought", row)
        self.assertEqual(decision_log.read_all(root=self.tmp), [row])

    def test_record_accepts_top_level_role(self):
        row = self._record(role="execute", deterministic={"role": "review"})
        self.assertEqual(row["role"], "execute")
        self.assertEqual(decision_log.read_all(root=self.tmp)[0]["role"], "execute")
        self.assertNotIn("role", self._record())

    def test_skill_selection_call_sites_persist_top_level_role(self):
        from orchestrator import executor, spawn

        task = {"id": "T-role", "role": "execute", "status": "queued",
                "complexity": 1, "constraints": {}, "worktree": str(self.tmp)}
        cfg = {"skills": {"mode": "shadow"}}
        with patch.object(decision_log.schedlog, "SCHED_DIR", self.tmp / "runs/sched"), \
                patch.object(spawn.skills_registry, "load", return_value={"skills": {}}):
            spawn._skill_exposure(task, "execute")
            spawn._skill_routing(task, "review", cfg, {})
            executor._route_skills(task, cfg, {})
            pool = Mock()
            pool.cfg = {"skills": {"mode": "off"}}
            pool.executors = {"test": Mock(id="test", provider="codex", model="test", day_tasks=0)}
            pool.codex.day_tasks = 0
            with patch.object(executor, "Pool", return_value=pool), \
                    patch.object(executor.bus, "get", return_value=task), \
                    patch.object(executor.bus, "update"), patch.object(executor.bus, "claim"), \
                    patch.object(executor, "_run", return_value={"status": "done"}):
                self.assertEqual(executor.start("T-role", "prompt", executor_id="test")["status"], "done")
        rows = decision_log.read_all(root=self.tmp)
        self.assertEqual([row["kind"] for row in rows], ["skill_selection"] * 4)
        self.assertEqual([row["role"] for row in rows],
                         ["execute", "review", "codex_execute", "codex_execute"])

    def test_explain_pairs_decisions_with_later_outcomes(self):
        decision_log.outcome("T-0570", "routing", root=self.tmp, merged=False)
        decision_a = self._record()
        decision_b = self._record(selected="careful", reason="Higher review complexity")
        result = decision_log.outcome(
            "T-0570", "routing", root=self.tmp,
            merged=True, conflict=False, fix_rounds=0, actual_duration_s=12, first_pass=True,
        )
        decision_log.outcome("T-other", "routing", root=self.tmp, merged=True)
        decision_log.outcome("T-0570", "wave", root=self.tmp, merged=True)

        rows = decision_log.explain("T-0570", root=self.tmp)
        self.assertEqual(rows, [
            {**decision_b, "outcomes": [result]},
            {**decision_a, "outcomes": []},
        ])
        self.assertEqual(decision_log.explain("T-0570", kinds=["wave"], root=self.tmp), [])

        # Timestamp order, rather than file order, determines the predecessor.
        decision_a = {**decision_a, "ts": 10}
        decision_b = {**decision_b, "ts": 30}
        result = {**result, "ts": 20}
        with patch.object(decision_log, "_read", return_value=(
            [decision_b, result, decision_a], 0,
        )):
            self.assertEqual(decision_log.explain("T-0570", root=self.tmp), [
                {**decision_b, "outcomes": []},
                {**decision_a, "outcomes": [result]},
            ])

    def test_invalid_kind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid decision kind"):
            decision_log.record(
                "guess", "T-0570", candidates=["fast"], hard_constraints={},
                deterministic={}, selected="fast", reason="guessing", root=self.tmp,
            )

        self.assertFalse((self.tmp / "runs/sched/decisions.jsonl").exists())
        self.assertEqual(decision_log.read_all(root=self.tmp), [])

    def test_context_program_kinds_record(self):
        legacy_kinds = set(decision_log.KINDS) - set(decision_log.CONTEXT_KINDS)
        self.assertTrue({"routing", "wave", "jev_sched", "strategy", "planner_route"} <= legacy_kinds)
        for kind in decision_log.CONTEXT_KINDS:
            with self.subTest(kind=kind):
                row = decision_log.record(
                    kind, "T-context", candidates=["candidate"], hard_constraints={},
                    deterministic={}, selected="candidate", reason="context decision",
                    root=self.tmp,
                )
                self.assertEqual(row["kind"], kind)
        for kind in legacy_kinds:
            with self.subTest(legacy_kind=kind):
                self.assertIn(kind, decision_log.KINDS)
        with self.assertRaisesRegex(ValueError, "invalid decision kind"):
            decision_log.record(
                "unknown_context_kind", "T-context", candidates=[], hard_constraints={},
                deterministic={}, selected=None, reason="invalid", root=self.tmp,
            )

    def test_skill_selection_kind_records_and_outcome(self):
        decision = decision_log.record(
            "skill_selection", "T-skill", candidates=["executor/implement-spec"],
            hard_constraints=["static exposure (stage 1)"],
            deterministic={"role": "codex_execute", "task_class": "feature",
                           "exposed": ["executor/implement-spec"], "skill_tokens_l0": 5},
            selected=["executor/implement-spec"], reason="stage1 static", mode="shadow", root=self.tmp)
        outcome = decision_log.outcome("T-skill", "skill_selection", root=self.tmp,
                                       skills_used=["executor/implement-spec"], skill_tokens_l2=30)
        self.assertIn("skill_selection", decision_log.CONTEXT_KINDS)
        self.assertEqual(decision_log.explain("T-skill", root=self.tmp),
                         [{**decision, "outcomes": [outcome]}])

    def test_malformed_lines_are_tolerated_and_counted(self):
        first = self._record()
        with (self.tmp / "runs/sched/decisions.jsonl").open("a") as stream:
            stream.write('\nnot json\n[]\n{"partial":\n')
        second = self._record(selected="careful", reason="Higher review complexity")

        rows = decision_log.explain("T-0570", root=self.tmp)
        self.assertEqual(rows, [{**second, "outcomes": []}, {**first, "outcomes": []}])
        self.assertEqual(rows.malformed_count, 3)

    def test_format_explain_is_readable(self):
        self._record(
            rejected={"careful": "Higher latency", "local": "Unavailable"},
            confidence=0.9, n=10,
        )
        decision_log.outcome("T-0570", "routing", root=self.tmp, merged=True)

        lines = decision_log.format_explain(
            decision_log.explain("T-0570", root=self.tmp)
        ).splitlines()
        self.assertIn("routing", lines[0])
        self.assertIn("selected: fast", lines)
        self.assertIn("reason: Lowest latency among eligible executors", lines)
        self.assertIn("rejected: careful: Higher latency, local: Unavailable", lines)
        self.assertIn("confidence/n: 0.9/10", lines)
        self.assertIn("outcome: merged=True", lines)


class MemoryRetrievalDocumentation(unittest.TestCase):
    def test_retrieval_kind_documented_for_memory_rows(self):
        self.assertIn("retrieval rows are memory retrievals", decision_log.__doc__)
        self.assertIn("retrieval", decision_log.CONTEXT_KINDS)
        with patch.object(decision_log, "_append", side_effect=lambda row, **kw: row):
            row = decision_log.record("retrieval", "T-memory", candidates=[], selected=[],
                                      hard_constraints={}, deterministic={}, reason="memory", mode="shadow",
                                      extra={"tokens_legacy": 10, "tokens_tiered": 5})
        self.assertEqual(row["extra"]["tokens_tiered"], 5)


class SteeringDecisionLogTests(unittest.TestCase):
    def test_steering_rows_omit_message_text(self):
        message = "repeated_failure: synthetic check failure"
        extra = {"trigger": "repeated_failure", "severity": "medium", "critical": False,
                 "action": "steer", "evidence_hash": "synthetic", "message_chars": len(message)}
        fields = dict(candidates=["continue", "steer", "cancel"], hard_constraints={},
                      deterministic={}, selected="steer", reason=["repeated_failure"], mode="shadow")
        with tempfile.TemporaryDirectory() as root:
            row = decision_log.record("steering", "T-steering", extra=extra, root=root, **fields)
            serialized = (Path(root) / "runs/sched/decisions.jsonl").read_text()
            self.assertEqual(row["extra"]["message_chars"], len(message))
            self.assertNotIn(message, serialized)
            self.assertNotIn('"message":', serialized)
            for key in decision_log.STEERING_KEYS:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    decision_log.record("steering", "T-steering", root=root,
                                        extra={k: v for k, v in extra.items() if k != key}, **fields)
            with self.assertRaises(ValueError):
                decision_log.record("steering", "T-steering", root=root,
                                    extra={**extra, "nested": {"message": message}}, **fields)
            self.assertEqual(decision_log.recent(root=root), [row])
