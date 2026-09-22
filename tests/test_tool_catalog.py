import _harness
import unittest

from orchestrator import spawn, tool_catalog


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

    def test_docs_only_task_drops_test_and_shell(self):
        result = tool_catalog.minimal_set(
            {"task_class": "docs", "scope": ["docs/readme.md"], "acceptance": []}, "execute")
        optional = [tool for tool in result["drop"]
                    if tool_catalog.CATALOG[tool]["category"] in {"test", "shell"}]
        self.assertTrue(optional)
        self.assertNotIn("Bash(pytest *)", result["keep"])


if __name__ == "__main__":
    unittest.main()
