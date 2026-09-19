"""orchestrator.goals: launch/track/stop a headless Planner session against a target repo. goals.py may only
import install.install, spawn.trust_workspace and spawn.resolve_secrets from the package (T-0115); these tests
exercise it against scratch git repos, never REPO itself."""
import contextlib, io, json, os, re, subprocess, sys, threading, tomllib, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_goals.py` doesn't add this dir itself
from _harness import REPO, TMP, scratch_repo
from orchestrator import bus, cli, goals
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
        # unknown account: fresh repo, no pool.toml yet, but a bogus account id -- refused off the preview
        # config (this repo's own default pool.toml) before install() ever runs, so nothing lands on disk.
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
        self.assertFalse((repo / ".orchestrator").exists())

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

    def test_start_refuses_missing_max_budget_planner_before_commit(self):
        # pool.toml pre-exists with a valid account and planner model but no limits.max_budget_usd.planner --
        # install() is skipped (pool.toml already present), so the KeyError refusal fires with no commit.
        repo = self.repo("missing-max-budget-planner")
        self.unignore(repo)
        (repo / ".orchestrator").mkdir()
        (repo / ".orchestrator" / "pool.toml").write_text(
            '[[claude_accounts]]\nid = "A"\nconfig_dir = "~/.claude-a"\nrole_affinity = ["planner"]\n\n'
            '[models]\nplanner = "claude-fable-5-1"\n\n'
            '[limits]\nmax_budget_usd = { execute = 6.0 }\n')
        (repo / ".orchestrator" / "prompts").mkdir()
        (repo / ".orchestrator" / "prompts" / "planner.md").write_text("# planner\n")
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("max_budget_usd", r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        log_count = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True,
                                       text=True).stdout.splitlines())
        self.assertEqual(log_count, 1)

    def test_start_refuses_missing_planner_prompt_before_commit(self):
        # pool.toml is fully valid but .orchestrator/prompts/planner.md was deleted after a prior install();
        # since pool.toml already exists, install() is skipped and the file is never recreated.
        repo = self.repo("missing-planner-prompt")
        self.unignore(repo)
        from orchestrator.install import install as _install
        _install(str(repo))
        (repo / ".orchestrator" / "prompts" / "planner.md").unlink()
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("planner.md", r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        log_count = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True,
                                       text=True).stdout.splitlines())
        self.assertEqual(log_count, 1)

    def test_start_refuses_uv_missing(self):
        repo = self.repo("no-uv")
        self.unignore(repo)
        real_run = subprocess.run

        def fake(cmd, *a, **k):
            if list(cmd[:2]) == ["uv", "run"]:
                raise FileNotFoundError("uv")
            return real_run(cmd, *a, **k)

        goals.subprocess.run = fake
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("uv", r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        tasks_dir = repo / ".orchestrator" / "tasks"
        self.assertEqual(list(tasks_dir.glob("T-*.json")) if tasks_dir.exists() else [], [])

    def test_start_refuses_staged_changes_leaves_them_staged(self):
        repo = self.repo("staged-changes")
        self.unignore(repo)
        (repo / "unrelated.txt").write_text("wip\n")
        subprocess.run(["git", "add", "unrelated.txt"], cwd=repo, capture_output=True, text=True)
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("staged", r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True,
                                text=True).stdout.split()
        self.assertIn("unrelated.txt", staged)
        log_count = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True,
                                       text=True).stdout.splitlines())
        self.assertEqual(log_count, 1)

    def test_start_refuses_when_goal_already_running(self):
        repo = self.repo("already-running")
        self.unignore(repo)
        goals_json = repo / ".orchestrator" / "runs" / "goals.json"
        goals_json.parent.mkdir(parents=True, exist_ok=True)
        goals_json.write_text(json.dumps([{"goal_id": "T-7777", "repo": str(repo), "pid": 555555, "pid_start": None,
                                           "status": "running"}]))
        orig_alive = goals._alive
        goals._alive = lambda pid: pid == 555555
        self.addCleanup(lambda: setattr(goals, "_alive", orig_alive))
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("T-7777", r["reason"])
        self.assertIn("running", r["reason"])
        self.assertEqual(FakePopen.calls, 0)


class InvalidTargetToml(GoalsTestCase):
    def test_preview_cfg_refuses_invalid_toml(self):
        repo = self.repo("invalid-toml")
        self.unignore(repo)
        (repo / ".orchestrator").mkdir()
        (repo / ".orchestrator" / "pool.toml").write_text("this is [ not valid toml")
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("toml", r["reason"].lower())
        self.assertNotIn("Traceback", r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        log_count = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True,
                                       text=True).stdout.splitlines())
        self.assertEqual(log_count, 1)


class ClaudeCliMissing(GoalsTestCase):
    def test_start_records_missing_claude_cli(self):
        repo = self.repo("claude-missing")
        self.unignore(repo)
        self.fake_run("T-9300")

        def raise_fnf(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "claude")

        goals.Popen = raise_fnf

        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertEqual(r["reason"], "claude CLI not found")
        self.assertIsNotNone(r.get("commit"))

        log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
        self.assertIn("orchestrator: scaffold (goal start)", log)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(r["commit"], head)

        entries = goals.status(str(repo), r["goal_id"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["record_status"], "failed")


class SingleGoalGuard(GoalsTestCase):
    def test_single_goal_guard_under_lock(self):
        repo = self.repo("single-guard")
        self.unignore(repo)
        from orchestrator.install import install as _install
        _install(str(repo))

        results = []
        barrier = threading.Barrier(2)

        def reserve():
            barrier.wait()
            results.append(goals._reserve_running_slot(repo, None))

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[1] is None]
        refusals = [r for r in results if r[1] is not None]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(refusals), 1, results)
        self.assertIn("starting", refusals[0][1])

        # the reservation is only a placeholder -- exactly one "starting" record landed, not two
        records = json.loads((repo / ".orchestrator" / "runs" / "goals.json").read_text())
        starting = [r for r in records if r.get("status") == "starting"]
        self.assertEqual(len(starting), 1)


class PrecheckGitRefusals(GoalsTestCase):
    def _assert_refused_no_commit(self, repo, needle):
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn(needle, r["reason"])
        self.assertEqual(FakePopen.calls, 0)
        log_count = len(subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True,
                                       text=True).stdout.splitlines())
        self.assertEqual(log_count, 1)

    def test_refuses_non_toplevel_path(self):
        repo = self.repo("nontop")
        self.unignore(repo)
        sub = repo / "subdir"
        sub.mkdir()
        self.fake_run()
        self._assert_refused_no_commit(sub, "toplevel")

    def test_refuses_detached_head(self):
        repo = self.repo("detached")
        self.unignore(repo)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", sha], cwd=repo, capture_output=True, text=True)
        self.fake_run()
        self._assert_refused_no_commit(repo, "detached")

    def test_refuses_rebase_in_progress(self):
        repo = self.repo("rebase")
        self.unignore(repo)
        (repo / ".git" / "rebase-merge").mkdir()
        self.fake_run()
        self._assert_refused_no_commit(repo, "rebase")

    def test_refuses_merge_in_progress(self):
        repo = self.repo("merge")
        self.unignore(repo)
        (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
        self.fake_run()
        self._assert_refused_no_commit(repo, "merge")


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

    def test_start_records_requester_and_status_returns_it(self):
        repo = self.repo("requester")
        self.unignore(repo)
        self.fake_run("T-9006")
        r = goals.start(str(repo), "goal text", account_id="A", requester="alice")
        self.assertTrue(r["launched"], r)
        entries = goals.status(str(repo), "T-9006")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["requester"], "alice")

    def test_start_uses_launch_planner(self):
        repo = self.repo("uses-launch-planner")
        self.unignore(repo)
        self.fake_run("T-9010")
        calls = []

        def fake_launch(repo_path, prompt, account_id, max_budget_usd, log_path, extra_env=None):
            calls.append({"repo_path": repo_path, "prompt": prompt, "account_id": account_id,
                          "max_budget_usd": max_budget_usd, "log_path": log_path, "extra_env": extra_env})
            return {"pid": 555555, "pid_start": None, "log": str(log_path)}

        orig = goals.launch_planner
        goals.launch_planner = fake_launch
        self.addCleanup(lambda: setattr(goals, "launch_planner", orig))

        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertTrue(r["launched"], r)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["account_id"], "A")
        self.assertEqual(call["max_budget_usd"], 10.0)
        self.assertIn("goal text", call["prompt"])
        self.assertIn(r["goal_id"], call["prompt"])
        self.assertIsNone(call["extra_env"])
        self.assertEqual(r["pid"], 555555)
        self.assertEqual(r["log"], str(call["log_path"]))
        # Popen must never be invoked directly by start() once launch_planner is stubbed out.
        self.assertEqual(FakePopen.calls, 0)

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
        self.assertEqual(cfg["limits"]["max_budget_usd"]["planner"], 10.0)
        self.assertEqual(cfg["limits"]["max_turns"]["planner"], 400)
        self.assertEqual(cfg["limits"]["timeout_s"]["planner"], 7200)

    def test_goals_lock_dedicated_not_bus_lock(self):
        self.assertEqual(bus.LOCK_NAME, "bus.lock")
        self.assertEqual(goals.GOALS_LOCK_NAME, "goals.lock")
        self.assertFalse(hasattr(goals, "LOCK_NAME"))
        repo = TMP / "goal-lock-check"
        repo.mkdir(parents=True, exist_ok=True)
        goals._append_goal_record(repo, {"goal_id": "T-LOCK", "status": "done"})
        self.assertTrue((repo / ".orchestrator" / "goals.lock").exists())
        self.assertFalse((repo / ".orchestrator" / "bus.lock").exists())


class ScaffoldCommitAndSecrets(GoalsTestCase):
    def test_scaffold_path_with_space_commits_via_status_z(self):
        repo = self.repo("space-path")
        self.unignore(repo)
        (repo / ".orchestrator").mkdir()
        (repo / ".orchestrator" / "a b.txt").write_text("hi\n")
        self.fake_run("T-9100")
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertTrue(r["launched"], r)
        tracked = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"], cwd=repo,
                                 capture_output=True, text=True).stdout
        self.assertIn(".orchestrator/a b.txt", tracked)

    def test_gitignored_scaffold_path_with_space_still_refused(self):
        repo = self.repo("space-path-ignored")
        (repo / ".gitignore").write_text(".orchestrator/\n")
        (repo / ".orchestrator").mkdir()
        (repo / ".orchestrator" / "a b.txt").write_text("hi\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=repo, capture_output=True, text=True)
        self.fake_run()
        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertFalse(r["launched"])
        self.assertIn("gitignored", r["reason"])
        self.assertEqual(FakePopen.calls, 0)

    def test_target_secrets_filtered_to_env_only(self):
        repo = self.repo("target-secrets")
        self.unignore(repo)
        from orchestrator.install import install as _install
        _install(str(repo))
        marker = "/tmp/T-0129-pwned-marker"
        self.addCleanup(lambda: os.path.exists(marker) and os.remove(marker))
        pool_toml = repo / ".orchestrator" / "pool.toml"
        text = pool_toml.read_text()
        self.assertIn("[secrets.planner]", text)
        old = "[secrets.planner]\nGH_WRITE_TOKEN = \"env:GH_WRITE_TOKEN\"\n"
        self.assertIn(old, text)
        text = text.replace(old, old + f'X = "echo pwned > {marker}"\nY = "env:HOME"\n')
        pool_toml.write_text(text)

        run_calls = []
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            run_calls.append(cmd)
            return real_run(cmd, *a, **k)

        goals.subprocess.run = lambda *a, **k: (
            subprocess.CompletedProcess(a[0], 0, stdout="T-9200\n", stderr="")
            if list(a[0][:2]) == ["uv", "run"] else spy(*a, **k))

        r = goals.start(str(repo), "goal text", account_id="A")
        self.assertTrue(r["launched"], r)
        env = FakePopen.last_kwargs["env"]
        self.assertNotIn("X", env)
        self.assertEqual(env.get("Y"), os.environ["HOME"])
        self.assertFalse(any("pwned" in " ".join(c) for c in run_calls if isinstance(c, list)))
        self.assertFalse(os.path.exists("/tmp/T-0129-pwned-marker"))


class DecisionPacket(unittest.TestCase):
    def test_decision_packet_held_has_packet_and_failures_fenced(self):
        goal = bus.create_task("GOAL: held decision", "goal spec", ["Planner closes the goal with a PR"], ["**"],
                               role="triage", complexity=5)
        goal_id = goal["id"]
        held = bus.create_task("fix the thing", "do the fix", ["x"], ["orchestrator/goals.py"],
                               role="execute", parent=goal_id, complexity=3)
        held_id = held["id"]
        bus.update(held_id, status="held", hold_reason="gate_red",
                  resume_hint={"failures": "AssertionError: expected 1 got 2"})
        review = bus.create_task("review of fix", "spec", ["x"], ["y"], role="review", parent=goal_id,
                                 complexity=3, inputs=[held_id])
        bus.post_result(review["id"], {"comments": [{"path": "orchestrator/goals.py", "line": 42,
                                                      "issue": "missing null check"}]}, status="done")

        packet = goals.decision_packet(goal_id, "held", f"{held_id}:123.456")

        self.assertIn(f"Held task: fix the thing ({held_id})", packet)
        self.assertIn("Held task packet:", packet)
        self.assertIn("Hold reason: gate_red", packet)
        self.assertIn("```data", packet)
        self.assertIn("AssertionError: expected 1 got 2", packet)
        self.assertIn("orchestrator/goals.py:42 missing null check", packet)
        # the fenced failures block closes, it isn't left open
        self.assertIn("```data\nAssertionError: expected 1 got 2\n```", packet)

    def test_decision_packet_scouts_done(self):
        goal = bus.create_task("GOAL: scouts done decision", "goal spec",
                               ["Planner closes the goal with a PR"], ["**"], role="triage", complexity=5)
        goal_id = goal["id"]
        s1 = bus.create_task("scout one", "spec", ["x"], ["y"], role="scout", parent=goal_id, complexity=3)
        bus.post_result(s1["id"], {"summary": "a" * 400}, status="done")
        s2 = bus.create_task("scout two", "spec", ["x"], ["y"], role="scout", parent=goal_id, complexity=3)
        bus.update(s2["id"], status="failed")

        packet = goals.decision_packet(goal_id, "scouts_done", goal_id)

        self.assertIn("Scouts:", packet)
        self.assertIn(f"{s1['id']}: {'a' * 300}", packet)
        self.assertNotIn("a" * 301, packet)  # summary capped to the first 300 chars
        self.assertIn(f"{s2['id']}: ", packet)

    def test_decision_packet_capped(self):
        goal = bus.create_task("GOAL: cap decision", "goal spec", ["Planner closes the goal with a PR"], ["**"],
                               role="triage", complexity=5)
        goal_id = goal["id"]
        for i in range(30):
            s = bus.create_task(f"scout {i}", "spec", ["x"], ["y"], role="scout", parent=goal_id, complexity=3)
            bus.post_result(s["id"], {"summary": "s" * 300}, status="done")

        cfg = tomllib.loads((goals.PACKAGE_REPO / ".orchestrator" / "pool.toml").read_text())
        cap = cfg["planner"]["decision_packet_chars"]

        packet = goals.decision_packet(goal_id, "scouts_done", goal_id)
        self.assertEqual(len(packet), cap)


class CliGoalOutput(GoalsTestCase):
    def _run_cli(self, argv):
        buf = io.StringIO()
        orig_argv = sys.argv
        sys.argv = ["orchestrator"] + argv
        try:
            with contextlib.redirect_stdout(buf):
                cli.main()
        finally:
            sys.argv = orig_argv
        return buf.getvalue()

    def test_goal_status_and_list_human_readable_unless_json(self):
        repo = self.repo("cli-output")
        fake_entries = [{"goal_id": "T-7000", "repo": str(repo), "record_status": "running",
                         "task_status": "queued", "children": {"done": [{"id": "T-7001", "hold_reason": None}]},
                         "merged": [], "pr_url": None, "planner_alive": True, "note": None}]
        orig_status, orig_list = goals.status, goals.list_goals
        goals.status = lambda repo_arg, id_arg=None: fake_entries
        goals.list_goals = lambda repo_arg: fake_entries
        self.addCleanup(lambda: setattr(goals, "status", orig_status))
        self.addCleanup(lambda: setattr(goals, "list_goals", orig_list))

        out = self._run_cli(["goal", "status", str(repo)]).strip()
        self.assertFalse(out.startswith("{") or out.startswith("["))
        self.assertIn("T-7000", out)

        out_json = self._run_cli(["goal", "status", str(repo), "--json"]).strip()
        self.assertTrue(out_json.startswith("{"))

        out_list = self._run_cli(["goal", "list", str(repo)]).strip()
        self.assertFalse(out_list.startswith("{") or out_list.startswith("["))

        out_list_json = self._run_cli(["goal", "list", str(repo), "--json"]).strip()
        self.assertTrue(out_list_json.startswith("{"))

    def test_format_goal_line_prints_note_when_present(self):
        entry = {"goal_id": "T-1", "record_status": "running", "planner_alive": None,
                  "children": {}, "pr_url": None,
                  "note": "process-start verification unavailable on this platform; liveness check only"}
        line = cli._format_goal_line(entry)
        self.assertIn("note=process-start verification unavailable on this platform", line)

        no_note = {**entry, "note": None}
        self.assertNotIn("note=", cli._format_goal_line(no_note))


if __name__ == "__main__":
    unittest.main()
