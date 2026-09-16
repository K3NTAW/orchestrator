"""One runnable check per non-trivial path: bus rules, pool selection, reset-hint parsing, merge on a scratch repo, and the hooks."""
import json, os, subprocess, sys, tempfile, time, unittest
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
from orchestrator import bus, pool as P, spawn, merge, executor  # noqa: E402

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


class Render(unittest.TestCase):
    def test_templates_fill(self):
        s = spawn.render("scout", id="T-1", title="t", spec="q", acceptance=["a"], turns="20")
        self.assertIn("T-1", s); self.assertNotIn("{{", s)
        self.assertEqual(spawn.extract_json('here: {"summary":"x"} bye')["summary"], "x")
        self.assertTrue(spawn.extract_json("no json")["summary"])


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


if __name__ == "__main__":
    unittest.main()
