import _harness
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import gitutil, stale


class StaleTest(unittest.TestCase):
    def task(self):
        return {"id": "T", "base": "base", "parent": "G", "worktree": "/wt",
                "scope": ["orchestrator/foo.py"], "changed_files": ["orchestrator/foo.py"]}

    def run_evidence(self, moved, **kwargs):
        with mock.patch.object(stale.gitutil, "moved_paths", return_value=moved), \
             mock.patch.object(stale.gitutil, "changed_paths", return_value=["orchestrator/foo.py"]):
            return stale.evidence(self.task(), git=kwargs.pop("git", self.git), **kwargs)

    @staticmethod
    def git(*args, cwd):
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_interface_change_in_moved_file_consumed_by_task_is_high_risk(self):
        links = {"files": {"a": "orchestrator/api.py", "b": "orchestrator/foo.py"},
                 "links": [{"source": "a", "target": "b", "relation": "calls"}]}
        with mock.patch.object(stale.interference, "graph_fresh_for", return_value=True):
            result = self.run_evidence(["orchestrator/api.py"], graph_links=links, graph={})
        self.assertTrue(result["signals"]["interface_changed"])
        self.assertEqual("high", result["risk"])
        self.assertIn("interface_changed", result["risk_reasons"])
        self.assertEqual("used", result["graph"])

    def test_relevant_test_change_is_medium_risk(self):
        result = self.run_evidence(["tests/test_foo.py"])
        self.assertEqual(["tests/test_foo.py"], result["signals"]["tests_changed"])
        self.assertEqual("medium", result["risk"])
        self.assertEqual(["tests_changed"], result["risk_reasons"])

    def test_dependency_merged_after_branch_point_is_high_risk(self):
        task = self.task()
        task["depends_on"] = ["D"]
        def git(*args, cwd):
            return subprocess.CompletedProcess(args, 1 if args[0] == "merge-base" else 0, "", "")
        with mock.patch.object(stale.gitutil, "moved_paths", return_value=[]), \
             mock.patch.object(stale.gitutil, "changed_paths", return_value=[]):
            result = stale.evidence(task, tasks=[{"id": "D", "sha": "dep"}], git=git)
        self.assertEqual(["D"], result["signals"]["dependency_changed"])
        self.assertEqual("high", result["risk"])

    def test_overlapping_earlier_merge_uses_scope_literals_and_optional_changed_files(self):
        records = [
            {"id": "A", "kind": "execute", "merged_into": "goal/G", "scope": ["orchestrator/foo.py"], "sha": "a"},
            {"id": "B", "kind": "execute", "merged_into": "goal/G", "scope": [], "changed_files": ["orchestrator/foo.py"], "sha": "b"},
        ]
        def git(*args, cwd):
            return subprocess.CompletedProcess(args, 1, "", "")
        result = self.run_evidence([], tasks=records, git=git)
        self.assertEqual(["A", "B"], result["signals"]["overlapping_merge"])
        self.assertEqual("medium", result["risk"])

    def test_git_adapter_and_freshness_use_task_worktree(self):
        calls = []
        def git(*args, cwd):
            calls.append((args, cwd))
            return subprocess.CompletedProcess(args, 0, "", "")
        def moved(_base, _goal, *, cwd, git):
            git(cwd, "diff")
            return ["orchestrator/api.py"]
        def changed(_task, *, git):
            git("/wt", "status")
            return ["orchestrator/foo.py"]
        links = {"files": {"a": "orchestrator/api.py", "b": "orchestrator/foo.py"}, "links": []}
        def fresh(_files, _graph, *, git):
            git("ignored", "diff", "HEAD")
            return True
        with mock.patch.object(stale.gitutil, "moved_paths", side_effect=moved), \
             mock.patch.object(stale.gitutil, "changed_paths", side_effect=changed), \
             mock.patch.object(stale.interference, "graph_fresh_for", side_effect=fresh):
            stale.evidence(self.task(), graph_links=links, graph={}, git=git)
        self.assertTrue(calls)
        self.assertTrue(all(cwd == "/wt" for _args, cwd in calls))

    def test_absent_stale_skipped_graph_and_git_failure(self):
        absent = self.run_evidence(["tests/test_foo.py"])
        self.assertEqual("absent", absent["graph"])
        with mock.patch.object(stale.interference, "graph_fresh_for", return_value=False):
            old = self.run_evidence(["orchestrator/api.py"], graph_links={"files": {}, "links": []}, graph={})
        self.assertEqual("stale", old["graph"])
        self.assertFalse(old["signals"]["interface_changed"])
        with mock.patch.object(stale.interference, "graph_fresh_for", return_value=True):
            skipped = self.run_evidence(["orchestrator/api.py"], graph_links={"files": {"x": "other.py"}, "links": []}, graph={})
        self.assertEqual("skipped", skipped["graph"])
        def broken(*args, cwd):
            raise gitutil.GitError("no git")
        with mock.patch.object(stale.gitutil, "moved_paths", side_effect=lambda *a, **kw: kw["git"]("/wt", "diff")):
            failed = stale.evidence(self.task(), git=broken)
        self.assertEqual("unknown", failed["risk"])
        self.assertEqual(["git_failed"], failed["risk_reasons"])
        logged = stale.row(self.task(), absent, "recorded")
        for key in ("base", "goal_head", "changed_relevant_paths", "risk", "risk_reasons", "action"):
            self.assertIn(key, logged)

    def test_severity_from_write_read_scope_and_acceptance_tests(self):
        task = self.task()
        task.update(read_scope=["lib/"], acceptance=["tests/test_named.py::test_case"])
        for moved, expected in [([], "none"), (["other.py"], "low"),
                                (["lib/api.py"], "medium"),
                                (["tests/test_named.py"], "high"),
                                (["orchestrator/foo.py", "lib/api.py"], "high")]:
            with self.subTest(moved=moved), \
                    mock.patch.object(stale.gitutil, "moved_paths", return_value=moved), \
                    mock.patch.object(stale.gitutil, "changed_paths", return_value=[]):
                result = stale.evidence(task, git=self.git)
                self.assertEqual(result["severity"], expected)
        with mock.patch.object(stale.gitutil, "moved_paths", side_effect=gitutil.GitError("failed")):
            self.assertEqual(stale.evidence(task)["severity"], "unknown")
        links = {"files": {"a": "api.py", "b": "orchestrator/foo.py"},
                 "links": [{"source": "b", "target": "a", "relation": "imports"}]}
        with mock.patch.object(stale.interference, "graph_fresh_for", return_value=True):
            self.assertEqual(self.run_evidence(["api.py"], graph_links=links, graph={})["severity"], "medium")

    def test_load_links(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "graphify-out"
            output.mkdir()
            (output / "graph.json").write_text(json.dumps({
                "nodes": [{"id": "n", "source_file": "a.py"}],
                "links": [{"source": "n", "target": "n", "relation": "calls"}],
            }))
            self.assertEqual("a.py", stale.load_links(directory)["files"]["n"])


if __name__ == "__main__":
    unittest.main()
