"""Tests for strict stale-work git evidence."""
import subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import scratch_repo
from orchestrator import gitutil


class GitUtilTests(unittest.TestCase):
    def test_execute_outcome_reports_ahead_head_and_dirty_split_by_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = scratch_repo(Path(directory) / "repo")
            subprocess.run(["git", "branch", "goal/P"], cwd=repo, check=True)
            (repo / "in.txt").write_text("committed\n")
            subprocess.run(["git", "add", "in.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "task change"], cwd=repo, check=True)
            (repo / "in.txt").write_text("dirty\n")
            (repo / "out.txt").write_text("untracked\n")
            (repo / "deleted.txt").write_text("delete me\n")
            subprocess.run(["git", "add", "deleted.txt"], cwd=repo, check=True)
            (repo / "deleted.txt").unlink()
            (repo / ".orchestrator").mkdir(exist_ok=True)
            (repo / ".orchestrator" / "state").write_text("ignored\n")
            (repo / ".venv").mkdir(exist_ok=True)
            (repo / ".venv" / "state").write_text("ignored\n")

            outcome = gitutil.execute_outcome(repo, "P", ["in.txt"])
            self.assertEqual(outcome["ahead"], 1)
            self.assertEqual(outcome["head"], subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                capture_output=True, text=True).stdout.strip())
            self.assertEqual(outcome["dirty_in_scope"], ["in.txt"])
            self.assertEqual(outcome["dirty_out_of_scope"], ["deleted.txt", "out.txt"])
            with mock.patch.object(gitutil, "head_sha", side_effect=[outcome["head"], "changed"]) as heads:
                self.assertIsNone(gitutil.execute_outcome(repo, "P", ["in.txt"]))
            self.assertEqual(heads.call_args_list, [mock.call(repo, timeout=60)] * 2)

    def test_execute_outcome_since_counts_only_commits_after_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = scratch_repo(Path(directory) / "repo")
            subprocess.run(["git", "branch", "goal/P"], cwd=repo, check=True)
            worktree = Path(directory) / "task"
            subprocess.run(["git", "worktree", "add", "-qb", "task/T", str(worktree)],
                           cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "first"], cwd=worktree, check=True)
            first = gitutil.head_sha(worktree)
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "second"], cwd=worktree, check=True)
            with mock.patch.object(gitutil, "_git_in", wraps=gitutil._git_in) as git:
                self.assertEqual(gitutil.execute_outcome(worktree, "P", ["."])["ahead"], 2)
                self.assertEqual(gitutil.execute_outcome(worktree, "P", ["."], since=first)["ahead"], 1)
            self.assertTrue(all(call.kwargs == {"timeout": 60} for call in git.call_args_list))
            with mock.patch.object(gitutil, "_git_in", wraps=gitutil._git_in) as git:
                self.assertIsNotNone(gitutil._resolve_base(worktree, "P"))
            self.assertTrue(all(not call.kwargs for call in git.call_args_list))

    def test_execute_outcome_none_for_non_git_dir_and_failed_git_call(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            self.assertIsNone(gitutil.execute_outcome(outside, "P", ["."]))
            self.assertIsNone(gitutil.execute_outcome(outside / "missing", "P", ["."]))
            repo = scratch_repo(outside / "repo")
            failed = subprocess.CompletedProcess(["git"], 1, "", "failed")
            with mock.patch.object(gitutil, "_git_in", return_value=failed):
                self.assertIsNone(gitutil.execute_outcome(repo, "P", ["."]))
            real_git = gitutil._git_in
            for command in ("rev-parse", "merge-base", "rev-list", "status"):
                for failure in (failed, OSError("failed"), subprocess.TimeoutExpired("git", 60)):
                    with self.subTest(command=command, failure=failure):
                        def git(worktree, *args, **kwargs):
                            if args[0] == command:
                                if isinstance(failure, Exception):
                                    raise failure
                                return failure
                            return real_git(worktree, *args, **kwargs)
                        with mock.patch.object(gitutil, "_git_in", side_effect=git):
                            self.assertIsNone(gitutil.execute_outcome(repo, "P", ["."]))

    def test_parse_porcelain_z_matches_daemon_dirty_scope_paths(self):
        from orchestrator import daemon

        sample = "R  new/name.txt\0old/name.txt\0D  removed.txt\0?? untracked.txt\0?? .orchestrator/state\0?? .venv/bin/x\0"
        result = type("Result", (), {"returncode": 0, "stdout": sample, "stderr": ""})()
        scope = ["."]
        with mock.patch.object(daemon, "_git_in", return_value=result):
            parsed = gitutil.parse_porcelain_z(sample)
            daemon_paths = daemon._dirty_scope_paths(Path("."), scope)
        self.assertEqual(set(parsed), {"new/name.txt", "old/name.txt", "removed.txt", "untracked.txt"})
        self.assertEqual(set(parsed), set(daemon_paths))
        self.assertFalse(hasattr(gitutil, "daemon"))

    def test_moved_paths_lists_files_changed_on_target_since_merge_base(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = scratch_repo(Path(directory))
            subprocess.run(["git", "branch", "task/T"], cwd=repo, check=True)
            (repo / "b.txt").write_text("b")
            (repo / "a.txt").write_text("a")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "move goal"], cwd=repo, check=True)
            self.assertEqual(gitutil.moved_paths("task/T", "main", repo), ["a.txt", "b.txt"])

    def test_moved_paths_raises_git_error_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(gitutil.GitError):
                gitutil.moved_paths("missing", "also-missing", directory)
