import _harness

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import memory_hot, memory_store, skill_discovery, skill_learning, skills_registry


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

    def memory_records(self, count=5, dates=3):
        (self.root / "pool.toml").write_text("[memory]\nhot_budget_tokens=1000\nhot_recent_days=30\nhot_never_compact=[]\n")
        for index in range(count):
            memory_store.add(memory_store.Record(
                id=f"mem-{index}", kind="gotcha", title=f"Procedure {index}",
                date=f"2026-09-{20 + index % dates:02d}", components=["parser"],
                source_tasks=[f"T-mem-{index}"], body="Always check parser output before merge."), self.root.parent)

    def test_candidates_from_memory_groups_recurring_procedural_records(self):
        self.memory_records(3, 2)
        candidate, = skill_learning.candidates_from_memory(self.root)
        self.assertEqual((candidate["source"], candidate["support"]), ("memory", 3))
        self.assertEqual(candidate["memory_record_ids"], ["mem-0", "mem-1", "mem-2"])

    def test_memory_candidates_confidence_formula_and_roles_from_task_files(self):
        self.memory_records()
        for index, role in ((0, "review"), (1, "execute")):
            (self.root / f"tasks/T-mem-{index}.json").write_text(json.dumps(
                {"id": f"T-mem-{index}", "role": role}))
        candidate, = skill_learning.candidates_from_memory(self.root)
        self.assertEqual(1.0, candidate["confidence"])
        self.assertEqual(5, candidate["evidence_strength"])
        self.assertEqual(["execute", "review"], candidate["roles"])
        proposed = skill_learning.propose(candidate, self.root)
        self.assertIn("execute", proposed["roles"])

    def test_promoted_skill_tags_source_records_and_hot_view_points_to_skill(self):
        self.memory_records()
        record = skill_learning.propose(skill_learning.candidates_from_memory(self.root)[0], self.root)
        states = skills_registry._state_document(self.root)
        states[record["id"]]["state"] = "testing"
        skills_registry._write_json(self.root / "skills/state.json", states)
        skills_registry._update_registry_state(self.root, record["id"], states[record["id"]])
        skills_registry.transition(record["id"], "shadow", "observe", self.root)
        skills_registry.transition(record["id"], "active", "promote", self.root)
        source = memory_store.get("mem-0", self.root.parent)
        self.assertIn(f"skill:{record['id']}", source["tags"])
        view = memory_hot.build(self.root.parent)["view"]
        self.assertIn(f"learned skill: {record['id']}", view)
        self.assertNotIn("Always check parser output", view)


if __name__ == "__main__":
    unittest.main()
