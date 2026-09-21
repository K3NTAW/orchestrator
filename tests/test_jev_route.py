import json, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from orchestrator import jev_route
from orchestrator.pool import Executor


class FakePool:
    def __init__(self, mode="shadow"):
        self.cfg = {"jev": {"routing": {"mode": mode, "cache_ttl_s": 86400}}}

    def eligible_executors(self, role, complexity, task):
        return task["eligible"]


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

    def test_cache_max_entries_zero_disables_caching(self):
        routing = {"mode": "shadow", "cache_ttl_s": 86400, "cache_max_entries": 0}
        with mock.patch.object(jev_route, "_routing_cfg", return_value=routing), \
             mock.patch.object(jev_route.jev, "_cfg", return_value=self.cfg), \
             mock.patch.object(jev_route.jev, "_day_tokens_used", return_value=0), \
             mock.patch.object(jev_route.jev, "ask", side_effect=[self.answer(), self.answer()]) as ask:
            jev_route.classify(self.task, FakePool(), self.eligible, self.root)
            jev_route.classify(self.task, FakePool(), self.eligible, self.root)
        self.assertEqual(ask.call_count, 2)
        cache = self.root / "runs" / "jev" / "route_cache.json"
        self.assertTrue(not cache.exists() or json.loads(cache.read_text()) == {})

    def route_signals(self, **overrides):
        signals = {key: .2 for key in jev_route.QUESTIONS}
        signals.update(overrides)
        return signals

    def test_active_mode_adjusts_ranking_within_bounds(self):
        eligible = self.eligible + [Executor("middle", "codex", "m", ["execute"])]
        evidence = {"weak": {"class_success": .4, "expected_cost": 1, "n": 20},
                    "middle": {"class_success": .6, "expected_cost": 2, "n": 10},
                    "strong": {"class_success": .9, "expected_cost": 3, "n": 3}}
        classification = {"signals": self.route_signals(substantial_reasoning=.8), "confidence": .9}
        scores = jev_route.active_scores(self.task, FakePool("active"), eligible,
                                         classification, evidence,
                                         {"weak": 1, "middle": 1, "strong": 1})
        multipliers = jev_route.adjustment(classification["signals"], evidence,
                                           [ex.id for ex in eligible],
                                           {"confidence": .9})
        self.assertEqual(sum(value > 1 for value in multipliers.values()), 1)
        self.assertTrue(all(1 <= value <= 1.25 for value in multipliers.values()))
        self.assertGreater(scores["strong"], max(scores["weak"], scores["middle"]))

    def test_shadow_mode_never_changes_scores(self):
        classification = {"signals": self.route_signals(substantial_reasoning=.9), "confidence": .9}
        evidence = {"weak": {"class_success": .4, "n": 3},
                    "strong": {"class_success": .9, "n": 3}}
        base = {"weak": 1.2, "strong": 1}
        self.assertEqual(jev_route.active_scores(self.task, FakePool("shadow"), self.eligible,
                                                classification, evidence, base), base)
        task = {**self.task, "eligible": self.eligible}
        with mock.patch.object(jev_route, "classify", return_value=classification), \
                mock.patch.object(jev_route, "evidence_for", return_value=evidence):
            context = jev_route.shadow_context(task, FakePool("shadow"))
        self.assertGreater(context["adjustment"]["strong"], 1)
        self.assertFalse(context["active"])

    def test_extreme_probability_cannot_overturn_strong_evidence(self):
        evidence = {"weak": {"class_success": .95, "n": 200},
                    "strong": {"class_success": 1, "n": 2}}
        classification = {"signals": self.route_signals(substantial_reasoning=1), "confidence": 1}
        scores = jev_route.active_scores(self.task, FakePool("active"), self.eligible,
                                         classification, evidence, {"weak": 1.3, "strong": 1})
        self.assertGreater(scores["weak"], scores["strong"])

    def test_cold_start_default_scores_and_failure_reproduce_baseline(self):
        signals = self.route_signals(substantial_reasoning=1)
        self.assertEqual(jev_route.adjustment(signals, {}, ["weak", "strong"], {"confidence": 1}),
                         {"weak": 1, "strong": 1})
        self.assertEqual(jev_route.active_scores(self.task, FakePool("active"), self.eligible,
                                                {"signals": signals, "confidence": 1}, {}, None),
                         {"weak": 1, "strong": 1})
        baseline = {"weak": .8, "strong": 1.2}
        self.assertEqual(jev_route.active_scores(self.task, FakePool("active"), self.eligible,
                                                None, {}, baseline), baseline)

    def test_active_scores_never_add_ineligible_executor(self):
        signals = self.route_signals(substantial_reasoning=1)
        evidence = {"weak": {"class_success": .4, "n": 3},
                    "strong": {"class_success": .8, "n": 3},
                    "outside": {"class_success": 1, "n": 0}}
        scores = jev_route.active_scores(self.task, FakePool("active"), self.eligible,
                                         {"signals": signals, "confidence": 1}, evidence,
                                         {"weak": 1, "strong": 1, "outside": 100})
        self.assertNotIn("outside", scores)
