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
        machine._START_CACHE.clear()

    def tearDown(self):
        machine._START_CACHE.clear()
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

    def _lease(self, kind, holder):
        leases = {"L1": {"kind": kind, "holder": holder}}
        (self.machine_dir / "leases.json").write_text(__import__("json").dumps(leases))

    def test_sweep_drops_lease_with_mismatched_process_start(self):
        self._lease("heavy_gate", {"pid": os.getpid(), "proc_start": "old start"})
        with mock.patch.object(machine, "process_start", return_value="new start"):
            self.assertEqual(machine.holders("heavy_gate"), [])
            self.assertEqual(machine.count("heavy_gate"), 0)

    def test_sweep_keeps_lease_when_process_start_is_none(self):
        self._lease("heavy_gate", {"pid": os.getpid(), "proc_start": "old start"})
        with mock.patch.object(machine, "process_start", return_value=None):
            self.assertEqual(machine.count("heavy_gate"), 1)

    def test_sweep_keeps_lease_without_proc_start_field(self):
        self._lease("heavy_gate", {"pid": os.getpid()})
        with mock.patch.object(machine, "process_start", return_value="other") as start:
            self.assertEqual(machine.count("heavy_gate"), 1)
        start.assert_not_called()

    def test_holders_lists_heavy_gate_holder_dicts(self):
        with mock.patch.object(machine, "process_start", return_value="Mon Oct 12 08:00:00 2026"), \
                mock.patch.object(machine.time, "time", return_value=1234.5):
            lease = machine.acquire("heavy_gate", 2, {"pid": os.getpid(), "proc_start": "Mon Oct 12 08:00:00 2026",
                                                      "task": "T-1"})
            machine.acquire("other", 2, {"pid": os.getpid()})
            found = machine.holders("heavy_gate")
        self.assertIsNotNone(lease)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["pid"], os.getpid())
        self.assertEqual(found[0]["since"], 1234.5)
        self.assertEqual(found[0]["proc_start"], "Mon Oct 12 08:00:00 2026")

    def test_acquire_with_ticket_is_fifo(self):
        holder = {"pid": os.getpid()}
        older = machine.wait_ticket("heavy_gate", holder)
        newer = machine.wait_ticket("heavy_gate", holder)
        self.assertIsNone(machine.acquire("heavy_gate", 1, holder, ticket=newer))
        lease = machine.acquire("heavy_gate", 1, holder, ticket=older)
        self.assertIsNotNone(lease)
        self.assertIsNone(machine.acquire("heavy_gate", 1, holder, ticket=newer))
        machine.release(lease)
        self.assertIsNotNone(machine.acquire("heavy_gate", 1, holder, ticket=newer))

    def test_dead_waiter_ticket_is_swept(self):
        dead = machine.wait_ticket("heavy_gate", {"pid": 999999})
        live = machine.wait_ticket("heavy_gate", {"pid": os.getpid()})
        alive = lambda pid: pid == os.getpid()
        with mock.patch.object(machine, "_pid_alive", side_effect=alive):
            self.assertIsNotNone(machine.acquire("heavy_gate", 1, {"pid": os.getpid()}, ticket=live))
            self.assertIsNone(machine.acquire("heavy_gate", 5, {"pid": os.getpid()}, ticket=dead))

    def test_drop_ticket_is_idempotent(self):
        first = machine.wait_ticket("heavy_gate", {"pid": os.getpid()})
        second = machine.wait_ticket("heavy_gate", {"pid": os.getpid()})
        machine.drop_ticket("heavy_gate", first)
        machine.drop_ticket("heavy_gate", first)
        machine.drop_ticket("heavy_gate", "unknown")
        self.assertIsNotNone(machine.acquire("heavy_gate", 1, {"pid": os.getpid()}, ticket=second))

    def test_process_start_cached_for_five_seconds(self):
        self._lease("heavy_gate", {"pid": os.getpid(), "proc_start": "start"})
        with mock.patch.object(machine, "process_start", return_value="start") as start, \
                mock.patch.object(machine.time, "time", side_effect=[100.0, 104.0, 106.0]):
            machine.count("heavy_gate")
            machine.count("heavy_gate")
            self.assertEqual(start.call_count, 1)
            machine.count("heavy_gate")
            self.assertEqual(start.call_count, 2)

    def test_acquire_never_calls_process_start(self):
        self._lease("heavy_gate", {"pid": os.getpid(), "proc_start": "start"})
        holder = {"pid": os.getpid(), "proc_start": "mine"}
        with mock.patch.object(machine, "process_start", return_value="start") as start:
            first = machine.acquire("heavy_gate", 3, holder)
            ticket = machine.wait_ticket("heavy_gate", {"pid": os.getpid()})
            start.reset_mock()
            second = machine.acquire("heavy_gate", 3, holder, ticket=ticket)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
