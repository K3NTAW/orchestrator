import _harness
import tempfile
import unittest
import json
from pathlib import Path

from orchestrator import context_router, decision_log, evidence


def ev(kind, location, content, task=None, commit=""):
    provenance = "memory" if kind == "memory_entry" else "repo"
    return evidence.make(kind, location, content, provenance=provenance, task=task, commit=commit,
                         observed_at=1)


class ContextRouterTests(unittest.TestCase):
    def test_cache_fields_on_rows_and_presented_level_unchanged_in_shadow(self):
        item = ev("test_result", "test_ok", "x" * 4000)
        plain = context_router.route({"id": "T"}, [item], role="execute", provider="codex")
        shadow = context_router.route({"id": "T"}, [item], role="execute", provider="codex",
                                      cfg={"context_router": {"cache_mode": "shadow",
                                                               "cache_downgrade_tokens": 100}})
        self.assertEqual([x.level for x in plain.items], [x.level for x in shadow.items])
        deterministic = context_router.decision_row({"id": "T"}, shadow, mode="shadow")["deterministic"]
        self.assertEqual(deterministic["cacheability"][item.id], "dynamic")
        self.assertGreater(deterministic["cache_cost"][item.id], 100)
        self.assertEqual(deterministic["cache_adjusted_level"][item.id], "SHORT")
        self.assertEqual(deterministic["cache_data"], "defaults")

    def test_cache_adjusted_level_algorithm(self):
        dynamic = ev("test_result", "test_ok", "x" * 4000, commit="head")
        named = evidence.make("worker_partial", "prior", "x" * 4000,
                              provenance="worker_partial", scope=["x.py"], commit="head")
        hidden = ev("test_result", "test_hidden", "x" * 4000, commit="old")
        task = {"id": "T", "packet_read_scope": ["x.py"]}
        packet = context_router.route(task, [dynamic, named, hidden], role="execute", head_sha="head",
                                      provider="codex",
                                      cfg={"context_router": {"cache_downgrade_tokens": 100}})
        self.assertEqual([item.cache_adjusted_level for item in packet.items],
                         ["SHORT", "LONG", "HIDE"])
        self.assertIsNone(context_router.route(task, [dynamic], role="execute").items[0].cache_cost)
        ambiguous = evidence.make("external_doc", "uncertain", "x" * 4000,
                                  provenance="worker_partial", commit="head")
        required = evidence.make("source_chunk", "other.py", "x" * 4000,
                                 provenance="worker_partial", commit="head")
        active = context_router.route(task, [dynamic, named, hidden, ambiguous, required],
                                      role="execute", head_sha="head", provider="codex",
                                      effective_mode="active", required_types=("source_chunk",),
                                      cfg={"context_router": {"cache_mode": "active",
                                                               "cache_downgrade_tokens": 100}})
        self.assertEqual([item.reason for item in active.items],
                         ["test_result", "prior_worker_overlap", "stale", "ambiguous", "skill_required_context"])
        self.assertEqual([item.level for item in active.items], ["SHORT", "LONG", "HIDE", "LONG", "LONG"])
        unknown = context_router.route(task, [dynamic], role="execute", provider="unknown")
        self.assertIsNone(unknown.items[0].cache_cost)
        self.assertEqual(unknown.cache_data, "missing")

    def test_active_cache_mode_applies_adjusted_levels_only_when_mode_not_off(self):
        item = ev("test_result", "test_ok", "x" * 4000)
        cfg = {"context_router": {"cache_mode": "active", "cache_downgrade_tokens": 100}}
        shadow = context_router.route({"id": "T"}, [item], role="execute", provider="codex",
                                      cfg=cfg, effective_mode="shadow")
        active = context_router.route({"id": "T"}, [item], role="execute", provider="codex",
                                      cfg=cfg, effective_mode="active")
        self.assertEqual((shadow.items[0].level, active.items[0].level), ("LONG", "SHORT"))
        self.assertLess(active.items[0].tokens_at_level, shadow.items[0].tokens_at_level)
        row = context_router.decision_row({"id": "T"}, active, mode="active")
        self.assertEqual(row["deterministic"]["presented_level"][item.id], "LONG")
        self.assertEqual(row["deterministic"]["cache_adjusted_level"][item.id], "SHORT")
        self.assertEqual(row["candidates"], [f"{item.id}:SHORT"])
        off = context_router.route({"id": "T"}, [item], role="execute", provider="codex",
                                   cfg=cfg, effective_mode="off")
        self.assertEqual(off.items[0].level, "LONG")

    def test_cache_mode_helper_validates_and_shadow_keeps_candidates_byte_identical(self):
        self.assertEqual(context_router.cache_mode({}), "off")
        self.assertEqual(context_router.cache_mode({"context_router": {"cache_mode": "shadow"}}), "shadow")
        with self.assertRaises(ValueError):
            context_router.cache_mode({"context_router": {"cache_mode": "future"}})
        item = ev("test_result", "test_ok", "x" * 4000)
        off = context_router.decision_row({"id": "T"}, context_router.route(
            {"id": "T"}, [item], role="execute", provider="codex"), mode="shadow")
        shadow = context_router.decision_row({"id": "T"}, context_router.route(
            {"id": "T"}, [item], role="execute", provider="codex",
            cfg={"context_router": {"cache_mode": "shadow"}}), mode="shadow")
        self.assertEqual(json.dumps(off["candidates"]).encode(), json.dumps(shadow["candidates"]).encode())

    def test_invalid_cache_mode_falls_back_to_off_with_flag(self):
        item = ev("test_result", "test_ok", "x" * 4000)
        packet = context_router.route({"id": "T"}, [item], role="execute", provider="codex",
                                      effective_mode="active",
                                      cfg={"context_router": {"cache_mode": "invalid"}})
        self.assertEqual(packet.items[0].level, "LONG")
        self.assertTrue(context_router.decision_row({"id": "T"}, packet, mode="shadow")
                        ["deterministic"]["invalid_config"])

    def test_stale_source_preserves_scope_and_security_precedence(self):
        item = ev("source_chunk", "orchestrator/auth.py:1-4", "code", commit="old")
        for role, scope, cfg, expected in (
                ("execute", ["orchestrator/auth.py"], {}, ("FULL", "in_scope_file")),
                ("scout", ["orchestrator/auth.py"], {}, ("FULL", "in_scope_file")),
                ("security_review", [], {"review": {"security_paths": ["orchestrator/*.py"]}},
                 ("FULL", "security_path")),
                ("execute", [], {}, ("HIDE", "stale"))):
            with self.subTest(role=role, scope=scope):
                routed = context_router.route({"id": "T", "scope": scope}, [item],
                                              role=role, cfg=cfg, head_sha="new").items[0]
                self.assertEqual((routed.level, routed.reason), expected)

    def test_stale_test_result_preserves_failure_precedence(self):
        for role, content, expected in (("review", "OK", "FULL"),
                                        ("execute", "FAIL test_example", "FULL"),
                                        ("execute", "OK", "HIDE")):
            with self.subTest(role=role, content=content):
                item = ev("test_result", "test_example", content, commit="old")
                routed = context_router.route({"id": "T"}, [item], role=role,
                                              head_sha="new").items[0]
                self.assertEqual(routed.level, expected)

    def test_worker_partial_fresh_overlap_never_hidden_and_stale_hidden(self):
        item = evidence.make("worker_partial", "T-old:abc", "file: router.py", commit="abc",
                             provenance="worker_partial", scope=["router.py"])
        task = {"id": "T-new", "scope": [], "packet_read_scope": ["router.py"]}
        fresh = context_router.route(task, [item], role="execute", head_sha="abc").items[0]
        stale = context_router.route(task, [item], role="execute", head_sha="def").items[0]
        self.assertEqual((fresh.level, fresh.reason), ("LONG", "prior_worker_overlap"))
        self.assertEqual((stale.level, stale.reason), ("HIDE", "stale"))

    def test_security_review_full_for_security_path_in_scope(self):
        task = {"id": "T", "scope": ["orchestrator/auth.py"]}
        item = ev("source_chunk", "orchestrator/auth.py:1-4", "authentication code")
        cfg = {"review": {"security_paths": ["orchestrator/*.py"]}}
        routed = context_router.route(task, [item], role="security_review", cfg=cfg).items[0]
        self.assertEqual(("FULL", "security_path"), (routed.level, routed.reason))
        for role, config in (("review", cfg), ("security_review", {})):
            with self.subTest(role=role, cfg=config):
                ordinary = context_router.route(task, [item], role=role, cfg=config).items[0]
                self.assertEqual(("LONG", "in_scope_file"), (ordinary.level, ordinary.reason))

    def test_execute_profile_full_for_scope_hide_unrelated_memory(self):
        task = {"id": "T-1", "title": "build router", "scope": ["router.py"]}
        source = ev("source_chunk", "router.py:1-4", "code", task)
        memory = ev("memory_entry", "old", "unrelated words", task)
        packet = context_router.route(task, [source, memory], role="execute")
        self.assertEqual(["FULL", "HIDE"], [item.level for item in packet.items])

    def test_review_profile_long_for_scope_and_full_tests(self):
        task = {"id": "T-1", "scope": ["router.py"]}
        packet = context_router.route(task, [ev("source_chunk", "router.py:2", "x"),
                                             ev("test_result", "test_x", "ok")], role="review")
        self.assertEqual(["LONG", "FULL"], [item.level for item in packet.items])

    def test_render_orders_and_counts_hidden(self):
        task = {"id": "T", "scope": ["z.py"]}
        values = [ev("source_chunk", "z.py:1", "full"),
                  ev("external_doc", "a", "long"),
                  ev("source_chunk", "b.py", "short"),
                  ev("memory_entry", "hidden", "nothing relevant")]
        packet = context_router.route(task, values, role="execute")
        rendered = context_router.render(packet, {item.id: item for item in values})
        self.assertLess(rendered.index("z.py"), rendered.index("- a"))
        self.assertLess(rendered.index("- a"), rendered.index("b.py"))
        self.assertIn("hidden: 1", rendered)

    def test_reduction_ratio_and_ambiguous_bucket(self):
        item = ev("external_doc", "doc", "word " * 200)
        packet = context_router.route({"id": "T"}, [item], role="execute")
        self.assertEqual([item.id], packet.ambiguous_ids)
        self.assertLess(packet.reduction_ratio, 1)

    def test_decision_row_has_no_content(self):
        secret = "CONTENT-MUST-NOT-APPEAR"
        item = ev("external_doc", "doc", secret)
        packet = context_router.route({"id": "T"}, [item], role="execute")
        row = context_router.decision_row({"id": "T"}, packet, mode="shadow")
        with tempfile.TemporaryDirectory() as root:
            stored = decision_log.record(**row, root=Path(root))
        self.assertNotIn(secret, repr(stored))

    def test_failing_ids_come_from_resume_hint(self):
        task = {"id": "T", "resume_hint": {"failures": "FAILED tests/test_x.py::test_y - no"}}
        item = ev("test_result", "tests/test_x.py::test_y", "ordinary output")
        routed = context_router.route(task, [item], role="execute").items[0]
        self.assertEqual(("FULL", "failing_output"), (routed.level, routed.reason))

    def test_write_scope_alias_routes_full(self):
        task = {"id": "T", "write_scope": ["x.py"]}
        routed = context_router.route(task, [ev("source_chunk", "x.py:1", "x")], role="execute").items[0]
        self.assertEqual("FULL", routed.level)

    def test_relevance_is_recomputed_per_task(self):
        item = ev("memory_entry", "notes", "alpha router detail",
                  {"id": "built", "title": "alpha router"})
        unrelated = context_router.route({"id": "U", "title": "banana widget"}, [item], role="execute")
        matching = context_router.route({"id": "M", "title": "alpha router"}, [item], role="execute")
        self.assertEqual("HIDE", unrelated.items[0].level)
        self.assertEqual("LONG", matching.items[0].level)


if __name__ == "__main__":
    unittest.main()
