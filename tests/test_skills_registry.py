import _harness

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import skills_registry
from orchestrator import skill_discovery
from orchestrator import memory_store


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

    def make_external(self, body, name="example", script=None):
        repository = Path(self.temp.name) / "external"
        skill = repository / "skills" / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(body)
        if script is not None:
            (skill / "run.py").write_text(script)
        subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
        candidates = skill_discovery.discover(repository, self.root)["candidates"]
        record = next(candidate for candidate in candidates
                      if Path(candidate["source"]).parent.name == name)
        skills_registry.transition(record["id"], "quarantined", "review", self.root)
        return record

    def test_quarantined_to_testing_blocked_on_high_risk_unless_human_reviewed(self):
        record = self.make_external("# Client\nhttps://example.invalid uses CLIENT_AUTH\n")
        report = skill_discovery.inspect(record["id"], self.root)
        self.assertEqual("high", report["risk"]["level"])
        with self.assertRaisesRegex(ValueError, "risk level high"):
            skills_registry.transition(record["id"], "testing", "reviewed", self.root)
        state = skills_registry.transition(record["id"], "testing", "human-reviewed: approved", self.root)
        self.assertEqual("human-reviewed: approved", state["history"][-1]["override"])

    def test_transition_requires_matching_inspection_hash(self):
        record = self.make_external("# Notes\n")
        skill_discovery.inspect(record["id"], self.root)
        Path(record["source"]).write_text("# Changed\n")
        with self.assertRaisesRegex(ValueError, "content changed"):
            skills_registry.transition(record["id"], "testing", "reviewed", self.root)

    def test_rollback_uses_the_same_wrapped_gate(self):
        record = self.make_external("# Client\nhttps://example.invalid uses CLIENT_AUTH\n")
        skill_discovery.inspect(record["id"], self.root)
        skills_registry.transition(record["id"], "testing", "human-reviewed: approved", self.root)
        skills_registry.transition(record["id"], "disabled", "pause", self.root)
        state = skills_registry._state_document(self.root)
        approval = next(row for row in state[record["id"]]["history"] if row["to"] == "testing")
        approval["reason"] = "reviewed"
        skills_registry._write_json(self.root / "skills/state.json", state)
        with self.assertRaisesRegex(ValueError, "risk level high"):
            skills_registry.rollback(record["id"], self.root)

        latest = self.make_external("# Script\n", name="scripted", script="print('ok')\n")
        skill_discovery.inspect(latest["id"], self.root)
        with self.assertRaisesRegex(ValueError, "blocked findings"):
            skills_registry.transition(latest["id"], "testing", "reviewed", self.root)

    def test_rollback_keeps_original_override_reason(self):
        record = self.make_external("# Client\nhttps://example.invalid uses CLIENT_AUTH\n")
        skill_discovery.inspect(record["id"], self.root)
        skills_registry.transition(record["id"], "testing", "override: approved", self.root)
        skills_registry.transition(record["id"], "disabled", "pause", self.root)
        self.assertEqual("testing", skills_registry.rollback(record["id"], self.root)["state"])

        state = skills_registry._state_document(self.root)
        state[record["id"]]["state"] = "disabled"
        state[record["id"]]["history"] = [{"from": "testing", "to": "disabled", "reason": "pause"}]
        skills_registry._write_json(self.root / "skills/state.json", state)
        skills_registry._update_registry_state(self.root, record["id"], state[record["id"]])
        with self.assertRaisesRegex(ValueError, "risk level high"):
            skills_registry.rollback(record["id"], self.root)

    def test_sync_builds_twelve_builtin_records_with_content_hashes(self):
        document = skills_registry.sync(self.root, _harness.REPO / "skills")
        self.assertEqual(12, len(document["skills"]))
        for record in document["skills"].values():
            self.assertRegex(record["version"], r"^[0-9a-f]{12}$")
            self.assertEqual(record["version"], record["content_hash"])
            self.assertEqual("builtin", record["provenance"])
            self.assertEqual("active", record["state"])

    def test_conflicts_with_parsed_from_frontmatter(self):
        directory = self.make_skill()
        (directory / "SKILL.md").write_text(
            "---\nname: finder\nconflicts_with: [scout/other, scout/third]\n---\nFind things.\n")
        record = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        self.assertEqual(record["conflicts_with"], ["scout/other", "scout/third"])
        self.make_skill(name="plain")
        plain = skills_registry.sync(self.root, self.skills)["skills"]["scout/plain"]
        self.assertEqual(plain["conflicts_with"], [])

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

    def test_dependency_hashes_never_escape_repo_or_quarantine(self):
        self.skills = Path(self.temp.name) / "repo/skills"
        directory = self.make_skill()
        sentinel = Path(self.temp.name) / "private/sentinel.txt"
        sentinel.parent.mkdir()
        sentinel.write_text("private sentinel")
        repo_dep = self.skills.parent / "dep.py"
        repo_dep.write_text("repository dependency")
        source = directory / "SKILL.md"
        source.write_text(
            f"---\ndepends_on: ['{sentinel}', '../private/*.txt', 'dep.py', 'link.txt', '?inked/*.txt']\n---\nSafe.\n")
        (self.skills.parent / "link.txt").symlink_to(sentinel)
        (directory / "linked").symlink_to(sentinel.parent, target_is_directory=True)
        quarantine = self.root / "skills/quarantine/external/example"
        quarantine.mkdir(parents=True)
        external_source = quarantine / "SKILL.md"
        external_source.write_text(source.read_text())
        (quarantine / "dep.py").write_text("quarantine dependency")
        (quarantine / "link.txt").symlink_to(sentinel)
        (quarantine / "references").mkdir()
        (quarantine / "references/leak.txt").symlink_to(sentinel)
        discovered_path = self.root / "skills/discovered.json"
        state_path = self.root / "skills/state.json"
        original_open = Path.open
        original_scandir = os.scandir

        def guarded_scandir(path):
            self.assertNotEqual(Path(path).resolve(), sentinel.parent.resolve(), "scan outside dependency boundary")
            return original_scandir(path)

        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path.resolve(), sentinel.resolve(), "read outside dependency boundary")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open), mock.patch.object(os, "scandir", guarded_scandir):
            builtin = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
            self.assertEqual(set(builtin["dependency_hashes"]), {str(repo_dep.resolve())})
            for provenance in ("external", "learned"):
                record = {**builtin, "id": "external/example", "source": str(external_source),
                          "provenance": provenance, "state": "quarantined", "dependency_hashes": {}}
                skills_registry._write_json(discovered_path, {"skills": {record["id"]: record}})
                states = skills_registry._state_document(self.root)
                states[record["id"]] = {"state": "testing", "trust": "untrusted", "history": []}
                skills_registry._write_json(state_path, states)
                skills_registry.sync(self.root, self.skills)
                result = skills_registry.load(self.root)["skills"][record["id"]]
                self.assertEqual(set(result["dependency_hashes"]), {str((quarantine / "dep.py").resolve())})
                history = skills_registry.state(record["id"], self.root)["history"]
                ignored = {row["dependency"] for row in history if row["reason"] == "dependency_ignored"}
                self.assertTrue({str(sentinel), "../private/*.txt", "link.txt", "references/leak.txt"} <= ignored)
            ignored = {row["dependency"] for row in skills_registry.state("scout/finder", self.root)["history"]
                       if row["reason"] == "dependency_ignored"}
            self.assertTrue({str(sentinel), "../private/*.txt", "link.txt", "?inked/*.txt"} <= ignored)

    def test_dependency_hashing_waits_until_testing(self):
        directory = self.make_skill()
        builtin = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        quarantine = self.root / "skills/quarantine/external/example"
        quarantine.mkdir(parents=True)
        source = quarantine / "SKILL.md"
        source.write_text("---\ndepends_on: [dep.py]\n---\nSafe.\n")
        (quarantine / "dep.py").write_text("dependency")
        external = {**builtin, "id": "external/example", "source": str(source), "provenance": "external"}
        skills_registry._write_json(self.root / "skills/discovered.json", {"skills": {external["id"]: external}})
        for status in ("discovered", "quarantined", "testing"):
            states = skills_registry._state_document(self.root)
            states["scout/finder"]["state"] = status
            states[external["id"]] = {"state": status, "trust": "untrusted", "history": []}
            skills_registry._write_json(self.root / "skills/state.json", states)
            with mock.patch.object(skills_registry, "dependency_hashes", wraps=skills_registry.dependency_hashes) as hashes:
                skills_registry.sync(self.root, self.skills)
                self.assertEqual(hashes.call_count, 2 if status == "testing" else 0)
            if status != "testing":
                self.assertEqual(skills_registry.load(self.root)["skills"][external["id"]]["dependency_hashes"], {})

    def test_sync_marks_stale_on_dependency_change(self):
        directory = self.make_skill()
        (directory / "SKILL.md").write_text("---\ndepends_on: [dep.py]\n---\nSafe.\n")
        dependency = self.skills.parent / "dep.py"
        dependency.write_text("before")
        first = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        self.assertFalse(first["stale"])
        skills_registry._update_record("scout/finder", {"validation": {"status": "tested"}}, self.root)
        dependency.write_text("after")
        second = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        self.assertTrue(second["stale"])
        self.assertIsNotNone(second["stale_since"])
        self.assertEqual(second["validation"]["status"], "untested")
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(skills_registry.state("scout/finder", self.root)["history"][-1]["changed_dependencies"],
                         [str(dependency.resolve())])

    def test_revalidate_clears_stale_on_success(self):
        self.make_skill()
        skills_registry.sync(self.root, self.skills)
        skills_registry._update_record("scout/finder", {
            "stale": True, "stale_since": "yesterday",
            "validation": {"tests": ["tests/test_skills_registry.py"]}}, self.root)
        for returncode, expected in ((1, True), (0, False)):
            with mock.patch.object(skills_registry.subprocess, "run", return_value=mock.Mock(returncode=returncode)):
                validation = skills_registry.revalidate("scout/finder", self.root)
            record = skills_registry.load(self.root)["skills"]["scout/finder"]
            self.assertEqual(record["stale"], expected)
            self.assertEqual(validation["status"], "failed" if expected else "tested")
            self.assertEqual(validation["version"], record["version"])
        self.assertIsNone(record["stale_since"])
        self.assertEqual(skills_registry.state("scout/finder", self.root)["history"][-1]["reason"], "revalidation passed")

    def test_rollback_skips_noop_transitions(self):
        directory = self.make_skill()
        skills_registry.sync(self.root, self.skills)
        skills_registry.transition("scout/finder", "shadow", "observe", self.root)
        with (directory / "SKILL.md").open("a") as stream:
            stream.write("Changed.\n")
        skills_registry.sync(self.root, self.skills)
        history = skills_registry.state("scout/finder", self.root)["history"]
        self.assertEqual(history[-1]["from"], history[-1]["to"])
        self.assertEqual(skills_registry.rollback("scout/finder", self.root)["state"], "active")
        self.assertEqual(skills_registry.load(self.root)["skills"]["scout/finder"]["state"], "active")

    def test_drafts_are_redacted_on_write(self):
        directory = self.root / "skills/quarantine/learned/example"
        directory.mkdir(parents=True)
        source = directory / "SKILL.md"
        secret = "sk-" + "a" * 40
        source.write_text("Draft contains " + secret)
        path = self.root / "skills/discovered.json"
        record = {"source": str(source), "version": "old", "content_hash": "old",
                  "provenance_info": {"description": secret}}
        skills_registry._write_json(path, {"skills": {"learned/example": record}})
        self.assertNotIn(secret, source.read_text())
        self.assertNotIn(secret, path.read_text())
        persisted = json.loads(path.read_text())["skills"]["learned/example"]
        self.assertNotEqual(persisted["version"], "old")
        from orchestrator.skill_discovery import _hash
        self.assertEqual(persisted["content_hash"], _hash({"SKILL.md": source.read_bytes()}))

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

    def test_transition_to_active_tags_source_memory_records(self):
        memory_store.add(memory_store.Record(id="source-memory", kind="gotcha", title="Source",
                                             date="2026-09-23", body="Always check it."), self.root.parent)
        self.make_skill()
        record = skills_registry.sync(self.root, self.skills)["skills"]["scout/finder"]
        skills_registry._update_record(record["id"], {
            "provenance_info": {"memory_record_ids": ["source-memory"]}}, self.root)
        skills_registry.transition(record["id"], "shadow", "observe", self.root)
        skills_registry.transition(record["id"], "active", "promote", self.root)
        self.assertIn("skill:scout/finder", memory_store.get("source-memory", self.root.parent)["tags"])


if __name__ == "__main__":
    unittest.main()
