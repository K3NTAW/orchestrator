import _harness

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import skill_discovery, skill_learning, skills_registry


class SkillLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".orchestrator"
        (self.root / "tasks").mkdir(parents=True)
        (self.root / "runs/jev").mkdir(parents=True)

    def seed(self, count=5, failures=1):
        gate = []
        for index in range(count):
            task = f"T-{index}"
            accepted = index < count - failures
            (self.root / f"tasks/{task}.json").write_text(json.dumps({
                "id": task, "role": "execute", "title": "Repair parser safely",
                "spec": "Inspect the parser implementation and run the focused verification before finishing.",
                "merged_into": "main" if accepted else None, "task_class": "feature"}))
            gate.extend([{"task": task, "role": "execute", "tool": "Read", "tool_target": "src/parser.py", "ts": index},
                         {"task": task, "role": "execute", "tool": "Bash", "tool_target": "uv run unittest", "ts": index + .1}])
        (self.root / "runs/jev/gate.jsonl").write_text("".join(json.dumps(row) + "\n" for row in gate))

    def test_repeated_successful_sequence_becomes_pattern_with_support_and_counterexamples(self):
        self.seed()
        found = [p for p in skill_learning.patterns(self.root) if p["source"] == "tool_sequence"]
        self.assertEqual(1, len(found))
        self.assertEqual(5, found[0]["support"])
        self.assertEqual(4, found[0]["successes"])
        self.assertEqual(["T-4"], found[0]["failures"])
        self.assertEqual(.8, found[0]["confidence"])
        self.assertEqual(.5, found[0]["evidence_strength"])

    def test_one_off_behaviour_never_qualifies(self):
        self.seed(1, 0)
        self.assertEqual([], skill_learning.patterns(self.root))

    def test_propose_writes_quarantined_draft_never_active(self):
        self.seed()
        record = skill_learning.propose(skill_learning.patterns(self.root)[0], self.root)
        self.assertTrue(Path(record["source"]).is_relative_to(self.root / "skills/quarantine/learned"))
        self.assertEqual("quarantined", record["state"])
        state = skills_registry.state(record["id"], self.root)
        self.assertEqual("quarantined", state["state"])
        self.assertEqual("learned proposal", state["history"][-1]["reason"])
        self.assertEqual([], skills_registry.candidates({"title": "Repair parser safely"}, "execute", root=self.root))

    def test_reproposal_updates_version_not_duplicate(self):
        self.seed()
        pattern = skill_learning.patterns(self.root)[0]
        first = skill_learning.propose(pattern, self.root)
        pattern["successes"] = 5
        second = skill_learning.propose(pattern, self.root)
        records = json.loads((self.root / "skills/discovered.json").read_text())["skills"]
        self.assertEqual(1, len(records))
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["version"], second["version"])
        self.assertIn("version", skills_registry.state(first["id"], self.root)["history"][-1]["reason"])

    def test_low_confidence_refused_with_reason(self):
        value = skill_learning.propose({"procedure_signature": "x", "support": 3,
                                        "support_tasks": ["a", "b", "c"], "confidence": .5}, self.root)
        self.assertTrue(value["refused"])
        self.assertIn("confidence", value["reason"])

    def test_learned_records_coexist_with_external_records_in_store(self):
        self.seed()
        external = {"skills": {"external/x/y": {"id": "external/x/y", "provenance": "external",
                    "provenance_info": {"source_repo": "x", "source_path": "y/SKILL.md"}}}}
        path = self.root / "skills/discovered.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps(external))
        learned = skill_learning.propose(skill_learning.patterns(self.root)[0], self.root)
        records = json.loads(path.read_text())["skills"]
        self.assertIn("external/x/y", records)
        self.assertIn(learned["id"], records)
        candidate = ("x", "z/SKILL.md", None, "external", {"SKILL.md": b"# Z\n"}, None, "x", None, [])
        skill_discovery._store(candidate, self.root)

    def test_cli_learn_lists_and_proposes(self):
        self.seed()
        env = {**os.environ, "ORCH_ROOT": str(Path(self.temp.name))}
        listed = subprocess.run(["uv", "run", "orchestrator", "skills", "learn"], cwd=_harness.REPO,
                                env=env, capture_output=True, text=True)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn("tool_sequence", listed.stdout)
        proposed = subprocess.run(["uv", "run", "orchestrator", "skills", "learn", "--propose"], cwd=_harness.REPO,
                                  env=env, capture_output=True, text=True)
        self.assertEqual(0, proposed.returncode, proposed.stderr)
        self.assertIn("learned/", proposed.stdout)


if __name__ == "__main__":
    unittest.main()
