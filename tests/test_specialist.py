import _harness

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import context_router, evidence, skill_router, skills_registry, specialist, spawn


class SpecialistTests(unittest.TestCase):
    def record(self, *, roles=("scout",), triggers=("who calls",), tools=(), context=(), **kw):
        return {"state": "active", "version": "abc123", "roles": list(roles),
                "task_classes": ["*"], "triggers": list(triggers), "tools": list(tools),
                "context_requirements": list(context), **kw}

    def compose(self, records, role="scout", task=None):
        task = task or {"id": "T-specialist", "title": "who calls authentication", "tier": "fast"}
        with mock.patch.object(skills_registry, "load", return_value={"skills": records}):
            return specialist.compose(task, role, {"models": {"fast": "model-existing"}})

    def test_compose_scout_with_trace_callers_declares_context_and_tools(self):
        result = self.compose({"scout/trace-callers": self.record(
            tools=["Read", "Search", "script:scripts/trace.sh"],
            context=["source_chunk", "graph_finding"])})
        self.assertEqual(result.skills, [{"id": "scout/trace-callers", "version": "abc123"}])
        self.assertEqual(result.context_requirements, ["graph_finding", "source_chunk"])
        self.assertIn("Bash(bash skills/*)", result.tools)
        self.assertIn("mcp__bus__bus_post_result", result.tools)
        self.assertEqual(result.decision["specialist"]["tools_added"], ["Bash(bash skills/*)"])
        self.assertEqual(result.model, "model-existing")

    def test_unavailable_tool_drops_skill_with_reason(self):
        result = self.compose({"scout/write": self.record(tools=["Write"])})
        self.assertEqual(result.skills, [])
        self.assertEqual(result.decision["rejected"], [
            {"id": "scout/write", "reason": "tool_unavailable", "tools": ["Write"]}])
        self.assertEqual(result.decision["specialist"]["tools_unavailable"], [
            {"id": "scout/write", "tool": "Write", "reason": "unavailable_tool"}])
        self.assertNotIn("Write", result.tools)
        unknown = self.compose({"scout/unknown": self.record(tools=["unlisted-tool"])})
        self.assertEqual(len(unknown.skills), 1)
        self.assertEqual(unknown.decision["specialist"]["tools_unknown"][0]["reason"], "unknown_tool")

    def test_tool_aliases_resolve_declared_tools(self):
        offered = specialist.tool_catalog.disclosed("scout")
        self.assertEqual(specialist.normalize_tool("Bash", offered),
                         [item for item in offered if item.startswith("Bash(")])
        self.assertEqual(specialist.normalize_tool("Search", offered), ["Grep", "Glob"])
        for tool in ("bus_post_result", "bus_read"):
            self.assertEqual(specialist.normalize_tool(tool, offered), ["mcp__bus__" + tool])
        for tool in ("spawn_scout", "spawn_review", "spawn_challenge", "spawn_spec_review",
                     "codex", "codex_reply", "merge", "status", "executor_fallback",
                     "hold_account", "resume_account", "Read", "Grep", "Glob", "Edit", "Write"):
            self.assertEqual(specialist.normalize_tool(tool, offered), [tool])
            self.assertIn(tool, specialist.tool_catalog.CATALOG)
        good = self.compose({"scout/trace-callers": self.record(
            tools=["Bash", "Search", "bus_post_result", "bus_read", "script:scripts/trace.sh"])})
        self.assertEqual(good.decision["specialist"]["tools_unavailable"], [])
        bad = self.compose({"review/adversarial-review": self.record(
            roles=["review"], tools=["script:scripts/trace.sh"])}, role="review")
        self.assertEqual(bad.skills, [])  # Even a mandatory skill cannot widen the allowlist.
        self.assertEqual(bad.decision["rejected"][0]["reason"], "tool_unavailable")

    def test_conflict_detected_and_resolved_by_trigger_strength(self):
        records = {"scout/a": self.record(conflicts_with=["scout/z"]),
                   "scout/z": self.record(triggers=["who calls", "authentication"])}
        result = self.compose(records)
        self.assertEqual([item["id"] for item in result.skills], ["scout/z"])
        self.assertEqual(result.conflicts, [{"kept": "scout/z", "dropped": "scout/a",
                                            "rule": "trigger_strength", "cause": "conflicts_with"}])
        records["scout/z"]["triggers"] = ["who calls"]
        tied = self.compose(records)
        self.assertEqual(tied.conflicts[0]["kept"], "scout/a")
        self.assertEqual(tied.conflicts[0]["rule"], "lexical")

    def test_contract_script_and_executor_stop_conflicts(self):
        for contracts, cause in (
            (("output: 'final result: JSON object'", "output: 'final result: Markdown table'"), "output_contract"),
            (("tools: ['script:shared.sh --json']", "tools: ['script:shared.sh --text']"), "script_arguments"),
        ):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as directory:
                records = {}
                for name, contract in zip(("a", "b"), contracts):
                    path = Path(directory) / (name + ".md")
                    path.write_text("---\n" + contract + "\n---\n## Procedure\nRun it.\n")
                    meta, _ = skills_registry._frontmatter(path.read_text())
                    records["scout/" + name] = self.record(source=str(path), tools=meta.get("tools", []))
                result = self.compose(records)
                self.assertEqual(result.conflicts[0]["cause"], cause)
        with tempfile.TemporaryDirectory() as directory:
            records = {}
            for name, stop in (("executor/implement-spec", "Commit when green."),
                               ("executor/a", "Do not commit.")):
                path = Path(directory) / (name.rsplit("/", 1)[-1] + ".md")
                path.write_text("## Stop conditions\n" + stop)
                records[name] = self.record(roles=["execute"], triggers=[], source=str(path))
            with mock.patch.object(skill_router, "_strength", return_value=2):
                result = self.compose(records, role="execute")
            self.assertEqual(result.conflicts[0]["cause"], "commit_rule")
            self.assertEqual(result.conflicts[0]["rule"], "mandatory")
            self.assertEqual(result.conflicts[0]["kept"], "executor/implement-spec")

    def test_review_specialist_excludes_other_reviewers_verdicts(self):
        target = {"id": "T-reviewed", "role": "execute"}
        task = {"id": "T-reviewer", "inputs": [target["id"]], "tier": "fast"}
        items = [evidence.make(kind, location, text, provenance="bus") for kind, location, text in (
            ("review_finding", "task:T-reviewed:review:1", "earlier verdict"),
            ("review_finding", "task:T-reviewed:review:round2", "another verdict"),
            ("review_finding", "task:T-other:review:1", "unrelated review"),
            ("scout_finding", "task:T-reviewed:review:1", "independent fact"))]
        for role in ("review", "security_review"):
            with self.subTest(role=role), mock.patch.object(specialist.bus, "get", return_value=target):
                result = self.compose({}, role=role, task=task)
            self.assertEqual(result.filter_evidence(items), items[2:])
            self.assertEqual(result.reviewed_id, target["id"])
        empty = self.compose({}, role="review", task={"id": "T-empty", "tier": "fast"})
        self.assertEqual(empty.filter_evidence(items), items)
        self.assertEqual(empty.decision["specialist"]["review_evidence"], "skipped: inputs empty")
        with mock.patch.object(specialist.bus, "get", return_value={"id": "T-scout", "role": "scout"}):
            wrong = self.compose({}, role="review", task=task)
        self.assertIsNone(wrong.reviewed_id)

    def test_name_is_deterministic_and_no_role_persisted(self):
        records = {"scout/trace-callers": self.record(), "scout/test-gap": self.record()}
        task = {"id": "T-names", "title": "who calls", "tier": "fast"}
        before = copy.deepcopy((records, task, spawn.TOOLS))
        with mock.patch.object(skills_registry, "_write_json") as write, \
                mock.patch.object(specialist.bus, "update") as update:
            a = self.compose(records, task=task)
            b = self.compose(dict(reversed(list(records.items()))), task=task)
        self.assertEqual(a.name, "scout+test-gap+trace-callers")
        self.assertEqual(a.name, b.name)
        self.assertEqual(before, (records, task, spawn.TOOLS))
        write.assert_not_called()
        update.assert_not_called()

    def test_compatible_output_shapes_and_supporting_artifacts_coexist(self):
        records = {
            "scout/a": self.record(output_contract="Scout findings JSON, with locations"),
            "scout/b": self.record(output_contract="Scout findings JSON, at most 20 findings"),
            "scout/c": self.record(output_contract="Supporting call graph artifact"),
        }
        result = self.compose(records)
        self.assertEqual(len(result.skills), 3)
        self.assertEqual(result.conflicts, [])

    def test_skill_selection_logs_specialist_without_changing_allowlist(self):
        records = {"scout/trace-callers": self.record(tools=["Bash"], context=["source_chunk"])}
        task = {"id": "T-log-specialist", "title": "who calls", "tier": "fast"}
        before = copy.deepcopy(spawn.TOOLS)
        cfg = {"skills": {"mode": "shadow"}}
        with mock.patch.object(skills_registry, "load", return_value={"skills": records}), \
                mock.patch.object(spawn.decision_log, "record") as record:
            choice = spawn._prepare_skills(task, "scout", cfg)
            spawn._skill_routing(task, "scout", cfg, {}, choice)
        row = record.call_args
        self.assertEqual(row.args[0], "skill_selection")
        self.assertEqual(row.kwargs["extra"]["specialist"]["name"], "scout+trace-callers")
        self.assertEqual(row.kwargs["extra"]["specialist"]["context_requirements"], ["source_chunk"])
        self.assertEqual(spawn.TOOLS, before)

    def test_active_review_consumes_pool_ids_but_preserves_fix_comments(self):
        target = {"id": "T-fix-target", "role": "execute", "spec": "Fix issue", "scope": [],
                  "constraints": {"fix_round_for": "T-original"}, "inputs": [], "acceptance": []}
        task = {"id": "T-final-review", "role": "review", "parent": "T-specialist-goal",
                "inputs": [target["id"]], "tier": "fast"}
        fact = evidence.make("scout_finding", "fact.py:1", "Independent fact", provenance="scout")
        verdict = evidence.make("review_finding", "task:T-fix-target:review:1", "PRIVATE VERDICT", provenance="bus")
        def get(task_id):
            return target if task_id == target["id"] else {"result": {"comments": ["Fix this bug"]}}
        with mock.patch.object(specialist.bus, "get", side_effect=get):
            composition = self.compose({}, role="review", task=task)
            with mock.patch.object(evidence, "EvidencePool") as pool, \
                    mock.patch.object(spawn, "scoped_diff", return_value="diff"), \
                    mock.patch.object(spawn, "_base_sha", return_value="head"):
                pool.return_value.by_type.side_effect = lambda kind: [
                    item for item in (fact, verdict) if item.source_type == kind]
                packet = spawn.review_packet(task, target, cfg={"context_router": {"mode": "off"}},
                    skills={"mode": "active", "_specialist": composition,
                            "specialist": composition.decision["specialist"]})
        self.assertIn(fact.id, packet)
        self.assertIn("Independent fact", packet)
        self.assertNotIn(verdict.id, packet)
        self.assertNotIn("PRIVATE VERDICT", packet)
        self.assertIn("## fix-round context", packet)
        self.assertIn("Fix this bug", packet)

    def test_planner_retains_mandatory_skills_without_tool_catalog(self):
        records = {name: self.record(roles=["planner"], tools=["Write", "unknown"])
                   for name in skill_router.MANDATORY["planner"]}
        with mock.patch.object(specialist.tool_catalog, "minimal_set") as minimum:
            result = self.compose(records, role="planner")
        self.assertEqual({item["id"] for item in result.skills}, set(records))
        self.assertEqual(result.tools, [])
        self.assertEqual(result.decision["reason"], "planner tools not catalogued")
        self.assertEqual(result.decision["rejected"], [])
        minimum.assert_not_called()

    def test_required_types_raise_hidden_and_short_to_long(self):
        items = [evidence.make("source_chunk", "outside.py", "code", provenance="repo"),
                 evidence.make("review_finding", "unrelated", "old", provenance="bus")]
        routed = context_router.route({"title": "absent"}, items, role="scout",
                                      required_types=["source_chunk", "review_finding"])
        self.assertEqual([item.level for item in routed.items], ["LONG", "LONG"])
        baseline = context_router.route({"title": "absent"}, items, role="scout")
        self.assertEqual([item.level for item in baseline.items], ["SHORT", "HIDE"])

    def test_shadow_routing_receives_requirements_and_filters_shared_evidence(self):
        task = {"id": "T-shadow-specialist", "parent": "T-goal", "inputs": ["T-reviewed"], "tier": "fast"}
        record = self.record(roles=["review"], context=["source_chunk"])
        with mock.patch.object(specialist.bus, "get", return_value={"id": "T-reviewed", "role": "execute"}):
            composition = self.compose({"review/adversarial-review": record}, role="review", task=task)
        fact = evidence.make("source_chunk", "outside.py", "fact", provenance="repo")
        verdict = evidence.make("review_finding", "task:T-reviewed:review:1", "verdict", provenance="bus")
        with mock.patch.object(specialist.Specialist, "shared_evidence", return_value=[fact, verdict]), \
                mock.patch.object(spawn.evidence, "EvidencePool") as pool, \
                mock.patch.object(spawn.decision_log, "record") as log, \
                mock.patch.object(spawn.context_router, "route", wraps=context_router.route) as route:
            pool.return_value.add.side_effect = lambda item: item
            result = spawn._shadow_route(task, [], role="review", head_sha="head",
                                         cfg={"context_router": {"mode": "shadow"}},
                                         skills={"_specialist": composition})
        self.assertEqual(route.call_args.kwargs["required_types"], ["source_chunk"])
        self.assertEqual(result["evidence_ids"], [fact.id])
        self.assertEqual(log.call_args.kwargs["deterministic"]["LONG"], 1)


if __name__ == "__main__":
    unittest.main()
