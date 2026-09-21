import unittest

from orchestrator.concurrency import compatible_groups, group_contribution, log_row, reason_kind, select_limit


def tasks(*ids):
    return {task_id: {"id": task_id, "complexity": 1} for task_id in ids}


def row(a, b, level):
    return {"a": a, "b": b, "level": level, "reasons": [f"same_file:{a}"]}


class ConcurrencyTests(unittest.TestCase):
    def test_coupled_cluster_caps_only_its_own_contribution(self):
        ready = list("abcdefg")
        rows = [row("a", "b", "hard"), row("b", "c", "soft"), row("c", "d", "soft")]
        result = select_limit(8, ready, tasks(*ready), rows, {})
        self.assertEqual(result["limit"], 4)
        self.assertEqual(result["groups"], [
            {"members": list("abcd"), "contribution": 1},
            {"members": ["e"], "contribution": 1},
            {"members": ["f"], "contribution": 1},
            {"members": ["g"], "contribution": 1},
        ])
        self.assertIn("coupled_group:a,b,c,d", result["reasons"])

    def test_soft_group_contributes_two_and_mixed_cluster_one(self):
        rows = [row("a", "b", "soft"), row("c", "d", "hard"), row("d", "e", "soft")]
        groups = compatible_groups(list("abcde"), rows)
        self.assertEqual(groups, [list("ab"), list("cde")])
        self.assertEqual([group_contribution(group, rows) for group in groups], [2, 1])
        self.assertEqual(reason_kind("same_file:a.py"), "same_file")

    def test_independent_modules_select_higher_concurrency(self):
        ready = list("abcde")
        result = select_limit(4, ready, tasks(*ready), [], {})
        self.assertEqual(result["limit"], 4)
        self.assertEqual(result["reasons"], ["hard_max=4"])

    def test_hard_max_is_never_exceeded_and_fixed_mode(self):
        ready = [f"t{i:02}" for i in range(20)]
        self.assertEqual(select_limit(3, ready, tasks(*ready), [], {})["limit"], 3)
        fixed = select_limit(
            3, ready, tasks(*ready), [], {}, snapshot={"executors": {}, "claude_workers_free": 0, "fallback": False},
            cfg={"scheduler": {"concurrency_mode": "fixed"}},
        )
        self.assertEqual(fixed["limit"], 3)
        self.assertEqual(fixed["reasons"], ["fixed"])

    def test_no_ready_work_yields_zero_benefit_zero(self):
        result = select_limit(4, [], {}, [], {})
        self.assertEqual(result["limit"], 0)
        self.assertEqual(result["reasons"], ["no_ready"])
        self.assertEqual(result["expected_benefit_s"], 0.0)

    def test_quota_and_merge_pressure_bound_the_limit(self):
        ready = list("abcd")
        task_map = tasks(*ready)
        snapshot = {
            "executors": {"one": {"free": 1}, "two": {"free": 1}},
            "claude_workers_free": 9,
            "fallback": False,
        }
        capacity = select_limit(4, ready, task_map, [], {}, snapshot=snapshot)
        self.assertEqual(capacity["limit"], 2)
        self.assertIn("capacity=2", capacity["reasons"])

        saturated = select_limit(4, ["a", "b", "c"], task_map,
                                 [row("a", "b", "soft")], {}, pressure={"pressure": "saturated"})
        self.assertEqual(saturated["limit"], 1)
        self.assertIn("merge_pressure=saturated", saturated["reasons"])

        logged = log_row(capacity, ["a", "b"])
        for key in ("hard_max", "limit", "reasons", "tasks_considered", "expected_benefit_s"):
            self.assertEqual(logged[key], capacity[key])
        self.assertEqual(logged["selected"], ["a", "b"])
        self.assertIsNone(logged["observed_outcome"])


if __name__ == "__main__":
    unittest.main()
