import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _harness  # noqa: F401 - use the suite's single shared ORCH_ROOT
from orchestrator import bus, daemon, decision, failures, gate, merge, planner_runs
from orchestrator import machine as gate_machine


class GateTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / ".orchestrator"
        self.state.mkdir()
        self.state_patch = mock.patch.object(gate, "STATE", self.state)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.tmp.cleanup()

    def script(self, body, name="gate.sh"):
        path = self.root / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def task_fixture(self):
        self.enterContext(mock.patch.multiple(bus, STATE=self.state,
                                             TASKS=self.state / "tasks", RUNS=self.state / "runs"))
        self.enterContext(mock.patch.object(planner_runs, "STATE", self.state))
        self.enterContext(mock.patch.object(daemon, "notify"))
        self.enterContext(mock.patch.object(gate.notify, "notify"))
        repo = _harness.scratch_repo(self.root / "repo")
        goal = bus.create_task("goal", "spec", ["works"], ["x.py"], role="triage")
        task = bus.create_task("execute", "spec", ["works"], ["x.py"],
                               role="execute", parent=goal["id"])
        bus.update(task["id"], status="done", worktree=str(repo), pipeline={"gate_reds": 3})
        pool = SimpleNamespace(cfg={"gate": {"timeout_s": 1},
                                    "planner": {"routes": {"enabled": True}}},
                               accounts=[], executors={})
        return task["id"], repo, pool

    def run_daemon_gate(self, pool, script):
        with mock.patch.object(merge, "TESTS_GREEN", script), \
                mock.patch.object(daemon, "stale", return_value=False), \
                mock.patch.object(daemon, "already_merged", return_value=False), \
                mock.patch.object(daemon, "_hold_stale_high", return_value=False) as stale_check, \
                mock.patch.object(daemon.gate, "run_gate", wraps=gate.run_gate) as runner:
            daemon.gate(pool)
        stale_check.assert_called_once()
        runner.assert_called_once()

    def timeout_task(self):
        tid, repo, pool = self.task_fixture()
        count = self.root / "attempts"
        script = self.script(f"echo attempt >> '{count}'\necho waiting\nsleep 60\n")
        self.run_daemon_gate(pool, script)
        self.assertEqual(count.read_text().splitlines(), ["attempt", "attempt"])
        return tid, repo, pool

    def test_daemon_gate_timeout_holds_as_infra_failure(self):
        tid, _, _ = self.timeout_task()
        task = bus.get(tid)
        self.assertEqual((task["status"], task["hold_reason"]), ("held", "gate_timeout"))
        self.assertEqual(task["pipeline"]["infra_failure"], "gate_timeout")
        self.assertEqual(task["pipeline"]["gate_reds"], 3)
        self.assertEqual(task["pipeline"]["gate_timeouts"], 2)
        self.assertTrue(task["pipeline"]["gated_at"])
        self.assertEqual(task["resume_hint"]["gate_timeout_s"], 1)
        self.assertIn("waiting", task["resume_hint"]["output_tail"])

    def test_gate_timeout_files_no_fix_round(self):
        tid, _, pool = self.timeout_task()
        before = bus.read()
        with mock.patch.object(daemon, "failure_kind") as classify, \
                mock.patch.object(failures, "_flaky_rerun_command") as probe, \
                mock.patch.object(failures.gate, "run_bounded") as rerun:
            daemon.auto_fix_round(pool)
        classify.assert_not_called()
        probe.assert_not_called()
        rerun.assert_not_called()
        self.assertEqual(bus.read(), before)
        task = bus.get(tid)
        point = {"goal_id": task["parent"], "kind": "held", "task_id": tid,
                 "payload_key": planner_runs._held_key(task)}
        ctx = planner_runs.build_ctx(point, pool)
        self.assertEqual(ctx["infra_failure_kind"], "gate_timeout")
        self.assertEqual(decision.route(point, ctx).name, "none")

    def test_gate_timeout_ctx_wins_over_cooling_account(self):
        tid, _, pool = self.task_fixture()
        bus.update(tid, status="held", hold_reason="gate_timeout", account="cooling-account",
                   pipeline={"infra_failure": "gate_timeout"})
        pool.accounts = [SimpleNamespace(id="cooling-account", hold_reason="usage limit",
                                         cooling=lambda: True)]
        task = bus.get(tid)
        point = {"goal_id": task["parent"], "kind": "held", "task_id": tid,
                 "payload_key": planner_runs._held_key(task)}
        ctx = planner_runs.build_ctx(point, pool)
        self.assertEqual(ctx["infra_failure_kind"], "gate_timeout")
        self.assertEqual(decision.route(point, ctx).name, "none")

    def test_merge_path_honours_timeout(self):
        tid, repo, pool = self.task_fixture()
        (repo / ".orchestrator").mkdir(exist_ok=True)
        _harness.g("branch", "goal/timeout", cwd=repo, check=True)
        before = _harness.g("rev-parse", "goal/timeout", cwd=repo, check=True).stdout
        _harness.g("checkout", "-qb", f"task/{tid}", cwd=repo, check=True)
        (repo / "x.py").write_text("VALUE = 1\n")
        _harness.g("add", "x.py", cwd=repo, check=True)
        _harness.g("commit", "-qm", "task change", cwd=repo, check=True)
        script = self.script("echo merge-waiting\nsleep 60\n")

        def git(*args, **kwargs):
            kwargs.setdefault("cwd", repo)
            return _harness.g(*args, **kwargs)

        with mock.patch.multiple(merge, ROOT=repo, TESTS_GREEN=script, git=git), \
                mock.patch.object(gate.pool, "config", return_value=pool.cfg):
            result = merge.merge(tid, target="goal/timeout", refresh_repomap=False)
        self.assertEqual(result, {"status": "gate_timeout", "reason": "gate_timeout"})
        self.assertEqual(_harness.g("rev-parse", "goal/timeout", cwd=repo, check=True).stdout, before)
        task = bus.get(tid)
        self.assertEqual((task["status"], task["hold_reason"]), ("held", "gate_timeout"))
        self.assertEqual(task["pipeline"]["infra_failure"], "gate_timeout")
        self.assertEqual(task["pipeline"]["gate_reds"], 3)
        self.assertIn("merge-waiting", task["resume_hint"]["output_tail"])

    def test_flaky_rerun_kills_tree_on_timeout(self):
        tid, repo, _ = self.task_fixture()
        parent_file, child_file = self.root / "parent", self.root / "child"
        script = self.script(f"echo $$ > '{parent_file}'\nsleep 60 &\n"
                             f"echo $! > '{child_file}'\necho hung-test\nwait\n")
        bus.update(tid, status="held", hold_reason="gate_red",
                   resume_hint={"failures": "FAILED tests/test_x.py::test_x"})
        with mock.patch.object(failures, "_flaky_rerun_command", return_value=[str(script)]):
            kind = failures.failure_kind(bus.get(tid), str(repo), rerun_timeout=1)
        self.assertEqual(kind, "code_defect")
        self.assertEqual(bus.get(tid)["resume_hint"]["flaky_runs"], [{
            "ids": ["tests/test_x.py::test_x"], "timed_out": True,
            "timeout_s": 1, "output": "hung-test\n"}])
        self.assert_gone(int(parent_file.read_text()))
        self.assert_gone(int(child_file.read_text()))

    def test_daemon_fast_red_gate_unchanged(self):
        tid, _, pool = self.task_fixture()
        self.run_daemon_gate(pool, self.script("echo assertion-failed >&2\nexit 1\n"))
        task = bus.get(tid)
        self.assertEqual((task["status"], task["hold_reason"]), ("held", "gate_red"))
        self.assertEqual(task["pipeline"]["gate_reds"], 4)
        self.assertNotIn("infra_failure", task["pipeline"])
        self.assertNotIn("gate_timeouts", task["pipeline"])
        self.assertIn("assertion-failed", task["resume_hint"]["failures"])

    @staticmethod
    def assert_gone(pid):
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        with unittest.TestCase().assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_kills_process_tree(self):
        parent_file, child_file = self.root / "parent", self.root / "child"
        script = self.script(f"echo $$ > {parent_file}\nsleep 60 &\necho $! > {child_file}\nsleep 60\n")
        result = gate.run_bounded([str(script)], cwd=self.root, timeout_s=1, kill_grace_s=0.05)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["returncode"])
        self.assert_gone(int(parent_file.read_text()))
        self.assert_gone(int(child_file.read_text()))

    def test_deadline_counts_wall_clock_sleep(self):
        script = self.script("sleep 60\n")
        started = []
        token = gate._ON_START.set(started.append)
        try:
            with mock.patch.object(gate.time, "time", side_effect=[100, 159.95, 161]):
                result = gate.run_bounded([str(script)], cwd=self.root, timeout_s=60,
                                          kill_grace_s=0.05)
        finally:
            gate._ON_START.reset(token)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["returncode"])
        self.assert_gone(started[0])

    def test_killpg_permission_error_is_treated_as_gone(self):
        script = self.script("exec sleep 60\n")
        real_killpg = os.killpg
        for denied_signal in (0, gate.signal.SIGKILL):
            with self.subTest(denied_signal=denied_signal):
                def killpg(pgid, sig):
                    if sig == gate.signal.SIGTERM:
                        return real_killpg(pgid, sig)
                    if sig == denied_signal:
                        raise PermissionError("process group ID reused")
                    return None

                with mock.patch.object(gate.os, "killpg", side_effect=killpg) as signals:
                    result = gate.run_bounded([str(script)], cwd=self.root,
                                              timeout_s=0.1, kill_grace_s=1)
                self.assertTrue(result["timed_out"])
                self.assertIsNone(result["returncode"])
                pid = signals.call_args_list[0].args[0]
                signals.assert_any_call(pid, denied_signal)
                self.assert_gone(pid)

    def test_cleanup_command_runs_on_timeout(self):
        marker = self.root / "cleaned"
        script = self.script("sleep 60\n")
        cfg = {"gate": {"timeout_s": 1, "cleanup_cmd": f"echo x >> {marker}",
                         "cleanup_timeout_s": 1}}
        with mock.patch.object(gate.notify, "notify"):
            gate.run_gate(self.root, script=script, cfg=cfg)
        self.assertEqual(marker.read_text().splitlines(), ["x", "x"])
        marker.unlink()
        with mock.patch.object(gate.notify, "notify"):
            gate.run_gate(self.root, script=script, cfg={"gate": {"timeout_s": 1}})
        self.assertFalse(marker.exists())

    def test_post_cmd_runs_after_green_red_and_timeout(self):
        marker = self.root / "post"
        cfg = {"gate": {"timeout_s": 1, "post_cmd": f"echo x >> {marker}", "cleanup_timeout_s": 1}}
        for code in (0, 3):
            script = self.script(f"exit {code}\n", f"exit-{code}.sh")
            result = gate.run_gate(self.root, script=script, cfg=cfg)
            self.assertEqual(result["returncode"], code)
            self.assertEqual(marker.read_text().splitlines(), ["x"])
            marker.unlink()
        with mock.patch.object(gate.notify, "notify"):
            result = gate.run_gate(self.root, script=self.script("sleep 60\n", "slow.sh"), cfg=cfg)
        self.assertTrue(result["timed_out"])
        self.assertEqual(marker.read_text().splitlines(), ["x", "x"])

    def test_post_cmd_failure_does_not_change_result(self):
        for post_cmd in ("exit 9", "sleep 60"):
            cfg = {"gate": {"post_cmd": post_cmd, "cleanup_timeout_s": 1}}
            for code in (0, 4):
                script = self.script(f"echo out\nexit {code}\n", f"exit-{code}.sh")
                with mock.patch("sys.stderr"):
                    result = gate.run_gate(self.root, script=script, cfg=cfg)
                self.assertEqual((result["returncode"], result["timed_out"], result["attempts"]),
                                 (code, False, 1))
                self.assertIn("out", result["stdout"])
        with mock.patch.object(gate, "run_bounded", wraps=gate.run_bounded) as runner, \
                mock.patch("sys.stderr"):
            def boom(argv, **kw):
                if argv[0] == "bash":
                    raise OSError("no bash")
                return gate._run(argv, input=kw.get("input"), kill_grace_s=gate.KILL_GRACE_S,
                                 **{k: kw[k] for k in ("cwd", "timeout_s", "env") if k in kw})
            runner.side_effect = boom
            result = gate.run_gate(self.root, script=self.script("exit 0\n", "ok.sh"),
                                   cfg={"gate": {"post_cmd": "true"}})
        self.assertEqual(result["returncode"], 0)

    def test_timeout_retries_once(self):
        count = self.root / "count"
        always = self.script(f"echo x >> {count}\nsleep 60\n", "always.sh")
        with mock.patch.object(gate.notify, "notify"):
            result = gate.run_gate(self.root, script=always, cfg={"gate": {"timeout_s": 1}})
        self.assertEqual((result["attempts"], result["timeouts"]), (2, 2))
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["returncode"])
        self.assertEqual(len(count.read_text().splitlines()), 2)

        marker = self.root / "once"
        once = self.script(f"if [ ! -e {marker} ]; then touch {marker}; sleep 60; fi\nexit 0\n", "once.sh")
        with mock.patch.object(gate.notify, "notify"):
            result = gate.run_gate(self.root, script=once, cfg={"gate": {"timeout_s": 1}})
        self.assertEqual((result["returncode"], result["attempts"], result["timeouts"]), (0, 2, 1))
        self.assertFalse(result["timed_out"])

    def test_fast_gate_unaffected(self):
        for code in (0, 7):
            script = self.script(f"echo out; echo err >&2; exit {code}\n", f"fast-{code}.sh")
            with mock.patch.object(gate.notify, "notify") as notify:
                result = gate.run_gate(self.root, script=script, task_id="T/1", cfg={})
            self.assertEqual(result["returncode"], code)
            self.assertEqual(result["attempts"], 1)
            self.assertIn("out", result["stdout"])
            self.assertIn("err", result["stderr"])
            notify.assert_not_called()
            self.assertFalse((self.state / "gates" / "T-1.json").exists())

    def test_run_gate_exports_mode_and_base_env(self):
        script = self.script('echo "mode=${ORCH_GATE_MODE:-unset} base=${ORCH_GATE_BASE:-unset} x=${X:-}"\n'
                             'sleep 0.5; cat "$ORCH_ROOT_GATES"/*.json\n', "env.sh")
        env = {**os.environ, "X": "kept", "ORCH_ROOT_GATES": str(self.state / "gates")}
        result = gate.run_gate(self.root, script=script, task_id="T-env", cfg={}, env=env, mode="task", base="abc123")
        self.assertIn("mode=task base=abc123 x=kept", result["stdout"])
        self.assertIn('"mode": "task"', result["stdout"])
        self.assertIn('"base": "abc123"', result["stdout"])
        self.assertEqual((result["mode"], result["base"]), ("task", "abc123"))
        self.assertNotIn("ORCH_GATE_MODE", env)
        result = gate.run_gate(self.root, script=script, cfg={}, env=env, mode="full")
        self.assertIn("mode=full base=unset", result["stdout"])

    def test_run_gate_without_mode_exports_nothing(self):
        script = self.script('echo "mode=${ORCH_GATE_MODE:-unset} base=${ORCH_GATE_BASE:-unset}"\n', "plain.sh")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_GATE_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            result = gate.run_gate(self.root, script=script, cfg={})
        self.assertIn("mode=unset base=unset", result["stdout"])
        self.assertEqual((result["mode"], result["base"]), (None, None))
        self.assertTrue(gate.settings({})["scoped_task_gates"])
        self.assertFalse(gate.settings({"gate": {"scoped_task_gates": False}})["scoped_task_gates"])

    def test_task_base_merge_base_fallbacks(self):
        repo = _harness.scratch_repo(self.root / "repo")
        git = lambda *a: __import__("subprocess").run(["git", *a], cwd=repo, capture_output=True, text=True,
                                                      check=True).stdout.strip()
        fork = git("rev-parse", "HEAD")
        git("branch", "goal/G")
        git("checkout", "-q", "-b", "task")
        (repo / "t.txt").write_text("t\n")
        git("add", "t.txt")
        git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "task")
        self.assertEqual(gate.task_base(repo, "goal/G"), fork)
        self.assertIsNone(gate.task_base(repo, "goal/missing"))
        self.assertIsNone(gate.task_base(repo, None))
        self.assertIsNone(gate.task_base(self.root / "nowhere", "goal/G"))
        with mock.patch.object(gate.subprocess, "run", side_effect=lambda argv, **k: SimpleNamespace(
                returncode=1 if argv[1] == "merge-base" else 0, stdout="x\n")):
            self.assertEqual(gate.task_base(repo, "goal/G"), "goal/G")

    def test_settings_defaults_and_override(self):
        machine_defaults = {"machine_max_parallel": 1, "heavy": False, "machine_wait_max_s": 6660,
                            "machine_wait_log_s": 300, "machine_ticket_reissue_max": 3}
        self.assertEqual(gate.settings({}), {"timeout_s": 2700, "cleanup_cmd": None,
                                             "cleanup_timeout_s": 300, "post_cmd": None,
                                             "max_parallel": 1, "scoped_task_gates": True, **machine_defaults})
        cfg = {"gate": {"timeout_s": 9, "cleanup_cmd": "true", "cleanup_timeout_s": 4, "post_cmd": "true",
                        "max_parallel": 3}}
        self.assertEqual(gate.settings(cfg), {"timeout_s": 9, "cleanup_cmd": "true",
                                              "cleanup_timeout_s": 4, "post_cmd": "true",
                                              "max_parallel": 3, "scoped_task_gates": True,
                                              **machine_defaults, "machine_wait_max_s": 94})
        for invalid in (0, -1, 1.5, "2", True):
            self.assertEqual(gate.settings({"gate": {"max_parallel": invalid}})["max_parallel"], 1)
        for invalid in (0, -1, 1.5, "1", True):
            self.assertEqual(gate.settings({"gate": {"timeout_s": invalid}})["timeout_s"], 2700)

    def test_running_gates_over_threshold(self):
        gates = self.state / "gates"
        gates.mkdir()
        old = {"task_id": "old", "worktree": "/old", "pid": 1,
               "started_at": 100, "timeout_s": 100}
        new = {**old, "task_id": "new", "started_at": 150}
        (gates / "old.json").write_text(json.dumps(old))
        (gates / "new.json").write_text(json.dumps(new))
        (gates / "bad.json").write_text("{")
        result = gate.running_gates(now=181)
        self.assertEqual([entry["task_id"] for entry in result], ["old"])
        self.assertEqual(result[0]["elapsed_s"], 81)

    # waiting registry entry --------------------------------------------------------------------------------
    def waiting(self, key="T-9", **fields):
        gates = self.state / "gates"
        gates.mkdir(exist_ok=True)
        entry = {"state": "waiting", "task_id": key, "worktree": "/w", "owner_pid": 4242, "owner_start": "S",
                 "waiting_for": "machine_slot", "waiting_since": 1.0, "mode": "task", "base": "abc", **fields}
        path = gates / f"{key}.json"
        path.write_text(json.dumps(entry))
        return path

    def owner(self, alive=True, start="S"):
        return mock.patch.multiple(gate_machine, _pid_alive=mock.Mock(return_value=alive),
                                   process_start=mock.Mock(return_value=start))

    def test_is_waiting_entry_predicate(self):
        self.assertTrue(gate.is_waiting({"state": "waiting"}))
        for entry in ({}, {"state": "running"}, {"pid": 1}, None, [], "waiting"):
            self.assertFalse(gate.is_waiting(entry))
        self.assertEqual(gate.WAITING, "waiting")

    def test_write_waiting_entry_shape_has_no_pid_or_timeout(self):
        with mock.patch.object(gate.time, "time", return_value=123.0):
            path = gate.write_waiting_entry("T-9", task_id="T-9", worktree=Path("/w"), mode="task", base="abc")
        self.assertEqual(path, self.state / "gates" / "T-9.json")
        entry = json.loads(path.read_text())
        owner = gate._owner()
        self.assertEqual(entry, {"state": "waiting", "task_id": "T-9", "worktree": "/w",
                                 "owner_pid": owner["pid"], "owner_start": owner["start"],
                                 "waiting_for": "machine_slot", "waiting_since": 123.0,
                                 "mode": "task", "base": "abc"})
        for key in ("pid", "pgid", "proc_start", "started_at", "timeout_s"):
            self.assertNotIn(key, entry)
        self.assertTrue(gate.is_waiting(entry))

    def test_registry_writes_are_atomic_and_leave_no_tmp(self):
        gates = self.state / "gates"
        seen = []
        real_replace = os.replace

        def replace(src, dst):
            seen.append((Path(src), Path(dst), Path(src).exists()))
            real_replace(src, dst)

        script = self.script("exit 0\n")
        self._machine_tmp()
        with mock.patch.object(gate.os, "replace", side_effect=replace):
            path = gate.write_waiting_entry("T-9", task_id="T-9", worktree="/w", mode=None, base=None)
            result = gate.run_gate(self.root, script=script, task_id="T-8", cfg={"gate": {"timeout_s": 30, "max_parallel": 2}})
        self.assertEqual(result["returncode"], 0)
        # The heavy gate T-8 writes its waiting entry, then register() overwrites it in place.
        self.assertEqual([(src.name, dst.name, existed) for src, dst, existed in seen if dst.parent == gates],
                         [("T-9.tmp", "T-9.json", True), ("T-8.tmp", "T-8.json", True),
                          ("T-8.tmp", "T-8.json", True)])
        self.assertTrue(path.exists())
        self.assertEqual(list(gates.glob("*.tmp")), [])
        self.assertEqual(sorted(p.name for p in gates.iterdir()), ["T-9.json"])

    def test_live_registry_counts_waiting_entry_of_live_owner(self):
        self.waiting()
        with self.owner(), mock.patch.object(gate, "_group_alive") as group:
            self.assertEqual(gate._live_registry_keys(), {"T-9"})
            self.assertEqual(gate.live_slots(), 1)
        group.assert_not_called()
        self.waiting(owner_start=None)
        with self.owner(start="other"):
            self.assertEqual(gate._live_registry_keys(), {"T-9"})

    def test_live_registry_skips_waiting_entry_of_dead_or_reused_owner(self):
        self.waiting()
        with self.owner(alive=False), mock.patch.object(gate, "_group_alive") as group:
            self.assertEqual(gate._live_registry_keys(), set())
        group.assert_not_called()
        with self.owner(start="other"):
            self.assertEqual(gate._live_registry_keys(), set())
        self.waiting(owner_pid="4242")
        with self.owner():
            self.assertEqual(gate._live_registry_keys(), set())

    def test_running_gates_skips_waiting_entry(self):
        self.waiting(waiting_since=0)
        self.assertEqual(gate.running_gates(now=10_000), [])

    def test_reap_orphans_unlinks_waiting_entry_of_dead_owner_without_signal(self):
        path = self.waiting()
        with self.owner(alive=False), mock.patch.object(gate.os, "killpg") as killpg, \
                mock.patch.object(gate, "_stop_group") as stop:
            self.assertEqual(gate.reap_orphans(grace_s=0), [])
        killpg.assert_not_called()
        stop.assert_not_called()
        self.assertFalse(path.exists())

    def test_reap_orphans_unlinks_stale_own_pid_waiting_entry_with_other_start(self):
        path = self.waiting(owner_pid=os.getpid(), owner_start="old-start")
        with mock.patch.object(gate_machine, "process_start", return_value="new-start"), \
                mock.patch.object(gate.os, "killpg") as killpg, mock.patch.object(gate, "_stop_group") as stop:
            self.assertEqual(gate.reap_orphans(grace_s=0), [])
        killpg.assert_not_called()
        stop.assert_not_called()
        self.assertFalse(path.exists())

    def test_reap_orphans_keeps_waiting_entry_of_live_owner(self):
        path = self.waiting()
        with self.owner(), mock.patch.object(gate.os, "killpg") as killpg, \
                mock.patch.object(gate, "_stop_group") as stop:
            self.assertEqual(gate.reap_orphans(grace_s=0), [])
        killpg.assert_not_called()
        stop.assert_not_called()
        self.assertTrue(path.exists())

    def test_terminate_own_unlinks_own_waiting_entry(self):
        own = self.waiting("T-1", owner_pid=os.getpid())
        other = self.waiting("T-2")
        self.assertEqual(gate.terminate_own(grace_s=0), [])
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())


    # machine heavy-gate slot ---------------------------------------------------------------------------------
    def _machine_tmp(self):
        """A fresh machine dir per test; the suite-wide harness machine dir is never asserted on."""
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.machine_dir = Path(tmp)
        self.enterContext(mock.patch.multiple(gate_machine, MACHINE_DIR=self.machine_dir,
                                              LEASES=self.machine_dir / "leases.json",
                                              LOCK=self.machine_dir / "machine.lock",
                                              REPOS=self.machine_dir / "repos.json"))
        self.enterContext(mock.patch.object(gate, "SLOT_POLL_S", 0.01))
        self.enterContext(mock.patch.object(gate.notify, "notify"))
        self.addCleanup(self._machine_clean)

    def _machine_clean(self):
        self.assertEqual(gate_machine.holders("heavy_gate"), [])
        waiters = self.machine_dir / "waiters-heavy_gate.json"
        self.assertTrue(not waiters.exists() or json.loads(waiters.read_text()) == [])

    def plant(self, pid=None, repo="/other", task="X"):
        lease = gate_machine.acquire("heavy_gate", 1, {"repo": repo, "task": task, "pid": pid or os.getpid()})
        self.assertIsNotNone(lease)
        return lease

    def waiters(self):
        path = self.machine_dir / "waiters-heavy_gate.json"
        return json.loads(path.read_text()) if path.exists() else []

    @staticmethod
    def until(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        raise AssertionError("condition not reached")

    def gate_thread(self, **kwargs):
        out = {}

        def run():
            try:
                out["result"] = gate.run_gate(self.root, **kwargs)
            except BaseException as e:  # surfaced by the test through out["error"]
                out["error"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, out

    def cfg(self, **gate_cfg):
        return {"gate": {"timeout_s": 30, "machine_wait_max_s": 30, "machine_wait_log_s": 1, **gate_cfg}}

    def test_heavy_gate_waits_for_machine_slot_then_runs(self):
        self._machine_tmp()
        marker = self.root / "ran"
        lease = self.plant()
        thread, out = self.gate_thread(script=self.script(f"touch {marker}\n"), task_id="T-h", cfg=self.cfg(),
                                       mode="full")
        self.until(lambda: len(self.waiters()) == 1)
        time.sleep(0.1)
        self.assertFalse(marker.exists())
        gate_machine.release(lease)
        thread.join(10)
        self.assertEqual(out["result"]["returncode"], 0)
        self.assertTrue(marker.exists())
        self.assertGreater(out["result"]["machine_wait_s"], 0.1)

    def test_machine_slot_wait_does_not_count_toward_timeout(self):
        self._machine_tmp()
        lease = self.plant()
        timer = threading.Timer(1.3, gate_machine.release, [lease])
        timer.start()
        self.addCleanup(timer.cancel)
        result = gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-w",
                               cfg=self.cfg(timeout_s=1), mode="full")
        self.assertEqual((result["returncode"], result["attempts"], result["timeouts"]), (0, 1, 0))
        self.assertGreaterEqual(result["machine_wait_s"], 1.0)
        self.assertFalse(result["machine_wait_exceeded"])

    def test_light_task_gate_skips_machine_slot(self):
        self._machine_tmp()
        lease = self.plant()
        with mock.patch.object(gate_machine, "acquire") as acquire, \
                mock.patch.object(gate_machine, "wait_ticket") as ticket:
            result = gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-l", cfg=self.cfg(),
                                   mode="task", base="abc")
        acquire.assert_not_called()
        ticket.assert_not_called()
        self.assertEqual((result["returncode"], result["machine_wait_s"], result["machine_wait_exceeded"]),
                         (0, 0.0, False))
        gate_machine.release(lease)

    def test_mode_none_gate_is_heavy(self):
        self._machine_tmp()
        light = {"heavy": False}
        self.assertTrue(gate._heavy(None, light))
        self.assertTrue(gate._heavy("full", light))
        self.assertFalse(gate._heavy("task", light))
        self.assertTrue(gate._heavy("task", {"heavy": True}))
        with mock.patch.object(gate_machine, "acquire", wraps=gate_machine.acquire) as acquire:
            result = gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-n", cfg=self.cfg())
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(acquire.call_count, 1)
        with mock.patch.object(gate_machine, "acquire", wraps=gate_machine.acquire) as acquire:
            gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-n", cfg=self.cfg(heavy=True),
                          mode="task")
        self.assertEqual(acquire.call_count, 1)

    def test_machine_cap_zero_takes_no_lease(self):
        self._machine_tmp()
        lease = self.plant()
        with mock.patch.object(gate_machine, "acquire") as acquire, \
                mock.patch.object(gate_machine, "wait_ticket") as ticket:
            result = gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-0",
                                   cfg=self.cfg(machine_max_parallel=0), mode="full")
        acquire.assert_not_called()
        ticket.assert_not_called()
        self.assertEqual((result["returncode"], result["machine_wait_s"]), (0, 0.0))
        self.assertFalse((self.state / "gates" / "T-0.json").exists())
        gate_machine.release(lease)

    def test_invalid_machine_settings_use_defaults(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            opts = gate.settings({"gate": {"timeout_s": 10, "cleanup_timeout_s": 2, "machine_max_parallel": True,
                                           "machine_wait_max_s": -5, "heavy": "yes", "machine_wait_log_s": 0,
                                           "machine_ticket_reissue_max": 1.5}})
        self.assertEqual((opts["machine_max_parallel"], opts["machine_wait_max_s"], opts["heavy"],
                          opts["machine_wait_log_s"], opts["machine_ticket_reissue_max"]), (1, 88, False, 300, 3))
        lines = err.getvalue().splitlines()
        self.assertIn("[gate] invalid [gate].machine_max_parallel=True, using default 1", lines)
        self.assertIn("[gate] invalid [gate].machine_wait_max_s=-5, using default 88", lines)
        self.assertIn("[gate] invalid [gate].heavy='yes', using default False", lines)
        self.assertEqual(len(lines), 5)
        with mock.patch("sys.stderr", io.StringIO()) as quiet:
            self.assertEqual(gate.settings({"gate": {"machine_max_parallel": 0}})["machine_max_parallel"], 0)
        self.assertEqual(quiet.getvalue(), "")

    def test_default_wait_max_covers_one_holder(self):
        self.assertEqual(gate.settings({"gate": {"timeout_s": 10, "cleanup_timeout_s": 2}})["machine_wait_max_s"],
                         88)
        self.assertEqual(gate.settings({"gate": {"timeout_s": 4500, "cleanup_timeout_s": 300}})
                         ["machine_wait_max_s"], 10260)
        self.assertEqual(gate.settings({"gate": {"timeout_s": 10, "cleanup_timeout_s": 2,
                                                 "machine_wait_max_s": 5}})["machine_wait_max_s"], 5)

    def test_waiting_gate_registry_entry_names_machine_slot(self):
        self._machine_tmp()
        lease = self.plant()
        path = self.state / "gates" / "T-q.json"
        thread, out = self.gate_thread(script=self.script("exit 0\n"), task_id="T-q", cfg=self.cfg(),
                                       mode="full", base="abc")
        self.until(path.exists)
        entry = json.loads(path.read_text())
        self.assertTrue(gate.is_waiting(entry))
        self.assertEqual((entry["waiting_for"], entry["owner_pid"], entry["task_id"], entry["mode"], entry["base"]),
                         ("machine_slot", os.getpid(), "T-q", "full", "abc"))
        gate_machine.release(lease)
        thread.join(10)
        self.assertEqual(out["result"]["returncode"], 0)
        self.assertFalse(path.exists())

    def test_register_overwrites_waiting_entry_in_place(self):
        self._machine_tmp()
        path = self.state / "gates" / "T-r.json"
        seen = []
        real_acquire = gate_machine.acquire

        def acquire(*args, **kwargs):
            lease = real_acquire(*args, **kwargs)
            if lease is not None:
                seen.append(gate.is_waiting(json.loads(path.read_text())))
            return lease

        script = self.script(f"cat '{path}'\n")
        with mock.patch.object(gate_machine, "acquire", side_effect=acquire):
            result = gate.run_gate(self.root, script=script, task_id="T-r", cfg=self.cfg(), mode="full")
        self.assertEqual(seen, [True])
        running = json.loads(result["stdout"])
        self.assertFalse(gate.is_waiting(running))
        self.assertEqual(running["task_id"], "T-r")
        self.assertIn("pid", running)
        self.assertFalse(path.exists())

    def test_lease_released_when_run_gate_raises(self):
        self._machine_tmp()
        with mock.patch.object(gate, "run_bounded", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-x", cfg=self.cfg(),
                              mode="full")
        self.assertEqual(gate_machine.holders("heavy_gate"), [])
        self.assertTrue(gate.try_acquire_slot("T-x", self.cfg()))
        gate.release_slot("T-x")

    def test_cleanup_when_register_raises(self):
        self._machine_tmp()
        real_write = gate._write_registry

        def write(path, entry):
            if not gate.is_waiting(entry):
                raise OSError("disk full")
            return real_write(path, entry)

        with mock.patch.object(gate, "_write_registry", side_effect=write):
            with self.assertRaises(OSError):
                gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-g", cfg=self.cfg(),
                              mode="full")
        self.assertEqual(self.waiters(), [])
        self.assertEqual(gate_machine.holders("heavy_gate"), [])
        self.assertFalse((self.state / "gates" / "T-g.json").exists())

    def test_stale_lease_with_dead_pid_swept_while_queued(self):
        self._machine_tmp()
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.kill)
        self.plant(pid=holder.pid)
        thread, out = self.gate_thread(script=self.script("exit 0\n"), task_id="T-s", cfg=self.cfg(), mode="full")
        self.until(lambda: len(self.waiters()) == 1)
        holder.kill()
        holder.wait()
        thread.join(10)
        self.assertEqual(out["result"]["returncode"], 0)
        self.assertGreater(out["result"]["machine_wait_s"], 0)

    def test_cap_shared_across_two_state_dirs(self):
        self._machine_tmp()
        other = self.root / "other" / ".orchestrator"
        other.mkdir(parents=True)
        go = self.root / "go"
        first, first_out = self.gate_thread(script=self.script(f"while [ ! -e {go} ]; do sleep 0.02; done\n", "a.sh"),
                                            task_id="T-a", cfg=self.cfg(), mode="full")
        self.until(lambda: [h["repo"] for h in gate_machine.holders("heavy_gate")] == [str(self.root)])
        with mock.patch.object(gate, "STATE", other):
            second, second_out = self.gate_thread(script=self.script("exit 0\n", "b.sh"), task_id="T-b",
                                                  cfg=self.cfg(), mode="full")
            self.until((other / "gates" / "T-b.json").exists)
            time.sleep(0.1)
            self.assertNotIn("result", second_out)
            self.assertEqual(len(gate_machine.holders("heavy_gate")), 1)
            go.touch()
            first.join(10)
            second.join(10)
        self.assertEqual(first_out["result"]["returncode"], 0)
        self.assertEqual(second_out["result"]["returncode"], 0)
        self.assertGreater(second_out["result"]["machine_wait_s"], 0.1)

    def test_timeout_retry_keeps_lease_without_rewait(self):
        self._machine_tmp()
        with mock.patch.object(gate_machine, "acquire", wraps=gate_machine.acquire) as acquire, \
                mock.patch.object(gate_machine, "wait_ticket", wraps=gate_machine.wait_ticket) as ticket, \
                mock.patch.object(gate_machine, "release", wraps=gate_machine.release) as release:
            result = gate.run_gate(self.root, script=self.script("sleep 60\n"), task_id="T-t",
                                   cfg=self.cfg(timeout_s=1), mode="full")
        self.assertEqual((result["attempts"], result["timeouts"], result["timed_out"]), (2, 2, True))
        self.assertFalse(result["machine_wait_exceeded"])
        self.assertEqual((acquire.call_count, ticket.call_count, release.call_count), (1, 1, 1))

    def test_acquire_error_returns_returncode_one(self):
        self._machine_tmp()
        with mock.patch.object(gate_machine, "acquire", side_effect=OSError("disk")):
            result = gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-e", cfg=self.cfg(),
                                   mode="full")
        self.assertEqual(result["returncode"], 1)
        self.assertTrue(bool(result["returncode"]))
        self.assertEqual(result["error"], "machine slot: OSError('disk')")
        self.assertEqual((result["stderr"], result["timed_out"], result["attempts"], result["timeouts"],
                          result["machine_wait_exceeded"], result["mode"]),
                         (result["error"], False, 0, 0, False, "full"))
        self.assertFalse((self.state / "gates" / "T-e.json").exists())
        with mock.patch.object(gate_machine, "wait_ticket", side_effect=ValueError("bad json")):
            result = gate.run_gate(self.root, script=self.script("exit 0\n"), task_id="T-e", cfg=self.cfg(),
                                   mode="full")
        self.assertEqual(result["returncode"], 1)
        self.assertTrue(gate.try_acquire_slot("T-e", self.cfg()))
        gate.release_slot("T-e")

    def exceeded(self, task_id):
        lease = self.plant()
        try:
            with mock.patch("sys.stderr", io.StringIO()):
                return gate.run_gate(self.root, script=self.script("exit 0\n"), task_id=task_id,
                                     cfg=self.cfg(machine_wait_max_s=1), mode="full")
        finally:
            gate_machine.release(lease)

    def test_wait_exceeding_max_returns_timeout_shape(self):
        self._machine_tmp()
        result = self.exceeded("T-m")
        self.assertIsNone(result["returncode"])
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["machine_wait_exceeded"])
        self.assertEqual((result["attempts"], result["timeouts"], result["stdout"]), (0, 0, ""))
        self.assertRegex(result["stderr"], r"^machine slot wait exceeded \d+s$")
        self.assertGreater(result["machine_wait_s"], 1)
        self.assertFalse((self.state / "gates" / "T-m.json").exists())

    def test_wait_exceeded_releases_repo_slot(self):
        self._machine_tmp()
        self.assertTrue(self.exceeded("T-k")["machine_wait_exceeded"])
        self.assertTrue(gate.try_acquire_slot("T-k", self.cfg()))
        gate.release_slot("T-k")

    def clear_waiters(self, times):
        waiters = self.machine_dir / "waiters-heavy_gate.json"
        seen = set()
        for _ in range(times):
            self.until(lambda: any(w["ticket"] not in seen for w in self.waiters()))
            seen.update(w["ticket"] for w in self.waiters())
            waiters.unlink()

    def test_ticket_vanished_is_reissued(self):
        self._machine_tmp()
        lease = self.plant()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            thread, out = self.gate_thread(script=self.script("exit 0\n"), task_id="T-v", cfg=self.cfg(),
                                           mode="full")
            self.clear_waiters(1)
            self.until(lambda: len(self.waiters()) == 1)
            gate_machine.release(lease)
            thread.join(10)
        self.assertEqual(out["result"]["returncode"], 0)
        self.assertIn("[gate] task_id=T-v machine slot ticket vanished, re-queued at the tail (1/3)",
                      err.getvalue())
        self.assertIn("[gate] task_id=T-v waiting for machine heavy-gate slot (cap=1, holders=[('/other', 'X', ",
                      err.getvalue())

    def test_ticket_reissue_bounded_and_clock_not_reset(self):
        self._machine_tmp()
        lease = self.plant()
        polls = []
        real_acquire = gate_machine.acquire

        def acquire(*args, **kwargs):
            polls.append(time.monotonic())
            return real_acquire(*args, **kwargs)

        with mock.patch.object(gate_machine, "acquire", side_effect=acquire), mock.patch("sys.stderr", io.StringIO()):
            thread, out = self.gate_thread(script=self.script("exit 0\n"), task_id="T-b",
                                           cfg=self.cfg(machine_ticket_reissue_max=1), mode="full")
            time.sleep(0.2)
            self.clear_waiters(2)
            thread.join(10)
        gate_machine.release(lease)
        result = out["result"]
        self.assertEqual((result["returncode"], result["timed_out"], result["machine_wait_exceeded"]),
                         (None, True, True))
        self.assertEqual(result["stderr"], "machine slot ticket vanished 2 times")
        self.assertGreaterEqual(result["machine_wait_s"], polls[-1] - polls[0])
        self.assertGreater(result["machine_wait_s"], 0.2)

    def test_unlink_waiting_keeps_foreign_running_entry(self):
        gates = self.state / "gates"
        gates.mkdir()
        running = gates / "T-1.json"
        running.write_text(json.dumps({"pid": 1, "owner_pid": os.getpid()}))
        foreign = self.waiting("T-2")
        own = self.waiting("T-3", owner_pid=os.getpid())
        (gates / "T-4.json").write_text("{")
        for key in ("T-1", "T-2", "T-3", "T-4", "T-missing"):
            gate._unlink_waiting(key)
        self.assertTrue(running.exists())
        self.assertTrue(foreign.exists())
        self.assertFalse(own.exists())
        self.assertTrue((gates / "T-4.json").exists())

    def test_two_threads_same_process_second_waits_under_cap_one(self):
        self._machine_tmp()
        cfg = self.cfg(max_parallel=2)
        peak = []
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                peak.append(len(gate_machine.holders("heavy_gate")))
                time.sleep(0.01)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        threads = [self.gate_thread(script=self.script("sleep 0.4\n", f"{n}.sh"), task_id=f"T-p{n}", cfg=cfg,
                                    mode="full") for n in (1, 2)]
        for thread, _ in threads:
            thread.join(10)
        stop.set()
        watcher.join(5)
        results = [out["result"] for _, out in threads]
        self.assertEqual([r["returncode"] for r in results], [0, 0])
        self.assertEqual(max(peak), 1)
        self.assertGreater(max(r["machine_wait_s"] for r in results), 0.2)

    def test_result_carries_machine_wait_fields_on_every_path(self):
        self._machine_tmp()
        ok = self.script("exit 0\n")
        results = [
            gate.run_gate(self.root, script=ok, task_id="T-f", cfg=self.cfg(), mode="task"),
            gate.run_gate(self.root, script=ok, task_id="T-f", cfg=self.cfg(machine_max_parallel=0)),
            gate.run_gate(self.root, script=self.script("exit 3\n", "red.sh"), task_id="T-f", cfg=self.cfg(),
                          mode="full"),
            gate.run_gate(self.root, script=self.script("sleep 60\n", "slow.sh"), task_id="T-f",
                          cfg=self.cfg(timeout_s=1), mode="full"),
            self.exceeded("T-f"),
        ]
        with mock.patch.object(gate_machine, "acquire", side_effect=OSError("x")):
            results.append(gate.run_gate(self.root, script=ok, task_id="T-f", cfg=self.cfg(), mode="full"))
        for result in results:
            self.assertIsInstance(result["machine_wait_s"], float)
            self.assertIsInstance(result["machine_wait_exceeded"], bool)
        self.assertEqual([r["machine_wait_exceeded"] for r in results], [False] * 4 + [True, False])
        self.assertEqual((results[0]["machine_wait_s"], results[1]["machine_wait_s"]), (0.0, 0.0))

if __name__ == "__main__":
    unittest.main()
