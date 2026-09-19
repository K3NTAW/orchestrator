"""Serial merge queue: rebase onto target -> tests-green -> fast-forward, conflict handling, orchestrator-state
commit. Builds its own scratch git repo at TMP (harness scratch_repo) so it never depends on another test file
having turned TMP into a repo first."""
import json, subprocess, sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_merge.py` doesn't add this dir itself
from _harness import TMP, g, scratch_repo
from orchestrator import STATE, bus, merge, spawn


class MergeQueue(unittest.TestCase):
    def _ensure_ci_fixture(self):
        """pyproject.toml + a passing test, committed on whatever branch TMP currently has checked out, so
        tests-green.sh has something to run regardless of which test method (in this file or another sharing
        TMP) ran first. Idempotent: no-ops when the files and commit already exist."""
        (TMP / "tests").mkdir(exist_ok=True)
        if not (TMP / "tests" / "test_ok.py").exists():
            (TMP / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_x(self): pass\n")
        if not (TMP / "pyproject.toml").exists():
            (TMP / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n")
        g("add", "-A"); g("commit", "-qm", "ensure ci fixture")

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

    def test_checkout_ff_when_root_has_target_checked_out(self):
        """ROOT has the target branch checked out and clean -> merge() fast-forwards ROOT's HEAD/index/worktree
        with `git merge --ff-only` instead of leaving them stale behind an update-ref move (2026-09-18)."""
        scratch_repo(TMP)
        self._ensure_ci_fixture()
        g("checkout", "-q", "-b", "goal/CO-sync")  # branches from current HEAD: no working-tree change, safe regardless of dirty state elsewhere
        t = bus.create_task("feat-sync", "s", ["a"], ["co_sync.py"], role="execute")
        wt = spawn.ensure_worktree(t["id"], base="HEAD"); bus.update(t["id"], worktree=str(wt))
        (wt / "co_sync.py").write_text("X = 1\n"); g("add", "-A", cwd=wt); g("commit", "-qm", "feat", cwd=wt)
        sha = g("rev-parse", "HEAD", cwd=wt).stdout.strip()
        r = merge.merge(t["id"], target="goal/CO-sync")
        self.assertEqual(r["status"], "merged", r)
        self.assertTrue(r["checkout_synced"], r)
        self.assertNotIn("checkout_stale", r)
        self.assertEqual(g("rev-parse", "HEAD").stdout.strip(), sha)
        # scoped to our own path: TMP is shared with other test files, which may leave unrelated dirty state
        self.assertEqual(g("status", "--porcelain", "--", "co_sync.py").stdout.strip(), "")

    def test_checkout_stale_when_root_has_conflicting_local_change(self):
        """ROOT has the target checked out with an uncommitted local edit to a file the merge also touches ->
        `git merge --ff-only` refuses; merge() falls back to update-ref (ref still moves) and reports
        checkout_stale, leaving ROOT's local modification untouched."""
        scratch_repo(TMP)
        self._ensure_ci_fixture()
        (TMP / "co_stale.py").write_text("BASE = 0\n")
        g("add", "-A"); g("commit", "-qm", "add co_stale baseline")
        g("checkout", "-q", "-b", "goal/CO-stale")
        t = bus.create_task("feat-stale", "s", ["a"], ["co_stale.py"], role="execute")
        wt = spawn.ensure_worktree(t["id"], base="HEAD"); bus.update(t["id"], worktree=str(wt))
        (wt / "co_stale.py").write_text("X = 2\n"); g("add", "-A", cwd=wt); g("commit", "-qm", "feat2", cwd=wt)
        sha = g("rev-parse", "HEAD", cwd=wt).stdout.strip()
        (TMP / "co_stale.py").write_text("LOCAL EDIT\n")  # uncommitted; overlaps the incoming change
        r = merge.merge(t["id"], target="goal/CO-stale")
        self.assertEqual(r["status"], "merged", r)
        self.assertTrue(r["checkout_stale"], r)
        self.assertFalse(r["checkout_synced"], r)
        self.assertEqual(g("rev-parse", "goal/CO-stale").stdout.strip(), sha)
        self.assertEqual((TMP / "co_stale.py").read_text(), "LOCAL EDIT\n")
        self.assertIn("co_stale.py", g("status", "--porcelain").stdout)

    def test_checkout_synced_false_when_target_not_checked_out(self):
        """ROOT is on another branch entirely -> behaviour is unchanged: update-ref moves the target, ROOT's
        HEAD is untouched, and no checkout_stale key is added."""
        scratch_repo(TMP)
        self._ensure_ci_fixture()
        orig_branch = g("symbolic-ref", "-q", "HEAD").stdout.strip()
        orig_sha = g("rev-parse", "HEAD").stdout.strip()
        t = bus.create_task("feat-none", "s", ["a"], ["co_none.py"], role="execute")
        wt = spawn.ensure_worktree(t["id"], base="HEAD"); bus.update(t["id"], worktree=str(wt))
        (wt / "co_none.py").write_text("X = 3\n"); g("add", "-A", cwd=wt); g("commit", "-qm", "feat3", cwd=wt)
        sha = g("rev-parse", "HEAD", cwd=wt).stdout.strip()
        r = merge.merge(t["id"], target="goal/CO-none")
        self.assertEqual(r["status"], "merged", r)
        self.assertFalse(r["checkout_synced"], r)
        self.assertNotIn("checkout_stale", r)
        self.assertEqual(g("rev-parse", "goal/CO-none").stdout.strip(), sha)
        self.assertEqual(g("symbolic-ref", "-q", "HEAD").stdout.strip(), orig_branch)
        self.assertEqual(g("rev-parse", "HEAD").stdout.strip(), orig_sha)

    def test_merge_refreshes_repomap_on_py_change(self):
        scratch_repo(TMP)
        self._ensure_ci_fixture()
        (TMP / "orchestrator").mkdir(exist_ok=True)
        (TMP / "orchestrator" / "repomap.py").write_text("\"\"\"repomap marker\"\"\"\n")
        (TMP / "orchestrator" / "changed.py").write_text("VALUE = 0\n")
        g("add", "-A"); g("commit", "-qm", "add orchestrator module")
        task = bus.create_task("repomap", "s", ["a"], ["orchestrator/changed.py"], role="execute")
        wt = spawn.ensure_worktree(task["id"], base="HEAD"); bus.update(task["id"], worktree=str(wt))
        (wt / "orchestrator" / "changed.py").write_text("VALUE = 1\n")
        g("add", "-A", cwd=wt); g("commit", "-qm", "change orchestrator module", cwd=wt)
        with patch("orchestrator.merge.build", return_value="repo map test\n") as mapped:
            result = merge.merge(task["id"], target="goal/repomap")
        self.assertEqual(result["status"], "merged", result)
        mapped.assert_called_once_with(TMP, rev=result["sha"])

    def test_merge_reports_repomap_error(self):
        scratch_repo(TMP)
        self._ensure_ci_fixture()
        (TMP / "orchestrator").mkdir(exist_ok=True)
        (TMP / "orchestrator" / "repomap.py").write_text("\"\"\"repomap marker\"\"\"\n")
        (TMP / "orchestrator" / "changed.py").write_text("VALUE = 0\n")
        g("add", "-A"); g("commit", "-qm", "add orchestrator module")
        task = bus.create_task("repomap-error", "s", ["a"], ["orchestrator/changed.py"], role="execute")
        wt = spawn.ensure_worktree(task["id"], base="HEAD"); bus.update(task["id"], worktree=str(wt))
        (wt / "orchestrator" / "changed.py").write_text("VALUE = 1\n")
        g("add", "-A", cwd=wt); g("commit", "-qm", "change orchestrator module", cwd=wt)
        with patch("orchestrator.merge.build", side_effect=RuntimeError("refresh exploded")):
            result = merge.merge(task["id"], target="goal/repomap-error")
        self.assertEqual(result["status"], "merged", result)
        self.assertEqual(result["repomap_error"], "refresh exploded")

    def test_merge_gate_runs_even_with_marker_env(self):
        scratch_repo(TMP)
        self._ensure_ci_fixture()
        task = bus.create_task("gate", "s", ["a"], ["gate.py"], role="execute")
        wt = spawn.ensure_worktree(task["id"], base="HEAD"); bus.update(task["id"], worktree=str(wt))
        (wt / "gate.py").write_text("VALUE = 1\n")
        g("add", "-A", cwd=wt); g("commit", "-qm", "gate", cwd=wt)
        completed = subprocess.CompletedProcess([], 0, "", "")
        real_run = subprocess.run
        def run_gate_only(*args, **kwargs):
            if args[0][:1] == [str(merge.TESTS_GREEN)]:
                return completed
            return real_run(*args, **kwargs)
        with patch.dict("orchestrator.merge.os.environ", {merge.IN_TESTS_GREEN: "1"}), \
             patch("orchestrator.merge.subprocess.run", side_effect=run_gate_only) as gated:
            result = merge.merge(task["id"], target="goal/gate")
        self.assertEqual(result["status"], "merged", result)
        gate_calls = [call for call in gated.call_args_list
                      if call.args[0][:1] == [str(merge.TESTS_GREEN)]]
        self.assertEqual(len(gate_calls), 1)
        self.assertEqual(gate_calls[0].args[0], [str(merge.TESTS_GREEN), str(wt)])


if __name__ == "__main__":
    unittest.main()
