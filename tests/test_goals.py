"""orchestrator.goals: launch/track/stop a headless Planner session against a target repo. goals.py may only
import install.install, spawn.trust_workspace, spawn.resolve_secrets and bus.LOCK_NAME from the package (T-0115);
these tests exercise it against scratch git repos, never REPO itself."""
import json, os, re, subprocess, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_goals.py` doesn't add this dir itself
from _harness import REPO, TMP, scratch_repo
from orchestrator import bus, goals
from orchestrator.pool import Pool


class FakePopen:
    """Records the single call it receives instead of spawning `claude`; .pid is fixed so tests can assert on it."""
    last_args = None
    last_kwargs = None
    calls = 0

    def __init__(self, args, **kwargs):
        FakePopen.last_args = args
        FakePopen.last_kwargs = kwargs
        FakePopen.calls += 1
        self.pid = 424242


def _fake_uv_run(goal_id):
    """subprocess.run wrapper: fakes only the `uv run ... python -` GOAL-task call, passing every other call
    (git, ps, ...) through to the real subprocess.run."""
    real_run = subprocess.run
    calls = []

    def fake(cmd, *a, **k):
        if list(cmd[:2]) == ["uv", "run"]:
            calls.append((cmd, k))
            return subprocess.CompletedProcess(cmd, 0, stdout=goal_id + "\n", stderr="")
        return real_run(cmd, *a, **k)

    fake.calls = calls
    return fake


class GoalsTestCase(unittest.TestCase):
    def setUp(self):
        FakePopen.last_args = None
        FakePopen.last_kwargs = None
        FakePopen.calls = 0
        self._orig_run = goals.subprocess.run
        self._orig_popen = goals.Popen
        self._orig_trust = goals.trust_workspace
        goals.Popen = FakePopen
        goals.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(self._restore)

    def _restore(self):
        goals.subprocess.run = self._orig_run
        goals.Popen = self._orig_popen
        goals.trust_workspace = self._orig_trust

    def fake_run(self, goal_id="T-9000"):
        fake = _fake_uv_run(goal_id)
        goals.subprocess.run = fake
        return fake

    def repo(self, name):
        return scratch_repo(TMP / f"goal-{name}").resolve()

    def unignore(self, repo):
        """Scratch repos ship a .gitignore covering .orchestrator/wt/.claude; several tests need the scaffold
        actually tracked, so clear it."""
        (repo / ".gitignore").write_text("")


class StartRefusals(GoalsTestCase):
    def test_start_refuses_when_scaffold_paths_are_gitignored(self):
        repo = self.repo("gitignored")
        self.fake_run()
        r = goals.start(str(repo), "goal text")
        self.assertFalse(r["launched"])
        self.assertIn("gitignored", r["reason"])
        self.assertEqual(FakePopen.calls, 0)

    def test_start_commits_scaffold_and_records_sha(self):
        repo = self.repo("commits")
        self.unignore(repo)
        self.fake_run("T-9001")
        r = goals.start(str(repo), "goal text")
        self.assertTrue(r["launched"], r)
        self.assertIsNotNone(r["commit"])
        log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
        self.assertIn("orchestrator: scaffold (goal start)", log)
        tracked = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"], cwd=repo,
                                 capture_output=True, text=True).stdout
        self.assertIn(".orchestrator/pool.toml", tracked)
        self.assertIn(".claude/settings.json", tracked)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(r["commit"], head)

    def test_start_refuses_unknown_account_and_missing_planner_model_before_any_write(self):
        # unknown account: fresh repo, default install, but a bogus account id
        repo = self.repo("unknown-account")
        self.unignore(repo)
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="Z")
        self.assertFalse(r["launched"])
        self.assertIn("account", r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        log_count = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True,
                                       text=True).stdout.splitlines())
        self.assertEqual(log_count, 1)  # only the scratch repo's "init" commit

        # missing [models].planner: pool.toml pre-exists (install is a no-op, "kept") without a planner model
        repo2 = self.repo("missing-planner-model")
        self.unignore(repo2)
        (repo2 / ".orchestrator").mkdir()
        (repo2 / ".orchestrator" / "pool.toml").write_text(
            '[[claude_accounts]]\nid = "A"\nconfig_dir = "~/.claude-a"\nrole_affinity = ["planner"]\n\n'
            '[models]\nsonnet = "claude-sonnet-5"\n')
        r2 = goals.start(str(repo2), "goal text", account_id="A")
        self.assertFalse(r2["launched"])
        self.assertIn("planner", r2["reason"])
        self.assertEqual(FakePopen.calls, 0)
        log_count2 = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo2, capture_output=True,
                                        text=True).stdout.splitlines())
        self.assertEqual(log_count2, 1)


class StartLaunch(GoalsTestCase):
    def test_start_creates_goal_task_via_stdin_script_with_env(self):
        repo = self.repo("stdin-script")
        self.unignore(repo)
        fake = self.fake_run("T-9002")
        marker = "MARKER-goal-text-9f3c"
        r = goals.start(str(repo), marker)
        self.assertTrue(r["launched"], r)
        self.assertEqual(r["goal_id"], "T-9002")
        self.assertEqual(len(fake.calls), 1)
        cmd, kwargs = fake.calls[0]
        self.assertEqual(list(cmd), ["uv", "run", "--project", str(goals.PACKAGE_REPO), "python", "-"])
        self.assertEqual(kwargs["input"], goals.SCRIPT)
        self.assertEqual(kwargs["env"]["ORCH_GOAL_TEXT"], marker)
        self.assertEqual(kwargs["env"]["ORCH_ROOT"], str(os.path.realpath(repo)))
        self.assertNotIn(marker, cmd)
        self.assertNotIn(marker, goals.SCRIPT)

    def test_start_launches_claude_with_expected_flags_and_env(self):
        repo = self.repo("launch-flags")
        self.unignore(repo)
        self.fake_run("T-9003")
        goal_text = "build the thing"
        r = goals.start(str(repo), goal_text, account_id="A")
        self.assertTrue(r["launched"], r)

        args, kwargs = FakePopen.last_args, FakePopen.last_kwargs
        self.assertEqual(args[0], "claude")
        self.assertEqual(args[1], "-p")
        self.assertIn(goal_text, args[2])
        self.assertIn("T-9003", args[2])
        self.assertEqual(args[3:5], ["--model", "claude-fable-5-1"])
        self.assertIn("--output-format", args); self.assertIn("json", args)
        self.assertIn("--max-budget-usd", args)
        self.assertEqual(args[args.index("--mcp-config") + 1], ".mcp.planner.json")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--append-system-prompt", args)
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertEqual(kwargs["cwd"], str(os.path.realpath(repo)))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/.claude-a"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", kwargs["env"])

        # configure an oauth token env var for account A directly in the already-installed pool.toml, relaunch
        pool_toml = repo / ".orchestrator" / "pool.toml"
        text = pool_toml.read_text().replace('id = "A"\nconfig_dir = "~/.claude-a"',
                                              'id = "A"\nconfig_dir = "~/.claude-a"\noauth_token_env = "T0115_OAUTH_VAR"')
        pool_toml.write_text(text)
        os.environ["T0115_OAUTH_VAR"] = "tok-abc"
        self.addCleanup(lambda: os.environ.pop("T0115_OAUTH_VAR", None))
        self.fake_run("T-9004")
        r2 = goals.start(str(repo), "second goal", account_id="A")
        self.assertTrue(r2["launched"], r2)
        self.assertEqual(FakePopen.last_kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "tok-abc")

    def test_start_marks_workspace_trusted(self):
        repo = self.repo("trust")
        self.unignore(repo)
        self.fake_run("T-9005")
        calls = []
        goals.trust_workspace = lambda config_dir, wt: calls.append((config_dir, wt))
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertTrue(r["launched"], r)
        self.assertEqual(len(calls), 1)
        config_dir, wt = calls[0]
        self.assertEqual(config_dir, "~/.claude-a")
        self.assertEqual(Path(wt), Path(os.path.realpath(repo)))


class StatusAndStop(GoalsTestCase):
    def _write_task(self, repo, tid, **fields):
        tasks_dir = Path(repo) / ".orchestrator" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task = {"id": tid, "parent": None, "role": "triage", "status": "queued", "result": None,
               "hold_reason": None, **fields}
        (tasks_dir / f"{tid}.json").write_text(json.dumps(task))
        return task

    def test_status_reconciles_exited_and_checks_pid_identity(self):
        repo = TMP / "goal-status"
        repo.mkdir(parents=True, exist_ok=True)
        goal_id = "T-8001"
        self._write_task(repo, goal_id, result={"summary": "done, see https://github.com/o/r/pull/42"})
        self._write_task(repo, "T-8002", parent=goal_id, status="done", merged_into="goal/T-8001")
        self._write_task(repo, "T-8003", parent=goal_id, status="held", hold_reason="no account with headroom")

        goals_json = repo / ".orchestrator" / "runs" / "goals.json"
        goals_json.parent.mkdir(parents=True, exist_ok=True)
        record = {"goal_id": goal_id, "repo": str(repo), "pid": 999999999, "pid_start": 12345.0,
                  "started_at": 1.0, "account": "A", "commit": "deadbeef", "status": "running"}
        goals_json.write_text(json.dumps([record]))

        orig_proc_start = goals._proc_start
        goals._proc_start = lambda pid: None
        self.addCleanup(lambda: setattr(goals, "_proc_start", orig_proc_start))

        entries = goals.status(str(repo), goal_id)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["record_status"], "exited")  # dead pid -> reconciled
        self.assertIsNone(e["planner_alive"])
        self.assertEqual(e["task_status"], "queued")
        self.assertEqual(e["merged"], ["goal/T-8001"])
        self.assertEqual(e["pr_url"], "https://github.com/o/r/pull/42")
        self.assertEqual(sorted(e["children"].keys()), ["done", "held"])
        self.assertEqual(e["children"]["held"][0]["hold_reason"], "no account with headroom")

        on_disk = json.loads(goals_json.read_text())
        self.assertEqual(on_disk[0]["status"], "exited")
        self.assertIn("exited_at", on_disk[0])

    def test_stop_uses_killpg_only_when_identity_matches(self):
        repo = TMP / "goal-stop"
        repo.mkdir(parents=True, exist_ok=True)
        goals_json = repo / ".orchestrator" / "runs" / "goals.json"
        goals_json.parent.mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(["sleep", "5"], start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        alive_record = {"goal_id": "T-8101", "repo": str(repo), "pid": proc.pid, "pid_start": None,
                        "status": "running"}
        dead_record = {"goal_id": "T-8102", "repo": str(repo), "pid": 999999999, "pid_start": None,
                       "status": "running"}
        goals_json.write_text(json.dumps([alive_record, dead_record]))

        calls = []
        orig_killpg = goals.os.killpg
        goals.os.killpg = lambda pgid, sig: calls.append((pgid, sig))
        self.addCleanup(lambda: setattr(goals.os, "killpg", orig_killpg))

        r1 = goals.stop(str(repo), "T-8101")
        self.assertEqual(r1["stopped"], "T-8101")
        self.assertEqual(len(calls), 1)

        r2 = goals.stop(str(repo), "T-8102")
        self.assertEqual(r2["stopped"], "T-8102")
        self.assertEqual(len(calls), 1)  # unchanged: dead pid never signalled

        r3 = goals.stop(str(repo), "T-nope")
        self.assertEqual(r3, {"error": "unknown goal"})

        on_disk = {r["goal_id"]: r for r in json.loads(goals_json.read_text())}
        self.assertEqual(on_disk["T-8101"]["status"], "stopped")
        self.assertEqual(on_disk["T-8102"]["status"], "stopped")


class PlannerPromptAndConfig(unittest.TestCase):
    def _section(self, text, name):
        m = re.search(rf"^## {name}\n(.*?)(?=\n## |\Z)", text, re.S | re.M)
        return m.group(1).strip() if m else None

    def test_planner_md_matches_claude_md_lists(self):
        claude_md = (goals.PACKAGE_REPO / "CLAUDE.md").read_text()
        planner_md = (goals.PACKAGE_REPO / ".orchestrator" / "prompts" / "planner.md").read_text()
        self.assertEqual(planner_md.splitlines()[0].strip(), claude_md.splitlines()[0].strip())
        self.assertEqual(self._section(planner_md, "Never"), self._section(claude_md, "Never"))
        planner_always = self._section(planner_md, "Always").split("\n\nHeadless")[0].strip()
        self.assertEqual(planner_always, self._section(claude_md, "Always"))
        self.assertIn("Headless", planner_md)
        self.assertIn("GOAL task id", planner_md)

    def test_pool_parses_new_tables(self):
        cfg = Pool().cfg
        self.assertEqual(cfg["models"]["planner"], "claude-fable-5-1")
        self.assertEqual(cfg["secrets"]["planner"], {"GH_WRITE_TOKEN": "env:GH_WRITE_TOKEN"})

    def test_bus_lock_name_shared(self):
        self.assertEqual(bus.LOCK_NAME, "bus.lock")
        self.assertIs(goals.LOCK_NAME, bus.LOCK_NAME)
        self.assertEqual(bus.LOCK, bus.STATE / bus.LOCK_NAME)


if __name__ == "__main__":
    unittest.main()
