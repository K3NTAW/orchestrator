import _harness
"""Worker snapshots and lifecycle integrations, using the shared bus fixture."""
import json
import os
import time
import unittest
from unittest import mock
from orchestrator import bus, worker_registry as registry


class WorkerRegistry(unittest.TestCase):
    def test_cancel_and_steer_event_kinds_and_source_field_whitelisted(self):
        for kind in ("cancel_requested", "cancelled", "steer", "steered"):
            registry.event(self.tid, kind, source="planner", cancel_reason="Plan has changed")
        self.assertEqual([e["kind"] for e in registry.events(self.tid)],
                         ["cancel_requested", "cancelled", "steer", "steered"])
        self.assertEqual(registry.get(self.tid)["source"], "planner")

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


class SteeringEpoch(unittest.TestCase):
    setUp = WorkerRegistry.setUp
    clean_registry = WorkerRegistry.clean_registry
    def test_finish_ignores_stale_epoch_after_steer(self):
        registry.upsert(self.tid, status="running")
        registry.event(self.tid, "steer", epoch=2, status="steering", message_chars=4)
        before = registry._path(self.tid).read_bytes()
        registry.finish(self.tid, "failed", epoch=1)
        self.assertEqual(registry._path(self.tid).read_bytes(), before)
        registry.finish(self.tid, "done", epoch=2)
        self.assertEqual(registry.get(self.tid)["status"], "done")

    def test_upsert_starting_carries_epoch_forward(self):
        self.assertEqual(registry.upsert(self.tid, status="starting")["epoch"], 1)
        registry.event(self.tid, "steer", epoch=3, message_chars=4)
        self.assertEqual(registry.upsert(self.tid, status="starting")["epoch"], 3)
        self.assertEqual(registry.upsert(self.tid, status="starting", epoch=4)["epoch"], 4)
        for fields in ({"epoch": 0}, {"epoch": True}, {"message_chars": 1.5}, {"tools": "Read"}):
            with self.assertRaises(ValueError):
                registry.upsert(self.tid, **fields)
