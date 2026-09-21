import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.interference import classify, graph_fresh_for, graph_signal, load_graph, select_wave


def task(task_id, *scope, depends_on=()):
    return {"id": task_id, "scope": list(scope), "depends_on": list(depends_on)}


class InterferenceTests(unittest.TestCase):
    def graph(self, root, *, links=((1, 2),), built_at_commit=None):
        return {
            "nodes_by_file": {"src/a.py": [1], "lib/b.py": [2]},
            "links": list(links),
            "built_at_commit": built_at_commit or subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.strip(),
            "path": str(Path(root) / "graphify-out" / "graph.json"),
        }

    def test_load_graph_reads_node_link_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "graphify-out" / "graph.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "built_at_commit": "abc", "nodes": [
                    {"id": 1, "source_file": "src/a.py"},
                    {"id": 2, "source_file": "lib/b.py"},
                ], "links": [{"source": 1, "target": 2}],
            }))
            self.assertEqual(load_graph(root), {
                "nodes_by_file": {"src/a.py": [1], "lib/b.py": [2]},
                "links": [(1, 2)], "built_at_commit": "abc", "path": str(path),
            })

    def test_graph_signal_soft_on_cross_file_link(self):
        graph = self.graph(Path.cwd())
        result = classify(task("a", "src/*.py"), task("b", "lib/b.py"), graph=graph)
        self.assertEqual(result, {
            "level": "soft", "reasons": ["graph_link:src/a.py->lib/b.py"], "score": 0.25,
        })

    def test_graph_absent_falls_back_deterministic(self):
        baseline = classify(task("a", "src/a.py"), task("b", "lib/b.py"))
        self.assertEqual(classify(task("a", "src/a.py"), task("b", "lib/b.py"), graph=None), baseline)
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(load_graph(root))
            path = Path(root) / "graphify-out" / "graph.json"
            path.parent.mkdir()
            path.write_text("not json")
            self.assertIsNone(load_graph(root))

    def test_graph_stale_for_pair_skips_signal(self):
        graph = self.graph(Path.cwd())
        with patch("orchestrator.interference.graph_fresh_for", return_value=False):
            result = classify(task("a", "src/a.py"), task("b", "lib/b.py"), graph=graph)
        self.assertEqual(result, {"level": "none", "reasons": ["graph_stale"], "score": 0.0})

    def test_graph_fresh_for_empty_paths_is_fresh_without_git(self):
        graph = {"built_at_commit": "abc", "path": str(Path.cwd() / "graphify-out" / "graph.json")}

        def raising_git(*args):
            raise AssertionError("git should not be called for empty paths")

        self.assertTrue(graph_fresh_for([], graph, git=raising_git))

    def test_graph_hard_links_threshold(self):
        graph = self.graph(Path.cwd(), links=((1, 2), (1, 2)))
        result = classify(task("a", "src/a.py"), task("b", "lib/b.py"), graph=graph,
                          rules={"graph_hard_links": 2})
        self.assertEqual(result["level"], "hard")
        self.assertEqual(result["reasons"], ["graph_link:src/a.py->lib/b.py"])

    def test_same_file_is_hard(self):
        result = classify(task("a", "orchestrator/daemon.py"), task("b", "orchestrator/daemon.py"))
        self.assertEqual(result, {
            "level": "hard",
            "reasons": ["same_dir:orchestrator", "same_file:orchestrator/daemon.py"],
            "score": 1.0,
        })

    def test_glob_covering_literal_is_hard(self):
        # Swap scopes across fixed IDs to cover both canonical directions.
        for left, right in [
            ("orchestrator/*.py", "orchestrator/daemon.py"),
            ("orchestrator/daemon.py", "orchestrator/*.py"),
        ]:
            with self.subTest(left=left, right=right):
                a, b = task("a", left), task("b", right)
                result = classify(a, b)
                self.assertEqual(result["level"], "hard")
                self.assertIn("glob_covers:orchestrator/*.py->orchestrator/daemon.py", result["reasons"])
                self.assertEqual(result, classify(b, a))

    def test_dependency_transitive_is_hard(self):
        tasks = {item["id"]: item for item in [
            task("a", "a.py", depends_on=("b",)),
            task("b", "b.py", depends_on=("c",)), task("c", "c.py"),
        ]}
        self.assertEqual(classify(tasks["a"], tasks["c"], tasks)["level"], "hard")
        self.assertEqual(classify(tasks["c"], tasks["a"], tasks), classify(tasks["a"], tasks["c"], tasks))

    def test_directory_prefix_is_soft(self):
        for left, right in [
            ("tests/", "tests/test_bus.py"),
            ("tests/test_bus.py", "tests/"),
        ]:
            with self.subTest(left=left, right=right):
                a, b = task("a", left), task("b", right)
                result = classify(a, b)
                self.assertEqual(result["level"], "soft")
                self.assertEqual(result["reasons"], ["dir_contains:tests/->tests/test_bus.py"])
                self.assertEqual(result, classify(b, a))

    def test_test_adjacency_is_soft(self):
        result = classify(task("a", "tests/test_bus.py"), task("b", "orchestrator/bus.py"))
        self.assertEqual(result["level"], "soft")
        self.assertIn("test_adjacent:bus", result["reasons"])

    def test_disjoint_scopes_are_none(self):
        self.assertEqual(classify(task("a", "orchestrator/bus.py"), task("b", "docs/readme.md"))["level"], "none")

    def test_select_wave_defers_hard_and_orders_soft_last(self):
        tasks = {item["id"]: item for item in [
            task("running", "orchestrator/bus.py"),
            task("hard", "orchestrator/bus.py"),
            task("soft", "orchestrator/daemon.py"),
            task("clean", "docs/readme.md"),
        ]}
        result = select_wave(["hard", "soft", "clean"], ["running"], tasks, 2)
        self.assertEqual(result["wave"], ["clean", "soft"])
        self.assertEqual(result["deferred"], [{"task": "hard", "reason": "hard:running"}])

    def test_select_wave_respects_capacity_and_is_deterministic(self):
        tasks = {item["id"]: item for item in [task("a", "a/x.py"), task("b", "b/y.py"), task("c", "c/z.py")]}
        first = select_wave(["a", "b", "c"], [], tasks, 2, order=["c", "a", "b"])
        self.assertEqual(first, select_wave(["a", "b", "c"], [], tasks, 2, order=["c", "a", "b"]))
        self.assertEqual(first, {"wave": ["c", "a"], "deferred": [{"task": "b", "reason": "capacity"}]})
        self.assertEqual(graph_signal(tasks["a"], tasks["b"], {}), [])


if __name__ == "__main__":
    unittest.main()
