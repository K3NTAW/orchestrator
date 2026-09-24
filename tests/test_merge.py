"""Serial merge queue: rebase onto target -> tests-green -> fast-forward, conflict handling, orchestrator-state
commit. Builds its own scratch git repo at TMP (harness scratch_repo) so it never depends on another test file
having turned TMP into a repo first."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_merge.py` doesn't add this dir itself
from _harness import TMP, g, scratch_repo
from orchestrator import STATE, bus, merge, spawn


class MergeQueue(unittest.TestCase):
    def test_diff_hash_failure_blocks_merge(self):
        for failed_command in ("diff", "patch-id"):
            for phase in ("before", "after"):
                with self.subTest(command=failed_command, phase=phase), \
                        tempfile.TemporaryDirectory(prefix="orch-diff-failure-") as directory:
                    repo = Path(directory)
                    scratch_repo(repo)
                    state = repo / ".orchestrator"
                    state.mkdir(exist_ok=True)
                    calls = []
                    failed_calls = []
                    gate_calls = []
                    command_calls = 0

                    def git(*args, **kwargs):
                        nonlocal command_calls
                        calls.append(args)
                        if args[:2] == ("diff", "--binary") and failed_command == "diff":
                            command_calls += 1
                            if command_calls == (1 if phase == "before" else 2):
                                failed_calls.append(args)
                                return subprocess.CompletedProcess(args, 1, "", "git diff failed")
                        return subprocess.CompletedProcess(args, 0, "unchanged", "")

                    real_run = subprocess.run
                    def run(*args, **kwargs):
                        nonlocal command_calls
                        if tuple(args[0][:3]) == ("git", "patch-id", "--stable") and failed_command == "patch-id":
                            command_calls += 1
                            if command_calls == (1 if phase == "before" else 2):
                                failed_calls.append(tuple(args[0][1:]))
                                return subprocess.CompletedProcess(args[0], 1, "", "git patch-id failed")
                        if args[0][:1] == [str(merge.TESTS_GREEN)]:
                            gate_calls.append(args[0])
                            return subprocess.CompletedProcess(args[0], 0, "", "")
                        return real_run(*args, **kwargs)

                    with patch.multiple(bus, STATE=state, TASKS=state / "tasks", RUNS=state / "runs"), \
                            patch.multiple(merge, ROOT=repo, git=git), patch.object(merge.subprocess, "run", side_effect=run):
                        task = bus.create_task("diff failure", "s", ["a"], ["feature.py"], role="execute")
                        bus.update(task["id"], worktree=str(repo), pipeline={"reviewed_sha": "reviewed"})
                        result = merge.merge(task["id"], target="goal/reviewed", refresh_repomap=False)
                        self.assertEqual(len(failed_calls), 1)
                        self.assertEqual(result["status"], "failed", result)
                        self.assertIn("git diff", result["reason"])
                        self.assertIn(failed_command, result["reason"])
                        self.assertIn(phase, result["reason"])
                        self.assertEqual(bus.get(task["id"])["status"], "failed")
                        self.assertFalse(bus.get(task["id"]).get("merged_into"))
                        self.assertFalse(any(call[0] in ("merge", "update-ref") for call in calls))
                        self.assertEqual(gate_calls, [])

    def reviewed_rebase(self, changed):
        """Real local rebase: an upstream duplicate drops a reviewed hunk; an unrelated commit does not."""
        with tempfile.TemporaryDirectory(prefix="orch-reviewed-rebase-") as directory:
            repo = Path(directory)
            scratch_repo(repo)
            state = repo / ".orchestrator"
            state.mkdir(exist_ok=True)
            def git(*args, cwd=repo, check=True):
                result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
                if check:
                    self.assertEqual(result.returncode, 0, result.stderr)
                return result
            with patch.multiple(bus, STATE=state, TASKS=state / "tasks", RUNS=state / "runs"), \
                    patch.multiple(merge, ROOT=repo, git=git):
                task = bus.create_task("reviewed rebase", "s", ["a"], ["feature.py"], role="execute", parent="G")
                git("branch", "goal/reviewed")
                wt = repo / "task-worktree"
                git("worktree", "add", "-b", "task/reviewed", str(wt), "HEAD")
                (wt / "feature.py").write_text("VALUE = 1\n")
                git("add", "feature.py", cwd=wt)
                git("commit", "-qm", "reviewed feature", cwd=wt)
                reviewed_sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
                bus.update(task["id"], worktree=str(wt), pipeline={"reviewed_sha": reviewed_sha, "first_green_at": 10})
                git("checkout", "goal/reviewed")
                upstream_path = "feature.py" if changed else "unrelated.txt"
                (repo / upstream_path).write_text("VALUE = 1\n" if changed else "upstream\n")
                git("add", upstream_path)
                git("commit", "-qm", "upstream change")
                target_sha = git("rev-parse", "HEAD").stdout.strip()
                gate = repo / "passing-gate.sh"
                gate.write_text("#!/bin/sh\nexit 0\n")
                gate.chmod(0o755)
                with patch.object(merge, "TESTS_GREEN", gate), \
                        patch.object(bus, "commit_state"), patch.object(merge.scorecard, "write"), \
                        patch.object(merge.scorecard, "build", return_value={}):
                    result = merge.merge(task["id"], target="goal/reviewed", refresh_repomap=False)
                if changed:
                    self.assertEqual(result["status"], "rebase_changed_diff", result)
                    self.assertEqual(git("rev-parse", "goal/reviewed").stdout.strip(), target_sha)
                    self.assertFalse(bus.get(task["id"]).get("merged_into"))
                else:
                    self.assertEqual(result["status"], "merged", result)
                    self.assertNotEqual(result["sha"], reviewed_sha)
                    self.assertEqual(git("rev-parse", "goal/reviewed").stdout.strip(), result["sha"])
                    merged = bus.get(task["id"])
                    self.assertIsInstance(merged["merged_at"], float)
                    self.assertEqual(merged["pipeline"]["merged_at"], merged["merged_at"])
                    self.assertEqual(merged["changed_files"], ["feature.py"])
                    for location in ("pipeline", "top-level"):
                        if location == "top-level":
                            merged["pipeline"].pop("merged_at")
                            bus.update(task["id"], pipeline=merged["pipeline"])
                        card = merge.scorecard.parallelism(root=state, goal="G")
                        self.assertEqual(card["totals"]["median_merge_wait_s"], merged["merged_at"] - 10)
                    self.assertEqual((wt / "feature.py").read_text(), "VALUE = 1\n")
                    self.assertEqual((wt / "unrelated.txt").read_text(), "upstream\n")

    def test_rebase_changing_diff_returns_status(self):
        self.reviewed_rebase(changed=True)

    def test_merge_stamps_merged_at_and_changed_files(self):
        self.reviewed_rebase(changed=False)

    def test_clean_rebase_keeps_approval(self):
        self.reviewed_rebase(changed=False)

    def reviewed_same_file_rebase(self, upstream_lines):
        with tempfile.TemporaryDirectory(prefix="orch-same-file-rebase-") as directory:
            repo = Path(directory)
            scratch_repo(repo)
            state = repo / ".orchestrator"
            state.mkdir(exist_ok=True)
            def git(*args, cwd=repo, check=True):
                result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
                if check:
                    self.assertEqual(result.returncode, 0, result.stderr)
                return result
            with patch.multiple(bus, STATE=state, TASKS=state / "tasks", RUNS=state / "runs"), \
                    patch.multiple(merge, ROOT=repo, git=git):
                base_lines = [f"line {number}\n" for number in range(20)]
                (repo / "feature.py").write_text("".join(base_lines))
                git("add", "feature.py")
                git("commit", "-qm", "feature baseline")
                git("branch", "goal/reviewed")
                wt = repo / "task-worktree"
                git("worktree", "add", "-b", "task/reviewed", str(wt), "HEAD")
                task_lines = list(base_lines)
                task_lines[5] = "task first change\n"
                task_lines[15] = "task second change\n"
                (wt / "feature.py").write_text("".join(task_lines))
                git("add", "feature.py", cwd=wt)
                git("commit", "-qm", "reviewed feature", cwd=wt)
                reviewed_sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
                task = bus.create_task("reviewed rebase", "s", ["a"], ["feature.py"], role="execute", parent="G")
                bus.update(task["id"], worktree=str(wt), pipeline={"reviewed_sha": reviewed_sha})
                git("checkout", "goal/reviewed")
                (repo / "feature.py").write_text("".join(upstream_lines(base_lines)))
                git("add", "feature.py")
                git("commit", "-qm", "upstream same-file change")
                gate = repo / "passing-gate.sh"
                gate.write_text("#!/bin/sh\nexit 0\n")
                gate.chmod(0o755)
                with patch.object(merge, "TESTS_GREEN", gate), patch.object(bus, "commit_state"), \
                        patch.object(merge.scorecard, "write"), patch.object(merge.scorecard, "build", return_value={}):
                    return merge.merge(task["id"], target="goal/reviewed", refresh_repomap=False)

    def test_context_only_rebase_merges(self):
        def insert_at_top(lines):
            return ["upstream insertion\n", *lines]
        result = self.reviewed_same_file_rebase(insert_at_top)
        self.assertEqual(result["status"], "merged", result)

    def test_patch_content_change_after_rebase_holds(self):
        def duplicate_one_task_hunk(lines):
            changed = list(lines)
            changed[5] = "task first change\n"
            return changed
        result = self.reviewed_same_file_rebase(duplicate_one_task_hunk)
        self.assertEqual(result["status"], "rebase_changed_diff", result)

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

    def test_merge_gate_runs(self):
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
        with patch("orchestrator.merge.subprocess.run", side_effect=run_gate_only) as gated:
            result = merge.merge(task["id"], target="goal/gate")
        self.assertEqual(result["status"], "merged", result)
        gate_calls = [call for call in gated.call_args_list
                      if call.args[0][:1] == [str(merge.TESTS_GREEN)]]
        self.assertEqual(len(gate_calls), 1)
        self.assertEqual(gate_calls[0].args[0], [str(merge.TESTS_GREEN), str(wt)])


if __name__ == "__main__":
    unittest.main()
