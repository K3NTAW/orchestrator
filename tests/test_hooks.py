"""The .claude/hooks/*.sh scripts: acceptance gating, scope guard, loop guard, retrospect/uncommitted checks,
tests-green, guardrails (destructive/protected command blocking), and planner-mode (Planner may not edit source)."""
import os, shutil, subprocess, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_hooks.py` doesn't add this dir itself
from _harness import HOOKS, REPO, TMP, hook
from orchestrator import bus


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

    def test_tests_green_picks_uv_runner_when_available(self):
        r = TMP / "pyproj-uv"; (r / "tests").mkdir(parents=True); (r / "pyproject.toml").write_text("[project]\nname='x'\n")
        (r / "tests" / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n")
        no_uv = hook("tests-green.sh", {"cwd": str(r)}, env={"PATH": "/usr/bin:/bin", "TESTS_GREEN_DRY": "1"})
        self.assertEqual(no_uv.returncode, 0); self.assertIn("python3 -m unittest", no_uv.stdout)
        uv_path = shutil.which("uv")
        if not uv_path: self.skipTest("uv not on PATH")
        with_uv = hook("tests-green.sh", {"cwd": str(r)}, env={"PATH": f"{os.path.dirname(uv_path)}:/usr/bin:/bin", "TESTS_GREEN_DRY": "1"})
        self.assertEqual(with_uv.returncode, 0); self.assertIn("uv run --project .", with_uv.stdout)


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
                   "uv run python -m unittest discover -q tests 2>&1 | tail -5", "git status --short && git log --oneline -3",
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


if __name__ == "__main__":
    unittest.main()
