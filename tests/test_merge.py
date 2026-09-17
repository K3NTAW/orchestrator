"""Serial merge queue: rebase onto target -> tests-green -> fast-forward, conflict handling, orchestrator-state
commit. Builds its own scratch git repo at TMP (harness scratch_repo) so it never depends on another test file
having turned TMP into a repo first."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_merge.py` doesn't add this dir itself
from _harness import TMP, g, scratch_repo
from orchestrator import STATE, bus, merge, spawn


class MergeQueue(unittest.TestCase):
    def test_rebase_tests_ff_and_conflict(self):
        scratch_repo(TMP)
        (TMP / "tests" / "test_ok.py").parent.mkdir(exist_ok=True)
        (TMP / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n")
        (TMP / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
        g("add", "-A"); g("commit", "-qm", "add pyproject and a passing test")
        t = bus.create_task("feat", "s", ["a"], ["feature.py"], role="execute")
        wt = spawn.ensure_worktree(t["id"], base="HEAD"); bus.update(t["id"], worktree=str(wt))
        (wt / "feature.py").write_text("X = 1\n"); g("add", "-A", cwd=wt); g("commit", "-qm", "feat", cwd=wt)
        r = merge.merge(t["id"], target="goal/G")
        self.assertEqual(r["status"], "merged", r)
        self.assertEqual(g("rev-parse", "goal/G").stdout, g("rev-parse", "HEAD", cwd=wt).stdout)
        card_path = STATE / "scorecard.json"
        self.assertTrue(card_path.exists())
        self.assertIn("generated_at", json.loads(card_path.read_text()))
        # second task conflicting on the same file -> conflict hunks back, task failed with resume_hint
        t2 = bus.create_task("feat2", "s", ["a"], ["feature.py"], role="execute")
        wt2 = spawn.ensure_worktree(t2["id"], base="main"); bus.update(t2["id"], worktree=str(wt2))
        (wt2 / "feature.py").write_text("X = 2\n"); g("add", "-A", cwd=wt2); g("commit", "-qm", "feat2", cwd=wt2)
        r2 = merge.merge(t2["id"], target="goal/G")
        self.assertEqual(r2["status"], "conflict"); self.assertEqual(r2["files"], ["feature.py"])
        self.assertEqual(bus.get(t2["id"])["resume_hint"]["conflicts"], ["feature.py"])
        self.assertTrue(bus.commit_state())                          # orchestrator-state branch got the task JSON
        self.assertIn(f"tasks/{t['id']}.json", g("ls-tree", "-r", "--name-only", "orchestrator-state").stdout)


if __name__ == "__main__":
    unittest.main()
