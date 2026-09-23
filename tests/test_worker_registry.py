import _harness
"""Worker snapshots and lifecycle integrations, using the shared bus fixture."""
import json
import os
import subprocess
import threading
import time
import unittest
from unittest import mock
from _harness import TMP
from orchestrator import bus, daemon, executor, pool, worker_registry as registry


class WorkerRegistry(unittest.TestCase):
    def setUp(self):
        self.task = bus.create_task("registry", "s", ["a"], ["x.py"], role="execute")
        self.tid = self.task["id"]
        self.addCleanup(self.clean_registry)

    def clean_registry(self):
        registry._path(self.tid).unlink(missing_ok=True)
        registry._path(self.tid, events=True).unlink(missing_ok=True)

    def test_upsert_event_finish_roundtrip(self):
        with mock.patch.object(registry.os, "replace", wraps=os.replace) as replace:
            registry.upsert(self.tid, role="execute", model="model", provider="codex",
                            started_at=time.time() - 10, parent="goal")
            replace.assert_called_once()
        registry.event(self.tid, "spawned", pid=123)
        registry.event(self.tid, "stage", stage="execute")
        registry.event(self.tid, "tool", current_tool="Read")
        registry.event(self.tid, "usage", tokens={"input_uncached": 4, "cache_read": 2, "output": 3}, usd=.01)
        self.assertIn(self.tid, [row["task"] for row in registry.active()])
        registry.finish(self.tid, "done")
        doc = registry.get(self.tid)
        self.assertGreaterEqual(doc["elapsed_s"], 10)
        self.assertEqual(doc["tokens"]["output"], 3)
        self.assertEqual(doc["status"], "done")
        stored = json.loads(registry._path(self.tid).read_text())
        self.assertNotIn("elapsed_s", stored)
        self.assertEqual(stored["current_tool"], "Read")
        self.assertFalse(list(registry._path(self.tid).parent.glob(self.tid + "*.tmp")))
        rows = registry.events(self.tid)
        self.assertEqual([r["kind"] for r in rows], ["spawned", "stage", "tool", "usage", "exit"])
        self.assertTrue(all(set(row) == {"ts", "kind", "data"} for row in rows))
        self.assertNotIn(self.tid, [row["task"] for row in registry.active()])
        with mock.patch.object(registry.time, "time", return_value=doc["last_event_at"] + 90000):
            self.assertEqual(registry.get(self.tid)["elapsed_s"], doc["elapsed_s"])
            self.assertNotIn(self.tid, [row["task"] for row in registry.listing(True)])

    def test_reconcile_marks_dead_pids(self):
        registry.upsert(self.tid, status="running", pid=123)
        before = registry._path(self.tid).read_bytes()
        registry.reconcile(lambda pid: True)
        self.assertEqual(registry._path(self.tid).read_bytes(), before)
        with mock.patch.object(registry, "active", return_value=[registry.get(self.tid)]):
            registry.reconcile(lambda pid: False)
        self.assertEqual(registry.get(self.tid)["status"], "failed")
        self.assertEqual(registry.get(self.tid)["status_reason"], "process_dead")
        self.assertEqual(registry.events(self.tid)[-1]["kind"], "reconciled")
        self.assertEqual(bus.get(self.tid)["status"], "queued")

    def test_registry_never_stores_prompt_text(self):
        for key in ("packet", "prompt", "transcript", "secret", "reasoning"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.upsert(self.tid, **{key: "private payload"})
            with self.assertRaises(ValueError):
                registry.event(self.tid, "usage", **{key: "private payload"})
        with self.assertRaises(ValueError):
            registry.upsert(self.tid, tokens={"output": "private payload"})
        self.assertIsNone(registry.get(self.tid))
        registry.upsert(self.tid)
        self.assertNotIn("current_tool", registry.get(self.tid))
        with self.assertRaises(ValueError):
            registry.upsert(self.tid, current_tool="Read")
        with self.assertRaises(ValueError):
            registry.upsert("../escape")

    def test_children_use_fix_round_for_only(self):
        bus.create_task("goal child", "s", ["a"], ["x.py"], parent=self.tid)
        fix = bus.create_task("fix", "s", ["a"], ["x.py"], parent="goal",
                              constraints={"fix_round_for": self.tid})
        registry.upsert(self.tid, parent="goal")
        self.assertEqual(registry.get(self.tid)["children"], [fix["id"]])

    def test_codex_registry_entry_is_live_before_exit(self):
        process = mock.Mock(pid=4231, returncode=0)
        def communicate(timeout):
            doc = registry.get(self.tid)
            self.assertEqual((doc["status"], doc["pid"], doc["provider"]), ("running", 4231, "codex"))
            self.assertIn(self.tid, [row["task"] for row in registry.active()])
            return '\n'.join(json.dumps(e) for e in [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}}
            ]), ""
        process.communicate.side_effect = communicate
        def launch(*args, **kwargs):
            self.assertEqual(registry.get(self.tid)["status"], "starting")
            return process
        with mock.patch.object(executor.subprocess, "Popen", side_effect=launch), \
                mock.patch.object(executor, "_thread_head", return_value=None):
            result = executor._run(pool.Pool(), self.task, ["private prompt"], TMP, 30)
        self.assertEqual(result["status"], "done")
        doc = registry.get(self.tid)
        self.assertEqual(doc["thread"], "thread-1")
        self.assertEqual(doc["tokens"], {"input_uncached": 8, "cache_read": 2, "output": 3})
        self.assertEqual([e["kind"] for e in registry.events(self.tid)], ["spawned", "usage", "claimed", "exit"])
        self.assertNotIn("private prompt", registry._path(self.tid).read_text())

    def test_codex_timeout_kills_process(self):
        process = mock.Mock(pid=4232)
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 1), ("", "")]
        with mock.patch.object(executor.subprocess, "Popen", return_value=process):
            result = executor._run(pool.Pool(), self.task, ["prompt"], TMP, 1)
        process.kill.assert_called_once_with()
        self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=1), mock.call()])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(registry.get(self.tid)["status"], "failed")
        self.assertEqual(registry.events(self.tid)[-1]["kind"], "exit")

    def test_tick_reconciles_registry_once_per_tick(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(daemon, "_load_review_cfg"), \
                mock.patch.object(daemon, "sweep_leases"), \
                mock.patch.object(daemon.bus, "read", return_value=[self.task]), \
                mock.patch.object(registry, "reconcile") as reconcile:
            daemon.tick(mock.Mock(), stop)
        reconcile.assert_called_once_with(daemon.alive)
