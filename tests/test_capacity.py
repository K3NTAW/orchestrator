import _harness
"""Capacity snapshots and deterministic admission."""

import copy
import sys
import time
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO  # noqa: F401
from orchestrator import capacity, executor as executor_module
from orchestrator.pool import Account, Executor, Pool, WINDOW_S


class FakePool:
    def __init__(self, executors, accounts=None, workers=4, cap=100):
        self.executors = {executor.id: executor for executor in executors}
        self.accounts = accounts or []
        self.cap = cap
        self.cfg = {"window_cap_tokens": cap,
                    "limits": {"max_parallel_claude_workers": workers}}


def executor(executor_id="fast", **changes):
    values = {"id": executor_id, "provider": "codex", "model": executor_id,
              "roles": ["execute"], "complexity_min": 1, "complexity_max": 10,
              "max_parallel": 1, "daily_budget_tasks": 5, "weight": 1.0}
    values.update(changes)
    return Executor(**values)


def task(task_id, complexity=3):
    return {"id": task_id, "complexity": complexity}


class Capacity(unittest.TestCase):
    def test_snapshot_fallback_flag_and_pure_claude_free(self):
        now = 1000
        codex = executor(enabled=False, cooldown_until=now + 60)
        sonnet = executor("claude:sonnet", provider="claude", max_parallel=4,
                          running=1, cooldown_until=now + 60)
        opus = executor("claude:opus", provider="claude", max_parallel=2,
                        running=1, cooldown_until=now + 60)
        pool = FakePool([codex, sonnet, opus], workers=3)
        before = copy.deepcopy(pool.__dict__)
        with patch.object(Pool, "claude_has_headroom", side_effect=AssertionError("headroom read")), \
                patch.object(Pool, "pick", side_effect=AssertionError("account selection")), \
                patch.object(capacity.time, "time", side_effect=AssertionError("clock read")):
            snap = capacity.snapshot(pool, now=now)
            self.assertFalse(snap["fallback"])
            self.assertFalse(executor_module.fallback_mode(pool, now=now))
            self.assertEqual(snap["executors"][sonnet.id]["free"], 3)
            self.assertEqual(snap["executors"][opus.id]["free"], 1)
            self.assertEqual(snap["claude_free_total"], 3)
            self.assertEqual(pool.__dict__, before)
            with patch.object(executor_module, "fallback_mode", return_value=True) as predicate:
                self.assertTrue(capacity.snapshot(pool, now=now)["fallback"])
                predicate.assert_called_once_with(pool, now=now)
            codex.enabled = True
            pool.cfg["codex"] = {"on_exhausted": "fallback_claude"}
            self.assertTrue(executor_module.fallback_mode(pool, now=now))
            self.assertTrue(capacity.snapshot(pool, now=now)["fallback"])
            for running, inflight, expected in ((0, 0, 2), (1, 1, 1), (3, 1, 0)):
                snap = capacity.snapshot(pool, running, inflight, now=now,
                                         claude_free={sonnet.id: 2, opus.id: 0})
                self.assertEqual(snap["executors"][sonnet.id]["free"], 2)
                self.assertEqual(snap["executors"][opus.id]["free"], 0)
                self.assertEqual(snap["claude_workers_free"], max(0, 3 - running - inflight))
                self.assertEqual(snap["claude_free_total"], expected)
                self.assertEqual(snap["executors"][codex.id]["free"], 1)
            del pool.executors[codex.id]
            snap = capacity.snapshot(pool, now=now, claude_free={sonnet.id: -2})
            self.assertFalse(snap["fallback"])
            self.assertEqual(snap["executors"][sonnet.id]["free"], 0)
            self.assertEqual(snap["executors"][opus.id]["free"], 0)
            self.assertEqual(snap["claude_free_total"], 0)
            self.assertEqual(capacity._reason(task("T"), snap, {sonnet.id: 0, opus.id: 0}), "cooldown")

    def test_snapshot_is_read_only_across_day_and_window_rollover(self):
        now = time.time()
        ex = executor(day="yesterday", day_tasks=5)
        account = Account("A", "/tmp/a", [], window_tokens=70,
                          planner_window_tokens=20, window_started=now - WINDOW_S - 1)
        pool = FakePool([ex], [account])
        before = copy.deepcopy(pool.__dict__)
        result = capacity.snapshot(pool, now=now)
        self.assertEqual(result["executors"]["fast"]["day_tasks_left"], 5)
        self.assertEqual(result["accounts"]["A"]["utilization"], 0.0)
        self.assertEqual(pool.__dict__, before)

    def test_unlimited_daily_budget_reports_none(self):
        today = datetime.fromtimestamp(time.time()).astimezone().date().isoformat()
        pool = FakePool([executor(daily_budget_tasks=0, day_tasks=7, day=today)])
        result = capacity.snapshot(pool)
        self.assertIsNone(result["executors"]["fast"]["day_tasks_left"])
        self.assertEqual(capacity.eligible_executors_from_snapshot(task("T"), result), ["fast"])

    def test_executor_full_defers_with_executor_capacity_reason(self):
        snap = capacity.snapshot(FakePool([executor(running=1)]))
        result = capacity.admit(["T"], {"T": task("T")}, snap, {})
        self.assertEqual(result["deferred"], {"T": "executor_capacity"})

    def test_quota_cooling_executor_is_not_eligible(self):
        snap = capacity.snapshot(FakePool([executor(cooldown_until=time.time() + 60)]))
        self.assertEqual(capacity.eligible_executors_from_snapshot(task("T", 9), snap), [])
        result = capacity.admit(["T"], {"T": task("T", 9)}, snap, {})
        self.assertEqual(result["deferred"], {"T": "cooldown"})

    def test_account_fallback_uses_claude_worker_headroom(self):
        pool = FakePool([executor(cooldown_until=time.time() + 60)], workers=3)
        pool.cfg["codex"] = {"on_exhausted": "fallback_claude"}
        snap = capacity.snapshot(pool, running_claude=1, inflight_claude=1)
        tasks = {name: task(name) for name in ("A", "B")}
        result = capacity.admit(["A", "B"], tasks, snap, {})
        self.assertEqual(result["admit"], ["A"])
        self.assertEqual(result["assignments"], {"A": "claude"})
        self.assertEqual(result["deferred"], {"B": "account_capacity"})

    def test_imminent_critical_task_reserves_last_slot(self):
        only = executor("only", complexity_max=10, weight=1)
        alternative = executor("alternative", complexity_max=5, weight=2)
        snap = capacity.snapshot(FakePool([only, alternative]))
        tasks = {"critical": task("critical", 9), "first": task("first", 3),
                 "second": task("second", 9)}
        priorities = {"critical": {"critical_path_s": 100},
                      "first": {"critical_path_s": 10}, "second": {"critical_path_s": 10}}
        result = capacity.admit(["first", "second"], tasks, snap, priorities,
                                imminent=["critical"])
        self.assertEqual(result["assignments"], {"first": "alternative"})
        self.assertEqual(result["deferred"], {"second": "reserved_for_critical:critical"})

    def test_idle_capacity_admits_everything_and_no_unnecessary_reservation(self):
        pool = FakePool([executor("a", max_parallel=2), executor("b", max_parallel=2)])
        snap = capacity.snapshot(pool)
        tasks = {name: task(name) for name in ("A", "B", "C")}
        result = capacity.admit(list(tasks), tasks, snap, {}, imminent=None)
        self.assertEqual(result["admit"], ["A", "B", "C"])
        self.assertEqual(result["deferred"], {})
        self.assertFalse(any(reason.startswith("reserved_for_critical")
                             for reason in result["reasons"].values()))


if __name__ == "__main__":
    unittest.main()
