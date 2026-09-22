import _harness
import unittest

from unittest import mock

from orchestrator import attribution, spawn, tool_catalog


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_covers_every_allowlisted_id(self):
        ids = {item.strip() for value in spawn.TOOLS.values() for item in value.split(",")}
        self.assertEqual(ids - set(tool_catalog.CATALOG), set())

    def test_minimal_set_never_drops_mandatory(self):
        task = {"task_class": "refactor", "scope": ["x.py"], "acceptance": []}
        for role in spawn.TOOLS:
            result = tool_catalog.minimal_set(task, role)
            self.assertTrue(set(result["mandatory"]) <= set(result["keep"]), role)
            self.assertFalse(set(result["mandatory"]) & set(result["drop"]), role)

    def test_mandatory_never_dropped_in_active(self):
        task = {"scope": ["docs/readme.md"], "acceptance": []}
        for role in spawn.TOOLS:
            result = tool_catalog.minimal_set(task, role)
            self.assertTrue(set(result["mandatory"]) <= set(result["keep"]), role)
        execute = tool_catalog.minimal_set(task, "execute")
        self.assertTrue({"Edit", "Write", "Bash(.claude/hooks/tests-green.sh*)"} <= set(execute["keep"]))

    def test_docs_only_task_drops_test_and_shell(self):
        task = {"scope": ["x.py"], "acceptance": []}
        code = tool_catalog.minimal_set(task, "execute")
        docs = tool_catalog.minimal_set({**task, "scope": ["docs/readme.md"]}, "execute")
        optional = {tool for tool in tool_catalog.disclosed("execute")
                    if tool_catalog.CATALOG[tool]["category"] in {"test", "shell"}
                    and tool not in docs["mandatory"]}
        self.assertTrue(optional <= set(code["keep"]))
        self.assertTrue(optional <= set(docs["drop"]))
        self.assertFalse(optional & set(docs["keep"]))
        self.assertEqual(set(code["keep"]) - set(docs["keep"]), optional)
        self.assertEqual(tool_catalog.tokens(code["keep"]) - tool_catalog.tokens(docs["keep"]),
                         tool_catalog.tokens(optional))
        self.assertTrue(set(docs["mandatory"]) <= set(docs["keep"]))

    def test_execute_feature_task_keeps_shell_tools(self):
        task = {"constraints": {"task_class": "feature"}, "scope": ["x.py"],
                "acceptance": ["implement feature"]}
        result = tool_catalog.minimal_set(task, "execute")
        shell = {"Bash(npm *)", "Bash(npx *)", "Bash(uv *)", "Bash(python3 *)",
                 "Bash(bash skills/*)"}
        self.assertTrue(shell <= set(result["keep"]))
        self.assertFalse(shell & set(result["drop"]))

    def test_task_class_uses_canonical_inference_and_explicit_override(self):
        tasks = [({"scope": ["secret.py"]}, "security"),
                 ({"complexity": 8}, "architectural"),
                 ({"title": "fix regression", "complexity": 4}, "debugging"),
                 ({"complexity": 2}, "mechanical"),
                 ({"complexity": 5}, "unfamiliar"),
                 ({"constraints": {"task_class": "feature"}}, "feature")]
        with mock.patch.object(attribution.bus, "pool_config",
                               return_value={"review": {"security_paths": ["secret.py"]}}):
            for fields, expected in tasks:
                task = {"scope": ["x.py"], "acceptance": [], **fields}
                with self.subTest(expected=expected):
                    self.assertEqual(tool_catalog._task_class(task), expected)
                    result = tool_catalog.minimal_set(task, "execute")
                    self.assertIn(f"execute/{expected}:", result["reason"])
                    self.assertIn("Bash(pytest *)", result["keep"])
                    self.assertIn("Bash(python3 *)", result["keep"])


if __name__ == "__main__":
    unittest.main()
