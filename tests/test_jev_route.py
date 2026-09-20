import json, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from orchestrator import jev_route
from orchestrator.pool import Executor


class FakePool:
    def __init__(self, mode="shadow"):
        self.cfg = {"jev": {"routing": {"mode": mode, "cache_ttl_s": 86400}}}


class JevRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = {"id": "T-1", "spec": "small change", "acceptance": ["passes"],
                     "scope": ["x.py"], "complexity": 3, "constraints": {}}
        self.eligible = [Executor("weak", "codex", "w", ["execute"]),
                         Executor("strong", "codex", "s", ["execute"], weight=2)]
        self.cfg = {"enabled": True, "model": "jev", "timeout_s": 1, "daily_budget_tokens": 10}

    def answer(self, confidence=.8):
        return {"answers": {key: {"noul": .2, "confidence": confidence}
                            for key in jev_route.QUESTIONS}, "usage": {"input_tokens": 1}}

    def classify(self, pool=None):
        with mock.patch.object(jev_route.jev, "_cfg", return_value=self.cfg), \
             mock.patch.object(jev_route.jev, "_day_tokens_used", return_value=0), \
             mock.patch.object(jev_route.jev, "ask", return_value=self.answer()) as ask:
            result = jev_route.classify(self.task, pool or FakePool(), self.eligible, self.root)
        return result, ask

    def test_mode_off_makes_no_call_and_no_row(self):
        result, ask = self.classify(FakePool("off"))
        self.assertIsNone(result); ask.assert_not_called()
        self.assertFalse((self.root / "runs").exists())

    def test_single_eligible_skips_call(self):
        with mock.patch.object(jev_route.jev, "ask") as ask:
            self.assertIsNone(jev_route.classify(self.task, FakePool(), self.eligible[:1], self.root))
        ask.assert_not_called()

    def test_multiple_eligible_batches_all_questions_in_one_call(self):
        result, ask = self.classify()
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(ask.call_args.args[1], jev_route.QUESTIONS)
        self.assertEqual(set(result["signals"]), set(jev_route.QUESTIONS))

    def test_budget_exhausted_and_disabled_skip_call(self):
        for cfg, used in (({**self.cfg, "enabled": False}, 0), (self.cfg, 10)):
            with mock.patch.object(jev_route.jev, "_cfg", return_value=cfg), \
                 mock.patch.object(jev_route.jev, "_day_tokens_used", return_value=used), \
                 mock.patch.object(jev_route.jev, "ask") as ask:
                self.assertIsNone(jev_route.classify(self.task, FakePool(), self.eligible, self.root))
                ask.assert_not_called()

    def test_timeout_and_malformed_fail_open_to_none(self):
        for value in (None, {"answers": {}}, RuntimeError("timeout")):
            effect = value if isinstance(value, Exception) else None
            with mock.patch.object(jev_route.jev, "_cfg", return_value=self.cfg), \
                 mock.patch.object(jev_route.jev, "_day_tokens_used", return_value=0), \
                 mock.patch.object(jev_route.jev, "ask", return_value=None if effect else value,
                                   side_effect=effect):
                self.assertIsNone(jev_route.classify(self.task, FakePool(), self.eligible, self.root))

    def test_cache_hit_skips_call_and_invalidates_on_spec_change_and_ttl(self):
        first, _ = self.classify()
        second, ask = self.classify()
        self.assertEqual(second["cache"], "hit"); ask.assert_not_called()
        self.task["spec"] = "edited"
        changed, ask = self.classify()
        self.assertEqual(changed["cache"], "miss"); self.assertEqual(ask.call_count, 1)
        path = self.root / "runs" / "jev" / "route_cache.json"
        rows = json.loads(path.read_text())
        rows[jev_route._key(self.task)]["saved_at"] = time.time() - 90000
        path.write_text(json.dumps(rows))
        expired, ask = self.classify()
        self.assertEqual(expired["cache"], "miss"); self.assertEqual(ask.call_count, 1)

    def test_cache_holds_entries_per_key_for_interleaved_tasks(self):
        other = {**self.task, "id": "T-2", "spec": "another change"}
        with mock.patch.object(jev_route.jev, "_cfg", return_value=self.cfg), \
             mock.patch.object(jev_route.jev, "_day_tokens_used", return_value=0), \
             mock.patch.object(jev_route.jev, "ask", return_value=self.answer()) as ask:
            first_a = jev_route.classify(self.task, FakePool(), self.eligible, self.root)
            first_b = jev_route.classify(other, FakePool(), self.eligible, self.root)
            second_a = jev_route.classify(self.task, FakePool(), self.eligible, self.root)
            second_b = jev_route.classify(other, FakePool(), self.eligible, self.root)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual([first_a["cache"], first_b["cache"], second_a["cache"], second_b["cache"]],
                         ["miss", "miss", "hit", "hit"])

    def test_cache_prunes_expired_and_caps_entries(self):
        path, _ = jev_route._cache_paths(self.root)
        path.parent.mkdir(parents=True)
        now = time.time()
        path.write_text(json.dumps({
            "expired": {"signals": {}, "confidence": .5, "saved_at": now - 11, "task": "old"},
            "oldest": {"signals": {}, "confidence": .5, "saved_at": now - 2, "task": "one"},
            "newest": {"signals": {}, "confidence": .5, "saved_at": now - 1, "task": "two"},
        }))
        row = {"signals": {}, "confidence": .5, "task": "three"}
        jev_route._cache_write(self.root, "added", row, ttl=10, max_entries=2)
        rows = json.loads(path.read_text())
        self.assertEqual(set(rows), {"newest", "added"})

    def test_pool_toml_jev_routing_table_follows_jev(self):
        path = Path(__file__).parents[1] / ".orchestrator" / "pool.toml"
        lines = path.read_text().splitlines()
        jev = lines.index("[jev]")
        routing = lines.index("[jev.routing]")
        secrets = min(i for i, line in enumerate(lines) if line.startswith("[secrets."))
        self.assertLess(jev, routing)
        self.assertLess(routing, secrets)
        self.assertEqual(lines[routing + 5], "cache_max_entries = 500")

    def test_state_contains_no_file_contents_and_is_redacted(self):
        self.task.update(spec="TOKEN=abcdefghijklmnopqrstuvwxyz123456 SECRET", memory_titles=["safe title"])
        captured = {}
        def ask(state, questions, **kwargs):
            captured.update(state)
            return self.answer()
        with mock.patch.object(jev_route.jev, "_cfg", return_value=self.cfg), \
             mock.patch.object(jev_route.jev, "_day_tokens_used", return_value=0), \
             mock.patch.object(jev_route.jev, "ask", side_effect=ask):
            jev_route.classify(self.task, FakePool(), self.eligible, self.root)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", json.dumps(captured))
        self.assertEqual(captured["scope"], ["x.py"])
        self.assertEqual(captured["memory_titles"], ["safe title"])

    def test_hypothetical_never_outside_eligible(self):
        evidence = {"weak": {"class_success": .4, "expected_cost": 1},
                    "strong": {"class_success": .9, "expected_cost": 2},
                    "outside": {"class_success": 1, "expected_cost": 0}}
        for signals in ({"substantial_reasoning": .9}, {"localized_simple": .9, "elevated_risk": .1}, {}):
            self.assertIn(jev_route.hypothetical(self.eligible, signals, evidence, "outside"), {"weak", "strong"})
