"""orchestrator.install: scaffold the §9 layout into another git repo so ORCH_ROOT can point at it. Uses its own
scratch git repos under TMP (harness scratch_repo), never REPO itself."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_install.py` doesn't add this dir itself
from _harness import REPO, TMP, scratch_repo
from orchestrator import cli, install


class Install(unittest.TestCase):
    def test_memory_index_is_ignored(self):
        self.assertIn(".orchestrator/memory/index.sqlite*", install.IGNORE_LINES)
        ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text().splitlines()
        self.assertIn(".orchestrator/memory/index.sqlite*", ignore)
        for suffix in ("", "-wal", "-shm"):
            self.assertTrue(Path(f".orchestrator/memory/index.sqlite{suffix}").match(
                ".orchestrator/memory/index.sqlite*"))

    def test_creates_scaffold_and_rewrites_mcp(self):
        target = scratch_repo(TMP / "install-target").resolve()
        report = install.install(target, orch_repo=REPO)

        self.assertTrue(any(l.startswith("created .orchestrator/pool.toml") for l in report))
        for p in (".orchestrator/pool.toml", ".orchestrator/protected-paths.txt", ".orchestrator/plan.md",
                  ".orchestrator/tasks/.gitkeep", ".orchestrator/memory/decisions.md",
                  ".claude/settings.json"):
            self.assertTrue((target / p).exists(), p)
        self.assertTrue((target / ".orchestrator" / "prompts").is_dir())
        self.assertTrue((target / ".claude" / "skills").is_dir())
        self.assertTrue((target / "skills").is_dir())

        hooks_dir = target / ".claude" / "hooks"
        self.assertTrue(hooks_dir.is_dir())
        some_hook = next(hooks_dir.glob("*.sh"))
        self.assertTrue(some_hook.stat().st_mode & 0o111, "hook should keep its executable bit")

        for name in ("orchestrator", "bus"):
            mcp = json.loads((target / ".mcp.planner.json").read_text())
            server = mcp["mcpServers"][name]
            self.assertEqual(server["args"][:3], ["run", "--project", str(REPO)])
            self.assertEqual(server["env"]["ORCH_ROOT"], str(target))
        github = json.loads((target / ".mcp.planner.json").read_text())["mcpServers"]["github"]
        self.assertEqual(github["command"], "github-mcp-server")
        self.assertNotIn("ORCH_ROOT", github.get("env", {}))

    def test_makefile_gates_tests_sh(self):
        with_make = scratch_repo(TMP / "install-with-make")
        (with_make / "Makefile").write_text("test:\n\techo t\nlint:\n\techo l\ntypecheck:\n\techo tc\n")
        install.install(with_make, orch_repo=REPO)
        tests_sh = with_make / ".orchestrator" / "tests.sh"
        self.assertTrue(tests_sh.exists())
        self.assertTrue(tests_sh.stat().st_mode & 0o111)
        self.assertIn("make test", tests_sh.read_text())

        without_make = scratch_repo(TMP / "install-without-make")
        install.install(without_make, orch_repo=REPO)
        self.assertFalse((without_make / ".orchestrator" / "tests.sh").exists())

    def test_existing_settings_json_kept_alongside_orchestrator_json(self):
        target = scratch_repo(TMP / "install-existing-settings")
        (target / ".claude").mkdir(exist_ok=True)
        (target / ".claude" / "settings.json").write_text('{"mine": true}\n')
        report = install.install(target, orch_repo=REPO)
        self.assertEqual((target / ".claude" / "settings.json").read_text(), '{"mine": true}\n')
        self.assertTrue((target / ".claude" / "settings.orchestrator.json").exists())
        self.assertTrue(any("note:" in l for l in report))

    def test_second_run_is_idempotent(self):
        target = scratch_repo(TMP / "install-idempotent").resolve()
        install.install(target, orch_repo=REPO)
        pool_before = (target / ".orchestrator" / "pool.toml").read_bytes()
        plan_before = (target / ".orchestrator" / "plan.md").read_bytes()
        mem_before = (target / ".orchestrator" / "memory" / "gotchas.md").read_bytes()

        report2 = install.install(target, orch_repo=REPO)

        self.assertEqual((target / ".orchestrator" / "pool.toml").read_bytes(), pool_before)
        self.assertEqual((target / ".orchestrator" / "plan.md").read_bytes(), plan_before)
        self.assertEqual((target / ".orchestrator" / "memory" / "gotchas.md").read_bytes(), mem_before)
        self.assertFalse(any(l.startswith("created ") for l in report2), report2)

        protected = (target / ".orchestrator" / "protected-paths.txt").read_text()
        self.assertIn(str(target / ".claude" / "hooks"), protected)

        gitignore_lines = (target / ".gitignore").read_text().splitlines()
        self.assertEqual(gitignore_lines.count("wt/"), 1)
        for line in install.IGNORE_LINES:
            self.assertIn(line, gitignore_lines)

    def test_nonexistent_and_non_git_target_exit_nonzero(self):
        with self.assertRaises(SystemExit) as cm:
            install.install(TMP / "does-not-exist")
        self.assertNotEqual(cm.exception.code, 0)

        non_git = TMP / "not-a-git-repo"
        non_git.mkdir(exist_ok=True)
        with self.assertRaises(SystemExit) as cm2:
            install.install(non_git)
        self.assertNotEqual(cm2.exception.code, 0)

        sys.argv = ["orchestrator", "install", str(TMP / "does-not-exist-cli")]
        with self.assertRaises(SystemExit) as cm3:
            cli.main()
        self.assertNotEqual(cm3.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
