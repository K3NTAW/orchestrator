import _harness

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import machine


class MachineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.machine_dir = Path(self.directory.name)
        self.machine_patch = mock.patch.object(machine, "MACHINE_DIR", self.machine_dir)
        self.machine_patch.start()
        self.path_patch = mock.patch.object(machine, "LEASES", self.machine_dir / "leases.json")
        self.path_patch.start()
        self.accounts_patch = mock.patch.object(machine, "ACCOUNTS", self.machine_dir / "accounts.json")
        self.accounts_patch.start()
        self.lock_patch = mock.patch.object(machine, "LOCK", self.machine_dir / "machine.lock")
        self.lock_patch.start()

    def tearDown(self):
        self.lock_patch.stop()
        self.accounts_patch.stop()
        self.path_patch.stop()
        self.machine_patch.stop()
        self.directory.cleanup()

    def test_worker_slot_cap_spans_repos(self):
        first = machine.acquire("claude_worker", 2, {"repo": "/one", "task": "a", "pid": os.getpid()})
        second = machine.acquire("claude_worker", 2, {"repo": "/two", "task": "b", "pid": os.getpid()})
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(machine.acquire("claude_worker", 2, {"repo": "/three", "task": "c", "pid": os.getpid()}))
        machine.release(first)
        self.assertEqual(machine.count("claude_worker"), 1)
        dead = machine.acquire("claude_worker", 2, {"repo": "/dead", "task": "d", "pid": 999999})
        self.assertIsNotNone(dead)
        self.assertEqual(machine.count("claude_worker"), 1)

    def test_usage_ledger_sums_across_repos(self):
        start = 1_800_000_000.0
        machine.record_usage("A", 7, repo="/one", now=start)
        machine.record_usage("A", 11, repo="/two", now=start + 60)
        self.assertEqual(machine.usage("A", now=start + 120), {
            "window_tokens": 18, "day_tokens": 18,
            "planner_window_tokens": 0, "planner_day_tokens": 0,
        })
        self.assertEqual(machine.usage("A", now=start + machine.WINDOW_S), {
            "window_tokens": 0, "day_tokens": 18,
            "planner_window_tokens": 0, "planner_day_tokens": 0,
        })
        next_day = start + 24 * 3600
        machine.record_usage("A", 5, repo="/three", now=next_day)
        result = machine.usage("A", now=next_day)
        self.assertEqual(result["window_tokens"], 5)
        self.assertEqual(result["day_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
