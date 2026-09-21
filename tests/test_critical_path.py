import unittest

from orchestrator import critical_path


def task(task_id, complexity=1, depends_on=None, **fields):
    return {"id": task_id, "complexity": complexity, "depends_on": depends_on or [], **fields}


class CriticalPath(unittest.TestCase):
    def test_rank_prefers_longer_downstream_chain(self):
        tasks = {t["id"]: t for t in [
            task("A"), task("B"), task("A1", depends_on=["A"]),
            task("A2", depends_on=["A1"]), task("B1", depends_on=["B"]),
        ]}
        self.assertEqual(critical_path.rank(["B", "A"], tasks), ["A", "B"])

    def test_rank_tie_breaks_on_blocked_descendants_then_id(self):
        tasks = {t["id"]: t for t in [
            task("A"), task("B"), task("C"), task("B1", depends_on=["B"]),
            task("B2", depends_on=["B"]), task("C1", depends_on=["C"]),
        ]}
        durations = {task_id: 1 for task_id in tasks}
        self.assertEqual(critical_path.rank(["C", "B", "A"], tasks, durations), ["B", "C", "A"])

    def test_explain_cold_start_durations_by_band(self):
        tasks = {"low": task("low", 3), "mid": task("mid", 5), "high": task("high", 9)}
        self.assertEqual(critical_path.explain("low", tasks), {
            "downstream_depth": 0, "blocked_descendants": 0, "est_duration_s": 600,
            "critical_path_s": 600, "priority": 600})
        self.assertEqual(critical_path.explain("mid", tasks)["est_duration_s"], 1200)
        self.assertEqual(critical_path.explain("high", tasks)["est_duration_s"], 2400)

    def test_rank_ignores_merged_and_failed_descendants(self):
        tasks = {t["id"]: t for t in [
            task("A"), task("B"), task("A1", 10, ["A"], merged_into="goal/x"),
            task("A2", 10, ["A"], status="failed"), task("B1", 1, ["B"]),
        ]}
        self.assertEqual(critical_path.rank(["A", "B"], tasks), ["B", "A"])

    def test_rank_handles_cycles(self):
        tasks = {"A": task("A", depends_on=["B"]), "B": task("B", depends_on=["A"])}
        self.assertEqual(set(critical_path.rank(["B", "A"], tasks)), {"A", "B"})
        self.assertEqual(critical_path.explain("A", tasks)["blocked_descendants"], 1)

    def test_durations_override_cold_start(self):
        tasks = {"A": task("A", 1), "B": task("B", 5), "C": task("C", 8)}
        durations = {"A": 7, "4-6": 8, "7-10": 9}
        self.assertEqual([critical_path.explain(tid, tasks, durations)["est_duration_s"]
                          for tid in tasks], [7, 8, 9])

    def test_critical_path_sums_along_the_whole_chain(self):
        tasks = {"A": task("A"), "B": task("B", depends_on=["A"]),
                 "C": task("C", depends_on=["B"])}
        explanation = critical_path.explain("A", tasks, {"A": 1, "B": 10, "C": 100})
        self.assertEqual(explanation["critical_path_s"], 111)
        self.assertEqual(explanation["downstream_depth"], 2)
        self.assertEqual(explanation["blocked_descendants"], 2)
