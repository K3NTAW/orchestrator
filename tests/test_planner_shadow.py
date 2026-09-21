import _harness

import hashlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from orchestrator import bus, goals, planner_router, planner_shadow as shadow, schedlog


class PlannerShadowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="planner-shadow-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.log = self.root / "shadow.log"

    def test_argv_has_no_bus_mcp_and_no_write_tools(self):
        prompt = self.root / "system.md"
        prompt.write_text("system")
        args = shadow.argv("packet", model="shadow-opus", budget_usd=1.5,
                           system_prompt_path=prompt)
        self.assertEqual(args[args.index("--model") + 1], "shadow-opus")
        self.assertEqual(args[args.index("--mcp-config") + 1], ".mcp.planner-shadow.json")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(set(args[args.index("--disallowedTools") + 1].split(",")),
                         {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task", "WebFetch", "WebSearch"})
        self.assertNotIn(".mcp.planner.json", args)
        self.assertEqual(json.loads((_harness.REPO / ".mcp.planner-shadow.json").read_text()),
                         {"mcpServers": {}})
        self.assertEqual(args[args.index("--append-system-prompt") + 1], "system")

    def test_launch_failure_is_isolated_and_production_unchanged(self):
        prompts = self.root / ".orchestrator/prompts"
        prompts.mkdir(parents=True)
        (prompts / "planner.md").write_text("system")
        tasks = self.root / ".orchestrator/tasks"
        tasks.mkdir()
        (tasks / "T-1.json").write_text('{"status":"queued"}')
        plan = self.root / ".orchestrator/plan.md"
        plan.write_text("production plan")
        def snapshot():
            return ({p.name: p.read_bytes() for p in tasks.iterdir()},
                    hashlib.sha256(plan.read_bytes()).hexdigest())
        before = snapshot()
        account = {"config_dir": "~/shadow-config", "oauth_token_env": "SHADOW_TEST_TOKEN"}
        kwargs = dict(model="shadow-opus", account=account, budget_usd=1.5,
                      log=self.log, root=self.root)
        with mock.patch.object(shadow.spawn, "render", return_value="rendered packet") as render, \
                mock.patch.dict(os.environ, {"SHADOW_TEST_TOKEN": "oauth"}), \
                mock.patch.object(bus, "pool_config", side_effect=AssertionError("must not resolve secrets")):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertIsNone(shadow.launch("packet", popen=mock.Mock(side_effect=OSError("failed")), **kwargs))
                self.assertIn("failed", stderr.getvalue())
            fake = mock.Mock(return_value=mock.Mock(pid=123))
            with mock.patch.object(goals, "Popen", fake):
                result = shadow.launch("packet", **kwargs)
            render.assert_called_with("planner-shadow", packet="packet")
        self.assertEqual(result["pid"], 123)
        call = fake.call_args.kwargs
        self.assertTrue(call["start_new_session"])
        self.assertEqual(call["cwd"], self.root)
        self.assertEqual(call["env"]["CLAUDE_CONFIG_DIR"], os.path.expanduser(account["config_dir"]))
        self.assertEqual(call["env"]["ORCH_SHADOW"], "1")
        self.assertEqual(call["env"]["ORCH_ROOT"], str(self.root))
        self.assertEqual(call["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "oauth")
        self.assertTrue(call["stdout"].closed)
        self.assertTrue(call["stderr"].closed)
        self.assertEqual(snapshot(), before)

    def test_parse_whitelists_and_caps(self):
        usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 30}
        result = {"proposed_action": "fix_round", "summary": "word " * 600,
                  "tasks_proposed": [{"title": "task " * 40, "scope": ["path"] * 15,
                                      "transcript": "private"}] * 15,
                  "transcript": "private", "confidence": 2, "needs_fable": True,
                  "unresolved": ["issue " * 50] * 15}
        envelope = {"result": json.dumps(result), "usage": usage,
                    "session_id": "session", "total_cost_usd": 0.2, "is_error": False}
        self.log.write_text('noise\n{}\n' + json.dumps(envelope) + '\ntrailing noise\n')
        parsed = shadow.parse(self.log)
        self.assertEqual(parsed["proposed_action"], "fix_round")
        self.assertEqual(parsed["summary"], "word " * 160)
        self.assertEqual(len(parsed["tasks_proposed"]), 10)
        self.assertEqual(parsed["tasks_proposed"][0], {"title": "task " * 24, "scope": ["path"] * 10})
        self.assertNotIn("transcript", parsed)
        self.assertEqual(parsed["confidence"], 1)
        self.assertEqual(parsed["unresolved"], [("issue " * 50)[:200]] * 10)
        self.assertEqual(parsed["usage"], bus.normalize_usage("claude", usage))
        envelope["result"] = json.dumps({"summary": "x" * 3000})
        self.log.write_text(json.dumps(envelope))
        self.assertNotIn("x" * 100, shadow.parse(self.log)["summary"])
        for invalid in ("not JSON", "{invalid}", "", None):
            envelope["result"] = invalid
            self.log.write_text(json.dumps(envelope))
            parsed = shadow.parse(self.log)
            self.assertTrue(parsed["parse_error"])
            self.assertEqual(parsed["usage"], bus.normalize_usage("claude", usage))
        envelope["result"] = json.dumps({"proposed_action": [], "confidence": "NaN"})
        self.log.write_text(json.dumps(envelope))
        self.assertEqual(shadow.parse(self.log)["proposed_action"], "other")
        self.assertIsNone(shadow.parse(self.log)["confidence"])
        self.assertIsNone(shadow.parse(self.root / "missing"))

    def test_score_priority_chain_and_needs_fable_matched(self):
        self.assertEqual(shadow.production_action({"escalated": True, "fix_strategy_for": ["T-1"], "new_tasks": ["T-2"]}), "escalate")
        self.assertEqual(shadow.production_action({"fix_strategy_for": ["T-1"], "new_tasks": ["T-2"]}), "fix_round")
        self.assertEqual(shadow.production_action({"new_tasks": ["T-2"]}), "write_specs")
        self.assertEqual(shadow.production_action({}), "noop")
        row = {"proposed_action": "synthesize_now", "needs_fable": True}
        scored = shadow.score(row, {"new_tasks": ["T-1"]})
        self.assertTrue(scored["agreement"])
        self.assertIn("synthesize_now", scored["basis"])
        self.assertIn("write_specs", scored["basis"])
        self.assertIsNone(shadow.score(row, None)["agreement"])
        self.assertIsNone(shadow.score({"parse_error": True}, {})["agreement"])
        self.assertTrue(shadow.score(row, {"escalated": True})["needs_fable_matched"])
        self.assertFalse(shadow.score(row, {"escalated": False})["needs_fable_matched"])
        self.assertIsNone(scored["needs_fable_matched"])
        self.assertFalse(shadow.score(row, {})["agreement"])

    def test_eligible_and_summary(self):
        decision = planner_router.RouterDecision(tier="fable", mode="shadow", hard=False,
                    hold=False, hold_reason=None, reasons=(), hard_reasons=(), soft_score=0,
                    shadow_tier="opus")
        cfg = {"shadow_sample_rate": 0.5}
        for changes, reason in (({"mode": "off", "hold": True}, "mode_not_shadow"),
                                ({"hold": True, "shadow_tier": None}, "held"),
                                ({"shadow_tier": None}, "no_shadow_tier")):
            self.assertEqual(shadow.eligible(replace(decision, **changes), cfg), (False, reason))
        self.assertEqual(shadow.eligible(decision, cfg, sample=0.6), (False, "sampled_out"))
        self.assertEqual(shadow.eligible(decision, cfg, sample=0.5), (True, "eligible"))
        self.assertEqual(shadow.eligible(decision, cfg), (True, "eligible"))
        self.assertEqual(shadow.summary(self.root)["n"], 0)
        original = schedlog.SCHED_DIR
        row = shadow.record(launch_id="L1", goal_id="T-1", decision_type="repair",
                            state_version=2, model="opus", tier="opus", started_at=1,
                            parsed={"proposed_action": "noop", "needs_fable": True,
                                    "summary": "word " * 160, "prompt": "private",
                                    "usage": {"total_tokens": 120}, "total_cost_usd": 0.2},
                            latency_s=2, root=self.root)
        self.assertIs(schedlog.SCHED_DIR, original)
        self.assertNotIn("prompt", row)
        self.assertEqual(len(row["summary"]), 800)
        path = self.root / "runs/sched/planner_shadow.jsonl"
        self.assertEqual(json.loads(path.read_text()), row)
        row["agreement"] = True
        path.write_text(json.dumps(row) + '\nmalformed\n' + json.dumps(
            {"parse_error": True, "agreement": None, "total_cost_usd": 0.1}) + '\n')
        report = shadow.summary(self.root)
        self.assertEqual(report["n"], 2)
        self.assertEqual(report["agreement_rate"], 1)
        self.assertEqual(report["needs_fable_rate"], 0.5)
        self.assertEqual(report["parse_error_rate"], 0.5)
        self.assertAlmostEqual(report["usd"], 0.3)
        self.assertEqual(report["tokens"], 120)


if __name__ == "__main__":
    unittest.main()
