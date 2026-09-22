"""spawn.run_worker's review-verdict propagation, prompt template rendering / result fitting, base-branch
selection for stacked/challenge/review tasks (review T-0026, T-0030), and headless-host secret/token wiring
(env-form secrets, CLAUDE_CODE_OAUTH_TOKEN injection)."""
import json, os, subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_spawn.py` doesn't add this dir itself
from _harness import TMP, g, scratch_repo
from orchestrator import bus, pool as P, spawn


class FakePopen:
    """Stand-in for subprocess.Popen: run_claude only reads .pid, .communicate() and .returncode."""
    def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None, text=None):
        FakePopen.last_env = env
        self.pid = 4242
        self.returncode = 0

    def communicate(self, timeout=None):
        return json.dumps({"result": "ok", "usage": {}}), ""

    def kill(self):
        pass


class ReviewVerdict(unittest.TestCase):
    def test_review_completion_keeps_reviewed_sha(self):
        reviewed = bus.create_task("review sha target", "s", ["a"], ["sha.py"], role="execute")
        review = bus.create_task("review sha", "s", ["a"], ["sha.py"], role="review",
                                 inputs=[reviewed["id"]])
        bus.update(review["id"], reviewed_sha="stamped-sha")
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(spawn, "run_claude", return_value={"status": "done", "output": {
                    "result": json.dumps({"verdict": "approve", "comments": []}), "usage": {}}}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP / "wt" / review["id"]):
            spawn.run_worker(review["id"])

        updated = bus.get(review["id"])
        self.assertEqual(updated["reviewed_sha"], "stamped-sha")
        self.assertEqual(updated["review_facts"]["reviewed_sha"], "stamped-sha")

    def test_review_run_row_and_task_carry_review_facts(self):
        reviewed = bus.create_task("review facts target", "s", ["a"], ["facts.py"], role="execute")
        bus.update(reviewed["id"], pipeline={"reviews_expected": 1})
        review = bus.create_task("review facts", "s", ["a"], ["facts.py"], role="review",
                                 inputs=[reviewed["id"]], complexity=7,
                                 constraints={"reviewed_sha": "deadbeef", "reviewer_role": "security"})
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)
        original_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", original_pick))
        comments = [{"severity": "high"}, {"severity": "medium"}, {"severity": "info"}]
        original = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "done", "output": {
            "result": json.dumps({"verdict": "request_changes", "comments": comments}), "usage": {}}}
        self.addCleanup(lambda: setattr(spawn, "run_claude", original))
        with mock.patch.object(spawn, "ensure_worktree", return_value=TMP / "wt" / review["id"]):
            spawn.run_worker(review["id"])
        updated = bus.get(review["id"])
        facts = updated["review_facts"]
        self.assertEqual(facts["findings_count"], 3)
        self.assertEqual(facts["packet_version"], updated["result"]["packet_version"])
        for key in ("verdict", "findings_count", "findings_by_severity", "reviewer_role", "checklist_used",
                    "reviewed_sha", "packet_version", "review_pass_index"):
            self.assertIn(key, updated)

    def test_packet_dependencies_section_lists_depends_on(self):
        dep = bus.create_task("D" * 110, "s", ["a"], ["x.py"], role="execute")
        bus.update(dep["id"], status="done", merged_into="goal/G", sha="abc12345")
        task = bus.create_task("dependent", "s", ["keep every criterion"], ["x.py"],
                               role="execute", depends_on=[dep["id"]])
        text = spawn.packet(task, TMP)
        self.assertGreater(text.index("## dependencies"), text.index("## evidence"))
        for value in (dep["id"], "D" * 90, "status: done", "merged_into: goal/G", "merged sha: abc12345"):
            self.assertIn(value, text)
        self.assertNotIn("D" * 91, text)
        self.assertNotIn("## dependencies", spawn.packet({**task, "depends_on": []}, TMP))
        large = {**task, "acceptance": ["criterion " + "x" * 5000]}
        bounded = spawn.packet(large, TMP)
        self.assertNotIn("## dependencies", bounded)
        self.assertIn(large["acceptance"][0], bounded)
        self.assertIn(".claude/hooks/tests-green.sh .", bounded)

    def test_render_flags_unfilled_placeholder(self):
        with self.assertRaisesRegex(ValueError, "unfilled_placeholder: packet"):
            spawn.render("execute", spec="s", acceptance=["a"], scope=["x.py"])
        text = spawn.render("execute", packet="brief", spec="s", acceptance=["a"], scope=["x.py"])
        self.assertNotIn("{{", text)

    def test_render_validates_against_template_not_output(self):
        token = "{" * 2 + "acceptance" + "}" * 2
        text = spawn.render("execute", packet="quotes " + token)
        self.assertEqual(text.count(token), 1)
        with self.assertRaisesRegex(ValueError, "unfilled_placeholder: packet"):
            spawn.render("execute")

    def test_render_single_pass_never_resubstitutes(self):
        token = "{" * 2 + "packet" + "}" * 2
        contract = "The packet above is the task contract"
        text = spawn.render("execute", packet=token)
        self.assertEqual(text.count(token), 1)
        self.assertEqual(text.count(contract), 1)

    def test_run_worker_holds_on_render_error(self):
        source = bus.create_task("render source", "s", ["a"], ["x.py"], role="execute")
        review = bus.create_task("render review", "s", ["a"], ["x.py"], role="review",
                                 inputs=[source["id"]])
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(spawn, "render", side_effect=ValueError("unfilled_placeholder: packet")), \
                mock.patch.object(spawn.notify, "notify") as notify:
            result = spawn.run_worker(review["id"])
        updated = bus.get(review["id"])
        self.assertEqual(result, {"status": "held", "reason": "render_error"})
        self.assertEqual(updated["status"], "held")
        self.assertTrue(updated["hold_reason"].startswith("render_error"))
        self.assertIsNone(updated.get("assigned_to"))
        notify.assert_called_once()

    def test_ensure_worktree_reuses_existing_task_branch(self):
        with tempfile.TemporaryDirectory(prefix="orch-worktree-") as directory:
            root = Path(directory)
            wt = root / "wt" / "T-x"
            self.assertFalse(wt.exists())

            def git(*args, **kwargs):
                if args[:2] == ("worktree", "add"):
                    self.assertEqual(args, ("worktree", "add", str(wt), "task/T-x"))
                    wt.mkdir()
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with mock.patch.object(spawn, "ROOT", root), mock.patch.object(spawn, "git", side_effect=git) as run:
                self.assertEqual(spawn.ensure_worktree("T-x", base="HEAD"), wt)
            self.assertTrue(wt.is_dir())
            run.assert_any_call("rev-parse", "--verify", "task/T-x", check=False)
            self.assertEqual(sum(call.args[:2] == ("worktree", "add") for call in run.call_args_list), 1)

    def test_run_worker_captures_verdict_on_review_and_reviewed_task(self):
        reviewed = bus.create_task("feat-rv", "s", ["a"], ["rv.py"], role="execute")
        review = bus.create_task("review feat-rv", "s", ["a"], ["rv.py"], role="review", inputs=[reviewed["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        fake_out = {"result": json.dumps({"verdict": "request_changes", "comments": []}), "usage": {}}
        orig_run_claude = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "done", "output": fake_out}
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])
        self.assertEqual(bus.get(review["id"])["review_verdict"], "request_changes")
        self.assertEqual(bus.get(reviewed["id"])["review_verdict"], "request_changes")


class ReviewWithoutInputs(unittest.TestCase):
    def test_review_without_inputs_falls_back_to_task(self):
        review = bus.create_task("review-without-inputs", "task spec", ["task acceptance"], ["task.py"],
                                 role="review", inputs=[])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        seen = {}
        orig_scoped_diff = spawn.scoped_diff
        def fake_scoped_diff(src):
            seen["src"] = src
            return "TASK_FALLBACK_DIFF"
        spawn.scoped_diff = fake_scoped_diff
        self.addCleanup(lambda: setattr(spawn, "scoped_diff", orig_scoped_diff))

        captured = {}
        orig_run_claude = spawn.run_claude
        def fake_run_claude(*args, **kwargs):
            captured["prompt"] = args[3]
            return {"status": "done", "output": {"result": json.dumps({"verdict": "approve", "comments": []}),
                                                       "usage": {}}}
        spawn.run_claude = fake_run_claude
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])

        self.assertEqual(seen["src"]["id"], review["id"])
        self.assertIn("task acceptance", captured["prompt"])


class ReviewAvoidsAccount(unittest.TestCase):
    def test_review_avoids_executing_account_from_assigned_to(self):
        reviewed = bus.create_task("feat-avoid", "s", ["a"], ["rv.py"], role="execute")
        bus.update(reviewed["id"], assigned_to="claude:B")   # no explicit "account" field on the reviewed task
        review = bus.create_task("review feat-avoid", "s", ["a"], ["rv.py"], role="review", inputs=[reviewed["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        captured = {}
        orig_pick = P.Pool.pick
        def fake_pick(self, role, avoid=None):
            captured["avoid"] = avoid
            return self.get("A")
        P.Pool.pick = fake_pick
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        fake_out = {"result": json.dumps({"verdict": "approve", "comments": []}), "usage": {}}
        orig_run_claude = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "done", "output": fake_out}
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])
        self.assertEqual(captured["avoid"], "B")


class ReviewVerdictSurvivesParseFailure(unittest.TestCase):
    """T-0139/T-0141: a reviewer that posts its verdict itself via bus_post_result mid-run, then ends with text
    the daemon can't parse, must not lose that verdict to a second, verdict-less post."""

    def test_verdict_survives_unparseable_final_text(self):
        reviewed = bus.create_task("feat-parsefail", "s", ["a"], ["rv.py"], role="execute")
        review = bus.create_task("review feat-parsefail", "s", ["a"], ["rv.py"], role="review", inputs=[reviewed["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        # Simulate the worker's own bus_post_result MCP call mid-run, before its final text fails to parse below.
        bus.post_result(review["id"], {"verdict": "approve", "comments": []})

        fake_out = {"result": "```json\n{unparseable: true}\n```", "usage": {}}
        orig_run_claude = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "done", "output": fake_out}
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])

        updated = bus.get(review["id"])
        self.assertEqual(updated["result"]["verdict"], "approve")
        self.assertEqual(updated["review_verdict"], "approve")
        self.assertEqual(bus.get(reviewed["id"])["review_verdict"], "approve")


class ReviewNoVerdictAnywhereFails(unittest.TestCase):
    def test_unparseable_with_no_prior_verdict_fails_with_raw_hint(self):
        reviewed = bus.create_task("feat-noverdict", "s", ["a"], ["rv.py"], role="execute")
        review = bus.create_task("review feat-noverdict", "s", ["a"], ["rv.py"], role="review", inputs=[reviewed["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        fake_out = {"result": "```json\n{unparseable: true}\n```", "usage": {}}
        orig_run_claude = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "done", "output": fake_out}
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])

        updated = bus.get(review["id"])
        self.assertEqual(updated["status"], "failed")
        self.assertIn("verdict", updated["reason"])
        self.assertIn("unparseable", updated["resume_hint"]["raw"])


class RunClaudeBudgetExitReason(unittest.TestCase):
    """T-0134: a non-zero exit with parseable JSON (typical of --max-budget-usd cutoffs) must carry a "reason"
    string, not silently drop into a dict run_worker can't read."""

    class FakePopenBudgetExceeded(FakePopen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.returncode = 1

        def communicate(self, timeout=None):
            return json.dumps({"is_error": True, "result": "budget exceeded", "usage": {}}), ""

    def setUp(self):
        self.orig_popen = spawn.subprocess.Popen
        spawn.subprocess.Popen = self.FakePopenBudgetExceeded
        self.addCleanup(lambda: setattr(spawn.subprocess, "Popen", self.orig_popen))
        orig_trust = spawn.trust_workspace
        spawn.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(lambda: setattr(spawn, "trust_workspace", orig_trust))

    def test_non_zero_exit_with_json_returns_reason(self):
        t = bus.create_task("budget-test", "s", ["a"], ["x.py"], role="execute", tier="sonnet", complexity=3)
        t["worktree"] = str(TMP)
        acct = P.Account("A", "~/.claude-a", ["execute"])
        pool = P.Pool()
        r = spawn.run_claude(pool, acct, t, "prompt", "claude-sonnet-5", spawn.TOOLS["execute"], 2.0, 60)
        self.assertEqual(r["status"], "failed")
        self.assertIn("rc=1", r["reason"])
        self.assertIn("budget exceeded", r["reason"])


class RunClaudeHoldsWhenCliMissing(unittest.TestCase):
    def setUp(self):
        orig_trust = spawn.trust_workspace
        spawn.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(lambda: setattr(spawn, "trust_workspace", orig_trust))

    def test_run_claude_holds_when_cli_missing(self):
        """Gotcha 2026-09-19: a missing `claude` binary must hold the task visibly (run logged with outcome
        "no_cli"), not crash the worker thread with FileNotFoundError and leave the task stuck "running" until
        something else requeues it as "process died" forever."""
        orig_which = spawn.shutil.which
        spawn.shutil.which = lambda name: None if name == "claude" else orig_which(name)
        self.addCleanup(lambda: setattr(spawn.shutil, "which", orig_which))

        popen_called = []
        def fail_if_called(*a, **k):
            popen_called.append(True)
            raise AssertionError("Popen must not be called when the claude CLI is missing")
        orig_popen = spawn.subprocess.Popen
        spawn.subprocess.Popen = fail_if_called
        self.addCleanup(lambda: setattr(spawn.subprocess, "Popen", orig_popen))

        t = bus.create_task("no-cli-test", "s", ["a"], ["x.py"], role="execute", tier="sonnet", complexity=3)
        t["worktree"] = str(TMP)
        acct = P.Account("A", "~/.claude-a", ["execute"])
        pool = P.Pool()

        runs_dir = TMP / ".orchestrator" / "runs"
        before = len(list(runs_dir.glob("*.jsonl"))) if runs_dir.exists() else 0
        r = spawn.run_claude(pool, acct, t, "prompt", "claude-sonnet-5", spawn.TOOLS["execute"], 2.0, 60)

        self.assertEqual(popen_called, [])
        self.assertEqual(r["status"], "held")
        self.assertIn("claude CLI not found", r["reason"])

        run_files = sorted(runs_dir.glob("*.jsonl"))
        self.assertGreaterEqual(len(run_files), max(before, 1))
        last_line = run_files[-1].read_text().strip().splitlines()[-1]
        self.assertEqual(json.loads(last_line)["outcome"], "no_cli")


class RunClaudeToolsAndMcpConfig(unittest.TestCase):
    """T-0212: TOOLS[role] was defined but never reached the `claude` argv (dead config); --strict-mcp-config +
    an explicit --mcp-config keeps workers off the github/orchestrator MCP schemas they never call."""

    class CapturingPopen(FakePopen):
        last_cmd = None

        def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None, text=None):
            RunClaudeToolsAndMcpConfig.CapturingPopen.last_cmd = cmd
            super().__init__(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr, text=text)

    def setUp(self):
        self.orig_popen = spawn.subprocess.Popen
        spawn.subprocess.Popen = self.CapturingPopen
        self.addCleanup(lambda: setattr(spawn.subprocess, "Popen", self.orig_popen))
        orig_trust = spawn.trust_workspace
        spawn.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(lambda: setattr(spawn, "trust_workspace", orig_trust))

    def run_claude_task(self, role="execute"):
        t = bus.create_task("tools-test", "s", ["a"], ["x.py"], role=role, tier="sonnet", complexity=3)
        t["worktree"] = str(TMP)
        acct = P.Account("A", "~/.claude-a", [role])
        pool = P.Pool()
        r = spawn.run_claude(pool, acct, t, "prompt", "claude-sonnet-5", spawn.TOOLS[role], 2.0, 60)
        self.assertEqual(r["status"], "done")
        return self.CapturingPopen.last_cmd

    def test_run_claude_passes_allowed_tools(self):
        cmd = self.run_claude_task(role="execute")
        self.assertIn("--allowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], spawn.TOOLS["execute"])

    def test_run_claude_uses_worker_mcp_config(self):
        """No .mcp.<role>.json override exists for "execute": falls back to the tracked, bus-only worker config."""
        cmd = self.run_claude_task(role="execute")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], str(spawn.ROOT / ".mcp.worker.json"))

    def test_run_claude_keeps_role_override_mcp_config_when_present(self):
        # "triage" (not "review"): [secrets.review] shells out for a real token in pool.toml, which would hit
        # this test's patched subprocess.Popen; [secrets.triage] is empty so resolve_secrets never calls it.
        override = spawn.ROOT / ".mcp.triage.json"
        override.write_text('{"mcpServers": {}}')
        self.addCleanup(override.unlink)
        cmd = self.run_claude_task(role="triage")
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], str(override))


class FakePopenWithTurns(FakePopen):
    def communicate(self, timeout=None):
        return json.dumps({"result": "ok", "usage": {}, "num_turns": 7}), ""


class RunRecordHasTurns(unittest.TestCase):
    """bus.log_run's run record must carry claude's num_turns so the scorecard can show turns per run."""

    def setUp(self):
        self.orig_popen = spawn.subprocess.Popen
        spawn.subprocess.Popen = FakePopenWithTurns
        self.addCleanup(lambda: setattr(spawn.subprocess, "Popen", self.orig_popen))
        orig_trust = spawn.trust_workspace
        spawn.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(lambda: setattr(spawn, "trust_workspace", orig_trust))

    def test_run_record_has_turns(self):
        t = bus.create_task("turns-test", "s", ["a"], ["x.py"], role="execute", tier="sonnet", complexity=3)
        t["worktree"] = str(TMP)
        acct = P.Account("A", "~/.claude-a", ["execute"])
        pool = P.Pool()

        runs_dir = TMP / ".orchestrator" / "runs"
        before = len(list(runs_dir.glob("*.jsonl"))) if runs_dir.exists() else 0
        r = spawn.run_claude(pool, acct, t, "prompt", "claude-sonnet-5", spawn.TOOLS["execute"], 2.0, 60)
        self.assertEqual(r["status"], "done")

        run_files = sorted(runs_dir.glob("*.jsonl"))
        self.assertGreaterEqual(len(run_files), max(before, 1))
        last_line = run_files[-1].read_text().strip().splitlines()[-1]
        self.assertEqual(json.loads(last_line)["turns"], 7)


class RunClaudeNormalisedUsage(unittest.TestCase):
    class UsagePopen(FakePopen):
        def communicate(self, timeout=None):
            return json.dumps({"result": "ok", "usage": {"input_tokens": 10,
                               "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
                               "output_tokens": 5, "reasoning_tokens": 4}}), ""

    def setUp(self):
        self.orig_popen = spawn.subprocess.Popen
        spawn.subprocess.Popen = self.UsagePopen
        self.addCleanup(lambda: setattr(spawn.subprocess, "Popen", self.orig_popen))
        orig_trust = spawn.trust_workspace
        spawn.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(lambda: setattr(spawn, "trust_workspace", orig_trust))

    def test_run_row_has_normalised_tokens(self):
        goal = bus.create_task("usage-goal", "s", ["a"], ["x.py"])
        task = bus.create_task("usage-task", "s", ["a"], ["x.py"], role="execute", parent=goal["id"])
        task["worktree"] = str(TMP)
        account = P.Account("A", "~/.claude-a", ["execute"])
        result = spawn.run_claude(P.Pool(), account, task, "prompt", "claude-sonnet-5",
                                  spawn.TOOLS["execute"], 2.0, 60)
        self.assertEqual(result["status"], "done")
        row = json.loads(next(bus.RUNS.glob("*.jsonl")).read_text().splitlines()[-1])
        self.assertEqual(row["goal_id"], goal["id"])
        self.assertEqual(row["provider"], "claude")
        self.assertEqual({key: row[key] for key in ("input_uncached_tokens", "cache_read_tokens",
                         "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens")},
                         {"input_uncached_tokens": 10, "cache_read_tokens": 3, "cache_write_tokens": 2,
                          "output_tokens": 5, "reasoning_tokens": 4, "total_tokens": 20})


class RunWorkerMissingReason(unittest.TestCase):
    """T-0134: run_worker must not KeyError when run_claude returns a failure dict without a "reason" key, and
    should preserve any partial output as a resume_hint for the next attempt."""

    def test_failed_without_reason_key_sets_default_and_resume_hint(self):
        task = bus.create_task("feat-noreason", "s", ["a"], ["rv.py"], role="execute")
        (TMP / "wt" / task["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        orig_run_claude = spawn.run_claude
        spawn.run_claude = lambda *a, **k: {"status": "failed", "output": {"result": "partial"}}
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(task["id"])
        updated = bus.get(task["id"])
        self.assertEqual(updated["status"], "failed")
        self.assertEqual(updated["reason"], "unknown failure")
        self.assertEqual(updated["resume_hint"]["partial_output"], "partial")


class SpecReview(unittest.TestCase):
    def test_run_worker_writes_verdict_on_both_tasks_and_prompt_has_minimal_packet(self):
        scratch_repo(TMP)
        (TMP / "spec_review_target.py").write_text("def handler():\n    return 1\n")

        execute = bus.create_task("feat-sr", "implement the thing precisely", ["it works"],
                                   ["spec_review_target.py"], role="execute")
        review = bus.create_task("spec review feat-sr", "s", ["a"], ["spec_review_target.py"],
                                  role="spec_review", inputs=[execute["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls

        orig_pick = P.Pool.pick
        P.Pool.pick = lambda self, role, avoid=None: self.get("A")
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))

        fake_result = json.dumps({"verdict": "request_changes",
                                   "risks": [{"path": "spec_review_target.py", "issue": "no error handling", "severity": "med"}]})
        fake_out = {"result": fake_result, "usage": {}}
        captured = {}

        def fake_run_claude(pool, acct, task, prompt, model, tools, max_budget_usd, timeout):
            captured["prompt"] = prompt
            return {"status": "done", "output": fake_out}

        orig_run_claude = spawn.run_claude
        spawn.run_claude = fake_run_claude
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run_claude))

        spawn.run_worker(review["id"])

        self.assertEqual(bus.get(review["id"])["spec_review_verdict"], "request_changes")
        self.assertEqual(bus.get(execute["id"])["spec_review_verdict"], "request_changes")
        self.assertEqual(bus.get(execute["id"])["spec_review_risks"][0]["severity"], "med")
        self.assertIn("implement the thing precisely", captured["prompt"])
        self.assertIn("## existing tests", captured["prompt"])
        self.assertIn("tests/test_spec_review_target.py: missing", captured["prompt"])
        self.assertNotIn("def handler():", captured["prompt"])


class Render(unittest.TestCase):
    def packet_fixture(self):
        scratch_repo(TMP)
        (TMP / "widget.py").write_text("import json\n\ndef build_widget():\n    return json.dumps({})\n")
        (TMP / "tests").mkdir(exist_ok=True)
        (TMP / "tests" / "test_widget.py").write_text(
            "from widget import build_widget\n\ndef test_build_widget():\n    assert build_widget()\n")
        return {"id": "T-P", "title": "Build widget", "acceptance": ["works"],
                "scope": ["widget.py"], "parent": "G", "inputs": []}

    def test_packet_sections_in_order(self):
        text = spawn.packet(self.packet_fixture(), TMP)
        names = ["objective", "acceptance", "base", "write_scope", "read_scope", "relevant_tests",
                 "symbols", "gotchas", "decisions", "verify", "evidence"]
        positions = [text.index(f"## {name}") for name in names]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("tests/test_widget.py", text)
        self.assertIn("widget.py:3 build_widget", text)

    def test_review_packet_has_spec_acceptance_diff_tests_gate_in_order(self):
        task = self.packet_fixture()
        task.update(spec="precise spec", pipeline={"gated_at": "now", "gate_attempts": 2,
                    "gate_reds": 1, "first_green_at": None, "last_failure_text": "boom\ndetail"},
                    acceptance=["tests/test_widget.py::test_build_widget passes"])
        text = spawn.review_packet(task, task)
        names = ["spec", "acceptance", "scope", "diff", "changed tests", "gate"]
        self.assertEqual([text.index(f"## {n}") for n in names], sorted(text.index(f"## {n}") for n in names))
        self.assertIn("test_build_widget: present", text)
        self.assertIn("last_failure_head: boom", text)

    def test_review_packet_role_section_for_each_role(self):
        reviewed = {**self.packet_fixture(), "spec": "ordinary change"}
        packets = {}
        for role in ("acceptance", "adversarial"):
            task = {**reviewed, "constraints": {"reviewer_role": role}}
            packets[role] = spawn.review_packet(task, reviewed)
            self.assertIn("## role", packets[role])
            self.assertIn(f"reviewer-role@{role}", packets[role].splitlines()[0])
        self.assertIn("every acceptance criterion", packets["acceptance"])
        self.assertIn("adversarial reasoning", packets["adversarial"])
        self.assertNotEqual(spawn.packet_run_meta(packets["acceptance"])["version"],
                            spawn.packet_run_meta(packets["adversarial"])["version"])

    def test_review_packet_security_section_present_for_both_roles_on_security_path(self):
        reviewed = {**self.packet_fixture(), "spec": "ordinary change", "scope": ["auth/login.py"]}
        cfg = {**P.config(), "review": {"security_paths": ["auth/*"]}}
        with mock.patch.object(P, "config", return_value=cfg):
            for role in ("acceptance", "adversarial"):
                task = {**reviewed, "constraints": {"reviewer_role": role}}
                text = spawn.review_packet(task, reviewed)
                self.assertGreater(text.index("## security"), text.index("## role"))

    def test_review_packet_without_role_unchanged(self):
        task = {**self.packet_fixture(), "spec": "ordinary change"}
        without_constraints = spawn.review_packet(task, task)
        with_empty_constraints = spawn.review_packet({**task, "constraints": {}}, task)
        self.assertEqual(without_constraints, with_empty_constraints)
        self.assertNotIn("## role", without_constraints)
        self.assertNotIn("reviewer-role@", without_constraints.splitlines()[0])

    def test_review_packet_security_section_only_on_security_path(self):
        task = {**self.packet_fixture(), "spec": "ordinary change"}
        cfg = {**P.config(), "review": {"security_paths": ["auth/*"]}}
        with mock.patch.object(P, "config", return_value=cfg):
            self.assertNotIn("## security", spawn.review_packet(task, task))
            task["scope"] = ["auth/login.py"]
            self.assertIn("## security", spawn.review_packet(task, task))

    def test_review_packet_security_section_on_complexity_bump_and_fail_closed_reasons(self):
        cfg = {**P.config(), "review": {"security_paths": ["auth/*"]}}
        reviewed = {**self.packet_fixture(), "spec": "ordinary change", "complexity": 4}
        with mock.patch.object(P, "config", return_value=cfg):
            review = {**reviewed, "complexity": 7}
            self.assertIn("## security", spawn.review_packet(review, reviewed))
            reviewed["pipeline"] = {"review_reason": "diff_unavailable"}
            text = spawn.review_packet({**reviewed, "complexity": 4}, reviewed)
            self.assertIn("## security", text)
            self.assertIn("review_reason: diff_unavailable", text)
            reviewed["pipeline"] = {}
            self.assertNotIn("## security", spawn.review_packet({**reviewed, "complexity": 4}, reviewed))
        empty_cfg = {**P.config(), "review": {"security_paths": []}}
        with mock.patch.object(P, "config", return_value=empty_cfg):
            text = spawn.review_packet({**reviewed, "complexity": 4}, reviewed)
            self.assertIn("## security", text)
            self.assertIn("review_reason: security_paths_empty", text)

    def test_review_packet_single_bounding_pass_no_nested_truncation(self):
        task = {**self.packet_fixture(), "spec": "ordinary change",
                "acceptance": ["a" * 1500], "scope": ["widget.py", "x" * 1500],
                "pipeline": {"last_failure_text": "g" * 1500}}
        raw = "diff --git a/widget.py b/widget.py\n" + "\n".join(f"+line {i} " + "x" * 80 for i in range(400))
        with mock.patch.object(spawn, "scoped_diff", return_value=raw):
            text = spawn.review_packet(task, task)
        self.assertLessEqual(len(text), 8200)
        self.assertEqual(text.count("expand with:"), 1)
        self.assertIn(f"expand with: git -C {TMP} diff -- widget.py {'x' * 1500}", text)

    def test_review_packet_excludes_other_tasks_and_memory(self):
        task = {**self.packet_fixture(), "spec": "only this task"}
        memory = TMP / ".orchestrator/memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "gotchas.md").write_text("## SECRET GOTCHA\nbody that must not leak\n")
        bus.create_task("OTHER FINISHED TASK", "other text", ["other acceptance"], ["other.py"])
        text = spawn.review_packet(task, task)
        self.assertNotIn("OTHER FINISHED TASK", text)
        self.assertNotIn("SECRET GOTCHA", text)

    def test_spec_review_packet_minimal_fields(self):
        task = {**self.packet_fixture(), "spec": "review me", "complexity": 4, "tier": "sonnet"}
        text = spawn.spec_review_packet(task)
        for name in ("spec", "acceptance", "scope", "depends_on", "existing tests", "complexity", "tier"):
            self.assertIn(f"## {name}", text)
        self.assertNotIn("## diff", text)

    def test_scout_packet_bounded_tree_and_memory_titles_only(self):
        memory = TMP / ".orchestrator/memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "index.md").write_text("## Useful title\nPRIVATE BODY\n")
        task = {**self.packet_fixture(), "spec": "find facts"}
        text = spawn.scout_packet(task)
        self.assertIn("Useful title", text)
        self.assertNotIn("PRIVATE BODY", text)
        tree = text.split("## scope tree\n", 1)[1].split("\n## ", 1)[0]
        self.assertLessEqual(len(tree.splitlines()), 60)

    def test_role_prompts_have_no_unfilled_placeholder(self):
        task = {**self.packet_fixture(), "spec": "s", "complexity": 3, "tier": "sonnet"}
        packets = {"review": spawn.review_packet(task, task),
                   "spec-review": spawn.spec_review_packet(task), "scout": spawn.scout_packet(task)}
        for role, role_packet in packets.items():
            self.assertNotIn("{{", spawn.render(role, packet=role_packet))

    def test_packet_meta_logged_for_review_and_scout(self):
        for role, builder in (("review", lambda t: spawn.review_packet(t, t)),
                              ("scout", spawn.scout_packet)):
            task = {**self.packet_fixture(), "spec": "s", "role": role}
            meta = {**spawn.packet_run_meta(builder(task)), "role": role}
            self.assertEqual(meta["role"], role)
            self.assertGreater(meta["chars"], 0)
            self.assertEqual(meta["hash"], meta["version"])

    def test_packet_over_cap_keeps_every_acceptance_criterion(self):
        task = self.packet_fixture()
        task["acceptance"] = [f"criterion {i} " + "x" * 100 for i in range(100)]
        text = spawn.packet(task, TMP)
        acceptance = text.split("## acceptance\n", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(acceptance.count("criterion "), 100)
        self.assertNotIn("more" + " in task", acceptance)
        self.assertIn("over cap by", text.splitlines()[0])
        self.assertNotIn("widget.py:3 build_widget", text)

    def test_packet_header_has_hash_base_and_sources(self):
        text = spawn.packet(self.packet_fixture(), TMP)
        meta = spawn.packet_meta(self.packet_fixture(), TMP)
        self.assertEqual(text.splitlines()[0],
                         f"packet v{meta['hash']} base {meta['base']} sources "
                         f"pool.toml@{meta['policy_version']} gotchas@{meta['gotchas']} memory@notes,bus")

    def test_packet_hash_changes_when_body_changes(self):
        task = self.packet_fixture()
        first = spawn.packet_meta(task, TMP)["hash"]
        task["acceptance"].append("another criterion")
        self.assertNotEqual(first, spawn.packet_meta(task, TMP)["hash"])

    def test_packet_never_trims_base_or_verify(self):
        task = self.packet_fixture()
        task["acceptance"] = [f"criterion {i} " + "x" * 100 for i in range(100)]
        text = spawn.packet(task, TMP)
        base = text.split("## base\n", 1)[1].split("\n## ", 1)[0]
        verify = text.split("## verify\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("- branch:", base)
        self.assertIn("- merge-base", base)
        self.assertEqual(verify, "- .claude/hooks/tests-green.sh .\n- On failure, report only scripts/failures_only.sh output.")

    def test_relevant_tests_scope_files_first(self):
        scratch_repo(TMP)
        (TMP / "widget.py").write_text("def inspect_widget():\n    return True\n")
        (TMP / "tests").mkdir(exist_ok=True)
        (TMP / "tests" / "test_scoped.py").write_text("def test_scoped():\n    assert True\n")
        (TMP / "tests" / "test_widget.py").write_text(
            "from widget import inspect_widget\n\ndef test_symbol_match():\n    assert inspect_widget()\n")
        task = {"title": "x", "acceptance": [], "scope": ["tests/test_scoped.py", "widget.py"], "inputs": []}
        relevant = spawn.packet(task, TMP).split("## relevant_tests\n", 1)[1].split("\n## ", 1)[0].splitlines()
        self.assertEqual(relevant[:2], ["- tests/test_scoped.py", "- tests/test_widget.py"])

    def test_relevant_tests_ignores_short_symbols(self):
        scratch_repo(TMP)
        (TMP / "tiny.py").write_text("def get():\n    return True\n")
        (TMP / "tests").mkdir(exist_ok=True)
        (TMP / "tests" / "test_other.py").write_text("def test_ordinary_word():\n    assert get is not None\n")
        task = {"title": "x", "acceptance": [], "scope": ["tiny.py"], "inputs": []}
        relevant = spawn.packet(task, TMP).split("## relevant_tests\n", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("test_other.py::test_ordinary_word", relevant)

    def test_relevant_tests_ranked_by_specificity(self):
        scratch_repo(TMP)
        (TMP / "widget.py").write_text("def build_widget():\n    return True\n\ndef inspect_widget():\n    return True\n")
        (TMP / "tests").mkdir(exist_ok=True)
        (TMP / "tests" / "test_other.py").write_text(
            "from widget import build_widget, inspect_widget\n\ndef test_one():\n    assert build_widget()\n\ndef test_two():\n    assert build_widget() and inspect_widget()\n")
        task = {"title": "x", "acceptance": [], "scope": ["widget.py"], "inputs": []}
        relevant = spawn.packet(task, TMP).split("## relevant_tests\n", 1)[1].split("\n## ", 1)[0].splitlines()
        self.assertEqual(relevant[1:3], ["- tests/test_other.py::test_two", "- tests/test_other.py::test_one"])

    def test_execute_prompt_carries_contract_once(self):
        task = self.packet_fixture()
        task["spec"] = "full-spec:" + "x" * 9000
        task["acceptance"] = [f"unique acceptance criterion {i}" for i in range(6)]
        task["scope"] = ["alpha.py", "nested/beta.py", "docs/gamma.md"]
        text = spawn.render("execute", packet=spawn.packet(task, TMP))
        self.assertEqual(text.count(task["spec"]), 1)
        for criterion in task["acceptance"]:
            self.assertEqual(text.count(criterion), 1)
        for path in task["scope"]:
            self.assertEqual(text.count(path), 1)
        self.assertNotIn("Spec:", text)

    def test_execute_prompt_contains_packet(self):
        p = spawn.packet(self.packet_fixture(), TMP)
        text = spawn.render("execute", packet=p)
        self.assertTrue(text.startswith("packet v"))
        self.assertIn("Build widget", text)

    def test_execute_prompt_names_gate_and_commit(self):
        text = spawn.render("execute", packet="")
        self.assertIn(".claude/hooks/tests-green.sh", text)
        self.assertIn("git commit", text)
        self.assertNotIn("scripts/tests_green.sh", text)

    def test_fix_delta_prompt_names_gate_and_commit(self):
        text = spawn.render("fix-delta", packet="brief", n=1, failing_tests="x", assertion_lines="y")
        self.assertIn(".claude/hooks/tests-green.sh", text)
        self.assertIn("git commit", text)
        self.assertNotIn("scripts/tests_green.sh", text)

    def test_packet_gotcha_match_by_path(self):
        task = self.packet_fixture()
        memory = TMP / ".orchestrator" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "gotchas.md").write_text("## Widget cache\nFacts: changing widget.py needs a cache reset.\n")
        text = spawn.packet(task, TMP)
        self.assertRegex(text, r"mem:gotchas\.md:1 Widget cache")

    def test_packet_memory_reads_from_monkeypatched_state(self):
        task = self.packet_fixture()
        memory = TMP / ".orchestrator" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "gotchas.md").write_text("## 2026-09-20 Widget cache\nFacts: widget.py needs a cache reset.\n")
        with mock.patch.object(spawn, "STATE", TMP / ".orchestrator"):
            text = spawn.packet(task, TMP)
        self.assertIn("Widget cache", text.split("## gotchas", 1)[1].split("## decisions", 1)[0])

    def test_packet_memory_uses_notes_and_bus_only(self):
        seen = {}
        def fake_recall(query, **kwargs):
            seen.update(kwargs)
            return {"hits": [], "layers_consulted": ["notes", "bus"], "stopped_at": None, "chars": 0}
        with mock.patch.object(spawn, "memory_recall", side_effect=fake_recall):
            text = spawn.packet(self.packet_fixture(), TMP)
        self.assertEqual(seen["layers"], ("notes", "bus"))
        self.assertEqual(seen["budget_hits"], 5)
        self.assertIn("memory@notes,bus", text.splitlines()[0])

    def test_templates_fill(self):
        task = {**self.packet_fixture(), "id": "T-1", "spec": "q", "acceptance": ["a"]}
        s = spawn.render("scout", packet=spawn.scout_packet(task), id="T-1", title="t", turns="20")
        self.assertIn("T-1", s); self.assertNotIn("{{", s)
        self.assertEqual(spawn.extract_json('here: {"summary":"x"} bye')["summary"], "x")
        self.assertTrue(spawn.extract_json("no json")["summary"])

    def test_extract_json_prefers_fenced_block_after_prose_with_braces(self):
        text = 'hold_reason=f"merge {status}"\n```json\n{"verdict":"approve","summary":"ok"}\n```'
        self.assertEqual(spawn.extract_json(text), {"verdict": "approve", "summary": "ok"})

    def test_extract_json_falls_back_to_last_balanced_object(self):
        text = 'review notes {x} then {"verdict":"approve","summary":"ok"}'
        self.assertEqual(spawn.extract_json(text), {"verdict": "approve", "summary": "ok"})

    def test_extract_json_plain_object_and_parse_error_unchanged(self):
        bare = {"verdict": "approve", "summary": "ok"}
        self.assertEqual(spawn.extract_json(json.dumps(bare)), bare)
        self.assertEqual(spawn.extract_json("broken {not json}"),
                         {"summary": "broken {not json}", "parse_error": True})
        self.assertEqual(spawn.extract_json("no braces"), {"summary": "no braces"})

    def test_bounded_diff_summary_first(self):
        diff = "diff --git a/widget.py b/widget.py\nindex 1..2 100644\n--- a/widget.py\n+++ b/widget.py\n@@ -1 +1 @@\n-old\n+new\n"
        bounded = spawn.bounded_diff(diff)
        self.assertTrue(bounded.startswith("Diffstat: "))
        self.assertLess(bounded.index("Diffstat: "), bounded.index("@@"))

    def test_bounded_diff_expansion_hint(self):
        diff = "diff --git a/widget.py b/widget.py\n" + "\n".join(f"+line {i}" for i in range(2000))
        hint = f"git -C {TMP} diff -- widget.py"
        bounded = spawn.bounded_diff(diff, 300, hint)
        self.assertLessEqual(len(bounded), 300)
        self.assertTrue(bounded.endswith(f"expand with: {hint}"))

    def test_review_prompt_diff_is_bounded(self):
        reviewed = bus.create_task("bounded review target", "s", ["a"], ["widget.py"], role="execute")
        bus.update(reviewed["id"], worktree=str(TMP))
        review = bus.create_task("review bounded target", "s", ["a"], ["widget.py"],
                                 role="review", inputs=[reviewed["id"]])
        (TMP / "wt" / review["id"]).mkdir(parents=True, exist_ok=True)  # short-circuits ensure_worktree's git calls
        raw = "diff --git a/widget.py b/widget.py\n" + "\n".join(f"+line {i}" for i in range(5000))
        captured = {}
        orig_diff, orig_pick, orig_run = spawn.scoped_diff, P.Pool.pick, spawn.run_claude
        spawn.scoped_diff = lambda task: raw
        P.Pool.pick = lambda pool, role, avoid=None: pool.get("A")
        def fake_run_claude(*args, **kwargs):
            captured["prompt"] = args[3]
            return {"status": "done", "output": {"result": "{}"}}
        spawn.run_claude = fake_run_claude
        self.addCleanup(lambda: setattr(spawn, "scoped_diff", orig_diff))
        self.addCleanup(lambda: setattr(P.Pool, "pick", orig_pick))
        self.addCleanup(lambda: setattr(spawn, "run_claude", orig_run))

        spawn.run_worker(review["id"])
        prompt = captured["prompt"]
        hint = f"git -C {TMP} diff -- widget.py"
        self.assertIn("Diffstat: ", prompt)
        self.assertLessEqual(len(prompt), P.Pool().cfg["limits"].get("review_diff_chars", 12000) + 2000)
        self.assertEqual(prompt.count(f"expand with: {hint}"), 1)

    def test_bounded_diff_hunk_header_once(self):
        diff = "diff --git a/widget.py b/widget.py\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(sum(line.startswith("@@") for line in spawn.bounded_diff(diff).splitlines()), 1)

    def test_render_does_not_rebound_diff(self):
        hint = f"git -C {TMP} diff -- widget.py"
        raw = "diff --git a/widget.py b/widget.py\n" + "\n".join(f"+line {i}" for i in range(1000))
        task = {**self.packet_fixture(), "spec": "ordinary", "complexity": 1}
        cfg = {**P.config(), "limits": {**P.config().get("limits", {}), "review_diff_chars": 100}}
        with mock.patch.object(P, "config", return_value=cfg), \
                mock.patch.object(spawn, "scoped_diff", return_value=raw):
            packet = spawn.review_packet(task, task)
        prompt = spawn.render("review", packet=packet, complexity="1")
        self.assertEqual(prompt.count("Diffstat: "), 1)
        self.assertEqual(prompt.count(f"expand with: {hint}"), 1)

    def test_render_requires_packet_for_review_spec_review_scout(self):
        for role in ("review", "spec-review", "scout"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, role):
                spawn.render(role)

    def test_fit_result_shrinks_oversize(self):
        big = {"summary": "s" * 3000, "findings": [{"claim": "c" * 380, "confidence": 0.5} for _ in range(40)]}
        fitted = spawn.fit_result(big)
        self.assertLess(len(json.dumps(fitted)), bus.MAX_RESULT_CHARS)
        self.assertEqual(fitted["truncated"]["reason"], "over MAX_RESULT_CHARS")
        self.assertGreater(fitted["truncated"]["original_chars"], bus.MAX_RESULT_CHARS)
        self.assertGreaterEqual(len(fitted["findings"]), 1)

    def test_fit_result_leaves_small_result_unchanged(self):
        small = {"summary": "ok", "findings": [{"claim": "x", "confidence": 0.9}]}
        fitted = spawn.fit_result(small)
        self.assertEqual(fitted, small)
        self.assertNotIn("truncated", fitted)

    def test_fit_result_trims_risks_when_no_findings(self):
        big = {"summary": "ok", "risks": [{"issue": "r" * 300, "severity": "low"} for _ in range(60)]}
        fitted = spawn.fit_result(big)
        self.assertLessEqual(len(json.dumps(fitted)), bus.MAX_RESULT_CHARS)
        self.assertGreater(fitted["truncated"]["trimmed"]["risks"], 0)


class SpawnBase(unittest.TestCase):
    """base_for/scoped_diff (review T-0026). Builds its own scratch repo + goal/G branch (harness scratch_repo)
    instead of relying on another test file having already turned TMP into a git repo with that branch."""

    @classmethod
    def setUpClass(cls):
        scratch_repo(TMP)
        g("branch", "goal/G")   # a no-op if some other test file's repo setup already created it

    def g(self, *a, cwd=TMP, **k):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, **k)

    def test_review_bases_on_reviewed_task_branch(self):
        reviewed = bus.create_task("feat3", "s", ["a"], ["feat3.py"], role="execute")
        wt = spawn.ensure_worktree(reviewed["id"], base="HEAD")
        bus.update(reviewed["id"], worktree=str(wt))
        review = bus.create_task("review feat3", "s", ["a"], ["feat3.py"], role="review", inputs=[reviewed["id"]])
        self.assertEqual(spawn.base_for(review), f"task/{reviewed['id']}")

    def test_review_falls_back_to_goal_branch_then_origin_main(self):
        no_wt = bus.create_task("no wt yet", "s", ["a"], ["x.py"], role="execute", parent="G")
        review = bus.create_task("review no wt", "s", ["a"], ["x.py"], role="review", inputs=[no_wt["id"]])
        self.assertEqual(spawn.base_for(review), "goal/G")            # task/<id> doesn't exist, parent's goal does
        no_parent = bus.create_task("no wt no goal", "s", ["a"], ["x.py"], role="execute")
        review2 = bus.create_task("review no parent", "s", ["a"], ["x.py"], role="review", inputs=[no_parent["id"]])
        self.assertEqual(spawn.base_for(review2), "origin/main")      # neither branch exists

    def test_execute_stacks_on_existing_goal_branch(self):
        t = bus.create_task("stack", "s", ["a"], ["more.py"], role="execute", parent="G")
        self.assertEqual(spawn.base_for(t), "goal/G")
        t2 = bus.create_task("no goal yet", "s", ["a"], ["more.py"], role="execute", parent="ghost")
        self.assertEqual(spawn.base_for(t2), "origin/main")

    def test_challenge_bases_on_goal_branch(self):
        challenge = bus.create_task("challenge x", "s", ["a"], ["x.py"], role="challenge", parent="G",
                                     inputs=[{"claim": "c", "evidence": "e", "confidence": 0.5}])
        self.assertEqual(spawn.base_for(challenge), "goal/G")
        challenge2 = bus.create_task("challenge y", "s", ["a"], ["y.py"], role="challenge", parent="ghost",
                                      inputs=[{"claim": "c", "evidence": "e", "confidence": 0.5}])
        self.assertEqual(spawn.base_for(challenge2), "origin/main")

    def test_scout_bases_on_goal_branch(self):
        scout = bus.create_task("scout x", "s", ["a"], ["x.py"], role="scout", parent="G")
        self.assertEqual(spawn.base_for(scout), "goal/G")

    def test_scout_without_goal_branch_uses_origin_main(self):
        scout = bus.create_task("scout y", "s", ["a"], ["y.py"], role="scout", parent="ghost")
        self.assertEqual(spawn.base_for(scout), "origin/main")

    def test_scout_prompt_names_base(self):
        task = {"id": "T-1", "title": "scout", "spec": "q", "acceptance": ["a"],
                "scope": [], "worktree": str(TMP)}
        packet = spawn.scout_packet(task)
        prompt = spawn.render("scout", packet=packet, id="T-1", title="scout", turns="20",
                              base_branch="goal/G", base_sha="abc123")
        self.assertEqual(prompt.splitlines()[0], packet.splitlines()[0])

    def test_base_for_prefers_fix_round_parent(self):
        """A fix-round execute task (constraints.fix_round_for names the task it's fixing) must cut its worktree
        from that task's own task/<id> branch, not the goal branch -- the original task's commits may not have
        landed on the goal branch yet (or ever, if the fix round replaces them)."""
        original = bus.create_task("feat4", "s", ["a"], ["feat4.py"], role="execute", parent="G")
        wt = spawn.ensure_worktree(original["id"], base="HEAD")
        bus.update(original["id"], worktree=str(wt))

        (wt / "feat4.py").write_text("original = True\n")
        self.g("add", "feat4.py", cwd=wt, check=True)
        self.g("commit", "-qm", "original task work", cwd=wt, check=True)

        fix = bus.create_task("fix feat4", "s", ["a"], ["feat4.py"], role="execute", parent="G",
                              constraints={"fix_round_for": original["id"]})
        self.assertEqual(spawn.base_for(fix), f"task/{original['id']}")
        fix_wt = spawn.ensure_worktree(fix["id"])
        self.assertEqual((fix_wt / "feat4.py").read_text(), "original = True\n")

        fix_missing = bus.create_task("fix ghost", "s", ["a"], ["feat4.py"], role="execute", parent="G",
                                      constraints={"fix_round_for": "T-9999"})
        self.assertEqual(spawn.base_for(fix_missing), "goal/G")   # named branch doesn't exist: falls back

    def test_ensure_worktree_resolves_base_when_none_given(self):
        t = bus.create_task("stacked-exec", "s", ["a"], ["stacked.py"], role="execute", parent="G")
        wt = spawn.ensure_worktree(t["id"])
        self.assertEqual(self.g("merge-base", "--is-ancestor", "goal/G", f"task/{t['id']}").returncode, 0)

    def test_scoped_diff_excludes_predecessor_hunks(self):
        wt_goal = TMP / "wt" / "_goal_seed"
        self.g("worktree", "add", str(wt_goal), "goal/G")
        (wt_goal / "shared.py").write_text("A = 1\n")
        self.g("add", "-A", cwd=wt_goal); self.g("commit", "-qm", "predecessor shared.py", cwd=wt_goal)
        self.g("worktree", "remove", str(wt_goal), "--force")

        t = bus.create_task("stack2", "s", ["a"], ["shared.py"], role="execute", parent="G")
        wt = spawn.ensure_worktree(t["id"]); bus.update(t["id"], worktree=str(wt))
        (wt / "shared.py").write_text("A = 1\nB = 1\n")
        self.g("add", "-A", cwd=wt); self.g("commit", "-qm", "stack2 add B", cwd=wt)

        review = bus.create_task("review stack2", "s", ["a"], ["shared.py"], role="review", inputs=[t["id"]])
        diff = spawn.scoped_diff(bus.get(t["id"]))
        self.assertIn("+B = 1", diff)
        self.assertNotIn("+A = 1", diff)                              # predecessor's hunk, already in goal/G


class SecretsForRole(unittest.TestCase):
    """env-form secrets (headless hosts, T-0079): ENV_NAME = "env:OTHER_NAME" reads OTHER_NAME from os.environ;
    a missing var is skipped, not raised; the command form (today's laptop config) is untouched."""

    def with_secrets_cfg(self, secrets):
        orig_config = P.config
        P.config = lambda: {**orig_config(), "secrets": secrets}
        self.addCleanup(lambda: setattr(P, "config", orig_config))

    def test_env_form_reads_present_var(self):
        self.with_secrets_cfg({"scout": {"FOO": "env:T0079_TEST_VAR"}})
        os.environ["T0079_TEST_VAR"] = "s3cr3t"
        self.addCleanup(lambda: os.environ.pop("T0079_TEST_VAR", None))
        self.assertEqual(spawn.secrets_for_role("scout"), {"FOO": "s3cr3t"})

    def test_env_form_missing_var_is_skipped_without_raising(self):
        self.with_secrets_cfg({"scout": {"FOO": "env:T0079_MISSING_VAR"}})
        os.environ.pop("T0079_MISSING_VAR", None)
        self.assertEqual(spawn.secrets_for_role("scout"), {})

    def test_command_form_behaviour_unchanged(self):
        self.with_secrets_cfg({"scout": {"FOO": "echo -n hello"}})
        self.assertEqual(spawn.secrets_for_role("scout"), {"FOO": "hello"})


class OauthTokenInjection(unittest.TestCase):
    """CLAUDE_CODE_OAUTH_TOKEN injection (headless hosts, T-0079): run_claude puts the env var named by the
    account's oauth_token_env into the child's CLAUDE_CODE_OAUTH_TOKEN when it's configured and set; leaves it
    out otherwise."""

    def setUp(self):
        self.orig_popen = spawn.subprocess.Popen
        spawn.subprocess.Popen = FakePopen
        self.addCleanup(lambda: setattr(spawn.subprocess, "Popen", self.orig_popen))
        FakePopen.last_env = None
        # run_claude's trust_workspace writes acct.config_dir/.claude.json; never touch the real ~/.claude-*
        # profiles from a test, so no-op it here.
        orig_trust = spawn.trust_workspace
        spawn.trust_workspace = lambda config_dir, wt: None
        self.addCleanup(lambda: setattr(spawn, "trust_workspace", orig_trust))
        # scrub so an ambient CLAUDE_CODE_OAUTH_TOKEN on the test host can't mask the "omit" assertions
        had = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if had is not None:
            self.addCleanup(lambda: os.environ.__setitem__("CLAUDE_CODE_OAUTH_TOKEN", had))

    def run_claude_task(self, oauth_token_env=""):
        t = bus.create_task("oauth-test", "s", ["a"], ["x.py"], role="execute", tier="sonnet", complexity=3)
        t["worktree"] = str(TMP)
        acct = P.Account("A", "~/.claude-a", ["execute"], oauth_token_env=oauth_token_env)
        pool = P.Pool()
        r = spawn.run_claude(pool, acct, t, "prompt", "claude-sonnet-5", spawn.TOOLS["execute"], 2.0, 60)
        self.assertEqual(r["status"], "done")
        return FakePopen.last_env

    def test_injects_token_when_configured_and_set(self):
        os.environ["T0079_OAUTH_VAR"] = "tok-abc"
        self.addCleanup(lambda: os.environ.pop("T0079_OAUTH_VAR", None))
        env = self.run_claude_task(oauth_token_env="T0079_OAUTH_VAR")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-abc")

    def test_omits_token_when_configured_but_unset(self):
        os.environ.pop("T0079_OAUTH_VAR_UNSET", None)
        env = self.run_claude_task(oauth_token_env="T0079_OAUTH_VAR_UNSET")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_omits_token_when_not_configured(self):
        env = self.run_claude_task(oauth_token_env="")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)


class ContextTelemetry(unittest.TestCase):
    def test_execute_packet_bytes_unchanged_under_context_router_shadow(self):
        task = {"id": "T-shadow", "title": "shadow", "spec": "route it", "acceptance": ["works"],
                "scope": [], "role": "execute", "constraints": {}}
        with mock.patch.object(spawn.promotion, "mode", return_value="off"):
            off = spawn.packet(task, TMP)
            off_meta = spawn.packet_run_meta(off)
        with mock.patch.object(spawn.promotion, "mode", return_value="shadow"), \
                mock.patch.object(spawn.decision_log, "record"):
            shadow = spawn.packet(task, TMP)
            shadow_meta = spawn.packet_run_meta(shadow)
        self.assertEqual(off, shadow)
        self.assertNotIn("routed_tokens", off_meta)
        self.assertIn("routed_tokens", shadow_meta)

    def test_execute_packet_logs_context_selection_decision(self):
        task = {"id": "T-log", "title": "log", "spec": "route it", "acceptance": ["works"],
                "scope": [], "role": "execute", "constraints": {}}
        with mock.patch.object(spawn.promotion, "mode", return_value="shadow"), \
                mock.patch.object(spawn.decision_log, "record") as record:
            spawn.packet(task, TMP)
        row = record.call_args.kwargs
        self.assertEqual((row["kind"], row["subject"]), ("context_selection", task["id"]))
        self.assertNotIn("content", row)

    def test_review_packet_routes_diff_files_per_path(self):
        raw = ("diff --git a/a.py b/a.py\n@@ -0,0 +1 @@\n+A=1\n"
               "diff --git a/b.py b/b.py\n@@ -0,0 +1 @@\n+B=1\n")
        task = {"id": "T-review-route", "spec": "s", "acceptance": [],
                "scope": ["a.py", "b.py"], "worktree": str(TMP), "complexity": 1}
        with mock.patch.object(spawn, "scoped_diff", return_value=raw), \
                mock.patch.object(spawn, "_base_sha", return_value="dead"), \
                mock.patch.object(spawn.promotion, "mode", return_value="shadow"), \
                mock.patch.object(spawn.context_router, "route", wraps=spawn.context_router.route) as route, \
                mock.patch.object(spawn.decision_log, "record"):
            spawn.review_packet(task, task)
        candidates = route.call_args.args[1]
        self.assertEqual([item.location for item in candidates if item.source_type == "source_chunk"],
                         ["a.py", "b.py"])

    def test_router_failure_never_breaks_packet(self):
        task = {"id": "T-router-fail", "title": "failure", "spec": "s", "acceptance": [],
                "scope": [], "role": "execute", "constraints": {}}
        with mock.patch.object(spawn.promotion, "mode", return_value="shadow"), \
                mock.patch.object(spawn.context_router, "route", side_effect=RuntimeError("boom")), \
                mock.patch.object(spawn.notify, "notify") as notice:
            packet = spawn.packet(task, TMP)
        self.assertIn("## objective", packet)
        self.assertTrue(any(task["id"] in call.args[0] for call in notice.call_args_list))

    def test_section_meta_splits_named_sections(self):
        text = "packet header\n## spec\nhello\n## scope\nx.py"
        meta = spawn.section_meta(text)
        self.assertEqual(list(meta), ["_preamble", "spec", "scope"])
        self.assertEqual(meta["spec"]["chars"], len("## spec\nhello\n"))
        self.assertEqual(len(meta["spec"]["sha256"]), 64)

    def test_packet_run_meta_reports_sections_and_presented_tokens(self):
        text = "packet vabcdef base deadbeef sources task@T-1\n## spec\nhello"
        meta = spawn.packet_run_meta(text)
        self.assertEqual(meta["presented_tokens"], len(text) // 4)
        self.assertIn("spec", meta["sections"])

    def test_review_packet_records_candidate_tokens_before_diff_budget(self):
        raw_diff = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n" + "-old\n+new\n" * 3000
        task = {"id": "T-review", "spec": "s", "acceptance": [], "scope": ["x.py"],
                "worktree": str(TMP), "complexity": 1}
        with mock.patch.object(spawn, "scoped_diff", return_value=raw_diff), \
                mock.patch.object(spawn, "_base_sha", return_value="dead"):
            packet = spawn.review_packet(task, task)
        meta = spawn.packet_run_meta(packet)
        self.assertEqual(meta["candidate_tokens"], len(raw_diff) // 4)
        self.assertLess(meta["presented_tokens"], meta["candidate_tokens"])
        self.assertTrue(meta["candidate_known"])

    def test_run_worker_records_instruction_tokens(self):
        reviewed = bus.create_task("telemetry target", "s", ["a"], ["x.py"], role="execute")
        review = bus.create_task("telemetry review", "s", ["a"], ["x.py"], role="review",
                                 inputs=[reviewed["id"]])
        execute = bus.create_task("telemetry execute", "s", ["a"], ["x.py"], role="execute")
        packet = "packet vabcdef base dead sources test\n## spec\nx"
        prompt = "instructions\n" + packet
        seen = []

        def run_claude(pool, account, task, rendered, *args, **kwargs):
            seen.append((task["role"], task["packet_meta"], rendered))
            result = json.dumps({"verdict": "approve", "comments": []}) if task["role"] == "review" else "ok"
            return {"status": "done", "output": {"result": result, "usage": {}}}

        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "review_packet", return_value=packet), \
                mock.patch.object(spawn, "packet", return_value=packet), \
                mock.patch.object(spawn, "render", return_value=prompt), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "run_claude", side_effect=run_claude):
            spawn.run_worker(review["id"])
            spawn.run_worker(execute["id"])

        self.assertEqual([role for role, _, _ in seen], ["review", "execute"])
        for _, meta, rendered in seen:
            self.assertEqual(meta["instruction_tokens"], len(rendered) // 4 - len(packet) // 4)

    def test_packet_build_meta_evicts_beyond_cap(self):
        original = spawn._PACKET_BUILD_META.copy()
        self.addCleanup(lambda: (spawn._PACKET_BUILD_META.clear(),
                                 spawn._PACKET_BUILD_META.update(original)))
        spawn._PACKET_BUILD_META.clear()
        first = spawn._role_packet("first", "dead", "test")
        first_version = spawn.packet_run_meta(first)["version"]
        for index in range(spawn._PACKET_BUILD_META_MAX):
            spawn._role_packet(f"packet {index}", "dead", "test")
        self.assertEqual(len(spawn._PACKET_BUILD_META), spawn._PACKET_BUILD_META_MAX)
        self.assertNotIn(first_version, spawn._PACKET_BUILD_META)


if __name__ == "__main__":
    unittest.main()
