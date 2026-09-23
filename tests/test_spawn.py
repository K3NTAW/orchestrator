import _harness
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
    def test_spawn_records_worker_registry_entry(self):
        from orchestrator import worker_registry as registry
        task = bus.create_task("registry spawn", "s", ["a"], ["x.py"], role="execute")
        task["worktree"] = str(TMP)
        self.addCleanup(registry._path(task["id"]).unlink, missing_ok=True)
        self.addCleanup(registry._path(task["id"], events=True).unlink, missing_ok=True)
        process = mock.Mock(pid=4242, returncode=0)
        def communicate(timeout):
            self.assertEqual(registry.get(task["id"])["status"], "running")
            return json.dumps({"result": "private result", "usage": {"input_tokens": 5, "output_tokens": 2},
                               "total_cost_usd": .01}), ""
        process.communicate.side_effect = communicate
        pl = P.Pool()
        with mock.patch.object(spawn.subprocess, "Popen", return_value=process), \
                mock.patch.object(spawn, "trust_workspace"), \
                mock.patch.object(spawn, "secrets_for_role", return_value={}), \
                mock.patch.object(spawn.shutil, "which", return_value="claude"):
            result = spawn.run_claude(pl, pl.get("A"), task, "private prompt", "model", "Read", 1, 30)
        self.assertEqual(result["status"], "done")
        self.assertEqual([e["kind"] for e in registry.events(task["id"])], ["spawned", "usage", "exit"])
        self.assertEqual(registry.get(task["id"])["usd"], .01)
        self.assertNotIn("private", registry._path(task["id"], events=True).read_text())

    def test_packet_meta_carries_prefix_identity(self):
        first = Render().packet_fixture(); second = Render().packet_fixture()
        second["spec"] = "different task-specific objective"
        first_packet = spawn.packet(first, TMP)
        second_packet = spawn.packet(second, TMP)
        spawn.render("execute", packet=first_packet, task=first)
        spawn.render("execute", packet=second_packet, task=second)
        first_meta = spawn.packet_run_meta(first_packet)
        second_meta = spawn.packet_run_meta(second_packet)
        self.assertEqual(first_meta["prefix_sha"], second_meta["prefix_sha"])

    def test_render_records_prefix_identity_before_packet_header(self):
        first = Render().packet_fixture(); second = Render().packet_fixture()
        second["acceptance"] = ["a longer criterion that changes the packet suffix"]
        packets = [spawn.packet(task, TMP) for task in (first, second)]
        prompts = [spawn.render("execute", packet=packet, task=task)
                   for packet, task in zip(packets, (first, second))]
        metas = [spawn.packet_run_meta(packet) for packet in packets]
        self.assertEqual(metas[0]["prefix_sha"], metas[1]["prefix_sha"])
        self.assertNotEqual(metas[0]["suffix_chars"], metas[1]["suffix_chars"])
        for prompt, packet, meta in zip(prompts, packets, metas):
            boundary = prompt.index(packet.splitlines()[0])
            self.assertEqual(meta["prefix_chars"], boundary)
            self.assertIn("objective", meta["dynamic_sections"])

    def test_render_bytes_identical_in_shadow(self):
        task = {"id": "T-shadow", "scope": ["x.py"]}
        with mock.patch.object(spawn.instructions, "mode", return_value="shadow"), \
                mock.patch.object(spawn.decision_log, "record"):
            cases = (("execute", {"packet": "p"}), ("review", {"packet": "p"}),
                     ("scout", {"packet": "p"}))
            for name, kwargs in cases:
                self.assertEqual(spawn.render(name, **kwargs), spawn.render(name, task=task, **kwargs))

    def test_render_active_appends_selected_modules_only(self):
        task = {"id": "T-active", "scope": ["src/x.py"]}
        with mock.patch.object(spawn.instructions, "mode", return_value="active"), \
                mock.patch.object(spawn.decision_log, "record"):
            text = spawn.render("execute", packet="p", task=task)
        selected = spawn.instructions.module_text("python-unittest").strip()
        self.assertEqual(text.count(selected), 1)
        self.assertNotIn(spawn.instructions.module_text("docs-task").strip(), text)

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
    def active_eval(self):
        from datetime import datetime, timezone
        path = spawn.STATE / "context_eval.json"
        previous = path.read_bytes() if path.exists() else None
        self.addCleanup(lambda: path.write_bytes(previous) if previous is not None else path.unlink(missing_ok=True))
        path.write_text(json.dumps({"ran_at": datetime.now(timezone.utc).isoformat(), "suite_passed": True}))
        return path

    def test_execute_packet_active_replaces_routed_sections_only(self):
        self.active_eval()
        task = self.packet_fixture()
        task.update(spec="Keep the contract", inputs=[{"summary": "widget prior result"},
                                                      {"summary": "irrelevant historical fact"}])
        hits = {"hits": [{"id": "mem:gotchas.md:1", "title": "widget useful gotcha"},
                         {"id": "mem:gotchas.md:2", "title": "irrelevant dinosaur"}],
                "layers_consulted": ["notes"]}
        with mock.patch.object(spawn, "memory_recall", return_value=hits):
            shadow = spawn.packet(task, TMP, cfg={"context_router": {"mode": "shadow"}})
            active = spawn.packet(task, TMP, cfg={"context_router": {"mode": "active"}})
        a, b = spawn.section_meta(active), spawn.section_meta(shadow)
        for name in ("objective", "acceptance", "base", "write_scope", "constraints", "relevant_tests", "symbols", "verify"):
            self.assertEqual(a[name], b[name], name)
        for name in ("gotchas", "decisions", "evidence", "read_scope"):
            self.assertNotEqual(a[name], b[name], name)
        self.assertIn("task:T-P:gotcha:1", active)
        self.assertIn("widget prior result", active)
        self.assertNotIn("irrelevant", active)
        self.assertNotIn("return json.dumps", active)
        self.assertIn("routed=active", active.splitlines()[0])
        self.assertLessEqual(len(active), 4800)

    def test_review_packet_active_keeps_diff_and_security_section(self):
        from dataclasses import replace
        self.active_eval()
        task = {**self.packet_fixture(), "spec": "security widget", "constraints": {"fix_round_for": "T-prior"},
                "result": {"summary": "widget previous result"}}
        comments = [{"text": "widget long finding details"}, {"text": "unrelated hidden finding"}]
        original = spawn.context_router.route
        def route(*args, **kwargs):
            routed = original(*args, **kwargs)
            return replace(routed, items=[replace(item, level="LONG")
                if item.evidence_id == next(ev.id for ev in args[1] if ev.source_type == "review_finding")
                else item for item in routed.items])
        raw = "diff --git a/widget.py b/widget.py\n@@ -1 +1 @@\n-old\n+new\n"
        with mock.patch.object(spawn, "scoped_diff", return_value=raw), \
             mock.patch.object(bus, "get", return_value={"role": "review", "result": {"comments": comments}}), \
             mock.patch.object(spawn.context_router, "route", side_effect=route):
            shadow = spawn.review_packet(task, task, cfg={"context_router": {"mode": "shadow"}})
            active = spawn.review_packet(task, task, cfg={"context_router": {"mode": "active"}})
        for name in ("diff", "security", "spec", "acceptance", "scope", "gate"):
            self.assertEqual(spawn.section_meta(active)[name], spawn.section_meta(shadow)[name])
        self.assertIn("## fix-round context\n\n## routed-findings", active)
        findings = active.split("## routed-findings\n")[1]
        self.assertIn("widget long finding details", findings)
        self.assertIn("widget previous result", findings)
        self.assertNotIn("unrelated hidden finding", active)
        self.assertIn("routed=active", active.splitlines()[0])

    def test_active_packet_never_contains_unredacted_evidence(self):
        from dataclasses import replace
        from orchestrator import jev
        self.active_eval()
        task = {**self.packet_fixture(), "id": "T-redaction", "parent": "G-redaction",
                "constraints": {"fix_round_for": "T-prior"}}
        secrets = ("sk-" + "a" * 24, "ghp_" + "b" * 36,
                   "API_KEY=synthetic-private-value", "Bearer synthetic-private-token")
        original_route = spawn.context_router.route
        cfg = {"context_router": {"mode": "active"}}
        for secret in secrets:
            value = "widget " + secret
            redacted = jev.redact(value)
            self.assertNotEqual(value, redacted)
            hits = {"hits": [{"id": "mem:gotchas.md:1", "title": value}],
                    "layers_consulted": ["notes"]}
            task["inputs"] = [{"summary": value}]
            task["result"] = {"summary": value}
            for level in ("SHORT", "LONG", "FULL"):
                with self.subTest(secret_type=secret.split("-")[0], level=level):
                    def route(*args, **kwargs):
                        # Every field reaching routing must already be sanitized.
                        for ev in args[1]:
                            for field in (ev.content, ev.summary_short, ev.summary_long):
                                self.assertNotIn(secret, field)
                        routed = original_route(*args, **kwargs)
                        return replace(routed, items=[replace(item, level=level) for item in routed.items])
                    with mock.patch.object(spawn, "memory_recall", return_value=hits), \
                         mock.patch.object(spawn, "_memory_entries", return_value=[(1, "G-redaction " + value, "")]), \
                         mock.patch.object(spawn, "scoped_diff", return_value=""), \
                         mock.patch.object(bus, "get", return_value={"role": "review", "result": {"comments": [{"text": value}]}}), \
                         mock.patch.object(spawn.context_router, "route", side_effect=route):
                        packets = (spawn.packet(task, TMP, cfg=cfg), spawn.review_packet(task, task, cfg=cfg))
                        source = spawn.evidence.make("source_chunk", "support.py", value, commit="base",
                            provenance="repo", task=task, section="read_scope")
                        meta = spawn._shadow_route(task, [source], role="execute", head_sha="base", cfg=cfg)
                    for packet in packets:
                        self.assertIn("routed=active", packet.splitlines()[0])
                        self.assertNotIn(secret, packet)
                        self.assertIn(redacted, packet)
                    source_text = meta["_routed_sections"]["read_scope"][0][1]
                    self.assertNotIn(secret, source_text)
                    self.assertIn(redacted, source_text)

    def test_active_refused_without_passing_context_eval(self):
        path = self.active_eval()
        task = self.packet_fixture()
        shadow = spawn.packet(task, TMP, cfg={"context_router": {"mode": "shadow"}})
        for report in (None, {"suite_passed": False},
                       {"suite_passed": True, "ran_at": "2000-01-01T00:00:00+00:00"}):
            if report is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(report))
            with mock.patch.object(spawn.notify, "notify") as notice:
                active = spawn.packet(task, TMP, cfg={"context_router": {"mode": "active"}})
            self.assertEqual(active, shadow)
            self.assertTrue(any("active refused" in call.args[0] for call in notice.call_args_list))

    def test_packet_run_meta_parses_routed_marker(self):
        for mode in ("active", "shadow"):
            text = f"packet vabcdef base deadbeef sources pool.toml@policy gotchas@notes memory@notes routed={mode}\n## objective\nwork"
            meta = spawn.packet_run_meta(text)
            self.assertEqual((meta["hash"], meta["base"], meta["policy_version"], meta["gotchas"]),
                             ("abcdef", "deadbeef", "policy", "notes"))
            self.assertTrue(meta["sources"].endswith("routed=" + mode))

    def test_cap_trim_removes_whole_routed_items(self):
        self.active_eval()
        task = self.packet_fixture()
        items = [("FULL", "full item\n```\n" + "body\n" * 40 + "```"),
                 ("SHORT", "short item " * 400), ("LONG", "long item " * 400)]
        meta = {"routed_mode": "active", "_routed_sections": {"evidence": items}}
        with mock.patch.object(spawn, "_shadow_route", return_value=meta):
            text = spawn.packet(task, TMP)
        self.assertLessEqual(len(text), 4800)
        self.assertIn("full item\n```", text)
        self.assertNotIn("short item", text)
        self.assertNotIn("long item", text)
        self.assertEqual(text.count("```"), 2)

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

    def test_packet_accepts_cfg_override(self):
        task = self.packet_fixture()
        off = spawn.packet(task, TMP, cfg={"context_router": {"mode": "off"}})
        off_meta = spawn.packet_run_meta(off)
        shadow = spawn.packet(task, TMP, cfg={"context_router": {"mode": "shadow"}})
        self.assertEqual(off, shadow)
        self.assertNotIn("routed_tokens", off_meta)
        self.assertIn("routed_tokens", spawn.packet_run_meta(shadow))

    def test_review_packet_accepts_cfg_override(self):
        task = {**self.packet_fixture(), "spec": "ordinary"}
        off_cfg = {"context_router": {"mode": "off"}, "review": {"security_paths": []}, "limits": {}}
        shadow_cfg = {**off_cfg, "context_router": {"mode": "shadow"}}
        with mock.patch.object(spawn, "Pool", side_effect=AssertionError("Pool constructed")):
            off = spawn.review_packet(task, task, cfg=off_cfg)
            off_meta = spawn.packet_run_meta(off)
            shadow = spawn.review_packet(task, task, cfg=shadow_cfg)
        self.assertEqual(off, shadow)
        self.assertNotIn("routed_tokens", off_meta)
        self.assertIn("routed_tokens", spawn.packet_run_meta(shadow))

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
                         f"pool.toml@{meta['policy_version']} gotchas@{meta['gotchas']} memory@notes,bus routed=shadow")

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
    def _active_review(self):
        reviewed = bus.create_task("active target", "s", ["a"], ["x.py"], role="execute")
        review = bus.create_task("active review", "s", ["a"], ["x.py"], role="review", inputs=[reviewed["id"]])
        return review

    def _handover_run(self, skill_mode, disclosure_mode, skill_choice=None, worker=None):
        review = self._active_review()
        minimal = spawn.tool_catalog.minimal_set(review, "review")
        choice = skill_choice or {
            "mode": "active", "selected": ["review/tool-user"],
            "specialist": {"tools": sorted(set(minimal["keep"]) | {"Glob"}),
                           "tools_added": ["Glob"]},
        }
        calls = []
        def run(*args):
            calls.append(args)
            return (worker(review, calls, *args) if worker else
                    {"status": "done", "output": {"result": '{"verdict":"approve"}', "usage": {}}})
        def mode(feature, cfg=None):
            return skill_mode if feature == "skill_routing" else disclosure_mode
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn, "_prepare_skills", return_value=choice), \
                mock.patch.object(spawn, "_skill_routing", return_value={}), \
                mock.patch.object(spawn, "_skill_records", return_value={}), \
                mock.patch.object(spawn.promotion, "mode", side_effect=mode), \
                mock.patch.object(spawn, "run_claude", side_effect=run):
            spawn.run_worker(review["id"])
        return review, choice, minimal, calls

    def test_specialist_allowlist_used_only_when_both_modes_active(self):
        for skill_mode in ("shadow", "active"):
            for disclosure_mode in ("shadow", "active"):
                with self.subTest(skill_mode=skill_mode, disclosure_mode=disclosure_mode):
                    _, choice, minimal, calls = self._handover_run(skill_mode, disclosure_mode)
                    expected = (",".join(choice["specialist"]["tools"])
                                if (skill_mode, disclosure_mode) == ("active", "active")
                                else ",".join(minimal["keep"]) if disclosure_mode == "active"
                                else spawn.TOOLS["review"])
                    self.assertEqual(calls[0][5], expected)

    def test_specialist_allowlist_keeps_mandatory_and_never_adds_unavailable_tools(self):
        review = self._active_review()
        minimal = spawn.tool_catalog.minimal_set(review, "review")
        tools = sorted(set(minimal["keep"]) | {"Glob"})
        choice = {"mode": "active", "selected": ["review/tool-user"],
                  "specialist": {"tools": tools, "tools_added": ["Glob"],
                                 "tools_unavailable": [{"tool": "Edit"}],
                                 "tools_unknown": [{"tool": "Mystery"}]}}
        _, _, _, calls = self._handover_run("active", "active", choice)
        selected = set(calls[0][5].split(","))
        self.assertTrue(set(minimal["mandatory"]) <= selected)
        self.assertNotIn("Edit", selected)
        self.assertNotIn("Mystery", selected)

    def test_skill_refusal_falls_back_to_minimal_allowlist(self):
        choice = {"mode": "shadow", "selected": ["review/tool-user"],
                  "specialist": {"tools": ["Read", "Edit"], "tools_added": ["Edit"]}}
        _, _, minimal, calls = self._handover_run("active", "active", choice)
        self.assertEqual(calls[0][5], ",".join(minimal["keep"]))

    def test_escalation_still_uses_legacy_allowlist_under_hand_over(self):
        def worker(review, calls, *args):
            if len(calls) == 1:
                bus.post_result(review["id"], {"reason": spawn.NEEDS_TOOL_PREFIX + "Edit"}, "held")
                return {"status": "done", "output": {"usage": {}, "total_cost_usd": .1}}
            return {"status": "done", "output": {"result": '{"verdict":"approve"}', "usage": {}}}
        _, choice, _, calls = self._handover_run("active", "active", worker=worker)
        self.assertEqual(calls[0][5], ",".join(choice["specialist"]["tools"]))
        self.assertEqual(calls[1][5], spawn.TOOLS["review"])

    def test_active_allowlist_is_minimal_set_with_mandatory(self):
        review = self._active_review()
        captured = []
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn.promotion, "mode", return_value="active"), \
                mock.patch.object(spawn, "run_claude", side_effect=lambda *a, **k: captured.append(a[5]) or
                                  {"status": "done", "output": {"result": '{"verdict":"approve"}', "usage": {}}}):
            spawn.run_worker(review["id"])
        expected = spawn.tool_catalog.minimal_set(review, "review")
        self.assertEqual(captured, [",".join(expected["keep"])])
        self.assertTrue(set(expected["mandatory"]) <= set(expected["keep"]))

    def test_hidden_tool_request_respawns_once_with_full_allowlist(self):
        review = self._active_review()
        calls = []
        def run(*args):
            calls.append(args)
            if len(calls) == 1:
                bus.post_result(review["id"], {"reason": spawn.NEEDS_TOOL_PREFIX + "Glob"}, "held")
                return {"status": "done", "output": {"usage": {}, "total_cost_usd": .25}}
            return {"status": "done", "output": {"result": '{"verdict":"approve"}', "usage": {}}}
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn.promotion, "mode", return_value="active"), \
                mock.patch.object(spawn, "run_claude", side_effect=run):
            spawn.run_worker(review["id"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][5], spawn.TOOLS["review"])
        self.assertLess(calls[1][6], calls[0][6])

    def test_tool_escalation_cap_persists_across_worker_invocations(self):
        review = self._active_review()
        reason = spawn.NEEDS_TOOL_PREFIX + "Glob"

        def run(*args):
            bus.post_result(review["id"], {"reason": reason}, "held")
            return {"status": "held", "reason": reason,
                    "output": {"usage": {}, "total_cost_usd": .25}}

        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}) as reserve, \
                mock.patch.object(P.Pool, "release") as release, \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn.promotion, "mode", return_value="active"), \
                mock.patch.object(spawn, "run_claude", side_effect=run) as worker:
            spawn.run_worker(review["id"])
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(worker.call_args.args[5], spawn.TOOLS["review"])
            self.assertTrue(bus.get(review["id"])["pipeline"]["tool_escalation_used"])

            bus.update(review["id"], status="queued", result=None, assigned_to=None)
            worker.reset_mock()
            reserve.reset_mock()
            release.reset_mock()
            result = spawn.run_worker(review["id"])

            worker.assert_called_once()
            reserve.assert_called_once()
            release.assert_called_once_with(review["id"], result)
            expected = spawn.tool_catalog.minimal_set(review, "review")
            self.assertEqual(worker.call_args.args[5], ",".join(expected["keep"]))
            self.assertEqual(result["status"], "held")
            self.assertEqual(bus.get(review["id"])["hold_reason"], reason)
            self.assertTrue(bus.get(review["id"])["pipeline"]["tool_escalation_used"])

    def test_respawn_skipped_when_budget_remainder_too_small(self):
        review = self._active_review()
        calls = []
        def run(*args):
            calls.append(args)
            bus.post_result(review["id"], {"reason": spawn.NEEDS_TOOL_PREFIX + "Glob"}, "held")
            return {"status": "done", "output": {"usage": {}, "total_cost_usd": args[6] * .95}}
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn.promotion, "mode", return_value="active"), \
                mock.patch.object(spawn, "run_claude", side_effect=run):
            spawn.run_worker(review["id"])
        self.assertEqual(len(calls), 1)
        self.assertIn("no budget for respawn", bus.get(review["id"])["hold_reason"])

    def test_escalation_stamps_tool_escalation_used_and_sums_usage(self):
        review = self._active_review()
        calls = []
        def run(*args):
            calls.append(args)
            if len(calls) == 1:
                bus.post_result(review["id"], {"reason": spawn.NEEDS_TOOL_PREFIX + "Glob"}, "held")
                return {"status": "done", "output": {"usage": {"input_tokens": 2}, "total_cost_usd": .2}}
            return {"status": "done", "output": {"result": '{"verdict":"approve"}',
                                                    "usage": {"input_tokens": 3}, "total_cost_usd": .3}}
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(P.Pool, "release") as release, \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn.promotion, "mode", return_value="active"), \
                mock.patch.object(spawn, "run_claude", side_effect=run):
            spawn.run_worker(review["id"])
        self.assertTrue(bus.get(review["id"])["pipeline"]["tool_escalation_used"])
        released = release.call_args.args[1]
        self.assertEqual(released["usage"]["input_tokens"], 5)
        self.assertAlmostEqual(released["total_cost_usd"], .5)

    def test_worker_allowlist_unchanged_under_tool_disclosure_shadow(self):
        reviewed = bus.create_task("disclosure target", "s", ["a"], ["x.py"], role="execute")
        review = bus.create_task("disclosure review", "s", ["a"], ["x.py"], role="review",
                                 inputs=[reviewed["id"]])
        captured = []

        def run_claude(*args, **kwargs):
            captured.append(args[5])
            return {"status": "done", "output": {"result": '{"verdict":"approve"}', "usage": {}}}

        common = (mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")),
                  mock.patch.object(P.Pool, "reserve", return_value={}),
                  mock.patch.object(spawn, "ensure_worktree", return_value=TMP),
                  mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"),
                  mock.patch.object(spawn, "render", return_value="prompt"),
                  mock.patch.object(spawn, "run_claude", side_effect=run_claude))
        with common[0], common[1], common[2], common[3], common[4], common[5], \
                mock.patch.object(spawn.promotion, "mode", return_value="off"), \
                mock.patch.object(spawn.decision_log, "record") as off_record:
            spawn.run_worker(review["id"])
        bus.update(review["id"], status="queued", result=None, assigned_to=None)
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "review_packet", return_value="packet v1 base x sources y"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn, "run_claude", side_effect=run_claude), \
                mock.patch.object(spawn.promotion, "mode", return_value="shadow"), \
                mock.patch.object(spawn.decision_log, "record") as shadow_record:
            spawn.run_worker(review["id"])
        self.assertEqual(captured, [spawn.TOOLS["review"], spawn.TOOLS["review"]])
        self.assertFalse(any(call.kwargs.get("kind") == "tool_disclosure" for call in off_record.call_args_list))
        self.assertTrue(any(call.kwargs.get("kind") == "tool_disclosure" for call in shadow_record.call_args_list))

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

    def test_run_worker_records_skills_exposed_and_l0_tokens(self):
        task = bus.create_task("skills", "s", ["a"], ["x.py"], role="execute")
        records = {"execute/a": {"state": "active", "provenance": "builtin", "est_tokens_l0": 3},
                   "review/b": {"state": "active", "provenance": "builtin", "est_tokens_l0": 5},
                   "execute/off": {"state": "disabled", "provenance": "builtin", "est_tokens_l0": 99}}
        seen = {}
        def run_claude(pool, account, worker, *args, **kwargs):
            seen.update(worker["packet_meta"])
            return {"status": "done", "output": {"result": "ok", "usage": {}}}
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "packet", return_value="packet"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn, "run_claude", side_effect=run_claude), \
                mock.patch.object(spawn.skills_registry, "load", return_value={"skills": records}), \
                mock.patch.object(spawn.decision_log, "record"):
            spawn.run_worker(task["id"])
        self.assertEqual(seen["skills_exposed"], ["execute/a", "review/b"])
        self.assertEqual(seen["skill_tokens_l0"], 8)

    def test_skill_selection_row_and_meta_in_shadow_without_exposure_change(self):
        task = bus.create_task("who calls", "who calls this", ["a"], ["x.py"], role="scout")
        records = {"executor/implement-spec": {"state": "active", "provenance": "builtin",
                    "roles": ["execute"], "task_classes": ["*"], "triggers": [], "est_tokens_l0": 3,
                    "est_tokens_l2": 7},
                   "scout/trace-callers": {"state": "active", "provenance": "builtin",
                    "roles": ["scout"], "task_classes": ["*"], "triggers": ["who calls"],
                    "est_tokens_l0": 5, "est_tokens_l2": 11}}
        seen = {}
        decisions = []
        def run_claude(pool, account, worker, *args, **kwargs):
            seen.update(worker["packet_meta"])
            return {"status": "done", "output": {"result": "ok", "usage": {}}}
        def capture(*args, **kwargs):
            decisions.append((args, kwargs))
        with mock.patch.object(P.Pool, "pick", lambda self, role, avoid=None: self.get("A")), \
                mock.patch.object(P.Pool, "reserve", return_value={}), \
                mock.patch.object(spawn, "ensure_worktree", return_value=TMP), \
                mock.patch.object(spawn, "scout_packet", return_value="packet"), \
                mock.patch.object(spawn, "render", return_value="prompt"), \
                mock.patch.object(spawn, "run_claude", side_effect=run_claude), \
                mock.patch.object(spawn.skills_registry, "load", return_value={"skills": records}), \
                mock.patch.object(spawn.decision_log, "record", side_effect=capture):
            spawn.run_worker(task["id"])
        self.assertEqual(seen["skills_exposed"], sorted(records))
        self.assertEqual(seen["skills_selected"], ["scout/trace-callers"])
        routed = next(kwargs for args, kwargs in decisions
                      if (args[0] if args else kwargs.get("kind")) == "skill_selection")
        self.assertEqual(routed["selected"], ["scout/trace-callers"])
        self.assertEqual(routed["rejected"], [])

    def test_skill_usage_from_gate_rows_for_claude_workers(self):
        path = spawn.STATE / "runs/jev/gate.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"task": "T-gate", "tool": "Read",
                                    "tool_target": "skills/scout/find/SKILL.md"}) + "\n")
        records = {"scout/find": {"est_tokens_l2": 17}}
        with mock.patch.object(spawn.skills_registry, "load", return_value={"skills": records}):
            self.assertEqual(spawn._skills_from_gate("T-gate"), (["scout/find"], 17))

    def test_skills_from_gate_matches_real_gate_row_shape(self):
        from orchestrator import jev_gate
        records = {"scout/find": {"est_tokens_l2": 17},
                   "review/check": {"est_tokens_l2": 23},
                   "scout/other": {"est_tokens_l2": 99}}
        with tempfile.TemporaryDirectory() as directory:
            gate_path = Path(directory) / "gate.jsonl"
            with mock.patch.object(jev_gate, "GATE_LOG", gate_path):
                for task, session, tool, target in (
                    ("T-real", "s-real", "Read", "/repo/skills/scout/find/SKILL.md"),
                    ("T-real", "s-real", "Read", "/repo/skills/scout/find/SKILL.md"),
                    ("", "s-real", "Bash", "bash skills/review/check/scripts/check.sh"),
                    ("T-other", "s-other", "Read", "skills/scout/other/SKILL.md"),
                    ("T-real", "s-real", "Edit", "skills/scout/other/SKILL.md"),
                ):
                    jev_gate._log(task, session, tool, {}, "shadow", False, False, 0,
                                  tool_target=target, input_hash="hash")
            rows = [json.loads(line) for line in gate_path.read_text().splitlines()]
            self.assertTrue(all("tool_target" in row and "input" not in row for row in rows))
            with mock.patch.object(spawn.skills_registry, "load", return_value={"skills": records}), \
                    mock.patch.object(Path, "read_text", return_value=gate_path.read_text()), \
                    mock.patch.object(Path, "exists", return_value=True):
                self.assertEqual(spawn._skills_from_gate("T-real", "s-real"),
                                 (["review/check", "scout/find"], 40))
                self.assertEqual(spawn._skills_from_gate("T-real"), (["scout/find"], 17))

    def test_skill_usage_parsed_from_codex_summary(self):
        from orchestrator import executor
        records = {"executor/implement-spec": {"est_tokens_l0": 4, "est_tokens_l2": 20}}
        with mock.patch("orchestrator.skills_registry.load", return_value={"skills": records}):
            meta = executor._codex_skill_meta("done\nSkill used: implement-spec\n")
        self.assertEqual(meta["skills_used"], ["executor/implement-spec"])
        self.assertEqual(meta["skill_tokens_l2"], 20)

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

    def test_packet_header_carries_specialist_name_in_active_only(self):
        task = {"id": "T-specialist-header", "title": "header", "spec": "s", "acceptance": [],
                "scope": [], "role": "execute", "tier": "sonnet", "constraints": {}}
        records = {"executor/implement-spec": {"state": "active", "roles": ["execute"],
                    "task_classes": ["*"], "version": "v1", "triggers": [], "tools": []},
                   "review/adversarial-review": {"state": "active", "roles": ["review"],
                    "task_classes": ["*"], "version": "v1", "triggers": [], "tools": []}}
        with mock.patch.object(spawn.skills_registry, "load", return_value={"skills": records}), \
                mock.patch.object(spawn.skills_registry, "render", return_value="Skill procedure"), \
                mock.patch.object(spawn.skill_scorecard, "selection_rows", return_value=30), \
                mock.patch.object(spawn.skill_scorecard, "recovery_rate", return_value=0), \
                mock.patch.object(spawn, "scoped_diff", return_value="diff"), \
                mock.patch.object(spawn, "_base_sha", return_value="head"), \
                mock.patch.object(spawn.decision_log, "record"):
            for mode in ("off", "shadow", "active"):
                cfg = {"skills": {"mode": mode}, "context_router": {"mode": "off"}}
                skills = spawn._prepare_skills(task, "execute", cfg)
                execute = spawn.packet(task, TMP, cfg=cfg, skills=skills)
                for role in ("review", "security_review"):
                    review_task = {**task, "role": role}
                    skills = spawn._prepare_skills(review_task, role, cfg)
                    review = spawn.review_packet(review_task, task, cfg=cfg, skills=skills)
                    with self.subTest(mode=mode, role=role):
                        self.assertEqual("specialist: " in review.splitlines()[0], mode == "active")
                        if mode == "active":
                            self.assertIn("specialist: " + role, review.splitlines()[0])
                            self.assertIn("## scope\n", review)
                            self.assertIn("## diff\n", review)
                self.assertEqual("specialist: " in execute.splitlines()[0], mode == "active")
                if mode == "active":
                    self.assertIn("specialist: execute+implement-spec", execute.splitlines()[0])

    def test_packet_with_skills_none_runs_no_specialist_machinery(self):
        from contextlib import ExitStack

        task = {"id": "T-no-specialist", "title": "legacy dispatch", "spec": "s",
                "acceptance": [], "scope": [], "role": "execute", "constraints": {}}
        off = {"skills": {"mode": "off"}, "context_router": {"mode": "off"}}
        active = {**off, "skills": {"mode": "active"}}
        with ExitStack() as stack:
            machinery = [stack.enter_context(mock.patch.object(owner, name,
                         side_effect=AssertionError("unexpected specialist machinery: " + name)))
                         for owner, name in ((spawn, "_prepare_skills"), (spawn.specialist, "compose"),
                                             (spawn.skill_router, "select"), (spawn.skills_registry, "load"),
                                             (spawn.tool_catalog, "minimal_set"), (P.Pool, "pick_executor"),
                                             (spawn.bus, "get"))]
            pool = stack.enter_context(mock.patch.object(spawn, "Pool"))
            pool.return_value.cfg = active
            stack.enter_context(mock.patch.object(spawn, "memory_recall",
                                return_value={"hits": [], "layers_consulted": []}))
            stack.enter_context(mock.patch.object(spawn, "scoped_diff", return_value="diff"))
            stack.enter_context(mock.patch.object(spawn, "_base_sha", return_value="head"))
            for constraints in ({}, {"fix_round_for": "T-original"}):
                execute_task = {**task, "constraints": constraints}
                baseline = spawn.packet(execute_task, TMP, cfg=off)
                # Match daemon dispatch calls: cfg and skills are both omitted.
                self.assertEqual(spawn.packet(execute_task, TMP), baseline)
                self.assertEqual(spawn.packet(execute_task, TMP, cfg=active, skills=None), baseline)
                self.assertNotIn("specialist:", baseline)
                self.assertNotIn("## skills", baseline)
            for role in ("review", "security_review"):
                review_task = {**task, "role": role, "inputs": ["T-reviewed"]}
                baseline = spawn.review_packet(review_task, task, cfg=off)
                self.assertEqual(spawn.review_packet(review_task, task), baseline)
                self.assertEqual(spawn.review_packet(review_task, task, cfg=active, skills=None), baseline)
                self.assertNotIn("specialist:", baseline)
                self.assertNotIn("## skills", baseline)
            for operation in machinery:
                operation.assert_not_called()

    def _active_choice(self):
        return {"selected": ["executor/implement-spec"], "mandatory": ["executor/implement-spec"],
                "ambiguous": [], "tokens_selected_l0": 2, "tokens_selected_l2": 3,
                "tokens_exposed_l0": 2, "candidates": ["executor/implement-spec"],
                "rejected": [], "triggers": {}, "task_class": "feature", "reason": "test"}

    def test_active_skills_section_carries_level2_of_selected_only(self):
        choice = self._active_choice()
        with mock.patch.object(spawn.skills_registry, "render", side_effect=lambda skill, level: f"L{level}:{skill}"), \
                mock.patch.object(spawn, "_skill_records", return_value={"executor/implement-spec": {"version": "1"}}):
            section = spawn._skills_section(choice)["section"]
        self.assertIn("### executor/implement-spec (v1)\nL2:executor/implement-spec", section)

    def test_skills_section_is_first_packet_section_and_capped(self):
        choice = {**self._active_choice(), "selected": ["executor/implement-spec", "executor/extra"]}
        with mock.patch.object(spawn.skills_registry, "render",
                               side_effect=lambda skill, level: ("x" * (100 if skill.endswith("implement-spec") else 2500)
                                                                  if level == 2 else "short")), \
                mock.patch.object(spawn, "_skill_records", return_value={
                    "executor/implement-spec": {"version": "1"}, "executor/extra": {"version": "1"}}):
            rendered = spawn._skills_section(choice)
        self.assertLessEqual(100, spawn._SKILL_PRESENTATION_CAP)
        self.assertIn("executor/extra", rendered["demoted"])

    def test_active_refused_without_shadow_evidence_or_with_high_recovery(self):
        cfg = {"skills": {"mode": "active", "max_recovery": .1}}
        with mock.patch.object(spawn.skill_router, "select", return_value=self._active_choice()), \
                mock.patch.object(spawn.skill_scorecard, "selection_rows", return_value=0), \
                mock.patch.object(spawn.skill_scorecard, "recovery_rate", return_value=0), \
                mock.patch.object(spawn.notify, "notify"):
            self.assertEqual(spawn._prepare_skills({"id": "T"}, "execute", cfg)["mode"], "shadow")

    def test_skill_use_detected_from_read_of_skill_file_and_script_invocation(self):
        rows = [json.dumps({"task": "T-use", "tool": "Read",
                            "tool_target": "skills/executor/a/SKILL.md"}),
                json.dumps({"task": "T-use", "tool": "Bash",
                            "tool_target": "bash skills/review/b/scripts/run.sh"})]
        records = {"executor/a": {"est_tokens_l2": 2}, "review/b": {"est_tokens_l2": 3}}
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value="\n".join(rows)), \
                mock.patch.object(spawn.skills_registry, "load", return_value={"skills": records}):
            self.assertEqual(spawn._skills_from_gate("T-use"), (["executor/a", "review/b"], 5))

    def test_active_launch_adds_disable_slash_commands_and_shadow_does_not(self):
        source = Path(spawn.__file__).read_text()
        self.assertIn('cmd.append("--disable-slash-commands")', source)
        self.assertIn('get("skill_routing_mode") == "active"', source)

    def test_selection_runs_once_before_packet_build(self):
        cfg = {"skills": {"mode": "shadow"}}
        with mock.patch.object(spawn.skill_router, "select", return_value=self._active_choice()) as select:
            choice = spawn._prepare_skills({"id": "T"}, "execute", cfg)
            self.assertEqual(choice["mode"], "shadow")
        select.assert_called_once()


if __name__ == "__main__":
    unittest.main()
def _jev_skill_choice():
    return {"candidates": ["executor/implement-spec", "execute/x"],
            "mandatory": ["executor/implement-spec"], "triggers": {"execute/x": ["x"]},
            "task_class": "code", "selected": ["executor/implement-spec"],
            "presented": [], "rejected": [{"id": "execute/x", "reason": "ambiguous"}],
            "reason": "mandatory skills plus firm trigger matches", "mode": "shadow",
            "tokens_exposed_l0": 2, "tokens_selected_l0": 1, "tokens_selected_l2": 2,
            "skill_tokens_presented_l2": 0, "ambiguous": ["execute/x"], "demoted": [],
            "jev": {"decisions": {"execute/x": {"select": True}}, "source": "jev",
                    "latency_ms": 1, "batch_size": 1}}


class JevSkillRoutingCallSiteTests(unittest.TestCase):
    def test_skill_routing_row_carries_top_level_jev_in_shadow(self):
        choice = _jev_skill_choice()
        with mock.patch.object(spawn.decision_log, "record") as record:
            result = spawn._skill_routing({"id": "T-jev"}, "execute", {}, {}, choice)
        self.assertEqual(result["skills_selected"], ["executor/implement-spec"])
        self.assertIs(record.call_args.kwargs["jev"], choice["jev"])
