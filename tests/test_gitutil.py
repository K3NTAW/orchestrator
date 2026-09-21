"""Tests for strict stale-work git evidence."""
import subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import scratch_repo
from orchestrator import gitutil


class GitUtilTests(unittest.TestCase):
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
