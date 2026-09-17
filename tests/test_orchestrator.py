"""One runnable check per non-trivial path: bus rules, pool selection, reset-hint parsing, merge on a scratch repo, and the hooks."""
import contextlib, io, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="orch-"))
os.environ["ORCH_ROOT"] = str(TMP)
(TMP / ".orchestrator").mkdir()
for f in ("pool.toml",):
    (TMP / ".orchestrator" / f).write_text((REPO / ".orchestrator" / f).read_text())
(TMP / ".orchestrator" / "prompts").symlink_to(REPO / ".orchestrator" / "prompts")
(TMP / ".claude").symlink_to(REPO / ".claude")
sys.path.insert(0, str(REPO))
from orchestrator import bus, pool as P, spawn, merge, executor, cli  # noqa: E402

HOOKS = REPO / ".claude" / "hooks"


def hook(name, payload, cwd=None, env=None):
    return subprocess.run([str(HOOKS / name)], input=json.dumps(payload), capture_output=True, text=True, cwd=cwd,
                          env={**os.environ, **(env or {})})


class Bus(unittest.TestCase):
    def test_lifecycle_and_rules(self):
        with self.assertRaises(ValueError):
            bus.create_task("x", "spec", [], ["src/**"])            # no acceptance
        t = bus.create_task("Map auth", "where are tokens read", ["cites path:line"], ["src/auth/**"], parent=None)
        self.assertEqual(t["status"], "queued"); self.assertTrue(t["constraints"]["read_only"])
        bus.claim(t["id"], "claude:B", "wt/x")
        self.assertEqual(bus.read(status="running")[0]["id"], t["id"])
        with self.assertRaises(PermissionError):
            bus.update(t["id"], scope=["everything"])            # only Planner fields are immutable
        with self.assertRaises(ValueError):
            bus.post_result(t["id"], {"summary": "x" * 7000})    # oversize rejected
        bus.post_result(t["id"], {"summary": "ok", "findings": []})
        done = bus.read(t["id"]); self.assertEqual(done["status"], "done"); self.assertIn("provenance", done["result"])
        ev = bus.events(0); self.assertEqual(ev[0]["kind"], "created"); self.assertEqual([e["kind"] for e in ev], ["created", "update", "update"])
        self.assertEqual(bus.next_id(), "T-0002")


class PoolSel(unittest.TestCase):
    def setUp(self):
        P.PERSIST.unlink(missing_ok=True); self.p = P.Pool()

    def test_affinity_reserve_cooldown_budget(self):
        self.assertEqual(self.p.pick("review").id, "B")            # A has no review affinity
        A, B = self.p.get("A"), self.p.get("B")
        self.p.record(A, int(self.p.cap * 0.7))                     # A above 1-reserve(0.35)=0.65 -> scouts go to B
        self.assertEqual(self.p.pick("scout").id, "B")
        self.assertEqual(self.p.pick("planner").id, "A")            # planner ceiling is 1.0
        self.p.cooldown(B, 600); self.assertIsNone(self.p.pick("review"))  # held, not failed
        self.p.resume("B"); self.assertEqual(self.p.pick("review").id, "B")
        B.day_tokens = B.daily_budget; self.assertIsNone(self.p.pick("review"))
        self.assertEqual(P.Pool().get("A").window_tokens, A.window_tokens)  # persisted across restarts

    def test_rate_limit_parsing_and_fallback(self):
        self.assertTrue(P.is_rate_limited("Error: You've hit your usage limit. Resets in 2h 15m"))
        self.assertEqual(P.parse_reset_hint("resets in 2h 15m"), 8100)
        self.assertEqual(P.parse_reset_hint("try again in 30 minutes"), 1800)
        self.assertEqual(P.parse_reset_hint("retry-after: 900"), 900)
        self.assertEqual(P.parse_reset_hint("nothing useful", default=42), 42)
        far = P.parse_reset_hint("You've hit your usage limit. Visit https://x to purchase more credits or try again at Sep 19th, 2099 2:00 PM.")
        self.assertGreater(far, 365 * 24 * 3600)                     # absolute date parsed, not the 1800 default
        self.assertEqual(P.parse_reset_hint("try again at Sep 19th, 2000 2:00 PM"), 60)  # past -> floor
        self.assertEqual([P.fallback_tier(c) for c in (3, 7, 9)], ["sonnet", "opus", None])
        self.assertTrue(P.is_rate_limited("You're out of usage credits. Switch to another model"))
        self.assertFalse(P.is_rate_limited("all good"))

    def test_trust_workspace(self):
        cfg = TMP / "prof"; spawn.trust_workspace(str(cfg), TMP / "wt" / "T-0099")
        self.assertTrue(json.loads((cfg / ".claude.json").read_text())["projects"][str(TMP / "wt" / "T-0099")]["hasTrustDialogAccepted"])


class Executor(unittest.TestCase):
    def test_parse_observed_stream(self):
        lines = ['{"type":"thread.started","thread_id":"01a0ab11-77b3-7431-a9f1-1527ef937b5c"}', '{"type":"turn.started"}',
                 '{"type":"error","message":"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage or try again at Sep 19th, 2026 2:00 PM."}',
                 '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}', "garbage line"]
        ev = executor.parse_events(lines)
        self.assertEqual(ev["thread_id"], "01a0ab11-77b3-7431-a9f1-1527ef937b5c"); self.assertTrue(P.is_rate_limited(ev["error"]))
        ok = executor.parse_events(['{"type":"thread.started","thread_id":"t1"}', '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                                    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'])
        self.assertEqual((ok["message"], ok["usage"]["input_tokens"], ok["error"]), ("done", 10, None))

    def test_reply_requires_thread_and_caps_rounds(self):
        t = bus.create_task("exec", "s", ["a"], ["x.py"], role="execute")
        self.assertEqual(executor.reply(t["id"], "d")["status"], "failed")
        bus.update(t["id"], codex_thread="t1", worktree=str(TMP), rounds=executor.MAX_ROUNDS)
        self.assertEqual(executor.reply(t["id"], "d")["reason"], "round budget exhausted")


    def test_fallback_claude_when_codex_cooling(self):
        pool = P.Pool(); pool.codex.cooldown_until = time.time() + 3600; pool.save()
        ran = []
        t5 = bus.create_task("small", "s", ["a"], ["x.py"], role="execute", complexity=5)
        t7 = bus.create_task("mid", "s", ["a"], ["x.py"], role="execute", complexity=7)
        t9 = bus.create_task("big", "s", ["a"], ["x.py"], role="execute", complexity=9)
        r5 = executor._exhausted(pool, bus.get(t5["id"]), run=lambda tid: ran.append(tid))
        r7 = executor._exhausted(pool, bus.get(t7["id"]), run=lambda tid: ran.append(tid))
        r9 = executor._exhausted(pool, bus.get(t9["id"]), run=lambda tid: ran.append(tid))
        self.assertEqual((r5["status"], r5["tier"], r7["tier"], r9["status"]), ("fallback", "sonnet", "opus", "held"))
        self.assertEqual(bus.get(t5["id"])["tier"], "sonnet")
        time.sleep(0.2); self.assertEqual(sorted(ran), sorted([t5["id"], t7["id"]]))
        pool.codex.cooldown_until = 0; pool.save()
        self.assertIn("tests_green", spawn.render("execute", spec="s", acceptance=["a"], scope=["x"]))


class Executors(unittest.TestCase):
    """[[executors]] routing: complexity bands, disabled placeholders, quota-group cooldowns, scored ranking."""
    LIVE = {"astra", "luna", "terra", "sol"}

    def setUp(self):
        P.PERSIST.unlink(missing_ok=True); self.p = P.Pool()

    def test_bands_and_enabled(self):
        self.assertIn(self.p.pick_executor("execute", 3).id, self.LIVE)
        self.assertEqual(self.p.pick_executor("execute", 8).id, "astra")   # only astra reaches complexity 8
        self.assertIsNone(self.p.pick_executor("review", 3))               # no executor takes that role
        self.assertEqual({e.id for e in self.p.executors.values() if e.enabled}, self.LIVE)
        for eid in ("luna6", "terra6", "sol6"):
            self.p.executors[eid].weight = 99.0                            # disabled wins nothing, whatever its weight
        self.assertIn(self.p.pick_executor("execute", 3).id, self.LIVE)

    def test_quota_group_cooldown_and_roundtrip(self):
        self.p.cooldown_executor("luna", 600, "usage limit")
        self.assertTrue(all(self.p.executors[i].cooling() for i in self.LIVE))  # one member cools the group
        self.assertIsNone(self.p.pick_executor("execute", 8))
        self.assertIsNone(self.p.pick_executor("execute", 3))
        fresh = P.Pool()                                                   # state survives an MCP restart
        self.assertTrue(fresh.executors["astra"].cooling())
        self.assertFalse(fresh.codex_available())

    def test_scores_and_limits(self):
        self.assertEqual(self.p.pick_executor("execute", 3, {"terra": 3.0}).id, "terra")
        self.p.executors["terra"].running = self.p.executors["terra"].max_parallel
        self.assertNotEqual(self.p.pick_executor("execute", 3, {"terra": 3.0}).id, "terra")
        self.p.executors["astra"].day_tasks = self.p.executors["astra"].daily_budget_tasks
        self.assertIn(self.p.pick_executor("execute", 3).id, {"luna", "sol"})
        st = self.p.status()
        self.assertEqual((len(st["executors"]), sum(e["enabled"] for e in st["executors"])), (7, 4))

    def test_missing_table_synthesizes_legacy_row(self):
        cfg = {k: v for k, v in P.config().items() if k != "executors"}
        old = P.Pool(cfg)
        self.assertEqual([e.model for e in old.executors.values()], [cfg["codex"]["model"]])
        self.assertTrue(old.codex_available())

    def test_codex_available_respects_complexity(self):
        self.p.executors["astra"].day_tasks = self.p.executors["astra"].daily_budget_tasks
        self.assertFalse(self.p.codex_available(8))   # only astra reaches band 8, and it's over budget
        self.assertTrue(self.p.codex_available(3))     # luna/terra/sol still have headroom at band 3

    def test_legacy_running_syncs_down_not_just_up(self):
        self.p.codex.running = 2; self.p.save()
        fresh = P.Pool()
        self.assertEqual(fresh.executors["astra"].running, 2)
        fresh.codex.running = 0; fresh.save()
        fresher = P.Pool()
        self.assertEqual(fresher.executors["astra"].running, 0)  # regression: used to ratchet up only


class Render(unittest.TestCase):
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


class Hooks(unittest.TestCase):
    def test_require_acceptance(self):
        self.assertEqual(hook("require-acceptance.sh", {"task_input": {"subject": "do x", "description": "just do it"}}).returncode, 2)
        self.assertEqual(hook("require-acceptance.sh", {"task_input": {"description": "Acceptance: tests pass\nScope: src/**"}}).returncode, 0)

    def test_scope_guard(self):
        t = bus.create_task("edit auth", "s", ["a"], ["src/auth/**", "tests/auth/*.py"], role="execute")
        env = {"ORCH_TASK_ID": t["id"], "ORCH_ROOT": str(TMP)}
        wt = TMP / "wt" / t["id"]; wt.mkdir(parents=True); subprocess.run(["git", "init", "-q"], cwd=wt)
        ok = hook("scope-guard.sh", {"tool_input": {"file_path": str(wt / "src/auth/deep/x.ts")}}, cwd=wt, env=env)
        bad = hook("scope-guard.sh", {"tool_input": {"file_path": str(wt / "src/billing/x.ts")}}, cwd=wt, env=env)
        self.assertEqual(ok.returncode, 0); self.assertEqual(bad.returncode, 2); self.assertIn("outside task", bad.stderr)
        # planner session (no task id) is never blocked
        self.assertEqual(hook("scope-guard.sh", {"tool_input": {"file_path": "/x/y.ts"}}, cwd=TMP, env={"ORCH_TASK_ID": ""}).returncode, 0)

    def test_loop_guard(self):
        log = TMP / ".orchestrator" / "runs"; log.mkdir(exist_ok=True)
        (log / "loop-s1.log").write_text("abc\nabc\nabc\n"); (log / "loop-s2.log").write_text("abc\nabd\nabc\n")
        self.assertEqual(hook("loop-guard.sh", {"session_id": "s1"}, cwd=TMP).returncode, 2)
        self.assertEqual(hook("loop-guard.sh", {"session_id": "s1", "stop_hook_active": True}, cwd=TMP).returncode, 0)
        self.assertEqual(hook("loop-guard.sh", {"session_id": "s2"}, cwd=TMP).returncode, 0)

    def test_retrospect_and_uncommitted(self):
        self.assertEqual(hook("retrospect-written.sh", {"task_input": {"subject": "GOAL: ship x"}}, cwd=TMP).returncode, 2)
        (TMP / ".orchestrator" / "memory").mkdir(exist_ok=True)
        (TMP / ".orchestrator" / "memory" / "gotchas.md").write_text(f"## {time.strftime('%Y-%m-%d')} learned\n")
        self.assertEqual(hook("retrospect-written.sh", {"task_input": {"subject": "GOAL: ship x"}}, cwd=TMP).returncode, 0)
        self.assertEqual(hook("retrospect-written.sh", {"task_input": {"subject": "scout: map"}}, cwd=TMP).returncode, 0)
        r = TMP / "wt" / "dirty"; r.mkdir(parents=True); subprocess.run(["git", "init", "-q"], cwd=r); (r / "a").write_text("x")
        self.assertEqual(hook("no-uncommitted.sh", {"cwd": str(r)}).returncode, 2)

    def test_tests_green_failures_only(self):
        r = TMP / "pyproj"; (r / "tests").mkdir(parents=True); (r / "pyproject.toml").write_text("[project]\nname='x'\n")
        (r / "tests" / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.assertEqual(1, 2)\n")
        out = hook("tests-green.sh", {"cwd": str(r), "session_id": "tg"}, env={"PATH": "/usr/bin:/bin"})  # no pytest -> unittest
        self.assertEqual(out.returncode, 2); self.assertIn("FAILED", out.stderr); self.assertNotIn("Ran 1 test", out.stderr.split("Failures only")[0])
        (r / "tests" / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n")
        self.assertEqual(hook("tests-green.sh", {"cwd": str(r), "session_id": "tg"}, env={"PATH": "/usr/bin:/bin"}).returncode, 0)


class Guardrails(unittest.TestCase):
    def bash(self, cmd):
        return hook("guardrails.sh", {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(TMP)}, cwd=REPO).returncode

    def test_blocks_destructive_and_protected(self):
        blocked = ["sudo rm -rf /var/db", "rm -rf ~/Documents/x", "rm -rf /", "cat ~/.ssh/id_ed25519", f"cat {os.path.expanduser('~')}/.codex/auth.json",
                   "git push --force origin main", "git push origin main", "security find-generic-password -s x", "diskutil eraseDisk x y z",
                   "launchctl bootout system/x", "cp foo ~/code/orchestrator/.claude/hooks/loop-guard.sh"]
        for c in blocked:
            self.assertEqual(self.bash(c), 2, c)

    def test_allows_normal_work(self):
        ok = ["git status", "rm -rf node_modules", f"rm -rf {TMP}/wt/T-0001", "npm test", "git push origin task/T-0001", "uv run pytest -q",
              "ls ~/code", "rm -f a.txt", "sudoku --help", "echo security"]
        for c in ok:
            self.assertEqual(self.bash(c), 0, c)

    def test_edit_protected_path(self):
        home = os.path.expanduser("~")
        self.assertEqual(hook("guardrails.sh", {"tool_name": "Write", "tool_input": {"file_path": f"{home}/.ssh/config"}}, cwd=REPO).returncode, 2)
        # not REPO-relative: a worktree's .claude/ is not protected; the list names the main checkout
        protected = [l.strip() for l in (REPO / ".orchestrator" / "protected-paths.txt").read_text().splitlines() if l.strip().endswith(".claude/settings.json")][0]
        self.assertEqual(hook("guardrails.sh", {"tool_name": "Edit", "tool_input": {"file_path": os.path.expanduser(protected)}}, cwd=REPO).returncode, 2)
        self.assertEqual(hook("guardrails.sh", {"tool_name": "Edit", "tool_input": {"file_path": str(REPO / "orchestrator/cli.py")}}, cwd=REPO).returncode, 0)


class MergeQueue(unittest.TestCase):
    def test_rebase_tests_ff_and_conflict(self):
        # scratch "project" repo: main with a passing unittest; tasks branch off it
        g = lambda *a, cwd=TMP, **k: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, **k)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (TMP / "tests" / "test_ok.py").parent.mkdir(exist_ok=True)
        (TMP / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n")
        (TMP / "pyproject.toml").write_text("[project]\nname='x'\n"); (TMP / ".gitignore").write_text(".orchestrator/\nwt/\n.claude\n")
        g("add", "-A"); g("commit", "-qm", "init")
        t = bus.create_task("feat", "s", ["a"], ["feature.py"], role="execute")
        wt = spawn.ensure_worktree(t["id"], base="HEAD"); bus.update(t["id"], worktree=str(wt))
        (wt / "feature.py").write_text("X = 1\n"); g("add", "-A", cwd=wt); g("commit", "-qm", "feat", cwd=wt)
        env = {**os.environ, "PATH": "/usr/bin:/bin"}
        r = merge.merge(t["id"], target="goal/G")
        self.assertEqual(r["status"], "merged", r)
        self.assertEqual(g("rev-parse", "goal/G").stdout, g("rev-parse", "HEAD", cwd=wt).stdout)
        # second task conflicting on the same file -> conflict hunks back, task failed with resume_hint
        t2 = bus.create_task("feat2", "s", ["a"], ["feature.py"], role="execute")
        wt2 = spawn.ensure_worktree(t2["id"], base="main"); bus.update(t2["id"], worktree=str(wt2))
        (wt2 / "feature.py").write_text("X = 2\n"); g("add", "-A", cwd=wt2); g("commit", "-qm", "feat2", cwd=wt2)
        r2 = merge.merge(t2["id"], target="goal/G")
        self.assertEqual(r2["status"], "conflict"); self.assertEqual(r2["files"], ["feature.py"])
        self.assertEqual(bus.get(t2["id"])["resume_hint"]["conflicts"], ["feature.py"])
        self.assertTrue(bus.commit_state())                          # orchestrator-state branch got the task JSON
        self.assertIn("tasks/T-0001.json", g("ls-tree", "-r", "--name-only", "orchestrator-state").stdout)


class Cli(unittest.TestCase):
    def test_status_plain_and_json(self):
        sys.argv = ["orchestrator", "status", "--plain"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("A\tutil="))
        self.assertTrue(lines[2].startswith("codex\tavailable="))

        sys.argv = ["orchestrator", "status"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        parsed = json.loads(out.getvalue())
        self.assertEqual(set(parsed.keys()), {"accounts", "executors", "codex", "queue"})
        self.assertEqual((len(parsed["executors"]), sum(e["enabled"] for e in parsed["executors"])), (7, 4))


if __name__ == "__main__":
    unittest.main()


class MemorySkill(unittest.TestCase):
    S = REPO / "skills" / "planner" / "memory" / "scripts"

    def run_s(self, name, *args, stdin=None):
        env = {**os.environ, "CLAUDE_MEM_DB": str(TMP / "no.db"), "GRAPHIFY_OUT": str(TMP / "no-graph")}
        return subprocess.run(["bash", str(self.S / name), *args], input=stdin, capture_output=True, text=True, cwd=REPO, env=env)

    def test_record_then_recall(self):
        today = time.strftime("%Y-%m-%d")
        goal = bus.create_task("GOAL: memory skill", "spec", ["works"], ["skills/**"])
        kid = bus.create_task("Map memory", "where", ["cites"], ["skills/**"], parent=goal["id"])
        bus.post_result(kid["id"], {"summary": "memory lives in .orchestrator/memory",
                                    "findings": [{"claim": "four files", "evidence": ["CLAUDE.md:11"], "confidence": 0.9}]})
        d = self.run_s("record.sh", "draft", goal["id"])
        self.assertEqual(d.returncode, 0, d.stderr); self.assertIn(kid["id"], d.stdout); self.assertIn("CLAUDE.md:11", d.stdout)
        a = self.run_s("record.sh", "add", "--file", "decisions", "--type", "decision", "--title", "Layered memory skill",
                       "--goal", goal["id"], "--fact", "skills/planner/memory/SKILL.md:1 — three layers", "--outcome", "shipped")
        self.assertEqual(a.returncode, 0, a.stderr)
        txt = (TMP / ".orchestrator" / "memory" / "decisions.md").read_text()
        self.assertIn(f"## {today} Layered memory skill", txt); self.assertNotIn("(empty", txt)
        for i in range(2):  # same gotcha title twice -> one entry, latest body
            self.run_s("record.sh", "add", "--file", "gotchas", "--type", "gotcha", "--title", "shebang trips guardrails",
                       "--goal", goal["id"], "--fact", f"fact {i}")
        g = (TMP / ".orchestrator" / "memory" / "gotchas.md").read_text()
        self.assertEqual(g.count("shebang trips guardrails"), 1); self.assertIn("fact 1", g); self.assertNotIn("fact 0", g)
        s = self.run_s("record.sh", "add", "--file", "decisions", "--type", "decision", "--title", "x", "--goal", goal["id"],
                       "--fact", "token ghp_" + "a" * 30)
        self.assertEqual(s.returncode, 1); self.assertIn("secret", s.stderr)
        bad = self.run_s("record.sh", "add", "--file", "architecture", "--type", "decision", "--title", "x", "--goal", goal["id"])
        self.assertEqual(bad.returncode, 1)
        self.run_s("record.sh", "set", "architecture", stdin="entry: orchestrator/cli.py")
        self.run_s("record.sh", "set", "architecture", stdin="entry: orchestrator/mcp.py")
        arch = (TMP / ".orchestrator" / "memory" / "architecture.md").read_text()
        self.assertIn("mcp.py", arch); self.assertNotIn("cli.py", arch); self.assertIn(f"updated {today}", arch)
        ix = self.run_s("recall.sh", "index", "layered memory skill")
        self.assertEqual(ix.returncode, 0, ix.stderr)
        self.assertIn("mem:decisions.md:", ix.stdout); self.assertIn(f"bus:{kid['id']}", ix.stdout); self.assertNotIn("cmem:", ix.stdout)
        mem_id = next(l.split(" · ")[0] for l in ix.stdout.splitlines() if l.startswith("mem:decisions"))
        got = self.run_s("recall.sh", "get", mem_id, f"bus:{kid['id']}", "mem:decisions.md:999")
        self.assertIn("three layers", got.stdout); self.assertIn("four files", got.stdout); self.assertIn("no entry", got.stdout)
        self.assertEqual(self.run_s("recall.sh", "index", "zzqx-nothing-matches").stdout.strip()[:7], "no hits")
        # the retrospect-written hook accepts what record.sh wrote
        self.assertEqual(hook("retrospect-written.sh", {"task_input": {"subject": "GOAL: memory"}}, cwd=TMP).returncode, 0)


@unittest.skipUnless((HOOKS / "planner-mode.sh").exists(), "planner-mode hooks not installed (see scratchpad apply.sh)")
class PlannerMode(unittest.TestCase):
    """Planner session = no ORCH_TASK_ID and a cwd whose basename is not T-xxxx. Workers are scope-guard's job."""
    P = {"ORCH_TASK_ID": ""}

    def pm(self, tool, inp, env=None):
        return hook("planner-mode.sh", {"tool_name": tool, "tool_input": inp, "cwd": str(TMP)}, cwd=TMP, env={**self.P, **(env or {})}).returncode

    def test_writes(self):
        src = str(REPO / "orchestrator" / "x.py")  # TMP is under /var/folders, a temp dir to the hook
        self.assertEqual(self.pm("Write", {"file_path": src}), 2)
        self.assertEqual(self.pm("Edit", {"file_path": str(REPO / "skills" / "planner" / "orchestrate" / "SKILL.md")}), 2)
        self.assertEqual(self.pm("Write", {"file_path": str(TMP / ".orchestrator" / "plan.md")}), 0)
        self.assertEqual(self.pm("Write", {"file_path": "/private/tmp/scratch/pr-body.md"}), 0)
        self.assertEqual(self.pm("Write", {"file_path": src}, {"ORCH_TASK_ID": "T-0007"}), 0)
        self.assertEqual(self.pm("Write", {"file_path": src}, {"ORCH_PLANNER_MODE": "0"}), 0)

    def test_bash(self):
        blocked = ["cat > skills/x/SKILL.md <<'EOF'\nhi\nEOF", "echo x >> README.md", "sed -i '' 's/a/b/' orchestrator/bus.py",
                   "ln -s ../../skills/x .claude/skills/x", "python3 - <<'EOF'\nfrom pathlib import Path\nPath('README.md').write_text('x')\nEOF",
                   "git rebase main", "git reset --hard HEAD~1", "git merge task/T-0001", "git -C ~/dotfiles cherry-pick abc123"]
        allowed = ["git push -u origin goal/T-0001", "gh pr create --base main --body-file /private/tmp/x/body.md",
                   "uv run python -m unittest tests/test_orchestrator.py 2>&1 | tail -5", "git status --short && git log --oneline -3",
                   "bash skills/planner/memory/scripts/record.sh add --file decisions --type decision --title x --goal T-1 --fact y",
                   "cat > .orchestrator/plan.md <<'EOF'\ngoal\nEOF", "bash skills/planner/memory/scripts/graph.sh update",
                   "grep -rn memory orchestrator/ > /dev/null; ls", "mkdir -p /private/tmp/s && echo hi > /private/tmp/s/a.md",
                   "git commit -m x", "git add -A && git commit -q -F - <<'EOF'\nmsg\nEOF", "git checkout -b memory-skill"]
        for c in blocked:
            self.assertEqual(self.pm("Bash", {"command": c}), 2, c)
        for c in allowed:
            self.assertEqual(self.pm("Bash", {"command": c}), 0, c)
        self.assertEqual(self.pm("Bash", {"command": "git commit -m x"}, {"ORCH_TASK_ID": "T-0007"}), 0)

    def test_prompt_hook(self):
        r = hook("planner-prompt.sh", {"prompt": "add a flag"}, cwd=TMP, env=self.P)
        self.assertEqual(r.returncode, 0); self.assertIn("Skill(orchestrate)", r.stdout)
        self.assertEqual(hook("planner-prompt.sh", {"prompt": "/orchestrate x"}, cwd=TMP, env=self.P).stdout, "")
        self.assertEqual(hook("planner-prompt.sh", {"prompt": "x"}, cwd=TMP, env={"ORCH_TASK_ID": "T-0001"}).stdout, "")
