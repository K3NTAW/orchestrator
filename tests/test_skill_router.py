import _harness  # noqa: F401
import unittest
from unittest import mock

from orchestrator import skill_router, skills_registry


def record(roles, triggers=(), l0=1, l2=2):
    return {"state": "active", "provenance": "builtin", "roles": roles,
            "task_classes": ["*"], "triggers": list(triggers),
            "est_tokens_l0": l0, "est_tokens_l2": l2}


class SkillRouterTests(unittest.TestCase):
    def route(self, task, role, records, cap=3):
        with mock.patch("orchestrator.skills_registry.load", return_value={"skills": records}):
            return skill_router.select(task, role, {"skills": {"max_selected": cap}})

    def test_executor_gets_implement_spec_mandatory_and_nothing_irrelevant(self):
        records = {"executor/implement-spec": record(["execute"]),
                   "scout/repo-map": record(["scout"], ["architecture"])}
        self.assertEqual(self.route({"title": "change code"}, "execute", records)["selected"],
                         ["executor/implement-spec"])

    def test_scout_who_calls_task_selects_trace_callers_not_repo_map(self):
        records = {"scout/trace-callers": record(["scout"], ["who calls", "impact"]),
                   "scout/repo-map": record(["scout"], ["architecture"])}
        result = self.route({"title": "who calls this symbol"}, "scout", records)
        self.assertEqual(result["selected"], ["scout/trace-callers"])

    def test_cap_keeps_mandatory_first(self):
        records = {"executor/implement-spec": record(["execute"]),
                   "execute/a": record(["execute"], ["change code"]),
                   "execute/b": record(["execute"], ["change code"])}
        result = self.route({"title": "change code"}, "execute", records, cap=1)
        self.assertEqual(result["selected"], ["executor/implement-spec", "execute/a"])

    def test_single_weak_match_goes_to_ambiguous_not_selected(self):
        records = {"scout/trace-callers": record(["scout"], ["impact"])}
        result = self.route({"title": "impact"}, "scout", records)
        self.assertEqual(result["ambiguous"], ["scout/trace-callers"])
        self.assertEqual(result["selected"], [])

    def test_never_empty_for_roles_with_mandatory(self):
        for role in ("execute", "codex_execute", "review", "spec_review", "challenge", "planner"):
            with self.subTest(role=role):
                self.assertTrue(self.route({"title": "x"}, role, {})["selected"])

    def test_mandatory_ids_exist_in_registry(self):
        # Build real records without changing the suite's shared registry state.
        with mock.patch.object(skills_registry, "_write_json"), \
                mock.patch.object(skills_registry, "_state_document", return_value={}):
            records = skills_registry.sync(skills_dir=_harness.REPO / "skills")["skills"]
        for role, mandatory in skill_router.MANDATORY.items():
            with self.subTest(role=role):
                registry_role = "execute" if role == "codex_execute" else role
                for skill_id in mandatory:
                    self.assertIn(skill_id, records)
                    self.assertIn(registry_role, records[skill_id]["roles"])
                result = self.route({"title": "x"}, role, records, cap=0)
                self.assertEqual(result["selected"], list(mandatory))
                for level in (0, 2):
                    expected = sum(records[item][f"est_tokens_l{level}"] for item in mandatory)
                    self.assertGreater(expected, 0)
                    self.assertEqual(result[f"tokens_selected_l{level}"], expected)
                # Reading the real mandatory skill must not produce a recovery.
                self.assertEqual(set(mandatory) - set(result["selected"]), set())


if __name__ == "__main__":
    unittest.main()
