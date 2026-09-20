"""orchestrator.scorecard (build/scores/prior_weights) and orchestrator.bench (RSC chunk parsing, display-hint
matching, the 20h fetch gate). Scorecard uses its own scratch root under TMP so counts are exact regardless of
what other test files created; Bench never fetches over the network (fetch_html is monkeypatched)."""
import contextlib, inspect, io, json, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_scorecard.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import STATE, bench, bus, cli, executor, pool as P, scorecard


class Scorecard(unittest.TestCase):
    """build()/scores() on a synthesized scratch root, isolated from the tasks every other test file creates
    under the shared TMP/.orchestrator, so counts are exact."""
    def setUp(self):
        self.root = TMP / "scorecard-src" / str(time.time())
        (self.root / "tasks").mkdir(parents=True); (self.root / "runs").mkdir(parents=True)

    def write_task(self, tid, **fields):
        base = {"id": tid, "role": "execute", "tier": "sonnet", "complexity": 3, "status": "queued",
                "acceptance": ["a"], "scope": ["x"], "spec": "s", "title": tid}
        (self.root / "tasks" / f"{tid}.json").write_text(json.dumps({**base, **fields}))

    def write_runs(self, *lines):
        (self.root / "runs" / f"{time.strftime('%Y-%m-%d')}.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n")

    def test_build_counts_and_scores(self):
        self.write_task("T-9001", executor="good", complexity=3, status="done", merged_into="goal/G", rounds=1)
        self.write_task("T-9002", executor="bad", complexity=4, status="failed")
        self.write_runs({"role": "execute", "executor": "bad", "outcome": "usage_limit", "duration_s": 1.0})
        card = scorecard.build(root=self.root)
        self.assertEqual(card["good"]["merged"], 1); self.assertEqual(card["good"]["rounds_avg"], 1.0)
        self.assertEqual(card["good"]["by_complexity"]["1-3"]["merged"], 1)
        self.assertEqual(card["bad"]["failed"], 1); self.assertEqual(card["bad"]["by_complexity"]["4-6"]["failed"], 1)
        self.assertEqual(card["bad"]["held_usage_limit"], 1)
        neutral = scorecard.scores(card)                       # default min_runs=5: too few resolved tasks
        self.assertEqual((neutral["good"], neutral["bad"]), (1.0, 1.0))
        tight = scorecard.scores(card, min_runs=1)
        self.assertLess(tight["bad"], 1.0)                     # 0 merged / 1 failed -> success=0, score=0.5
        self.assertGreater(tight["good"], 1.0)                 # 1 merged / 0 failed -> success=1, score=1.5

    def test_review_verdict_rolls_up(self):
        self.write_task("T-9003", executor="reviewed-by", status="done", review_verdict="request_changes")
        card = scorecard.build(root=self.root)
        self.assertEqual(card["reviewed-by"]["review_request_changes"], 1)

    def test_review_task_does_not_double_count_verdict(self):
        # review T-0031: a review task has no executor and used to bucket under claude:<tier>, double-counting
        # the same request_changes verdict that spawn.run_worker already stamped on the reviewed execute task
        self.write_task("T-9006", role="review", tier="sonnet", review_verdict="request_changes")
        self.write_task("T-9007", tier="sonnet", review_verdict="request_changes")
        card = scorecard.build(root=self.root)
        self.assertEqual(card["claude:sonnet"]["review_request_changes"], 1)

    def test_scout_run_does_not_create_executor_row(self):
        self.write_runs({"role": "scout", "executor": "x", "outcome": "usage_limit", "duration_s": 1.0})
        card = scorecard.build(root=self.root)
        self.assertNotIn("x", card)

    def test_by_tier_regroups(self):
        self.write_task("T-9004", executor="astra", tier="astra", complexity=2, status="done", merged_into="goal/G")
        card = scorecard.build(root=self.root, by="tier")
        self.assertIn("astra", card); self.assertEqual(card["astra"]["merged"], 1)

    def test_write_sets_generated_at(self):
        self.write_task("T-9005", executor="x", status="done", merged_into="goal/G")
        payload = scorecard.write(scorecard.build(root=self.root))
        self.assertIn("generated_at", payload)
        self.assertIn("generated_at", json.loads((STATE / "scorecard.json").read_text()))

    def test_by_task_sums_runs(self):
        self.write_runs(
            {"task": "T-9010", "role": "execute", "tier": "sonnet", "duration_s": 10.0, "usd": 0.5,
             "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 20},
            {"task": "T-9010", "role": "execute", "tier": "sonnet", "duration_s": 5.0, "usd": 0.25,
             "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
        )
        card = scorecard.by_task(root=self.root)
        self.assertEqual(card["T-9010"]["usd"], 0.75)
        self.assertEqual(card["T-9010"]["wall_s"], 15.0)
        self.assertEqual(card["T-9010"]["tokens"], 167)              # (100+50+20//10) + (10+5+0//10)
        self.assertEqual(card["T-9010"]["role"], "execute")
        self.assertEqual(card["T-9010"]["tier"], "sonnet")

    def test_jev_lines_ignored(self):
        # a jev usage line (no "role" key) landing in a top-level runs/*.jsonl file must not be counted as a
        # worker run by build() or by_task() -- it should contribute to neither an executor row nor a task total.
        self.write_task("T-9011", executor="good", complexity=3, status="done", merged_into="goal/G", rounds=1)
        self.write_runs(
            {"task": "T-9011", "role": "execute", "executor": "good", "duration_s": 1.0, "usd": 1.0,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"ts": time.time(), "caller": "noul", "input_tokens": 999999, "model": "jev-latest",
             "latency_ms": 12.0, "ok": True},
        )
        card = scorecard.build(root=self.root)
        self.assertEqual(card["good"]["tokens"]["in"], 10)
        by_task = scorecard.by_task(root=self.root)
        self.assertEqual(by_task["T-9011"]["tokens"], 10)
        self.assertEqual(scorecard.malformed_run_lines(), 0)  # the jev line is well-formed JSON, just role-less

    def test_by_goal_splits_by_role_and_includes_planner_runs(self):
        goal = "T-9100"
        self.write_task(goal, role="triage", parent=None)
        self.write_task("T-9101", role="execute", parent=goal)
        self.write_task("T-9102", role="review", parent=goal)
        self.write_task("T-9103", role="scout", parent=goal)
        self.write_runs(
            {"task": "T-9101", "role": "execute", "usd": 3.0, "duration_s": 1,
             "input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9102", "role": "review", "usd": 1.0, "duration_s": 1,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9103", "role": "scout", "usd": 0.0, "duration_s": 1,
             "input_tokens": 50, "output_tokens": 0, "cache_read_input_tokens": 0},
        )
        (self.root / "runs" / "planner_runs.json").write_text(json.dumps(
            [{"goal_id": goal, "kind": "goal", "payload_key": "x", "pid": 1, "started_at": 0, "log": "l"}]))
        card = scorecard.by_goal(root=self.root)
        g = card[goal]
        self.assertEqual(g["total_usd"], 4.0)
        self.assertEqual(g["roles"]["execute"]["usd"], 3.0)
        self.assertEqual(g["roles"]["review"]["usd"], 1.0)
        self.assertEqual(g["roles"]["scout"]["usd"], 0.0)
        self.assertEqual(g["planner"]["n_runs"], 1)
        self.assertIsNone(g["planner"]["usd"])                        # decision runs here carry no usd field
        pct = scorecard.goal_percentages(g)
        self.assertAlmostEqual(pct["execute"], 75.0)
        self.assertAlmostEqual(pct["review"], 25.0)
        self.assertEqual(scorecard.format_planner_runs_cell(g), "1 runs")

    def test_planner_runs_cell_distinguishes_missing_from_zero(self):
        goal = "T-9200"
        self.write_task("T-9201", role="execute", parent=goal)
        self.write_runs({"task": "T-9201", "role": "execute", "usd": 2.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        card = scorecard.by_goal(root=self.root)
        self.assertIsNone(card[goal]["planner"])                      # planner_runs.json missing entirely
        self.assertEqual(scorecard.format_planner_runs_cell(card[goal]), "-")

        (self.root / "runs" / "planner_runs.json").write_text(json.dumps(
            [{"goal_id": "T-other", "kind": "goal", "payload_key": "x", "pid": 1, "started_at": 0, "log": "l"}]))
        card = scorecard.by_goal(root=self.root)
        self.assertEqual(card[goal]["planner"]["n_runs"], 0)          # file exists, none for this goal
        self.assertEqual(scorecard.format_planner_runs_cell(card[goal]), "0 runs")

    def test_planner_runs_cell_sums_usd_when_present(self):
        goal = "T-9300"
        self.write_task("T-9301", role="execute", parent=goal)
        self.write_runs({"task": "T-9301", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        (self.root / "runs" / "planner_runs.json").write_text(json.dumps([
            {"goal_id": goal, "kind": "goal", "usd": 0.4},
            {"goal_id": goal, "kind": "goal", "usd": 0.1},
        ]))
        card = scorecard.by_goal(root=self.root)
        self.assertAlmostEqual(card[goal]["planner"]["usd"], 0.5)
        self.assertEqual(scorecard.format_planner_runs_cell(card[goal]), "2 runs ($0.5)")

    def test_planner_usage_totals_read_from_pool_shape(self):
        goal = "T-9400"
        self.write_task("T-9401", role="execute", parent=goal)
        self.write_runs({"task": "T-9401", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        from orchestrator.pool import _save_planner_account
        orig_usage = P.PLANNER_USAGE
        P.PLANNER_USAGE = self.root / "planner_usage.json"
        self.addCleanup(lambda: setattr(P, "PLANNER_USAGE", orig_usage))
        _save_planner_account("A", 10, 400, {})
        _save_planner_account("B", 5, 100, {})
        self.assertEqual(scorecard.planner_footer(root=self.root),
                          "planner (transcripts, today): 500 tokens across 2 accounts")

    def test_planner_footer_dash_when_usage_file_missing(self):
        self.assertEqual(scorecard.planner_footer(root=self.root), "planner: -")

    def test_other_bucket_covers_non_standard_roles(self):
        goal = "T-9500"
        self.write_task("T-9501", role="execute", parent=goal)
        self.write_task("T-9502", role="challenge", parent=goal)
        self.write_runs(
            {"task": "T-9501", "role": "execute", "usd": 2.0, "duration_s": 1,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9502", "role": "challenge", "usd": 5.0, "duration_s": 1,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
        )
        card = scorecard.by_goal(root=self.root)
        g = card[goal]
        self.assertEqual(g["total_usd"], 7.0)
        self.assertEqual(g["roles"]["other"]["usd"], 5.0)
        pct = scorecard.goal_percentages(g)
        self.assertAlmostEqual(pct["other"], 500 / 7, places=3)       # ~71%

    def test_default_output_unchanged(self):
        self.addCleanup(setattr, sys, "argv", sys.argv)
        bus.create_task("default-unchanged", "s", ["a"], ["x.py"], role="execute", complexity=3)
        bus.post_result(bus.read(role="execute")[-1]["id"], {"summary": "ok"})
        sys.argv = ["orchestrator", "scorecard"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "id\tmerged\tfailed\trounds_avg\twall_s\tusd\thits\tscore")
        self.assertGreaterEqual(len(lines), 2)
        for line in lines[1:]:
            self.assertEqual(len(line.split("\t")), 8)

    def test_by_goal_cli_output_has_numeric_cell(self):
        self.addCleanup(setattr, sys, "argv", sys.argv)
        t = bus.create_task("goal-cli-numeric", "s", ["a"], ["x.py"], role="execute", complexity=3,
                             parent="T-9600")
        bus.log_run(task=t["id"], role="execute", usd=2.5, duration_s=1,
                    input_tokens=10, output_tokens=0, cache_read_input_tokens=0)
        sys.argv = ["orchestrator", "scorecard", "--by", "goal"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "goal\tusd\texecute%\treview%\tspec_review%\tscout%\tother%\tplanner_runs")
        row = next(l for l in lines[1:] if l.startswith("T-9600\t"))
        cells = row.split("\t")
        self.assertEqual(len(cells), 8)
        self.assertEqual(float(cells[1]), 2.5)                        # a real numeric cell, not just shape

    def test_malformed_jsonl_line_skipped_and_counted(self):
        self.write_task("T-9800", executor="good", complexity=3, status="done", merged_into="goal/G")
        runs_file = self.root / "runs" / f"{time.strftime('%Y-%m-%d')}.jsonl"
        runs_file.write_text(
            json.dumps({"role": "execute", "executor": "good", "duration_s": 1.0}) + "\n"
            "{not valid json\n"
        )
        card = scorecard.build(root=self.root)
        self.assertEqual(card["good"]["merged"], 1)
        self.assertEqual(scorecard.malformed_run_lines(), 1)
        self.assertEqual(scorecard.malformed_footer(), "malformed run lines skipped: 1")

    def test_malformed_footer_empty_when_no_bad_lines(self):
        self.write_runs({"role": "execute", "executor": "good", "duration_s": 1.0})
        scorecard.build(root=self.root)
        self.assertEqual(scorecard.malformed_run_lines(), 0)
        self.assertEqual(scorecard.malformed_footer(), "")

    def test_by_task_also_skips_malformed_lines(self):
        runs_file = self.root / "runs" / f"{time.strftime('%Y-%m-%d')}.jsonl"
        runs_file.write_text(
            json.dumps({"task": "T-9810", "role": "execute", "usd": 1.0, "duration_s": 1.0,
                        "input_tokens": 5, "output_tokens": 0, "cache_read_input_tokens": 0}) + "\n"
            "]] this is not json [[\n"
        )
        card = scorecard.by_task(root=self.root)
        self.assertEqual(card["T-9810"]["usd"], 1.0)
        self.assertEqual(scorecard.malformed_run_lines(), 1)

    def test_runs_without_usd_reported(self):
        goal = "T-9700"
        self.write_task("T-9701", role="execute", parent=goal)
        self.write_runs(
            {"task": "T-9701", "role": "execute", "usd": 1.0, "duration_s": 1,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9701", "role": "execute", "duration_s": 1,          # codex run: no usd field
             "input_tokens": 20, "output_tokens": 0, "cache_read_input_tokens": 0},
        )
        task_card = scorecard.by_task(root=self.root)
        self.assertEqual(task_card["T-9701"]["runs_no_usd"], 1)
        goal_card = scorecard.by_goal(root=self.root)
        self.assertEqual(goal_card[goal]["runs_no_usd"], 1)

    def test_runs_without_usd_zero_when_all_runs_carry_usd(self):
        goal = "T-9710"
        self.write_task("T-9711", role="execute", parent=goal)
        self.write_runs({"task": "T-9711", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        goal_card = scorecard.by_goal(root=self.root)
        self.assertEqual(goal_card[goal]["runs_no_usd"], 0)

    def write_gate(self, *rows):
        (self.root / "runs" / "jev").mkdir(parents=True, exist_ok=True)
        (self.root / "runs" / "jev" / "gate.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    def test_waste_pct_from_gate_log(self):
        self.write_runs({"task": "T-9900", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        self.write_gate(
            {"ts": 1, "task": "T-9900", "tool": "Read", "p_needed": 0.9, "p_redundant": 0.1,
             "p_destructive": 0.0, "blocked": False, "scored": True},
            {"ts": 2, "task": "T-9900", "tool": "Read", "p_needed": 0.1, "p_redundant": 0.1,
             "p_destructive": 0.0, "blocked": False, "scored": True},   # wasteful: p_needed < 0.3
            {"ts": 3, "task": "T-9900", "tool": "Bash", "p_needed": 0.5, "p_redundant": 0.9,
             "p_destructive": 0.0, "blocked": False, "scored": True},   # wasteful: p_redundant > 0.7
            {"ts": 4, "task": "T-9900", "tool": "Bash", "p_needed": 0.5, "p_redundant": 0.5,
             "p_destructive": 0.0, "blocked": False, "scored": False},  # not scored -- excluded from calls
        )
        card = scorecard.by_task(root=self.root)
        self.assertEqual(card["T-9900"]["calls"], 3)
        self.assertAlmostEqual(card["T-9900"]["waste_pct"], round(200 / 3, 1))

    def test_blocked_count(self):
        self.write_runs({"task": "T-9901", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        self.write_gate(
            {"ts": 1, "task": "T-9901", "tool": "Bash", "p_needed": 0.9, "p_redundant": 0.1,
             "p_destructive": 0.9, "blocked": True, "scored": True},
            {"ts": 2, "task": "T-9901", "tool": "Read", "p_needed": 0.9, "p_redundant": 0.1,
             "p_destructive": 0.0, "blocked": False, "scored": True},
        )
        card = scorecard.by_task(root=self.root)
        self.assertEqual(card["T-9901"]["blocked"], 1)

    def test_turns_column(self):
        self.write_runs(
            {"task": "T-9902", "role": "execute", "usd": 1.0, "duration_s": 1, "turns": 4,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9902", "role": "execute", "usd": 1.0, "duration_s": 1, "turns": 3,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9903", "role": "execute", "usd": 1.0, "duration_s": 1,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},  # no turns field
        )
        card = scorecard.by_task(root=self.root)
        self.assertEqual(card["T-9902"]["turns"], 7)
        self.assertEqual(card["T-9903"]["turns"], "-")

    def test_missing_gate_log_renders_dash(self):
        self.write_runs({"task": "T-9904", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        card = scorecard.by_task(root=self.root)
        self.assertEqual(card["T-9904"]["calls"], "-")
        self.assertEqual(card["T-9904"]["waste_pct"], "-")
        self.assertEqual(card["T-9904"]["blocked"], "-")

    def test_by_goal_gate_and_turns_columns(self):
        goal = "T-9910"
        self.write_task("T-9911", role="execute", parent=goal)
        self.write_task("T-9912", role="execute", parent=goal)
        self.write_runs(
            {"task": "T-9911", "role": "execute", "usd": 1.0, "duration_s": 1, "turns": 2,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
            {"task": "T-9912", "role": "execute", "usd": 1.0, "duration_s": 1, "turns": 5,
             "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0},
        )
        self.write_gate(
            {"ts": 1, "task": "T-9911", "tool": "Read", "p_needed": 0.1, "p_redundant": 0.1,
             "p_destructive": 0.0, "blocked": False, "scored": True},
            {"ts": 2, "task": "T-9912", "tool": "Read", "p_needed": 0.9, "p_redundant": 0.1,
             "p_destructive": 0.9, "blocked": True, "scored": True},
        )
        card = scorecard.by_goal(root=self.root)
        g = card[goal]
        self.assertEqual(g["calls"], 2)
        self.assertAlmostEqual(g["waste_pct"], 50.0)
        self.assertEqual(g["blocked"], 1)
        self.assertEqual(g["turns"], 7)

    def test_by_goal_missing_gate_log_renders_dash(self):
        goal = "T-9920"
        self.write_task("T-9921", role="execute", parent=goal)
        self.write_runs({"task": "T-9921", "role": "execute", "usd": 1.0, "duration_s": 1,
                          "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0})
        card = scorecard.by_goal(root=self.root)
        g = card[goal]
        self.assertEqual(g["calls"], "-")
        self.assertEqual(g["waste_pct"], "-")
        self.assertEqual(g["blocked"], "-")
        self.assertEqual(g["turns"], "-")

    def test_jev_footer_cost(self):
        jev_dir = self.root / "runs" / "jev"
        jev_dir.mkdir(parents=True, exist_ok=True)
        (jev_dir / "2026-09-18.jsonl").write_text(
            "\n".join(json.dumps(l) for l in [
                {"ts": 1, "caller": "scorecard-test", "input_tokens": 500_000, "model": "jev-latest",
                 "latency_ms": 10.0, "ok": True},
                {"ts": 2, "caller": "scorecard-test", "input_tokens": 500_000, "model": "jev-latest",
                 "latency_ms": 10.0, "ok": True},
            ]) + "\n")
        # gate.jsonl sits in the same dir but must not be counted as a jev question
        self.write_gate({"ts": 1, "task": "T-1", "tool": "Read", "p_needed": 0.9, "p_redundant": 0.1,
                          "p_destructive": 0.0, "blocked": False, "scored": True})
        self.assertEqual(scorecard.jev_footer(root=self.root), "jev: 2 questions, 0.042 USD")

    def test_jev_footer_missing_dir(self):
        self.assertEqual(scorecard.jev_footer(root=self.root), "jev: 0 questions, 0.0 USD")

    def test_token_totals_are_int(self):
        self.write_runs(
            {"task": "T-9820", "role": "execute", "duration_s": 1.0,
             "input_tokens": 100.0, "output_tokens": 50.0, "cache_read_input_tokens": 20.0},
        )
        card = scorecard.by_task(root=self.root)
        self.assertIsInstance(card["T-9820"]["tokens"], int)
        self.assertEqual(card["T-9820"]["tokens"], 152)

        self.write_runs({"role": "execute", "executor": "tokfloat", "duration_s": 1.0,
                          "input_tokens": 100.0, "output_tokens": 0, "cache_read_input_tokens": 0})
        card = scorecard.build(root=self.root)
        self.assertIsInstance(card["tokfloat"]["tokens"]["in"], int)

    def write_bench(self, models):
        bench.STATE.mkdir(parents=True, exist_ok=True)
        bench.FILE.write_text(json.dumps({"models": models}))
        self.addCleanup(bench.FILE.unlink, True)

    def test_prior_weights_scales_by_bench_coding_score(self):
        # pool.toml's four codex executors (astra/luna/terra/sol); only two have bench numbers here.
        self.write_bench({"gpt-6-astra": {"coding": 80.0}, "gpt-5.6-luna": {"coding": 40.0}})
        weights = scorecard.prior_weights(P.Pool().executors)
        self.assertEqual(weights["astra"], 1.5)              # 0.5 + 80/80 (max among scored executors)
        self.assertEqual(weights["luna"], 1.0)                # 0.5 + 40/80
        self.assertEqual(weights["terra"], 1.0)                # no bench number -> neutral
        self.assertEqual(weights["sol"], 1.0)

    def test_prior_weights_missing_bench_file_is_neutral(self):
        bench.FILE.unlink(missing_ok=True)
        weights = scorecard.prior_weights(P.Pool().executors)
        self.assertTrue(weights and all(v == 1.0 for v in weights.values()))

    def test_scores_empty_card_returns_priors(self):
        self.write_bench({"gpt-6-astra": {"coding": 80.0}, "gpt-5.6-luna": {"coding": 40.0}})
        weights = scorecard.scores({})
        self.assertEqual(weights["astra"], 1.5)
        self.assertEqual(weights["luna"], 1.0)

    def test_scores_warm_failure_overrides_prior(self):
        self.write_bench({"gpt-6-astra": {"coding": 80.0}})    # astra's prior alone would be 1.5
        card = {"astra": {"merged": 0, "failed": 1}}
        weights = scorecard.scores(card, min_runs=1)
        self.assertLess(weights["astra"], 1.0)                 # live 0/1 record wins over the bench prior

    def test_start_forwards_scorecard_scores_to_pick_executor(self):
        P.PERSIST.unlink(missing_ok=True); self.addCleanup(P.PERSIST.unlink, True)
        t = bus.create_task("scored", "s", ["a"], ["x.py"], role="execute", complexity=3)
        bus.update(t["id"], worktree=str(TMP))
        seen = {}
        orig_run = executor._run
        executor._run = lambda pool, task, args, cwd, timeout, ex=None: seen.update(ex=ex) or {"status": "done"}
        self.addCleanup(lambda: setattr(executor, "_run", orig_run))
        orig_scores = scorecard.scores
        scorecard.scores = lambda card, min_runs=5: {"terra": 3.0}
        self.addCleanup(lambda: setattr(scorecard, "scores", orig_scores))
        executor.start(t["id"], "do it")
        self.assertEqual(seen["ex"].id, "terra")                # weight x score(3.0) beats every other row


class Bench(unittest.TestCase):
    """orchestrator.bench: RSC chunk parsing, display-hint matching, and bench.json's 20h fetch gate.
    No network here -- fetch_html is monkeypatched to the fixture, so importing this module never fetches."""
    FIXTURE = (REPO / "tests" / "fixtures" / "aa_models_sample.html").read_text()

    def setUp(self):
        bench.FILE.unlink(missing_ok=True)
        self.addCleanup(bench.FILE.unlink, True)

    def test_parse_chunks_skips_malformed(self):
        records = bench.parse_chunks(self.FIXTURE)
        self.assertEqual(len(records), 2)
        astra = next(r for r in records if r["name"] == "GPT-6 Astra")
        luna = next(r for r in records if r["name"] == "GPT-5.6 Luna")
        self.assertEqual((astra["intelligence"], astra["speed_tps"]), (70.1, 120.0))
        self.assertEqual((luna["intelligence"], luna["speed_tps"], luna["coding"]), (61.0, 200.0, 55.0))

    def test_match_maps_to_model_ids(self):
        records = bench.parse_chunks(self.FIXTURE)
        hints = {"gpt-6-astra": "GPT-6 Astra", "gpt-5.6-luna": "GPT-5.6 Luna", "gpt-5.6-terra": "GPT-5.6 Terra"}
        matched = bench.match(records, hints)
        self.assertEqual(matched["gpt-6-astra"]["name"], "GPT-6 Astra")
        self.assertEqual(matched["gpt-5.6-luna"]["name"], "GPT-5.6 Luna")
        self.assertIsNone(matched["gpt-5.6-terra"])

    def test_norm_strips_effort_suffix_and_punctuation(self):
        self.assertEqual(bench.norm("GPT-6 Astra (max)"), "gpt-6 astra")
        self.assertEqual(bench.norm("Claude Opus 5 (Adaptive Reasoning, Max Effort)"), "claude opus 5")
        self.assertEqual(bench.norm("  Foo   Bar. "), "foo bar")

    def test_match_handles_effort_suffixed_display_names(self):
        records = [
            {"name": "GPT-6 Astra (max)"},
            {"name": "GPT-5.6 Luna (max)"},
            {"name": "GPT-5.6 Terra (max)"},
            {"name": "GPT-5.6 Sol (max)"},
            {"name": "Claude Opus 5 (Adaptive Reasoning, Max Effort)"},
            {"name": "Claude Fable 5.1 (Adaptive Reasoning, Max Effort, Default Fallback)"},
            {"name": "GPT-5.5 Pro (xhigh)"},
        ]
        matched = bench.match(records, dict(bench.DEFAULT_HINTS, **{"gpt-5.5": "GPT-5.5"}))
        for model_id in ("gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
                          "claude-opus-5", "claude-fable-5-1"):
            self.assertIsNotNone(matched[model_id], model_id)
            self.assertEqual(matched[model_id]["display_name"], records[
                ["gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
                 "claude-opus-5", "claude-fable-5-1"].index(model_id)]["name"])
        # "GPT-5.5 Pro (xhigh)" must not satisfy a "GPT-5.5" hint (prefix over-match guard).
        self.assertIsNone(matched["gpt-5.5"])

    def test_match_prefers_max_variant_over_high(self):
        records = [{"name": "GPT-6 Astra (high)"}, {"name": "GPT-6 Astra (max)"}]
        matched = bench.match(records, {"gpt-6-astra": "GPT-6 Astra"})
        self.assertEqual(matched["gpt-6-astra"]["display_name"], "GPT-6 Astra (max)")
        self.assertEqual(set(matched["gpt-6-astra"]["variants"]), {"GPT-6 Astra (high)", "GPT-6 Astra (max)"})

    def test_match_token_fallback_normalizes_dash_and_space(self):
        records = [{"name": "GPT-5.6 Luna (max)"}]
        matched = bench.match(records, {"gpt-5.6-luna": "GPT 5.6 Luna"})
        self.assertEqual(matched["gpt-5.6-luna"]["display_name"], "GPT-5.6 Luna (max)")

    # fetch()'s fail-closed guard rejects bodies under 10KB; the real fixture is much smaller,
    # so pad it with an HTML comment (parse_chunks ignores it) to clear that floor in tests.
    PADDED_FIXTURE = FIXTURE + ("<!-- " + "x" * 10240 + " -->")

    def test_fetch_force_writes_then_second_call_is_skipped(self):
        orig = bench.fetch_html
        bench.fetch_html = lambda *a, **k: (200, self.PADDED_FIXTURE)
        self.addCleanup(lambda: setattr(bench, "fetch_html", orig))
        result = bench.fetch(force=True, by="tester")
        self.assertEqual(result["provenance"], "web:artificialanalysis.ai (untrusted data)")
        self.assertEqual(result["fetched_by"], "tester")
        self.assertEqual(result["models"]["gpt-6-astra"]["name"], "GPT-6 Astra")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["request"]["impersonate"], False)
        self.assertTrue(bench.FILE.exists())
        self.assertIn("skipped", bench.fetch())          # within 20h, force not given

    def test_fetch_failure_status_leaves_existing_file_untouched(self):
        orig = bench.fetch_html
        bench.fetch_html = lambda *a, **k: (200, self.PADDED_FIXTURE)
        bench.fetch(force=True, by="tester")
        before = bench.FILE.read_bytes()

        bench.fetch_html = lambda *a, **k: (403, "<html>blocked</html>")
        self.addCleanup(lambda: setattr(bench, "fetch_html", orig))
        result = bench.fetch(force=True, by="tester")

        self.assertIn("error", result)
        self.assertEqual(bench.FILE.read_bytes(), before)

    def test_fetch_html_source_disables_impersonation_and_stealth(self):
        src = inspect.getsource(bench.fetch_html)
        self.assertIn("stealthy_headers=False", src)
        self.assertIn("orchestrator-bench", src)

    def test_fetch_html_raises_if_scrapling_drops_a_required_param(self):
        orig = bench._supported_params
        bench._supported_params = lambda func: {"headers", "impersonate", "timeout"}  # no stealthy_headers
        self.addCleanup(lambda: setattr(bench, "_supported_params", orig))
        with self.assertRaises(RuntimeError) as cm:
            bench.fetch_html()
        self.assertIn("stealthy_headers", str(cm.exception))

    def test_fetch_returns_error_and_leaves_bench_json_untouched_if_param_missing(self):
        orig_fetch_html, orig_supported = bench.fetch_html, bench._supported_params
        bench.fetch_html = lambda *a, **k: (200, self.PADDED_FIXTURE)
        bench.fetch(force=True, by="tester")
        before = bench.FILE.read_bytes()

        bench.fetch_html = orig_fetch_html
        bench._supported_params = lambda func: {"headers", "impersonate", "timeout"}
        self.addCleanup(lambda: setattr(bench, "fetch_html", orig_fetch_html))
        self.addCleanup(lambda: setattr(bench, "_supported_params", orig_supported))

        result = bench.fetch(force=True, by="tester")
        self.assertIn("error", result)
        self.assertIn("stealthy_headers", result["error"])
        self.assertEqual(bench.FILE.read_bytes(), before)

    def test_set_model_marks_manual(self):
        rec = bench.set_model("gpt-6-astra", "tester", intelligence=99.0)
        self.assertTrue(rec["manual"])
        self.assertEqual(bench.load()["models"]["gpt-6-astra"]["intelligence"], 99.0)


if __name__ == "__main__":
    unittest.main()
