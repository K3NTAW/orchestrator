import unittest

from orchestrator.interference import classify, graph_signal, select_wave


def task(task_id, *scope, depends_on=()):
    return {"id": task_id, "scope": list(scope), "depends_on": list(depends_on)}


class InterferenceTests(unittest.TestCase):
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
