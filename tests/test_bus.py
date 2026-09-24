"""Bus rules: acceptance is required, immutable fields, oversize results rejected, events, id sequencing."""
import contextlib, gc, io, json, sys, tempfile, threading, unittest, warnings
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_bus.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401  (TMP/ORCH_ROOT must exist before the orchestrator import below)
from orchestrator import bus


class BusSandbox(unittest.TestCase):
    def setUp(self):
        # Each test's cursor starts at its own stream, not events from other tests.
        state = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bus-test-")))
        for name, path in {"STATE": state, "TASKS": state / "tasks",
                           "RUNS": state / "runs", "LOCK": state / bus.LOCK_NAME}.items():
            self.enterContext(patch.object(bus, name, path))

    def tearDown(self):
        # Close the per-test singleton before unittest restores the patched paths.
        bus._close_db()


class Bus(BusSandbox):

    def test_db_connection_is_per_thread(self):
        connections = []

        def connect_twice():
            first = bus.db()
            connections.append((first, bus.db()))
            bus._close_db()

        threads = [threading.Thread(target=connect_twice) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(connections), 2)
        self.assertIs(connections[0][0], connections[0][1])
        self.assertIs(connections[1][0], connections[1][1])
        self.assertIsNot(connections[0][0], connections[1][0])

    def test_read_skips_rows_without_task_file(self):
        task = bus.create_task("Readable", "spec", ["ok"], ["src/**"])
        bus.db().execute("insert into tasks values(?,?,?,?,?,?)",
                         ("T-missing", "queued", "scout", "sonnet", None, 0))

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rows = bus.read()

        self.assertEqual([row["id"] for row in rows], [task["id"]])
        self.assertEqual(stderr.getvalue(), "bus.read: skipped 1 unreadable rows\n")

    def test_concurrent_reads_and_writes_never_raise(self):
        tasks = [bus.create_task(f"Task {i}", "spec", ["ok"], ["src/**"])
                 for i in range(8)]
        errors = []

        def exercise(task):
            try:
                for iteration in range(200):
                    bus.update(task["id"], assigned_to=f"worker-{iteration % 2}")
                    rows = bus.read()
                    if not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                               and row["id"] for row in rows):
                        raise AssertionError("read returned a malformed task")
            except BaseException as exc:
                errors.append(exc)
            finally:
                bus._close_db()

        threads = [threading.Thread(target=exercise, args=(task,)) for task in tasks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])

    def test_log_run_annotates_cache_fields(self):
        row = self.written_row(provider="claude", usage={"input_tokens": 10,
            "cache_read_input_tokens": 30, "output_tokens": 2},
            packet_meta={"hash": "v", "prefix_sha": "prefix"})
        self.assertEqual(row["input_uncached_tokens"], 10)
        self.assertEqual(row["cache_read_tokens"], 30)
        self.assertEqual(row["hit_ratio"], .75)
        self.assertEqual(row["prefix_sha"], "prefix")
        with patch("orchestrator.cache_telemetry.annotate", side_effect=RuntimeError("telemetry")):
            self.written_row(provider="claude", usage={"input_tokens": 1})

    def test_create_task_sets_created_at(self):
        with patch.object(bus.time, "time", return_value=1234.5):
            task = bus.create_task("Timestamp", "spec", ["ok"], ["x.py"])
        self.assertEqual(task["created_at"], 1234.5)
        self.assertEqual(bus.get(task["id"])["created_at"], 1234.5)
        task.pop("created_at")
        bus._save(task)
        self.assertNotIn("created_at", bus.get(task["id"]))

    def written_row(self, **fields):
        bus.log_run(**fields)
        return json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])

    def test_log_run_enriches_bucket_lineage_band_class(self):
        root = bus.create_task("Root", "s", ["a"], ["x"], role="execute")
        task = bus.create_task("Repair", "s", ["a"], ["x"], role="execute", complexity=5,
                               constraints={"fix_round_for": root["id"]})
        row = self.written_row(task=task["id"], role="execute", executor="astra", tier="astra", account="codex")
        self.assertEqual([row[k] for k in ("bucket", "lineage_root", "round_index", "band", "task_class")],
                         ["fix_round", root["id"], 1, "4-6", "debugging"])
        self.assertEqual(row["model"], "gpt-6-astra")

    def test_log_run_without_task_uses_other_bucket(self):
        row = self.written_row(role="unknown")
        self.assertEqual(row["bucket"], "other")
        self.assertNotIn("usd", row)
        self.assertNotIn("usd_source", row)
        self.assertEqual(self.written_row(role="planner_decision")["bucket"], "planner")

    def test_log_run_writes_normalized_tokens_for_claude_and_codex(self):
        from orchestrator.executor import _tokens
        for provider, usage, expected in [
            ("codex", {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}, [60, 40, 0, 5, 105]),
            ("claude", {"input_tokens": 100, "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 10, "output_tokens": 5}, [100, 40, 10, 5, 155]),
        ]:
            expanded = _tokens(usage) if provider == "codex" else usage
            row = self.written_row(provider=provider, usage=usage, **expanded)
            self.assertEqual(row["usage"], usage)
            self.assertEqual([row[k] for k in ("input_uncached_tokens", "cache_read_tokens",
                                              "cache_write_tokens", "output_tokens", "total_tokens")], expected)
        row = self.written_row(role="planner_decision", provider="claude", **usage)
        self.assertEqual(row["usage"], usage)
        self.assertEqual(row["total_tokens"], 155)

    def test_codex_usd_estimated_with_source(self):
        cfg = {"executors": [{"id": "test", "usd_per_token": 0.01}]}
        with patch.object(bus, "pool_config", return_value=cfg):
            row = self.written_row(provider="codex", executor="test", usage={"input_tokens": 100, "output_tokens": 5})
        self.assertAlmostEqual(row["usd"], 1.05)
        self.assertEqual(row["usd_source"], "token_estimate")

    def test_claude_usd_reported_with_source(self):
        row = self.written_row(provider="claude", usd=0.123, usage={"input_tokens": 100})
        self.assertEqual(row["usd"], 0.123)
        self.assertEqual(row["usd_source"], "reported")

    def test_log_run_carries_decision_identity_and_versions(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, patch.object(bus, "RUNS", Path(directory)):
            fields = dict(attempt=3, decision_kind="held", payload_key="hold:123", route="codex",
                          route_reason="lowest_cost", client_version="2.1.273", policy_version="abc123")
            bus.log_run(**fields)
            bus.log_run(**dict.fromkeys(fields))
            bus.log_run(policy_version=None)
            rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            for key, value in fields.items():
                self.assertEqual(rows[0][key], value)
                self.assertNotIn(key, rows[1])
            self.assertEqual(rows[2]["attempt"], 1)

    def test_policy_version_stable_until_prompt_or_pool_changes(self):
        import hashlib
        import os
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, patch.object(bus, "STATE", Path(directory)), \
                patch.object(bus, "_policy_cache", None):
            pool = Path(directory) / "pool.toml"
            prompts = Path(directory) / "prompts"
            prompts.mkdir()
            pool.write_bytes(b"pool")
            (prompts / "b.md").write_bytes(b"b")
            prompt = prompts / "a.md"
            prompt.write_bytes(b"a")
            first = bus.policy_version()
            self.assertEqual(first, hashlib.sha256(b"poolab").hexdigest()[:12])
            with patch.object(Path, "read_bytes", side_effect=AssertionError("cache reread")):
                self.assertEqual(bus.policy_version(), first)
            for path, content in ((prompt, b"changed"), (pool, b"new pool")):
                previous_mtime = path.stat().st_mtime_ns
                path.write_bytes(content)
                os.utime(path, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
                current = bus.policy_version()
                self.assertNotEqual(current, first)
                first = current
            (prompts / "c.md").write_bytes(b"c")
            self.assertNotEqual(bus.policy_version(), first)
            (prompts / "c.md").unlink()
            self.assertEqual(bus.policy_version(), first)
            pool.unlink()
            self.assertIsNone(bus.policy_version())


    def test_normalize_usage_claude(self):
        self.assertEqual(bus.normalize_usage("claude", {"input_tokens": 10, "cache_read_input_tokens": 3,
                         "cache_creation_input_tokens": 2, "output_tokens": 5}),
                         {"input_uncached_tokens": 10, "cache_read_tokens": 3, "cache_write_tokens": 2,
                          "output_tokens": 5, "reasoning_tokens": 0, "total_tokens": 20})

    def test_normalize_usage_codex(self):
        self.assertEqual(bus.normalize_usage("codex", {"input_tokens": 10, "cached_input_tokens": 4,
                         "output_tokens": 5, "reasoning_output_tokens": 2}),
                         {"input_uncached_tokens": 6, "cache_read_tokens": 4, "cache_write_tokens": 0,
                          "output_tokens": 5, "reasoning_tokens": 2, "total_tokens": 15})

    def test_log_run_adds_goal_id(self):
        goal = bus.create_task("Goal", "spec", ["ok"], ["src/**"])
        task = bus.create_task("Child", "spec", ["ok"], ["src/**"], parent=goal["id"])
        bus.log_run(task=task["id"], account="codex")
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        self.assertEqual(row["goal_id"], goal["id"])
        self.assertEqual(row["provider"], "codex")

    def test_log_run_missing_task_still_appends(self):
        bus.log_run(task="T-9999", account="codex")
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        self.assertIsNone(row["goal_id"])
        self.assertEqual(row["provider"], "codex")

    def test_log_run_corrupt_task_still_appends(self):
        task = bus.create_task("Corrupt", "spec", ["ok"], ["src/**"])
        task_path = bus.TASKS / f"{task['id']}.json"
        original = task_path.read_text()
        self.addCleanup(task_path.write_text, original)
        task_path.write_text("{")
        bus.log_run(task=task["id"], account="claude:A")
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        self.assertIsNone(row["goal_id"])
        self.assertEqual(row["provider"], "claude")

    def test_db_leaves_no_unclosed_connection(self):
        gc.collect()
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always", ResourceWarning)
            for i in range(3):
                task = bus.create_task(f"Connection lifetime {i}", "spec", ["ok"], ["src/**"])
                bus.update(task["id"], status="running")
                bus.post_result(task["id"], {"summary": "ok"})
            bus.read()
            bus.events()
            gc.collect()
        self.assertEqual([str(w.message) for w in recorded
                          if issubclass(w.category, ResourceWarning) and "sqlite3.Connection" in str(w.message)], [])

    def test_lifecycle_and_rules(self):
        with self.assertRaises(ValueError):
            bus.create_task("x", "spec", [], ["src/**"])            # no acceptance
        n0 = bus.next_id()
        t = bus.create_task("Map auth", "where are tokens read", ["cites path:line"], ["src/auth/**"], parent=None)
        self.assertEqual(t["status"], "queued"); self.assertTrue(t["constraints"]["read_only"])
        bus.claim(t["id"], "claude:B", "wt/x")
        self.assertEqual(bus.read(status="running")[0]["id"], t["id"])
        with self.assertRaises(PermissionError):
            bus.update(t["id"], scope=["everything"])            # only Planner fields are immutable
        with self.assertRaises(ValueError):
            bus.post_result(t["id"], {"summary": "x" * 7000})    # oversize rejected
        bus.post_result(t["id"], {"summary": "ok", "findings": []})
        done = bus.read(t["id"]); self.assertEqual(done["status"], "done"); self.assertIn("provenance", done["result"])
        ev = [e for e in bus.events(0) if e["task"] == t["id"]]
        self.assertEqual([e["kind"] for e in ev], ["created", "update", "update"])
        self.assertEqual(t["id"], n0)
        self.assertEqual(bus.next_id(), f"T-{int(n0.split('-')[1]) + 1:04d}")

    def test_events_role_filter_keeps_seq_and_cursor(self):
        scout = bus.create_task("Scout", "s", ["a"], ["x"], role="scout")
        execute = bus.create_task("Execute", "s", ["a"], ["x"], role="execute")
        all_events = bus.events(0, 200)
        filtered = bus.events(0, 200, role="execute")
        self.assertTrue(filtered)
        self.assertTrue(all(event["task"] == execute["id"] for event in filtered))
        self.assertEqual([event["seq"] for event in filtered],
                         [event["seq"] for event in all_events if event["task"] == execute["id"]])
        self.assertNotIn(scout["id"], [event["task"] for event in filtered])

    def test_events_truncated_flag_and_next_since(self):
        bus.create_task("A", "s", ["a"], ["x"])
        bus.create_task("B", "s", ["a"], ["x"])
        from orchestrator import bus_mcp
        reply = bus_mcp.bus_events(0, 1)
        self.assertTrue(reply["truncated"])
        self.assertEqual(reply["next_since"], reply["events"][-1]["seq"])

    def test_events_filtered_page_never_exceeds_next_since(self):
        scout = bus.create_task("Scout", "s", ["a"], ["x"], role="scout")
        execute = bus.create_task("Execute", "s", ["a"], ["x"], role="execute")
        from orchestrator import bus_mcp
        original_events = bus.events

        def insert_after_page(*args, **kwargs):
            page = original_events(*args, **kwargs)
            bus._event(execute["id"], "concurrent")
            return page

        with patch.object(bus, "events", side_effect=insert_after_page) as events:
            reply = bus_mcp.bus_events(0, 2, role="execute")

        self.assertEqual(events.call_count, 1)
        self.assertTrue(reply["events"])
        self.assertTrue(all(event["seq"] <= reply["next_since"] for event in reply["events"]))
        self.assertNotIn("concurrent", [event["kind"] for event in reply["events"]])

    def test_events_filters_preserve_unfiltered_page_metadata(self):
        from orchestrator import bus_mcp
        scout = bus.create_task("Scout", "s", ["a"], ["x"], role="scout")
        execute = bus.create_task("Execute", "s", ["a"], ["x"], role="execute")
        bus._event(execute["id"], "update")
        page = bus.events(0, 2)
        with patch.object(bus, "read", side_effect=AssertionError("use the task index")):
            for filters, expected in [
                ({"role": "execute"}, [page[1]]),
                ({"task_ids": [execute["id"]]}, [page[1]]),
                ({"role": "execute", "task_ids": [scout["id"]]}, []),
                ({"task_ids": []}, []),
            ]:
                with self.subTest(filters=filters):
                    self.assertEqual(bus.events(0, 2, **filters), expected)
                    self.assertEqual(bus_mcp.bus_events(0, 2, **filters),
                                     {"events": expected, "next_since": page[-1]["seq"], "truncated": True})
            empty = bus_mcp.bus_events(0, 1, role="execute")
            self.assertEqual(empty, {"events": [], "next_since": page[0]["seq"], "truncated": True})
            tail = bus_mcp.bus_events(page[-1]["seq"], 2, role="execute")
            self.assertEqual([event["kind"] for event in tail["events"]], ["update"])
            self.assertFalse(tail["truncated"])
            self.assertEqual(tail["next_since"], tail["events"][-1]["seq"])
            self.assertEqual(bus_mcp.bus_events(tail["next_since"], 2, role="execute"),
                             {"events": [], "next_since": tail["next_since"], "truncated": False})

    def test_events_without_filters_preserve_orphaned_rows_and_shape(self):
        task = bus.create_task("Execute", "s", ["a"], ["x"], role="execute")
        page = bus.events()
        bus.db().execute("delete from tasks where id=?", (task["id"],))

        with patch.object(bus, "_filter_events", side_effect=AssertionError("no filtering requested")):
            self.assertEqual(bus.events(), page)
        self.assertEqual(bus._filter_events(page), page)
        self.assertEqual(bus._filter_events(page, role="execute"), [])

    def test_depends_on(self):
        a = bus.create_task("A", "spec a", ["ok"], ["src/**"])
        b = bus.create_task("B", "spec b", ["ok"], ["src/**"], depends_on=[a["id"]])
        self.assertFalse(bus.ready(b))
        bus.update(a["id"], merged_into="goal/x", sha="abc")
        self.assertTrue(bus.ready(b))
        with self.assertRaises(ValueError):
            bus.create_task("C", "spec c", ["ok"], ["src/**"], depends_on=["T-9999"])
        with self.assertRaises(PermissionError):
            bus.update(b["id"], depends_on=[])
        self.assertEqual(bus.dependents(a["id"]), [b])

    def test_fix_round_drops_own_parent_from_depends_on(self):
        parent = bus.create_task("Parent", "spec", ["ok"], ["src/**"], role="execute")
        other = bus.create_task("Other", "spec", ["ok"], ["src/**"], role="execute")
        fix = bus.create_task("Fix", "spec", ["ok"], ["src/**"], role="execute",
                              constraints={"fix_round_for": parent["id"]},
                              depends_on=[parent["id"], other["id"]])

        self.assertEqual(fix["depends_on"], [other["id"]])
        self.assertFalse(bus.ready(fix))
        bus.update(other["id"], merged_into="goal/x", sha="abc")
        self.assertTrue(bus.ready(fix))
        events = [event for event in bus.events() if event["task"] == fix["id"]]
        self.assertEqual(events[-1]["kind"], "depends_on_normalized")
        self.assertEqual(events[-1]["data"], {"dropped": parent["id"]})

    def test_non_fix_round_depends_on_unchanged(self):
        dependency = bus.create_task("Dependency", "spec", ["ok"], ["src/**"])
        task = bus.create_task("Normal", "spec", ["ok"], ["src/**"],
                               depends_on=[dependency["id"]])

        self.assertEqual(task["depends_on"], [dependency["id"]])
        self.assertEqual([event["kind"] for event in bus.events()
                          if event["task"] == task["id"]], ["created"])

    def test_read_compact_rows(self):
        t = bus.create_task("A" * 200, "spec " * 1000, ["ok"], ["src/**"])  # 200-char title, ~5,000-char spec
        bus.claim(t["id"], "claude:B")
        bus.update(t["id"], status="held", hold_reason="waiting", reason="x" * 300)
        bus.post_result(t["id"], {"summary": "y" * 300, "findings": []}, status="held")
        rows = bus.read(status_not="done", compact=True)
        row = next(r for r in rows if r["id"] == t["id"])
        self.assertEqual(set(row), {"id", "parent", "role", "status", "complexity", "tier", "title", "depends_on",
                                     "hold_reason", "reason", "merged_into", "assigned_to", "has_result",
                                     "result_summary"})
        self.assertEqual(len(row["title"]), 90)
        self.assertEqual(len(row["reason"]), 120)
        self.assertTrue(row["has_result"])
        self.assertEqual(len(row["result_summary"]), 160)
        self.assertNotIn("spec", row); self.assertNotIn("events", row); self.assertNotIn("acceptance", row)

        modest = bus.create_task("Modest title", "spec " * 1000, ["ok"], ["src/**"])
        modest_row = next(r for r in bus.read(status_not="done", compact=True) if r["id"] == modest["id"])
        self.assertLess(len(json.dumps(modest_row)), 600)

    def test_read_full_shape_unchanged(self):
        t = bus.create_task("Full shape", "a spec", ["ok"], ["src/**"])
        rows = bus.read(status="queued")
        row = next(r for r in rows if r["id"] == t["id"])
        self.assertIn("spec", row); self.assertIn("acceptance", row); self.assertIn("events", row)
        self.assertEqual(row, t)

    def test_read_single_task_full(self):
        t = bus.create_task("Single", "a spec", ["ok"], ["src/**"])
        self.assertEqual(bus.read(t["id"]), t)
        self.assertEqual(bus.read(t["id"], compact=True), t)  # compact never applies to a single-task read


class ContextLog(BusSandbox):
    def test_log_run_carries_routed_keys(self):
        routed = {"routed_tokens": 12, "routed_reduction_ratio": .4, "routed_hidden": 2,
                  "routed_ambiguous": 1, "routed_rules_version": "v1", "evidence_ids": ["abc"]}
        bus.log_run(packet_meta={"hash": "abc", **routed})
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        for key, value in routed.items():
            self.assertEqual(row["context"][key], value)

    def test_log_run_derives_context_from_packet_meta(self):
        meta = {"hash": "abc", "version": "abc", "sections": {}, "presented_tokens": 3,
                "candidate_tokens": 4, "candidate_known": True, "instruction_tokens": 2}
        bus.log_run(packet_meta=meta)
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        self.assertEqual(row["context"]["presented_tokens"], 3)
        bus.log_run()
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        self.assertIsNone(row["context"])

    def test_log_run_carries_skill_keys(self):
        values = {"skills_exposed": ["executor/x"], "skills_used": ["executor/x"],
                  "skill_tokens_l0": 4, "skill_tokens_l2": 12}
        bus.log_run(packet_meta={"hash": "skill", **values})
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        for key, value in values.items():
            self.assertEqual(row["context"][key], value)


if __name__ == "__main__":
    unittest.main()


class ToolDisclosureCacheRows(unittest.TestCase):
    def test_tool_disclosure_row_carries_cache_view_fields(self):
        from unittest import mock
        from orchestrator import bus, decision_log, tool_catalog
        task = {"id": "cache-task", "scope": ["example.py"]}
        for mode in ("shadow", "active", "invalid", "off"):
            cfg = {"tool_disclosure": {"mode": "shadow", "cache_mode": mode}}
            with mock.patch.object(bus, "get", return_value=task), \
                 mock.patch.object(bus, "pool_config", return_value=cfg), \
                 mock.patch.object(decision_log, "last_row", return_value=None), \
                 mock.patch.object(decision_log, "record") as record:
                bus.log_run(task=task["id"], provider="codex", role="execute", outcome="ok")
            data = next(call.kwargs["deterministic"] for call in record.call_args_list
                        if call.kwargs.get("kind") == "tool_disclosure")
            if mode in ("shadow", "active"):
                for key, value in tool_catalog.cache_view("codex_execute", ["CODEX_TOOLS"], None).items():
                    self.assertEqual(data[key], value)
            else:
                self.assertNotIn("stable_catalog_chars", data)
                if mode == "invalid":
                    self.assertTrue(data["invalid_config"])
