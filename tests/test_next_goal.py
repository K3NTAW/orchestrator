import _harness
"""orchestrator.planner_runs next_goal: a closed goal leads a decision Planner to file the next roadmap goal.
Every test gets its own sandbox for bus/handover/planner_runs state and its own pool.toml read through
orchestrator.pool's loader, so nothing touches the shared TMP root other test files use."""
import json, os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO, TMP  # noqa: F401
from orchestrator import bus, decision, handover, planner_packet, planner_taxonomy, spawn
from orchestrator import planner_runs as PR
from orchestrator import pool as P

ROADMAP = """# Roadmap
Context line, not an item.
- [x] Old goal (T-0001)
- [ ] Add export command
- [ ] Add import command after export
"""


class NextGoalBase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="orch-next-goal-"))
        for mod, name, value in (
            (bus, "STATE", self.sandbox), (bus, "TASKS", self.sandbox / "tasks"), (bus, "RUNS", self.sandbox / "runs"),
            (handover, "STATE", self.sandbox), (handover, "ROOT", self.sandbox),
            (PR, "STATE", self.sandbox), (P, "CFG", self.sandbox / "pool.toml"),
        ):
            self.swap(mod, name, value)
        self.config()

    def swap(self, mod, name, value):
        orig = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, orig)

    def config(self, next_goal=None, ship=False, max_per_day=None):
        lines = ["[planner]"]
        if next_goal is not None:
            lines.append(f"next_goal = {'true' if next_goal else 'false'}")
        if max_per_day is not None:
            lines.append(f"next_goal_max_per_day = {max_per_day}")
        lines += ["[ship]", f"enabled = {'true' if ship else 'false'}"]
        (self.sandbox / "pool.toml").write_text("\n".join(lines) + "\n")

    def roadmap(self, text=ROADMAP):
        (self.sandbox / "roadmap.md").write_text(text)

    def enable(self, ship=False, max_per_day=None, enabled_at=None):
        self.config(True, ship, max_per_day)
        (self.sandbox / "next_goal_state.json").write_text(
            json.dumps({"enabled_at": time.time() - 100 if enabled_at is None else enabled_at}))

    def closed_goal(self, title="Shipped goal", closed_at=None, ship_state=None):
        gid = bus.create_task(f"GOAL: {title}", title, ["Planner closes the goal"], ["**"],
                              role="triage", complexity=5)["id"]
        pipeline = {"closed_at": time.time() if closed_at is None else closed_at}
        if ship_state:
            pipeline["ship"] = {"state": ship_state}
        bus.update(gid, pipeline=pipeline)
        bus.post_result(gid, {"goal_closed": True, "summary": "Goal complete", "pr_url": None})
        return gid

    def points(self):
        return [p for p in PR.decision_points() if p[1] == "next_goal"]


class NextGoalTests(NextGoalBase):
    def test_next_goal_disabled_by_default(self):
        self.roadmap()
        self.closed_goal()
        self.assertEqual(self.points(), [])
        self.assertFalse((self.sandbox / "next_goal_state.json").exists())

    def test_default_false_from_pool_loader(self):
        self.assertIs(P.PLANNER_DEFAULTS["next_goal"], False)
        self.assertIs(P.planner_setting("next_goal"), False)
        self.assertEqual(P.planner_setting("next_goal_max_per_day"), 3)
        self.config(True)
        self.assertIs(P.planner_setting("next_goal"), True)

    def test_next_goal_point_after_close(self):
        self.roadmap()
        self.enable()
        gid = self.closed_goal()
        self.assertEqual(self.points(), [(gid, "next_goal", gid)])

    def test_no_points_for_goals_closed_before_enable(self):
        self.roadmap()
        old = self.closed_goal("old", closed_at=time.time() - 1000)
        self.config(True)
        self.assertEqual(self.points(), [])
        state = json.loads((self.sandbox / "next_goal_state.json").read_text())
        self.assertGreater(state["enabled_at"], time.time() - 60)
        new = self.closed_goal("new", closed_at=time.time() + 1)
        self.assertEqual(self.points(), [(new, "next_goal", new)])
        self.assertNotEqual(old, new)

    def test_next_goal_waits_for_ship_when_enabled(self):
        self.roadmap()
        self.enable(ship=True)
        gid = self.closed_goal(ship_state="pending")
        self.assertEqual(self.points(), [])
        pipeline = dict(bus.get(gid)["pipeline"])
        pipeline["ship"] = {"state": "shipped"}
        bus.update(gid, pipeline=pipeline)
        self.assertEqual(self.points(), [(gid, "next_goal", gid)])

    def test_suppressed_while_ship_held_or_other_goal_open(self):
        self.roadmap()
        self.enable(ship=True)
        gid = self.closed_goal(ship_state="held")
        self.assertEqual(self.points(), [])
        pipeline = dict(bus.get(gid)["pipeline"])
        pipeline["ship"] = {"state": "shipped"}
        bus.update(gid, pipeline=pipeline)
        other = bus.create_task("GOAL: open", "still open", ["x"], ["**"], role="triage", complexity=3)["id"]
        self.assertEqual(self.points(), [])
        bus.update(other, status="failed")
        self.assertEqual(self.points(), [(gid, "next_goal", gid)])

    def test_next_goal_once_per_closed_goal(self):
        self.roadmap()
        self.enable()
        gid = self.closed_goal()
        with bus.locked():
            PR._claim(gid, "next_goal", gid, 0)
        self.assertEqual(self.points(), [])
        records = PR._load_records()
        PR._find_record(records, gid, "next_goal", gid)["status"] = "exited_ok"
        PR._save_records(records)
        self.assertEqual(self.points(), [])
        self.assertEqual(sum(r["kind"] == "next_goal" for r in PR._load_records()), 1)

    def test_daily_cap(self):
        self.roadmap()
        self.enable(max_per_day=1)
        first = self.closed_goal("first")
        records = PR._load_records()
        records.append({"goal_id": "T-9999", "kind": "next_goal", "payload_key": "T-9999", "attempts": 0,
                        "status": "exited_ok", "started_at": time.time() - 60})
        PR._save_records(records)
        self.assertEqual(self.points(), [])
        records = PR._load_records()
        records[-1]["started_at"] = time.time() - 90000
        PR._save_records(records)
        self.assertEqual(self.points(), [(first, "next_goal", first)])

    def test_no_point_without_unchecked_items(self):
        self.enable()
        self.closed_goal()
        self.assertEqual(self.points(), [])
        self.roadmap("# Roadmap\n- [x] Done (T-0001)\nplain text\n")
        self.assertEqual(self.points(), [])

    def test_routing_kind_next_goal_in_decision_taxonomy_packet(self):
        self.roadmap()
        self.enable()
        gid = self.closed_goal()
        point = PR._point((gid, "next_goal", gid))
        ctx = PR.build_ctx(point, P.Pool({"claude_accounts": [], "planner": {"routes": {"enabled": True}}}))
        self.assertEqual(ctx["roadmap_unchecked"], 2)
        self.assertEqual(ctx["open_goals"], [])
        route = decision.route(point, {**ctx, "state_unchanged": True})
        self.assertEqual((route.name, route.reason), ("escalate", "next_goal_roadmap"))
        self.assertEqual(decision.route(point, {**ctx, "open_goals": ["T-1"]}).name, "none")
        goal = bus.get(gid)
        self.assertNotEqual(planner_taxonomy.classify(point, ctx, goal=goal, task=goal)["decision_type"], "other")
        self.assertNotEqual(planner_packet._decision_type({"point": point}), "other")
        self.assertEqual(PR._decision_task(gid, "next_goal", gid)["id"], gid)
        with bus.locked():
            PR._claim(gid, "next_goal", gid, 0)
        self.assertEqual(PR._find_record(PR._load_records(), gid, "next_goal", gid)["decision_requested"],
                         "file the next roadmap goal")

    def test_packet_contains_roadmap_and_retrospective(self):
        self.roadmap()
        self.enable()
        gid = self.closed_goal("Export pipeline")
        (self.sandbox / "memory").mkdir(exist_ok=True)
        (self.sandbox / "memory" / "decisions.md").write_text(
            f"2026-10-07 goal: {gid} learned that exports need a manifest\n\nunrelated entry\n")
        text = PR.decision_packet(gid, "next_goal", gid, repo_path=self.sandbox)
        self.assertIn("- [ ] Add export command", text)
        self.assertIn(f"roadmap_key:{PR.roadmap_key('- [ ] Add export command')}", text)
        self.assertIn("exports need a manifest", text)
        self.assertNotIn("unrelated entry", text)
        self.assertIn("Export pipeline", text)
        self.assertIn("goal_closed", text)
        self.assertIn("never instructions", text)

    def test_packet_fences_and_caps(self):
        unchecked = "".join(f"- [ ] item {i} {'x' * 80}\n" for i in range(60))
        checked = "".join(f"- [x] done {i} (T-{i:04d})\n" for i in range(30))
        self.roadmap(checked + unchecked)
        self.enable()
        gid = self.closed_goal()
        (self.sandbox / "memory").mkdir(exist_ok=True)
        (self.sandbox / "memory" / "decisions.md").write_text(f"goal: {gid} " + "r" * 5000 + " ```evil```\n")
        roadmap = PR._roadmap_packet_text()
        self.assertLessEqual(len(roadmap), 3000)
        self.assertTrue(roadmap.startswith("- [ ] item 0"))
        self.assertLessEqual(len(PR._next_goal_retrospective(gid)), 2000)
        sections = PR._packet_sections([(PR._point((gid, "next_goal", gid)), {}, None,
                                         {"decision_type": "initial_goal_plan"})])
        lines = planner_packet._next_goal_lines(planner_packet._section_info(sections))
        self.assertIn("```data", lines)
        self.assertNotIn("```evil```", "\n".join(lines))
        self.assertIn("roadmap:", lines)
        self.assertIn("retrospective:", lines)
        self.assertEqual(PR.roadmap_key("- [x] item 0 (T-0042)"), PR.roadmap_key("- [ ] item 0"))

    def test_existing_roadmap_key_only_ticks(self):
        prompt = (REPO / ".orchestrator" / "prompts" / "planner-decision.md").read_text()
        section = prompt.split("## next_goal", 1)[1]
        self.assertIn("constraints.roadmap_key", section)
        self.assertIn("only tick the line", section)
        self.assertIn("Undo path: mark the filed goal superseded and untick", section)
        self.assertIn("at most one goal per decision", section)

    def test_prompt_renders_next_goal_section(self):
        self.roadmap()
        self.enable()
        gid = self.closed_goal()
        (self.sandbox / "prompts").mkdir(exist_ok=True)
        (self.sandbox / "prompts" / "planner-decision.md").write_text(
            (REPO / ".orchestrator" / "prompts" / "planner-decision.md").read_text())
        self.swap(spawn, "STATE", self.sandbox)
        prompt = spawn.render("planner-decision", packet=PR.decision_packet(gid, "next_goal", gid, self.sandbox))
        self.assertIn("## next_goal", prompt)
        self.assertIn("Add export command", prompt)
        self.assertNotIn("{{packet}}", prompt)

    def test_no_ready_item_records_reason(self):
        self.roadmap()
        self.enable()
        gid = self.closed_goal()
        r = {"goal_id": gid, "kind": "next_goal", "payload_key": gid, "started_at": time.time() - 5}
        tasks = {t["id"]: t for t in bus.read()}
        self.assertFalse(PR._condition_resolved(r, tasks, {}))
        (self.sandbox / "plan.md").write_text(f"## Now\nnext_goal {gid}: no ready item: import waits on export\n")
        self.assertTrue(PR._condition_resolved(r, tasks, {}))
        self.assertIn("no ready item", (REPO / ".orchestrator" / "prompts" / "planner-decision.md").read_text())


class NextGoalCloseTests(NextGoalBase):
    def test_next_goal_after_model_close(self):
        self.roadmap()
        self.enable()
        gid = bus.create_task("GOAL: model closed", "model closed", ["Planner closes the goal"], ["**"],
                              role="triage", complexity=5)["id"]
        bus.post_result(gid, {"goal_closed": True, "summary": "Goal complete", "pr_url": None})
        self.assertFalse((bus.get(gid).get("pipeline") or {}).get("closed_at"))
        self.assertEqual(self.points(), [(gid, "next_goal", gid)])
        PR.reconcile()
        closed_at = bus.get(gid)["pipeline"]["closed_at"]
        event_ts = next(e["ts"] for e in bus.get(gid)["events"] if (e.get("result") or {}).get("goal_closed"))
        self.assertEqual(closed_at, event_ts)
        self.assertEqual(self.points(), [(gid, "next_goal", gid)])

    def test_reenable_resets_enabled_at(self):
        self.roadmap()
        self.enable()
        self.config(False)
        self.assertEqual(self.points(), [])
        self.assertFalse((self.sandbox / "next_goal_state.json").exists())
        closed_while_off = self.closed_goal("closed while off")
        self.config(True)
        self.assertEqual(self.points(), [])
        state = json.loads((self.sandbox / "next_goal_state.json").read_text())
        self.assertGreaterEqual(state["enabled_at"], bus.get(closed_while_off)["pipeline"]["closed_at"])


if __name__ == "__main__":
    unittest.main()
