import _harness

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import skills_registry


class SkillsRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".orchestrator"
        self.skills = Path(self.temp.name) / "skills"

    def tearDown(self):
        self.temp.cleanup()

    def make_skill(self, role="scout", name="finder", description="Find things. Use when locating callers."):
        directory = self.skills / role / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n# Finder\nFinish with evidence.\n"
        )
        return directory

    def test_sync_builds_twelve_builtin_records_with_content_hashes(self):
        document = skills_registry.sync(self.root, _harness.REPO / "skills")
        self.assertEqual(12, len(document["skills"]))
        for record in document["skills"].values():
            self.assertRegex(record["version"], r"^[0-9a-f]{12}$")
            self.assertEqual(record["version"], record["content_hash"])
            self.assertEqual("builtin", record["provenance"])
            self.assertEqual("active", record["state"])

    def test_triggers_parsed_from_use_when_clause(self):
        self.make_skill(description="Find callers. Use when tracing risky symbols and configs.")
        record = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        self.assertEqual(["tracing", "risky", "symbols", "configs"], record["triggers"])

    def test_transition_validates_and_records_history(self):
        self.make_skill()
        skills_registry.sync(self.root, self.skills)
        entry = skills_registry.transition("scout/finder", "shadow", "observe", self.root)
        self.assertEqual("shadow", entry["state"])
        self.assertEqual("observe", entry["history"][-1]["reason"])
        with self.assertRaises(ValueError):
            skills_registry.transition("scout/finder", "quarantined", "bad", self.root)

    def test_rollback_restores_previous_state(self):
        self.make_skill()
        skills_registry.sync(self.root, self.skills)
        skills_registry.transition("scout/finder", "shadow", "observe", self.root)
        entry = skills_registry.rollback("scout/finder", self.root)
        self.assertEqual("active", entry["state"])
        self.assertEqual("active", skills_registry.load(self.root)["skills"]["scout/finder"]["state"])

    def test_state_override_beats_provenance(self):
        self.make_skill()
        skills_registry.sync(self.root, self.skills)
        state_path = self.root / "skills" / "state.json"
        values = json.loads(state_path.read_text())
        values["scout/finder"]["state"] = "disabled"
        state_path.write_text(json.dumps(values))
        record = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        self.assertEqual("disabled", record["state"])

    def test_sync_marks_missing_skill_disabled_and_version_bump_on_content_change(self):
        directory = self.make_skill()
        first = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]["version"]
        (directory / "SKILL.md").write_text((directory / "SKILL.md").read_text() + "Changed.\n")
        second = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]["version"]
        self.assertNotEqual(first, second)
        self.assertIn("content changed", skills_registry.state("scout/finder", self.root)["reason"])
        (directory / "SKILL.md").unlink()
        record = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        self.assertEqual("disabled", record["state"])
        self.assertEqual("source removed", skills_registry.state("scout/finder", self.root)["reason"])

    def test_cli_list_and_invalid_transition_exit_code(self):
        env = {**os.environ, "ORCH_ROOT": str(Path(self.temp.name))}
        sync = subprocess.run(["uv", "run", "orchestrator", "skills", "sync"], cwd=_harness.REPO,
                              env=env, capture_output=True, text=True)
        self.assertEqual(0, sync.returncode, sync.stderr)
        listed = subprocess.run(["uv", "run", "orchestrator", "skills", "list"], cwd=_harness.REPO,
                                env=env, capture_output=True, text=True)
        self.assertIn("executor/implement-spec", listed.stdout)
        invalid = subprocess.run(
            ["uv", "run", "orchestrator", "skills", "transition", "executor/implement-spec", "testing",
             "--reason", "invalid from active"], cwd=_harness.REPO, env=env, capture_output=True, text=True)
        self.assertNotEqual(0, invalid.returncode)


if __name__ == "__main__":
    unittest.main()
