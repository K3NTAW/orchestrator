"""spawn.run_worker's review-verdict propagation, prompt template rendering / result fitting, base-branch
selection for stacked/challenge/review tasks (review T-0026, T-0030), and headless-host secret/token wiring
(env-form secrets, CLAUDE_CODE_OAUTH_TOKEN injection)."""
import json, os, subprocess, sys, unittest
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
    def test_run_worker_writes_verdict_on_both_tasks_and_prompt_has_spec_and_code_excerpt(self):
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
        self.assertIn("def handler():", captured["prompt"])
        self.assertRegex(captured["prompt"], r"(?m)^\s*\d+\| ")


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

    def test_packet_trims_acceptance_before_discovery(self):
        task = self.packet_fixture()
        task["acceptance"] = [f"criterion {i} " + "x" * 100 for i in range(100)]
        text = spawn.packet(task, TMP)
        self.assertLess(len(text), 4800)
        acceptance = text.split("## acceptance\n", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(acceptance.count("criterion "), 5)
        self.assertIn("- 95 more in the task", acceptance)
        self.assertIn("widget.py:3 build_widget", text)
        relevant = text.split("## relevant_tests\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("tests/test_widget.py", relevant)
        self.assertIn("packet truncated:", text)

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

    def test_execute_prompt_contains_packet(self):
        p = spawn.packet(self.packet_fixture(), TMP)
        text = spawn.render("execute", packet=p, spec="s", acceptance=["a"], scope=["widget.py"])
        self.assertTrue(text.startswith("## objective"))
        self.assertIn("Build widget", text)

    def test_packet_gotcha_match_by_path(self):
        task = self.packet_fixture()
        memory = TMP / ".orchestrator" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "gotchas.md").write_text("## Widget cache\nFacts: changing widget.py needs a cache reset.\n")
        text = spawn.packet(task, TMP)
        self.assertRegex(text, r"mem:gotchas\.md:1 Widget cache")

    def test_templates_fill(self):
        s = spawn.render("scout", id="T-1", title="t", spec="q", acceptance=["a"], turns="20")
        self.assertIn("T-1", s); self.assertNotIn("{{", s)
        self.assertEqual(spawn.extract_json('here: {"summary":"x"} bye')["summary"], "x")
        self.assertTrue(spawn.extract_json("no json")["summary"])

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
        diff = spawn.scoped_diff(review)
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


if __name__ == "__main__":
    unittest.main()
