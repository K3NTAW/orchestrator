import _harness

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import skills_registry


HEADINGS = [
    "Trigger", "Objective", "Procedure", "Tools", "Evidence requirements",
    "Output contract", "Stop conditions", "Failure/recovery",
]
REQUIRED = {
    "roles", "task_classes", "triggers", "tools", "requires_context",
    "output", "security", "repo",
}


def skill_files(root):
    return sorted((root / "skills").glob("*/*/SKILL.md"))


class SkillsCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".orchestrator"

    def tearDown(self):
        self.temp.cleanup()

    def test_every_skill_has_p6_headings_in_order(self):
        for path in skill_files(_harness.REPO):
            _, body = skills_registry._frontmatter(path.read_text())
            found = re.findall(r"(?m)^## (.+)$", body)
            self.assertEqual(HEADINGS, found, path)

    def test_scripts_and_paths_referenced_before_are_referenced_after(self):
        pattern = re.compile(r"(?:skills|scripts)/[A-Za-z0-9_./<>-]+")
        for path in skill_files(_harness.REPO):
            relative = path.relative_to(_harness.REPO).as_posix()
            before = subprocess.check_output(
                ["git", "show", f"HEAD~:{relative}"], cwd=_harness.REPO, text=True
            )
            self.assertLessEqual(set(pattern.findall(before)), set(pattern.findall(path.read_text())), path)

    def test_frontmatter_declares_required_keys(self):
        for path in skill_files(_harness.REPO):
            meta, _ = skills_registry._frontmatter(path.read_text())
            self.assertTrue(REQUIRED.issubset(meta), path)

    def test_l2_tokens_not_higher_than_before(self):
        parent = Path(self.temp.name) / "parent-skills"
        for path in skill_files(_harness.REPO):
            relative = path.relative_to(_harness.REPO / "skills")
            target = parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(subprocess.check_output(
                ["git", "show", f"HEAD~:skills/{relative.as_posix()}"],
                cwd=_harness.REPO, text=True,
            ))
        before = skills_registry.sync(self.root / "before", parent)["skills"]
        after = skills_registry.sync(self.root / "after", _harness.REPO / "skills")["skills"]
        for skill_id in after:
            self.assertLessEqual(after[skill_id]["est_tokens_l2"], before[skill_id]["est_tokens_l2"], skill_id)

    def test_candidates_select_trace_callers_for_who_calls_task_and_not_for_docs_task(self):
        skills_registry.sync(self.root, _harness.REPO / "skills")
        calls = {row["id"] for row in skills_registry.candidates(
            {"title": "Who calls dispatch?", "spec": "Find impact", "scope": ["orchestrator/daemon.py"]},
            "scout", root=self.root,
        )}
        docs = {row["id"] for row in skills_registry.candidates(
            {"title": "Improve docs", "spec": "Clarify wording", "scope": ["README.md"]},
            "scout", root=self.root,
        )}
        self.assertIn("scout/trace-callers", calls)
        self.assertNotIn("scout/trace-callers", docs)

    def test_render_levels_nest(self):
        skills_registry.sync(self.root, _harness.REPO / "skills")
        for skill_id in skills_registry.load(self.root)["skills"]:
            zero = skills_registry.render(skill_id, 0, self.root)
            one = skills_registry.render(skill_id, 1, self.root)
            two = skills_registry.render(skill_id, 2, self.root)
            self.assertEqual(1, len(zero.splitlines()), skill_id)
            for line in one.splitlines():
                self.assertIn(line, two, skill_id)


if __name__ == "__main__":
    unittest.main()
