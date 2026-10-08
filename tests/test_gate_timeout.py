import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _harness  # noqa: F401 - use the suite's single shared ORCH_ROOT
from orchestrator import bus, daemon, decision, failures, gate, merge, planner_runs


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

    def test_settings_defaults_and_override(self):
        self.assertEqual(gate.settings({}), {"timeout_s": 2700, "cleanup_cmd": None,
                                             "cleanup_timeout_s": 300, "post_cmd": None})
        cfg = {"gate": {"timeout_s": 9, "cleanup_cmd": "true", "cleanup_timeout_s": 4, "post_cmd": "true"}}
        self.assertEqual(gate.settings(cfg), {"timeout_s": 9, "cleanup_cmd": "true",
                                              "cleanup_timeout_s": 4, "post_cmd": "true"})
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


if __name__ == "__main__":
    unittest.main()
