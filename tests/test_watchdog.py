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
        # Fixture repos live under the temp dir, which is_install rejects; tests of that rule restore it.
        self.real_temp_roots = machine._temp_roots
        p = mock.patch.object(machine, "_temp_roots", return_value=()); p.start(); self.addCleanup(p.stop)
        self.root = root
        self.sent = []
        self.started = []

    # helpers ------------------------------------------------------------------------------------------------
    def repo(self, name="r"):
        repo = self.root / name
        (repo / ".orchestrator" / "tasks").mkdir(parents=True)
        (repo / ".orchestrator" / "pool.toml").write_text("")
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
        with mock.patch.object(claude_cli, "resolve", return_value="/opt/fake/claude"):
            cmd = planner_shadow.argv("p", model="m", budget_usd=1, system_prompt_path=__file__)
        self.assertEqual(cmd[0], "/opt/fake/claude")
        for module in (goals, planner_shadow):
            src = inspect.getsource(module)
            self.assertIn("claude_cli.command(", src, module.__name__)
            self.assertNotIn("executable=", src, module.__name__)
        self.assertIn("claude_cli.argv(", inspect.getsource(daemon.spawn))
        spawn_src = inspect.getsource(daemon.spawn)
        self.assertIn("cli = claude_cli.resolve()", spawn_src)
        self.assertIn("Popen([cli, *cmd[1:]]", spawn_src)

    def test_no_literal_claude_argv_outside_resolver(self):
        # A list or tuple literal (not a call like normalize("claude", ...)) whose first item is "claude".
        pattern = re.compile(r"""(?<![\w.])[\[(]\s*["']claude["']\s*,""")
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
                marker = (state / "daemon.lock").read_text()
                self.assertTrue(marker.startswith(f"{os.getpid()} cli "), marker)
                self.assertIsNone(daemon.acquire_lock())
                self.assertEqual((state / "daemon.lock").read_text(), marker)
            finally:
                fh.close()
        self.assertEqual(machine.repos(), [str(repo.resolve())])

    def test_missing_repo_pruned(self):
        repo = self.repo()
        machine.register_repo(repo)
        gone = self.root / "gone"
        machine.REPOS.write_text(json.dumps(machine.repos() + [str(gone)]))
        self.task(repo, "T-1")
        out = self.run_once()
        self.assertEqual(out["pruned"], [str(gone)])
        self.assertEqual(machine.repos(), [str(repo.resolve())])
        self.assertEqual(self.started, [str(repo.resolve())])

    # daemon liveness and restart ----------------------------------------------------------------------------
    def test_alive_by_pid_file_no_flock(self):
        repo = self.repo()
        lock = repo / ".orchestrator" / "daemon.lock"
        lock.write_text(f"{os.getpid()}\n")
        with mock.patch.object(watchdog.fcntl, "flock", side_effect=AssertionError("no flock")), \
                mock.patch.object(watchdog, "_process_root", return_value=str(repo)), \
                mock.patch.object(watchdog, "_command", return_value="python -m orchestrator daemon"):
            self.assertTrue(watchdog.daemon_alive(repo))
        with mock.patch.object(watchdog, "_command", return_value="python other.py"), \
                mock.patch.object(watchdog, "_process_root", return_value=str(repo)):
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
        self.task(repo, "T-2", status="held", hold_reason="launch_error", mtime=NOW - 31 * 60)
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
        path = self.task(repo, "T-1", status="held", hold_reason="launch_error")
        self.run_once(alive=True)
        self.task(repo, "T-1", status="held", hold_reason="other")
        self.run_once(alive=True)
        self.task(repo, "T-1", status="held", hold_reason="launch_error")
        self.run_once(alive=True)
        self.assertEqual(len(self.sent), 2)
        state = json.loads((self.mdir / "watchdog-state.json").read_text())
        key = f"{repo.resolve()}:spawn_failure"
        self.assertEqual(set(state[key]), {"first_seen", "notified_at"})

    def test_clear_pass_drops_keys(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1", status="held", hold_reason="launch_error")
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

    # review fix round (T-1687) ------------------------------------------------------------------------------
    def test_h1_restart_runs_orchestrator_project(self):
        repo = self.repo()
        with mock.patch.object(watchdog.subprocess, "Popen") as popen, \
                mock.patch.object(watchdog.claude_cli, "resolve_uv", return_value="/opt/uv"):
            log = watchdog.start_daemon(repo)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["/opt/uv", "run", "--project", str(watchdog.PACKAGE_REPO), "orchestrator", "daemon"])
        self.assertTrue((watchdog.PACKAGE_REPO / "orchestrator" / "watchdog.py").is_file())
        self.assertEqual(popen.call_args.kwargs["cwd"], str(repo))
        self.assertEqual(popen.call_args.kwargs["env"]["ORCH_ROOT"], str(repo))
        self.assertTrue(log.startswith(str(repo / ".orchestrator" / "runs")))

    def test_plist_uses_project(self):
        text = watchdog.launchd_plist(self.root)
        args = re.search(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", text, re.S).group(1)
        strings = re.findall(r"<string>([^<]*)</string>", args)
        self.assertEqual(strings[1:], ["run", "--project", str(watchdog.PACKAGE_REPO), "orchestrator", "watchdog",
                                       "--once"])

    def test_empty_lock_is_unknown_not_dead(self):
        repo = self.repo()
        machine.register_repo(repo)
        self.task(repo, "T-1")
        lock = repo / ".orchestrator" / "daemon.lock"
        lock.write_text("")
        self.run_once()
        self.run_once()
        self.assertEqual(self.started, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("daemon on old code, restart it", self.sent[0])
        self.assertNotIn("not running", self.sent[0])
        lock.write_text("not a pid\n")  # malformed but not empty: still treated as dead
        self.run_once()
        self.assertEqual(self.started, [str(repo.resolve())])

    def test_h2_harness_isolates_machine_dir(self):
        home = Path("~/.orchestrator-machine").expanduser().resolve()
        env_dir = Path(os.environ["ORCH_MACHINE_DIR"]).resolve()
        self.assertEqual(env_dir, _harness.MACHINE.resolve())
        self.assertNotEqual(env_dir, home)
        self.assertTrue(env_dir.is_relative_to(Path(tempfile.gettempdir()).resolve()))

    def test_h2_register_accepts_only_real_installs(self):
        bare = self.root / "bare"
        (bare / ".orchestrator").mkdir(parents=True)
        machine.register_repo(bare)  # no pool.toml
        self.assertEqual(machine.repos(), [])
        installed = self.repo("installed")
        with mock.patch.object(machine, "_temp_roots", self.real_temp_roots):
            self.assertFalse(machine.is_install(installed))  # under the temp dir
            machine.register_repo(installed)
            self.assertEqual(machine.repos(), [])
            state = installed / ".orchestrator"
            with mock.patch.object(daemon, "STATE", state), \
                    mock.patch.object(daemon, "LOCK_PATH", state / "daemon.lock"):
                fh = daemon.acquire_lock()
                fh.close()
            self.assertEqual(machine.repos(), [])
        self.assertFalse(machine.is_install("/private/var/folders/xy/T/orch-1"))
        machine.register_repo(installed)
        self.assertEqual(machine.repos(), [str(installed.resolve())])

    def test_h2_watchdog_prunes_non_installs(self):
        good = self.repo("good")
        fixture = self.repo("fixture")
        bare = self.root / "bare"
        bare.mkdir()
        machine.REPOS.write_text(json.dumps([str(good), str(fixture), str(bare)]))
        with mock.patch.object(machine, "_temp_roots", return_value=(fixture.resolve(),)):
            out = self.run_once(alive=True)
        self.assertEqual(sorted(out["pruned"]), sorted([str(fixture), str(bare)]))
        self.assertEqual(machine.repos(), [str(good)])

    def test_h3_mcp_hosted_daemon_alive(self):
        repo = self.repo()
        lock = repo / ".orchestrator" / "daemon.lock"
        lock.write_text(f"{os.getpid()} mcp Tue Oct 6 10:00:00 2026\n")
        mcp_cmd = "/r/.venv/x/python3 -m orchestrator.mcp"
        cli_cmd = "/r/.venv/x/python3 /r/.venv/x/orchestrator daemon"
        with mock.patch.object(machine, "process_start", return_value="Tue Oct 6 10:00:00 2026"), \
                mock.patch.object(watchdog, "_process_root", return_value=str(repo)):
            with mock.patch.object(watchdog, "_command", return_value=mcp_cmd):
                self.assertTrue(watchdog.daemon_alive(repo))
            with mock.patch.object(watchdog, "_command", return_value=cli_cmd):
                self.assertFalse(watchdog.daemon_alive(repo))  # kind mismatch
            lock.write_text(f"{os.getpid()} cli Tue Oct  6 10:00:00 2026\n")
            with mock.patch.object(watchdog, "_command", return_value=cli_cmd):
                self.assertTrue(watchdog.daemon_alive(repo))
        self.assertEqual(watchdog.read_lock(repo), (os.getpid(), "cli", "Tue Oct 6 10:00:00 2026"))

    def test_h3_lock_marker_written_by_cli_and_mcp(self):
        repo = self.repo()
        state = repo / ".orchestrator"
        with mock.patch.object(daemon, "STATE", state), mock.patch.object(daemon, "LOCK_PATH", state / "daemon.lock"):
            fh = daemon.acquire_lock("mcp")
            fh.close()
            pid, kind, start = watchdog.read_lock(repo)
            self.assertEqual((pid, kind), (os.getpid(), "mcp"))
            self.assertIsNotNone(start)
            with mock.patch.object(daemon, "_loop"):
                thread = daemon.start_background({"daemon": {"autostart": True}})
                thread.join(5)
            self.assertEqual(watchdog.read_lock(repo)[1], "mcp")
        # The marker for this live process passes the start-time check under any caller locale or TZ.
        with mock.patch.dict(os.environ, {"LC_ALL": "de_CH.UTF-8", "TZ": "Europe/Zurich"}):
            self.assertEqual(machine.process_start(os.getpid()), start)

    def test_m4_reused_pid_or_other_repo_is_dead(self):
        repo = self.repo()
        (repo / ".orchestrator" / "daemon.lock").write_text(f"{os.getpid()} cli Tue Oct 6 10:00:00 2026\n")
        cli_cmd = "/r/.venv/x/python3 /r/.venv/x/orchestrator daemon"
        with mock.patch.object(watchdog, "_command", return_value=cli_cmd), \
                mock.patch.object(watchdog, "_process_root", return_value=str(repo)), \
                mock.patch.object(machine, "process_start", return_value="Wed Oct 7 11:00:00 2026"):
            self.assertFalse(watchdog.daemon_alive(repo))  # pid reused by a later process
        with mock.patch.object(machine, "process_start", return_value="Tue Oct 6 10:00:00 2026"):
            with mock.patch.object(watchdog, "_process_root", return_value=str(self.root / "other")), \
                    mock.patch.object(watchdog, "_command", return_value=cli_cmd):
                self.assertFalse(watchdog.daemon_alive(repo))  # a daemon of another repo
            worker = "claude -p --mcp-config /x/orchestrator/.mcp.worker.json fix the daemon"
            with mock.patch.object(watchdog, "_process_root", return_value=str(repo)), \
                    mock.patch.object(watchdog, "_command", return_value=worker):
                self.assertFalse(watchdog.daemon_alive(repo))  # not a python orchestrator daemon
            with mock.patch.object(watchdog, "_process_root", return_value=None), \
                    mock.patch.object(watchdog, "_command", return_value=cli_cmd):
                self.assertTrue(watchdog.daemon_alive(repo))  # repo not determinable: start time decides
        self.assertTrue(watchdog.is_daemon_command("/opt/x/Python -m orchestrator.cli daemon", "cli"))
        self.assertFalse(watchdog.is_daemon_command("python -m orchestrator.mcp", "other"))

    def test_m5_bad_repo_does_not_abort_pass(self):
        bad = self.repo("bad")
        good = self.repo("good")
        machine.REPOS.write_text(json.dumps([str(bad), str(good)]))
        self.task(good, "T-1", status="held", hold_reason="claude CLI not found on PATH")
        real = watchdog.evaluate

        def evaluate(repo, now, *, registered):
            if repo == str(bad):
                raise RuntimeError("boom")
            return real(repo, now, registered=registered)
        err = io.StringIO()
        with mock.patch.object(watchdog, "evaluate", side_effect=evaluate), mock.patch("sys.stderr", err):
            out = self.run_once(alive=True)
        self.assertEqual(out["conditions"], [f"{good}:spawn_failure"])
        self.assertIn("boom", err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_m5_pid_and_task_fields_validated(self):
        repo = self.repo()
        lock = repo / ".orchestrator" / "daemon.lock"
        for text in ("99999999999999999999999\n", "-5\n", "0\n", f"{2 ** 31}\n", "abc\n", "1e5\n", ""):
            lock.write_text(text)
            self.assertIsNone(watchdog.daemon_pid(repo), text)
            self.assertFalse(watchdog.daemon_alive(repo), text)
        tasks = repo / ".orchestrator" / "tasks"
        (tasks / "a.json").write_text(json.dumps({"id": 5, "status": "queued"}))
        (tasks / "b.json").write_text(json.dumps({"id": "T-b", "status": ["queued"]}))
        (tasks / "c.json").write_text(json.dumps({"id": "T-c", "status": "queued", "depends_on": "T-1"}))
        (tasks / "d.json").write_text(json.dumps({"id": "T-d", "status": "queued", "depends_on": [1]}))
        (tasks / "e.json").write_text(json.dumps([1, 2]))
        self.task(repo, "T-ok", status="running", pipeline=["x"], constraints={"timeout_s": "100"})
        loaded = watchdog.load_tasks(repo)
        self.assertEqual([t["id"] for t in loaded], ["T-ok"])
        self.assertEqual(watchdog.running_too_long(loaded, NOW, alive=False), [])
        (repo / ".orchestrator" / "pool_state.json").write_text(json.dumps({"accounts": [1], "codex": 3}))
        self.assertFalse(watchdog.cooling(repo, NOW))
        machine.register_repo(repo)
        self.run_once()  # nothing above raises

    def test_m5_task_file_vanishing_between_glob_and_stat(self):
        repo = self.repo()
        self.task(repo, "T-1")
        self.task(repo, "T-2")
        real_stat = Path.stat

        def stat_(path, *a, **k):
            if path.name == "T-1.json":
                raise FileNotFoundError(path)
            return real_stat(path, *a, **k)
        with mock.patch.object(Path, "stat", stat_):
            self.assertEqual([t["id"] for t in watchdog.load_tasks(repo)], ["T-2"])

    def test_m6_only_specific_spawn_markers(self):
        repo = self.repo()
        routine = ("re-spawned after account B cooldown", "respawn path", "spawn error")
        for i, reason in enumerate(routine):
            self.task(repo, f"T-r{i}", status="held", hold_reason=reason)
        real = ("claude CLI not found; looked in: PATH", "uv not found on PATH", "no_cli", "launch_error: exec")
        for i, reason in enumerate(real):
            self.task(repo, f"T-s{i}", status="held", hold_reason=reason)
        found = watchdog.spawn_failures(watchdog.load_tasks(repo), NOW)
        self.assertEqual(sorted(tid for tid, _ in found), [f"T-s{i}" for i in range(len(real))])

    def test_l7_relative_which_result_discarded(self):
        fake = self.fake_bin("claude")
        with mock.patch.object(claude_cli.shutil, "which", return_value="rel/claude"), \
                mock.patch.object(claude_cli, "CANDIDATES", (fake,)):
            self.assertEqual(claude_cli.resolve(), fake)
        with mock.patch.object(claude_cli.shutil, "which", return_value="./claude"), \
                mock.patch.object(claude_cli, "CANDIDATES", ()):
            self.assertIsNone(claude_cli.resolve())
        with mock.patch.object(claude_cli.shutil, "which", return_value=fake):
            self.assertEqual(claude_cli.resolve(), fake)

    def test_l8_plist_paths_escaped(self):
        root = self.root / "a&b<c>"
        root.mkdir()
        with mock.patch.object(claude_cli, "resolve_uv", return_value="/opt/u&v/uv"):
            text = watchdog.launchd_plist(root)
        self.assertIn("a&amp;b&lt;c&gt;", text)
        self.assertIn("<string>/opt/u&amp;v/uv</string>", text)
        self.assertNotIn("a&b<c>", text)
        import xml.dom.minidom
        xml.dom.minidom.parseString(text)  # well-formed


if __name__ == "__main__":
    unittest.main()
