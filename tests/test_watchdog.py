import _harness

import fcntl
import inspect
import io
import json
import os
import re
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from orchestrator import claude_cli, daemon, goals, machine, planner_shadow, watchdog

PKG = Path(__file__).resolve().parents[1] / "orchestrator"
NOW = 1_800_000_000.0


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mdir = root / "machine"
        self.mdir.mkdir()
        for name, value in (("MACHINE_DIR", self.mdir), ("LEASES", self.mdir / "leases.json"),
                            ("ACCOUNTS", self.mdir / "accounts.json"), ("LOCK", self.mdir / "machine.lock"),
                            ("REPOS", self.mdir / "repos.json")):
            p = mock.patch.object(machine, name, value); p.start(); self.addCleanup(p.stop)
        for name, value in (("STATE_PATH", self.mdir / "watchdog-state.json"),
                            ("LOCK_PATH", self.mdir / "watchdog.lock"),
                            ("CONFIG", root / "pool.toml")):
            p = mock.patch.object(watchdog, name, value); p.start(); self.addCleanup(p.stop)
        self.root = root
        self.sent = []
        self.started = []

    # helpers ------------------------------------------------------------------------------------------------
    def repo(self, name="r"):
        repo = self.root / name
        (repo / ".orchestrator" / "tasks").mkdir(parents=True)
        return repo

    def task(self, repo, tid, mtime=NOW, **fields):
        t = {"id": tid, "status": "queued", "role": "execute", "depends_on": [], "constraints": {}, "pipeline": {},
             **fields}
        path = repo / ".orchestrator" / "tasks" / f"{tid}.json"
        path.write_text(json.dumps(t))
        os.utime(path, (mtime, mtime))
        return path

    def run_once(self, now=NOW, alive=False):
        with mock.patch.object(watchdog, "daemon_alive", return_value=alive):
            return watchdog.run_once(now=now, start=lambda r: self.started.append(str(r)) or "log",
                                     confirm=lambda r: True, send=self.sent.append)

    # resolver -----------------------------------------------------------------------------------------------
    def fake_bin(self, name):
        path = self.root / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_claude_resolved_without_path_entry(self):
        fake = self.fake_bin("claude")
        with mock.patch.object(claude_cli.shutil, "which", return_value=None), \
                mock.patch.object(claude_cli, "CANDIDATES", ("/nonexistent/claude", fake)):
            self.assertEqual(claude_cli.resolve(), fake)
        with mock.patch.object(claude_cli.shutil, "which", return_value=None), \
                mock.patch.object(claude_cli, "CANDIDATES", ("/nonexistent/claude",)):
            self.assertIsNone(claude_cli.resolve())
            self.assertIn("/nonexistent/claude", claude_cli.missing_reason())

    def test_uv_resolved_without_path(self):
        fake = self.fake_bin("uv")
        with mock.patch.object(claude_cli.shutil, "which", return_value=None), \
                mock.patch.object(claude_cli, "UV_CANDIDATES", ("/nonexistent/uv", fake)):
            self.assertEqual(claude_cli.resolve_uv(), fake)
            env = watchdog.daemon_env(self.root)
        self.assertTrue(env["PATH"].startswith(claude_cli.HOMEBREW_BIN))
        self.assertIn(str(Path(fake).parent), env["PATH"].split(os.pathsep))
        self.assertEqual(env["ORCH_ROOT"], str(self.root))

    def test_resolver_used_by_goals_and_shadow(self):
        cmd = planner_shadow.argv("p", model="m", budget_usd=1, system_prompt_path=__file__)
        self.assertEqual(cmd[0], claude_cli.NAME)
        for module in (goals, planner_shadow, daemon.spawn):
            src = inspect.getsource(module)
            self.assertIn("claude_cli.argv(", src, module.__name__)
            self.assertRegex(src, r"executable=(claude_cli\.resolve\(\)|cli)", module.__name__)
        self.assertIn("cli = claude_cli.resolve()", inspect.getsource(daemon.spawn))

    def test_no_literal_claude_argv_outside_resolver(self):
        pattern = re.compile(r"""[\[(]\s*["']claude["']\s*,""")
        offenders = [f.name for f in PKG.glob("*.py")
                     if f.name != "claude_cli.py" and pattern.search(f.read_text())]
        self.assertEqual(offenders, [])

    # registry -----------------------------------------------------------------------------------------------
    def test_registry_adds_repo_on_lock(self):
        repo = self.repo()
        machine.register_repo(repo)
        machine.register_repo(repo)
        self.assertEqual(machine.repos(), [str(repo.resolve())])

    def test_registry_written_on_acquire_lock(self):
        repo = self.repo()
        state = repo / ".orchestrator"
        with mock.patch.object(daemon, "STATE", state), mock.patch.object(daemon, "LOCK_PATH", state / "daemon.lock"):
            fh = daemon.acquire_lock()
            try:
                self.assertIsNotNone(fh)
                self.assertEqual((state / "daemon.lock").read_text(), f"{os.getpid()}\n")
                self.assertIsNone(daemon.acquire_lock())
                self.assertEqual((state / "daemon.lock").read_text(), f"{os.getpid()}\n")
            finally:
                fh.close()
        self.assertEqual(machine.repos(), [str(repo.resolve())])

    def test_missing_repo_pruned(self):
        repo = self.repo()
        machine.register_repo(repo)
        gone = self.root / "gone"
        machine.register_repo(gone)
        self.task(repo, "T-1")
        out = self.run_once()
        self.assertEqual(out["pruned"], [str(gone.resolve())])
        self.assertEqual(machine.repos(), [str(repo.resolve())])
        self.assertEqual(self.started, [str(repo.resolve())])

    # daemon liveness and restart ----------------------------------------------------------------------------
    def test_alive_by_pid_file_no_flock(self):
        repo = self.repo()
        lock = repo / ".orchestrator" / "daemon.lock"
        lock.write_text(f"{os.getpid()}\n")
        with mock.patch.object(watchdog.fcntl, "flock", side_effect=AssertionError("no flock")), \
                mock.patch.object(watchdog, "_command", return_value="python -m orchestrator daemon"):
            self.assertTrue(watchdog.daemon_alive(repo))
        with mock.patch.object(watchdog, "_command", return_value="python other.py"):
            self.assertFalse(watchdog.daemon_alive(repo))
        lock.write_text("999999999\n")
        self.assertFalse(watchdog.daemon_alive(repo))

    def test_lock_probe_readonly_and_released(self):
        repo = self.repo()
        lock = repo / ".orchestrator" / "daemon.lock"
        lock.write_text("999999999\n")
        before = (lock.read_text(), lock.stat().st_mtime_ns)
        watchdog.daemon_alive(repo)
        self.assertEqual((lock.read_text(), lock.stat().st_mtime_ns), before)
        with open(lock) as fh:  # nothing left holding it
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse((self.repo("fresh") / ".orchestrator" / "daemon.lock").exists())
        watchdog.daemon_alive(self.root / "fresh")
        self.assertFalse((self.root / "fresh" / ".orchestrator" / "daemon.lock").exists())

    def test_restarts_dead_daemon_with_work(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1")
        self.run_once()
        self.assertEqual(self.started, [str(repo.resolve())])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("restarted", self.sent[0])
        self.run_once()
        self.assertEqual(len(self.sent), 1)  # still dead: restart retried, not re-notified

    def test_no_restart_without_work(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1", status="done")
        unregistered = self.repo("extra")
        self.task(unregistered, "T-2")  # extra repo without a lock file never had a daemon
        watchdog.CONFIG.write_text(f'[watchdog]\nrepos = ["{unregistered}"]\n')
        self.run_once()
        self.assertEqual(self.started, [])
        self.assertEqual(self.sent, [])

    def test_second_watchdog_exits(self):
        lock = self.mdir / "watchdog.lock"
        with open(lock, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(watchdog, "run_once", side_effect=AssertionError("ran")):
                self.assertEqual(watchdog.run(once=True), 0)
        with mock.patch.object(watchdog, "run_once") as ran:
            self.assertEqual(watchdog.run(once=True), 0)
        ran.assert_called_once()

    # conditions ---------------------------------------------------------------------------------------------
    def test_running_too_long_only_when_daemon_dead(self):
        tasks = [{"id": "T-1", "status": "running", "constraints": {"timeout_s": 100},
                  "pipeline": {"dispatched_at": NOW - 201}},
                 {"id": "T-2", "status": "running", "constraints": {}, "pipeline": {"dispatched_at": NOW - 1700}}]
        self.assertEqual(watchdog.running_too_long(tasks, NOW, alive=False), ["T-1"])
        self.assertEqual(watchdog.running_too_long(tasks, NOW, alive=True), [])

    def test_spawn_failure_from_task_files(self):
        repo = self.repo()
        self.task(repo, "T-1", status="held", hold_reason="claude CLI not found on PATH")
        self.task(repo, "T-2", status="held", hold_reason="spawn error", mtime=NOW - 31 * 60)
        self.task(repo, "T-3", status="held", result={"reason": "claude CLI not found; looked in: x"})
        found = watchdog.spawn_failures(watchdog.load_tasks(repo), NOW)
        self.assertEqual([tid for tid, _ in found], ["T-1", "T-3"])

    def test_reports_spawn_failure_once(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1", status="held", hold_reason="claude CLI not found on PATH")
        self.run_once(alive=True)
        self.run_once(alive=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("claude CLI not found on PATH", self.sent[0])

    def test_reports_stalled_queue_once(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1", status="done", merged_into="goal/T-0", mtime=NOW - 2 * 3600)
        self.task(repo, "T-2", depends_on=["T-1"], mtime=NOW - 2 * 3600)
        self.run_once(alive=True)
        self.run_once(alive=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("queue stalled", self.sent[0])
        tasks = watchdog.load_tasks(repo)
        self.assertFalse(watchdog.queue_stalled(repo, tasks, NOW - 3600 - 7200 + 60))
        self.task(repo, "T-3", depends_on=["T-9"], mtime=NOW - 2 * 3600)
        self.assertTrue(watchdog.queue_stalled(repo, watchdog.load_tasks(repo), NOW))

    def test_cooling_from_repo_pool_state(self):
        repo = self.repo()
        self.task(repo, "T-2", mtime=NOW - 2 * 3600)
        pool_state = repo / ".orchestrator" / "pool_state.json"
        pool_state.write_text(json.dumps({"accounts": {"A": {"cooldown_until": NOW + 60}}}))
        self.assertTrue(watchdog.cooling(repo, NOW))
        self.assertFalse(watchdog.queue_stalled(repo, watchdog.load_tasks(repo), NOW))
        pool_state.write_text(json.dumps({"accounts": {"A": {"cooldown_until": NOW - 60}}, "codex": {}}))
        self.assertFalse(watchdog.cooling(repo, NOW))
        self.assertTrue(watchdog.queue_stalled(repo, watchdog.load_tasks(repo), NOW))

    # dedupe -------------------------------------------------------------------------------------------------
    def test_dedupe_reports_again_after_clear(self):
        repo = self.repo()
        machine.register_repo(repo)
        path = self.task(repo, "T-1", status="held", hold_reason="spawn error")
        self.run_once(alive=True)
        self.task(repo, "T-1", status="held", hold_reason="other")
        self.run_once(alive=True)
        self.task(repo, "T-1", status="held", hold_reason="spawn error")
        self.run_once(alive=True)
        self.assertEqual(len(self.sent), 2)
        state = json.loads((self.mdir / "watchdog-state.json").read_text())
        key = f"{repo.resolve()}:spawn_failure"
        self.assertEqual(set(state[key]), {"first_seen", "notified_at"})

    def test_clear_pass_drops_keys(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1", status="held", hold_reason="spawn error")
        self.run_once(alive=True)
        state_file = self.mdir / "watchdog-state.json"
        stale = json.loads(state_file.read_text())
        stale["/gone/repo:queue_stalled"] = {"first_seen": 1, "notified_at": 1}
        state_file.write_text(json.dumps(stale))
        self.task(repo, "T-1", status="done")
        self.run_once(alive=True)
        self.assertEqual(json.loads(state_file.read_text()), {})

    # launchd ------------------------------------------------------------------------------------------------
    def test_install_launchd_prints_only(self):
        home = self.root / "home"
        home.mkdir()
        before = sorted(p for p in self.root.rglob("*"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch("sys.argv", ["orchestrator", "watchdog", "--install-launchd"]), \
                mock.patch.object(watchdog, "run", side_effect=AssertionError("ran")), redirect_stdout(out):
            from orchestrator import cli
            cli.main()
        text = out.getvalue()
        self.assertIn("<string>watchdog</string>", text)
        self.assertIn("<integer>300</integer>", text)
        self.assertIn("launchctl bootstrap", text)
        path = re.search(r"<key>PATH</key><string>([^<]*)</string>", text).group(1)
        self.assertTrue(path.startswith(claude_cli.HOMEBREW_BIN))
        self.assertEqual(sorted(p for p in self.root.rglob("*")), before)
        self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
