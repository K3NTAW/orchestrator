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
        result = fields.get("result")
        if "role" not in fields and fields.get("parent") is None and isinstance(result, dict) and any(
                result.get(key) for key in ("pr", "pr_url", "url")):
            base["role"] = "triage"
        (self.root / "tasks" / f"{tid}.json").write_text(json.dumps({**base, **fields}))

    def test_parallelism_concurrency_sweep_and_waits(self):
        self.write_task("G", role="triage", created_at=0, events=[{"status": "done", "ts": 40}])
        self.write_task("A", parent="G", created_at=0, claimed_at=10,
                        pipeline={"first_ready_at": 2, "dispatched_at": 5, "gated_at": 20,
                                  "first_green_at": 21}, merged_at=25)
        self.write_task("B", parent="G", created_at=0, claimed_at=15,
                        pipeline={"first_ready_at": 4, "dispatched_at": 10, "gated_at": 30,
                                  "first_green_at": 31}, merged_at=35)
        self.write_task("R", parent="G", role="review", created_at=20,
                        events=[{"status": "done", "ts": 24}])
        self.write_runs({"task": "A", "role": "execute", "account": "A", "ts": 20, "duration_s": 10},
                        {"task": "B", "role": "execute", "account": "B", "ts": 30, "duration_s": 15},
                        {"task": "B", "role": "execute", "account": "codex", "ts": 30, "duration_s": 30})
        card = scorecard.parallelism(self.root)
        row = card["goals"]["G"]
        for kind in ("executors", "claude_workers"):
            self.assertEqual(row["max_concurrent_" + kind], 2)
            self.assertEqual(row["avg_concurrent_" + kind], 1.25)
        for key, value in {"wall_clock_s": 40, "median_queue_wait_s": 4.5,
                           "median_dependency_wait_s": 3, "median_execution_s": 12.5,
                           "total_execution_s": 25, "median_review_s": 4,
                           "median_merge_wait_s": 4}.items():
            self.assertEqual(row[key], value)
        self.assertEqual(card["totals"]["max_concurrent_executors"], 2)
        self.assertEqual(scorecard._parallelism_sweep([(0, 10), (10, 20), (30, 40)]), (1, 1))

    def test_parallelism_critical_path_over_dag(self):
        for tid, duration, deps in (("A", 10, []), ("B", 20, ["A"]),
                                    ("C", 5, ["A"]), ("D", 7, ["B", "C"])):
            self.write_task(tid, parent="G", claimed_at=0, gated_at=duration, depends_on=deps)
        self.assertEqual(scorecard.parallelism(self.root)["goals"]["G"]["critical_path_s"], 37)
        self.write_task("C", parent="G", depends_on=["A"])
        partial = scorecard.parallelism(self.root)["totals"]
        self.assertEqual(partial["critical_path_s"], 37)
        self.assertTrue(partial["critical_path_partial"])
        self.write_runs({"task": "C", "role": "execute", "account": "codex", "duration_s": 5})
        self.assertEqual(scorecard.parallelism(self.root)["totals"]["critical_path_s"], 37)
        self.write_task("A", parent="G", claimed_at=0, gated_at=10, depends_on=["D"])
        self.assertIsNone(scorecard.parallelism(self.root)["totals"]["critical_path_s"])

    def test_parallelism_critical_path_ignores_unstamped_tasks_off_the_path(self):
        self.write_task("A", parent="G", claimed_at=0, gated_at=10)
        self.write_task("B", parent="G", claimed_at=10, gated_at=30, depends_on=["A"])
        self.write_task("C", parent="G", claimed_at=5)
        row = scorecard.parallelism(self.root)["goals"]["G"]
        self.assertEqual((row["critical_path_s"], row["critical_path_tasks"]), (30, 2))
        self.assertTrue(row["critical_path_partial"])

    def test_parallelism_critical_path_partial_flag(self):
        self.write_task("A", parent="G", claimed_at=0, gated_at=10)
        self.write_task("legacy", parent="G", status="done", merged_into="goal/G", depends_on=["A"])
        self.write_task("B", parent="G", claimed_at=10, gated_at=25, depends_on=["legacy"])
        row = scorecard.parallelism(self.root)["goals"]["G"]
        self.assertEqual((row["critical_path_s"], row["critical_path_tasks"]), (25, 2))
        self.assertTrue(row["critical_path_partial"])
        self.assertIn("critical_path_tasks", scorecard.format_parallelism(scorecard.parallelism(self.root)))

    def test_parallelism_skip_reasons_from_sched_log(self):
        sched = self.root / "runs" / "sched"
        sched.mkdir()
        (sched / "dispatch.jsonl").write_text(json.dumps({"considered": [
            {"task": "A", "goal_id": "G", "action": "skip", "reason": "dependency"},
            {"task": "B", "goal_id": "G", "action": "skip", "reason": "capacity"},
            {"task": "C", "goal_id": "H", "action": "skip", "reason": "capacity"},
            {"task": "D", "goal_id": "G", "action": "dispatch", "reason": "ready"}]}) + "\ninvalid\n[]\n")
        (sched / "waves.jsonl").write_text('{"applied": true}\n{"applied": false}\n')
        (sched / "stale.jsonl").write_text('{"task":"A","goal_id":"G","risk":"high"}\n')
        self.write_task("A", parent="G", claimed_at=0, gated_at=10,
                        reason="rebase_conflict", last_merge={"status": "conflict"},
                        failure_kind="rebase_failed", pipeline={"stale_check": {"risk": "high"}})
        self.write_task("B", parent="G", merged_at=5)
        self.write_task("F", parent="G", constraints={"fix_round_for": "A"})
        card = scorecard.parallelism(self.root)
        self.assertEqual(card["skip_reasons"], {"dependency": 1, "capacity": 2})
        self.assertEqual(card["waves"], {"rows": 2, "applied": 1})
        self.assertEqual(card["malformed"], 2)
        for key in ("merge_conflicts", "rebase_failures", "stale_work_events", "fix_rounds",
                    "fix_rounds_after_concurrent_merge"):
            self.assertEqual(card["totals"][key], 1)
        filtered = scorecard.parallelism(self.root, goal="G")
        self.assertEqual(set(filtered["goals"]), {"G"})
        self.assertEqual(filtered["skip_reasons"], {"dependency": 1, "capacity": 1})

    def test_parallelism_skip_reasons_count_skipped_rows(self):
        sched = self.root / "runs" / "sched"
        sched.mkdir()
        (sched / "dispatch.jsonl").write_text(json.dumps({"ts": 123, "free_slots": 1,
            "fallback": False, "running_execute": 0, "running_claude": 0, "considered": [
                {"task": "T-2", "goal_id": "G", "ready": True, "action": "skipped",
                 "reason": "reserved_for_critical:T-1"}]}) + "\n")
        self.assertEqual(scorecard.parallelism(self.root)["skip_reasons"], {"reserved_for_critical:T-1": 1})

    def test_parallelism_missing_files_are_zero(self):
        self.write_task("A", parent="G")
        card = scorecard.parallelism(self.root)
        self.assertEqual(card["waves"], {"rows": 0, "applied": 0})
        self.assertEqual(card["skip_reasons"], {})
        self.assertEqual(card["totals"]["stale_work_events"], 0)
        self.assertIsNone(card["totals"]["median_queue_wait_s"])
        self.assertIsNone(card["totals"]["critical_path_s"])
        self.assertIn("undefined", scorecard.format_parallelism(card))

    def test_scorecard_planner_text_and_json(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from orchestrator import planner_runs as PR
        config_dir = self.root / "claude"
        project = config_dir / "projects" / P.encode_project_dir(str(PR.ROOT.resolve()))
        project.mkdir(parents=True)
        self.root.joinpath("pool.toml").write_text(
            '[[claude_accounts]]\nid = "A"\nconfig_dir = ' + json.dumps(str(config_dir)) + '\n')
        transcript = json.dumps({"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                                "message": {"usage": {"input_tokens": 50, "output_tokens": 10,
                                                      "cache_read_input_tokens": 100}}}) + "\n"
        (project / "session.jsonl").write_text(transcript + transcript)
        (self.root / "planner_usage.json").write_text(json.dumps({"A": {"offsets": {
            "session.jsonl": len(transcript.encode())}}}))
        rows = [{"goal_id": "G", "kind": "held", "route": "escalate", "reason": "review",
                 "status": "exited_ok", "started_at": time.time(), "input_tokens": 100,
                 "output_tokens": 20, "cache_read_input_tokens": 100, "usd": 1} for _ in range(3)]
        (self.root / "runs" / "planner_runs.json").write_text(json.dumps(rows))
        for json_output in (False, True):
            output = io.StringIO()
            argv = ["orchestrator", "scorecard", "--planner"] + (["--json"] if json_output else [])
            with patch.object(sys, "argv", argv), patch.object(scorecard, "STATE", self.root), \
                    contextlib.redirect_stdout(output):
                cli.main()
            if json_output:
                summary = json.loads(output.getvalue())
                self.assertEqual(summary["headless"]["count"], 3)
                self.assertEqual(summary["interactive"]["sessions_count"], 1)
                self.assertEqual(summary["interactive"]["sessions"][0]["input_tokens"], 50)
                self.assertEqual(summary["interactive"]["day_totals"][0]["cache_read_tokens"], 100)
                self.assertEqual(summary["exceptions"][0]["goal_id"], "G")
            else:
                for value in ("3 invocations", "mean input 100", "output 60", "cache share 50.0%",
                              "usd 3.00", "top reasons: review=3", "interactive A session",
                              "soft-budget exception G", "advisory"):
                    self.assertIn(value, output.getvalue())
        self.write_task("T-child", parent="G")
        card = scorecard.by_goal(self.root)
        self.assertEqual(card["G"]["routes"], {"escalate": 3})

    def write_runs(self, *lines):
        (self.root / "runs" / f"{time.strftime('%Y-%m-%d')}.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n")

    def test_tokens_per_accepted_goal_undefined_at_zero(self):
        from unittest.mock import patch
        self.write_task("T-goal", role="triage")
        self.write_task("T-child", parent="T-goal")
        self.write_runs({"task": "T-child", "role": "execute", "total_tokens": 42})
        result = scorecard.tokens_per_accepted_goal(self.root)
        self.assertIsNone(result["tokens"])
        self.assertEqual(result["count"], 0)
        card = scorecard.by_goal(self.root)
        self.assertEqual(card["T-goal"]["total_tokens"], 42)
        for json_output in (False, True):
            output = io.StringIO()
            argv = ["orchestrator", "scorecard", "--by", "goal"] + (["--json"] if json_output else [])
            with patch.object(sys, "argv", argv), patch.object(scorecard, "STATE", self.root), \
                    patch.object(scorecard, "by_goal", return_value=card), contextlib.redirect_stdout(output):
                cli.main()
            if json_output:
                self.assertIsNone(json.loads(output.getvalue())["tokens_per_accepted_goal"]["tokens"])
            else:
                self.assertIn("tokens per accepted goal: undefined (0 accepted goals)", output.getvalue())
                self.assertIn("n=1 range 42-42", output.getvalue())
        self.write_task("T-goal", role="triage", status="done")
        self.write_task("T-child", parent="T-goal", merged_into="goal/G")
        self.assertEqual(scorecard.tokens_per_accepted_goal(self.root)["tokens"], 42)

    def test_tokens_of_discounts_cache_read_on_normalised_rows(self):
        row = {"total_tokens": 1_000, "input_uncached_tokens": 100,
               "cache_read_tokens": 200, "cache_write_tokens": 30, "output_tokens": 10}
        self.assertEqual(scorecard._tokens_of(row), 160)

    def test_by_goal_buckets_exact_for_normalised_rows(self):
        self.write_task("T-goal")
        self.write_task("T-child", parent="T-goal")
        self.write_runs({"task": "T-child", "role": "execute", "total_tokens": 1_000,
                         "input_uncached_tokens": 100, "cache_read_tokens": 200,
                         "cache_write_tokens": 30, "output_tokens": 10})
        row = scorecard.by_goal(self.root)["T-goal"]
        self.assertEqual((row["tokens_uncached"], row["tokens_cache_read"],
                          row["tokens_cache_write"], row["tokens_output"]), (100, 200, 30, 10))
        self.assertEqual(row["total_tokens"], 160)

    def test_by_goal_median_and_range_small_sample(self):
        rows = []
        for i, amount in enumerate((10, 20, 30, 40, 100)):
            tid = f"T-child{i}"
            self.write_task(tid, parent="T-goal")
            # Include a legacy total-only row and a retry for the same task.
            rows.append({"task": tid, "role": "execute", "total_tokens": amount - 2})
            rows.append({"task": tid, "role": "execute", **bus.normalize_usage("codex", {"output_tokens": 2})})
            self.write_runs(*rows)
            entry = scorecard.by_goal(self.root)["T-goal"]
            self.assertEqual(entry["n_tasks"], i + 1)
            self.assertEqual(entry["tokens_max_per_task"], amount)
            if i < 4:
                self.assertEqual(scorecard.format_task_tokens_cell(entry), f"n={i + 1} range 10-{amount}")
        self.assertEqual(entry["tokens_median_per_task"], 30)
        self.assertEqual(scorecard.format_task_tokens_cell(entry), "30")
        self.assertEqual(entry["total_tokens"], 200)

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

    def test_task_class_inference(self):
        self.assertEqual(scorecard.task_class({"constraints": {"task_class": "security"}}), "security")
        self.assertEqual(scorecard.task_class({"scope": ["docs/design.md"], "complexity": 7}), "architectural")
        self.assertEqual(scorecard.task_class({"title": "Fix timeout", "complexity": 1}), "debugging")
        self.assertEqual(scorecard.task_class({"title": "new", "complexity": 3}), "mechanical")
        self.assertEqual(scorecard.task_class({"title": "new", "complexity": 4}), "unfamiliar")

    def test_class_success_is_per_class(self):
        for n in range(3):
            self.write_task(f"T-mech{n}", executor="good", status="done", merged_into="goal/G",
                            constraints={"task_class": "mechanical"})
            self.write_task(f"T-sec{n}", executor="good", status="failed" if n else "done",
                            merged_into="goal/G" if n == 0 else None,
                            constraints={"task_class": "security"})
        self.write_runs(*([{"task": f"T-mech{n}", "role": "execute"} for n in range(3)] +
                          [{"task": f"T-sec{n}", "role": "execute"} for n in range(3)]))
        self.assertEqual(scorecard.class_success("good", "mechanical", self.root), 1.0)
        self.assertAlmostEqual(scorecard.class_success("good", "security", self.root), 1 / 3)

    def test_class_sample_size_matches_class_success_filter(self):
        for tid, fields in (
                ("T-merged", {"executor": "good", "merged_into": "goal/G"}),
                ("T-failed", {"executor": "good", "status": "failed"}),
                ("T-unresolved", {"executor": "good"}),
                ("T-other", {"executor": "other", "merged_into": "goal/G"}),
                ("T-review", {"executor": "good", "role": "review", "merged_into": "goal/G"})):
            self.write_task(tid, constraints={"task_class": "mechanical"}, **fields)
        self.write_runs(*[{"task": tid, "role": "execute"}
                          for tid in ("T-merged", "T-failed", "T-unresolved", "T-other", "T-review")])
        self.assertEqual(scorecard.class_sample_size("good", "mechanical", self.root), 2)
        self.assertEqual(scorecard.class_success("good", "mechanical", self.root, min_samples=1), .5)
        self.assertIsNone(scorecard.class_success("good", "mechanical", self.root))

    def test_expected_cost_needs_samples(self):
        for n in range(2):
            self.write_task(f"T-ec{n}", executor="cheap", status="done", merged_into="goal/G")
        self.write_runs(*[{"task": f"T-ec{n}", "role": "execute", "input_tokens": 100} for n in range(2)])
        self.assertIsNone(scorecard.expected_cost("cheap", "mechanical", self.root, min_samples=3))

    def test_expected_cost_includes_repairs(self):
        for n in range(3):
            self.write_task(f"T-base{n}", executor="cheap", status="done", merged_into="goal/G")
        self.write_task("T-fix", executor="cheap", status="done", merged_into="goal/G",
                        constraints={"fix_round_for": "T-base0"})
        self.write_task("T-review", role="review", constraints={"review_for": "T-base0"})
        self.write_task("T-spec", role="spec_review", constraints={"spec_review_for": "T-base0"})
        self.write_runs(
            *[{"task": f"T-base{n}", "role": "execute", "input_tokens": 100} for n in range(3)],
            {"task": "T-fix", "role": "execute", "input_tokens": 60},
            {"task": "T-review", "role": "review", "input_tokens": 10},
            {"task": "T-spec", "role": "spec_review", "input_tokens": 20},
        )
        self.assertEqual(scorecard.expected_cost("cheap", "mechanical", self.root, min_samples=3), 150)

    def test_review_verdict_rolls_up(self):
        self.write_task("T-9003", executor="reviewed-by", status="done", review_verdict="request_changes")
        card = scorecard.build(root=self.root)
        self.assertEqual(card["reviewed-by"]["review_request_changes"], 1)

    def _routing_fixture(self):
        self.write_task("T-route-a", executor="worker-a", merged_into="main", complexity=2,
                        accepted_at=200, pipeline={"first_green_at": 150, "gate_reds": 0},
                        lineage_fix_rounds=0, constraints={"task_class": "mechanical"})
        self.write_task("T-route-b", executor="worker-b", status="failed", complexity=7,
                        pipeline={"gate_reds": 2}, lineage_fix_rounds=1,
                        constraints={"task_class": "architectural"})
        self.write_task("T-route-review", role="review", inputs=["T-route-b"],
                        review_verdict="request_changes")
        self.write_runs(
            {"task": "T-route-a", "role": "execute", "input_tokens": 100, "usd": 1},
            {"task": "T-route-b", "role": "execute", "input_tokens": 200, "usd": 2},
            {"task": "T-route-a", "goal_id": "G", "role": "jev_route", "mode": "shadow",
             "eligible": ["worker-a", "worker-b"], "baseline": "worker-a", "hypothetical": "worker-a",
             "signals": {"risk": {"p": .8}}, "latency_ms": 10, "usage": {"tokens": 5, "usd": .01}},
            {"task": "T-route-b", "goal_id": "G", "role": "jev_route", "mode": "shadow",
             "eligible": ["worker-a", "worker-b"], "baseline": "worker-b", "hypothetical": "worker-a",
             "signals": {"risk": {"p": .4}}, "latency_ms": 30, "usage": {"tokens": 7, "usd": .02},
             "reason": "budget"})
        return scorecard.routing_eval(self.root, min_samples=2)

    def test_routing_eval_joins_rows_to_outcomes_by_lineage_root(self):
        card = self._routing_fixture()
        rows = {row["task"]: row for row in card["rows"]}
        self.assertTrue(rows["T-route-a"]["accepted"])
        self.assertEqual((rows["T-route-a"]["executor"], rows["T-route-a"]["tokens"]), ("worker-a", 100))
        self.assertEqual(rows["T-route-b"]["review_request_changes"], 1)

    def test_routing_eval_agree_vs_disagree_groups_and_defined_counts(self):
        groups = self._routing_fixture()["groups"]
        self.assertEqual((groups["agree"]["n"], groups["disagree"]["n"]), (1, 1))
        self.assertEqual(groups["agree"]["first_pass_defined_count"], 1)
        self.assertEqual(groups["disagree"]["gate_red_defined_count"], 1)

    def test_routing_eval_per_signal_threshold_split(self):
        signal = self._routing_fixture()["groups"]["signals"]["risk"]
        self.assertEqual((signal["p>=0.6"]["n"], signal["p<0.6"]["n"]), (1, 1))

    def test_routing_eval_coverage_and_skip_reasons(self):
        card = self._routing_fixture()
        self.assertEqual(card["coverage"], {"classified": 2, "execute_dispatches": 2, "share": 1})
        self.assertEqual(card["skip_reasons"], {"budget": 1})
        self.assertEqual((card["jev_latency_ms"]["median"], card["jev_usage"]["tokens"]), (20, 12))

    def test_routing_eval_insufficient_below_min_samples(self):
        self.assertEqual(self._routing_fixture()["evidence_verdict"], "insufficient")

    def test_routing_eval_none_when_no_rows(self):
        self.write_task("T-no-route", executor="worker")
        self.write_runs({"task": "T-no-route", "role": "execute"})
        self.assertIsNone(scorecard.routing_eval(self.root))

    def _economics_fixture(self):
        self.write_task("T-root", executor="cheap", status="done", merged_into="goal/G", complexity=4,
                        pipeline={"first_green_at": "2026-01-01T00:00:00Z", "gate_reds": 0})
        self.write_task("T-failed", executor="cheap", status="failed", complexity=4,
                        pipeline={"gate_reds": 1, "failure_kind": "lint"})
        self.write_task("T-fix", executor="cheap", status="done", constraints={"fix_round_for": "T-failed"})
        self.write_task("T-review", role="review", inputs=["T-root"], review_verdict="request_changes")
        self.write_runs(
            {"task": "T-root", "role": "execute", "executor": "cheap", "input_tokens": 100, "usd": 2},
            {"task": "T-failed", "role": "execute", "executor": "cheap", "input_tokens": 200, "usd": 3},
            {"task": "T-fix", "role": "execute", "executor": "cheap", "input_tokens": 50, "usd": 1},
            {"task": "T-review", "role": "review", "input_tokens": 10, "usd": .5})
        return scorecard.executor_economics(self.root)

    def test_executor_economics_first_pass_and_fix_round_probability(self):
        row = self._economics_fixture()["cheap"]
        self.assertEqual(row["first_pass_green_rate"], 1)
        self.assertEqual(row["first_pass_defined_count"], 1)
        self.assertEqual(row["fix_round_probability"], .5)

    def test_executor_economics_tokens_and_cost_to_accepted_median(self):
        row = self._economics_fixture()["cheap"]
        self.assertEqual(row["initial_execution_tokens"], {"median": 150.0, "mean": 150})
        self.assertEqual((row["tokens_to_accepted"], row["cost_to_accepted"]), (110, 2.5))

    def test_executor_economics_gate_failure_reasons_histogram(self):
        self.assertEqual(self._economics_fixture()["cheap"]["gate_failure_reasons"], {"lint": 1})

    def test_executor_economics_review_request_changes_rate(self):
        row = self._economics_fixture()["cheap"]
        self.assertEqual((row["review_request_changes_rate"], row["review_request_changes_defined_count"]), (1, 1))

    def test_executor_economics_by_band_and_class(self):
        self._economics_fixture()
        self.assertIn(("cheap", "4-6"), scorecard.executor_economics(self.root, by="band"))
        self.assertIn(("cheap", "unfamiliar"), scorecard.executor_economics(self.root, by="class"))

    def test_scores_rank_by_cost_to_accepted_falls_back_below_min_samples(self):
        card = {"cheap": {"merged": 1, "failed": 0, "cost_to_accepted": 2,
                           "cost_to_accepted_defined_count": 2}}
        self.assertEqual(scorecard.scores(card, min_runs=1, rank_by="cost_to_accepted")["cheap"],
                         scorecard.scores(card, min_runs=1)["cheap"])

    def test_scores_default_rank_unchanged(self):
        card = {"x": {"merged": 3, "failed": 2}}
        self.assertEqual(scorecard.scores(card), scorecard.scores(card, rank_by="success"))

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
        self.assertEqual(
            lines[0],
                "goal\tusd\texecute%\treview%\tspec_review%\tscout%\tother%\tplanner_runs\ttotal_tokens\tuncached\tcache_read\toutput\tjev\tplanner\troute\tcalls\twaste_pct\tturns",
        )
        row = next(l for l in lines[1:] if l.startswith("T-9600\t"))
        cells = row.split("\t")
        self.assertEqual(len(cells), 18)
        self.assertEqual(float(cells[1]), 2.5)                        # a real numeric cell, not just shape
        self.assertEqual(cells[-3:], ["-", "-", "-"])

    def test_by_goal_token_buckets(self):
        self.write_task("T-9930", status="done", result={"pr": "https://github.com/acme/repo/pull/3"})
        self.write_task("T-9931", parent="T-9930", role="execute")
        self.write_task("T-9932", parent="T-9930", role="review", status="failed")
        self.write_task("T-9940", status="queued")
        self.write_task("T-9941", parent="T-9940", role="execute")  # no run rows must still appear
        self.write_runs(
            {"task": "T-9931", "role": "execute", "input_tokens": 100, "output_tokens": 20,
             "cache_read_input_tokens": 100},
            {"task": "T-9932", "role": "review", "input_tokens": 30},  # Codex/no-usd failure
        )
        (self.root / "runs" / "jev").mkdir()
        (self.root / "runs" / "jev" / "2026-01-01.jsonl").write_text("\n".join(json.dumps(row) for row in (
            {"goal_id": "T-9930", "caller": "gate", "input_tokens": 7},
            {"goal_id": "T-9930", "caller": "rank", "input_tokens": 11},
        )))
        (self.root / "runs" / "planner_runs.json").write_text(json.dumps([
            {"goal_id": "T-9930", "tokens": 5},
        ]))
        card = scorecard.by_goal(root=self.root)
        row = card["T-9930"]
        self.assertEqual(row["tokens_by_role"], {"execute": 130, "review": 30, "spec_review": 0, "scout": 0, "other": 0})
        self.assertEqual((row["tokens_uncached"], row["tokens_cache_read"], row["tokens_output"]), (130, 100, 20))
        self.assertEqual((row["jev_tokens"], row["jev_tokens_gate"], row["jev_tokens_rank"]), (18, 7, 11))
        self.assertEqual((row["planner_tokens"], row["failed_tokens"], row["total_tokens"]), (5, 30, 183))
        self.assertEqual(card["T-9940"]["total_tokens"], 0)

    def test_tokens_per_accepted_goal(self):
        self.write_task("T-9950", role="triage", status="done", result={"url": "https://github.com/acme/repo/pull/5"})
        self.write_task("T-9951", parent="T-9950", role="execute")
        self.write_task("T-9960", status="queued")
        self.write_task("T-9961", parent="T-9960", role="execute")
        self.write_runs(
            {"task": "T-9951", "role": "execute", "usd": 2, "input_tokens": 20},
            {"task": "T-9961", "role": "execute", "usd": 9, "input_tokens": 90},
        )
        self.assertEqual(scorecard.accepted_goals(root=self.root), ["T-9950"])
        self.assertEqual(scorecard.tokens_per_accepted_goal(root=self.root),
                         {"tokens": 20.0, "count": 1, "goal_ids": ["T-9950"]})
        self.assertEqual(scorecard.usd_per_accepted_goal(root=self.root)["usd"], 2.0)

    def test_accepted_goals_ignores_non_goal_tasks_with_pr_url(self):
        self.write_task("T-goal", role="triage", result={"pr_url": "https://example.test/pull/1"})
        self.write_task("T-execute", merged_into="main", pr_url="https://example.test/pull/2")
        self.assertEqual(scorecard.accepted_goals(root=self.root), ["T-goal"])

    def test_tokens_per_accepted_goal_scans_runs_once(self):
        from unittest.mock import patch
        self.write_task("T-goal", role="triage", result={"pr_url": "https://example.test/pull/1"})
        self.write_task("T-child", parent="T-goal", merged_into="main")
        self.write_runs({"task": "T-child", "role": "execute", "total_tokens": 42})
        original = scorecard._efficiency_rows
        calls = []
        with patch.object(scorecard, "_efficiency_rows",
                          side_effect=lambda *args: calls.append(args) or original(*args)):
            self.assertEqual(scorecard.tokens_per_accepted_goal(self.root)["tokens"], 42)
        self.assertEqual(len(calls), 1)

    def test_scorecard_by_goal_footer_unaffected_by_efficiency(self):
        runs_file = self.root / "runs" / f"{time.strftime('%Y-%m-%d')}.jsonl"
        runs_file.write_text('{not valid json\n')
        scorecard.efficiency(self.root)
        self.assertEqual(scorecard.malformed_run_lines(), 0)
        scorecard.by_goal(self.root)
        self.assertEqual(scorecard.malformed_run_lines(), 1)
        scorecard.efficiency(self.root)
        self.assertEqual(scorecard.malformed_run_lines(), 1)

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


class Efficiency(unittest.TestCase):
    write_task = Scorecard.write_task
    write_runs = Scorecard.write_runs

    def setUp(self):
        Scorecard.setUp(self)
        self.write_task("T-goal", role="goal", pr_url="https://example.test/pull/1")
        self.write_task("T-root", parent="T-goal", merged_into="main", executor="worker", complexity=5,
                        created_at=100, accepted_at=160, pipeline={"first_green_at": 130, "gate_reds": 0})
        self.write_task("T-fix", parent="T-goal", constraints={"fix_round_for": "T-root"})
        self.write_task("T-review", parent="T-goal", role="review", inputs=["T-fix"])
        self.write_runs(
            {"task": "T-root", "role": "execute", "total_tokens": 100, "ts": 110, "usd": 1, "model": "m", "turns": 2},
            {"task": "T-fix", "role": "execute", "total_tokens": 20, "ts": 120, "usd": 2, "model": "m"},
            {"task": "T-review", "role": "review", "total_tokens": 30, "ts": 140, "usd": 3},
            {"role": "planner", "goal_id": "T-goal", "total_tokens": 10},
            {"total_tokens": 7})

    def test_efficiency_legacy_rows_attributed_at_read_time(self):
        tasks = {"T-root": {"id": "T-root"}, "T-fix": {"id": "T-fix", "constraints": {"fix_round_for": "T-root"}}}
        row = scorecard._attributed({"task": "T-fix", "role": "execute"}, tasks)
        self.assertEqual((row["bucket"], row["lineage_root"], row["round_index"]), ("fix_round", "T-root", 1))
        self.assertIs(scorecard._attributed(row, tasks), row)

    def test_efficiency_collapses_fix_round_lineage_into_root(self):
        jev = self.root / "runs/jev"
        jev.mkdir()
        (jev / "usage.jsonl").write_text(json.dumps({"task": "T-fix", "input_tokens": 4}) + "\n")
        card = scorecard.efficiency(self.root)
        self.assertEqual(set(card["tasks"]), {"T-root"})
        task = card["tasks"]["T-root"]
        self.assertEqual((task["tokens"], task["usd"], task["calls"], task["turns"]), (154, 6, 4, 2))
        self.assertEqual(task["fix_round_tokens"], 20)

    def test_efficiency_first_pass_and_fix_round_rates(self):
        self.write_task("T-clean", merged_into="main", pipeline={"first_green_at": 120, "gate_reds": 0}, lineage_fix_rounds=0)
        card = scorecard.efficiency(self.root)
        self.assertEqual((card["first_pass_rate"], card["fix_round_rate"], card["avg_fix_rounds"]), (.5, .5, .5))
        self.assertEqual(card["first_pass_defined_count"], 2)
        self.assertEqual(card["fix_round_defined_count"], 2)

    def test_efficiency_tokens_and_time_to_first_green_use_stamps(self):
        task = scorecard.efficiency(self.root)["tasks"]["T-root"]
        self.assertEqual((task["tokens_to_first_green"], task["time_to_first_green_s"], task["time_to_accepted_s"]), (120, 30, 60))
        self.assertEqual(scorecard._stamp("1970-01-01T00:02:10Z"), 130)

    def test_efficiency_missing_stamps_give_none_not_crash(self):
        self.write_task("T-root", merged_into="main")
        task = scorecard.efficiency(self.root)["tasks"]["T-root"]
        for key in ("tokens_to_first_green", "time_to_first_green_s", "time_to_accepted_s", "first_pass"):
            self.assertIsNone(task[key])
        self.assertEqual(task["fix_rounds"], 1)

    def test_efficiency_amplification_breakdown_sums_to_total_and_none_at_zero_execution(self):
        card = scorecard.efficiency(self.root)
        breakdown = card["breakdown"]
        self.assertEqual(list(breakdown), ["Planner", "Scout", "Execution", "Fix rounds", "Spec review", "Code review", "Challenge", "Jev", "Other", "Total", "Amplification"])
        self.assertEqual(sum(breakdown[key] for key in list(breakdown)[:9]), 167)
        self.assertEqual(card["pipeline_amplification"], 1.67)
        self.write_runs({"role": "review", "total_tokens": 2})
        self.assertIsNone(scorecard.efficiency(self.root)["pipeline_amplification"])

    def test_efficiency_unknown_bucket_is_reported(self):
        card = scorecard.efficiency(self.root, by="goal")
        self.assertEqual(card["groups"]["unknown"]["tokens"], 7)
        self.assertEqual(sum(g["tokens"] for g in card["groups"].values()), card["tokens"])

    def test_efficiency_goal_total_matches_tokens_per_accepted_goal(self):
        card = scorecard.efficiency(self.root)
        self.assertEqual(card["goals"]["T-goal"]["tokens"], scorecard.tokens_per_accepted_goal(self.root)["tokens"])
        self.assertEqual(card["usd_per_accepted_goal"], scorecard.usd_per_accepted_goal(self.root)["usd"])

    def test_efficiency_group_by_band_class_executor_role(self):
        for by, key in (("band", "4-6"), ("class", "unfamiliar"), ("executor", "worker"), ("role", "execute")):
            card = scorecard.efficiency(self.root, by=by)
            self.assertEqual(card["groups"][key]["tokens"], 120 if by == "role" else 150)
            self.assertEqual(sum(g["tokens"] for g in card["groups"].values()), 167)
            self.assertIn("unknown", card["groups"])

    def test_efficiency_model_distribution(self):
        model = scorecard.efficiency(self.root)["model_distribution"]["m"]
        self.assertEqual(model, {"execute": {"rows": 1, "tokens": 100}, "fix_round": {"rows": 1, "tokens": 20}})

    def _review(self, tid, packet=None, pass_index=1, comments=(), tokens=100):
        self.write_task(tid, role="review", status="done", inputs=["T-root"],
                        result={"verdict": "request_changes", "comments": list(comments)},
                        packet_version=packet, review_pass_index=pass_index)
        self.write_runs({"task": tid, "role": "review", "total_tokens": tokens, "usd": 1,
                         "packet_version": packet, "review_pass_index": pass_index})

    def test_review_quality_counts_verdicts_and_findings_by_severity(self):
        self._review("T-review-high", comments=[{"path": "a.py", "line": 1, "issue": "bug", "severity": "high"}])
        row = scorecard.review_quality(self.root)["general"]
        self.assertEqual((row["n_reviews"], row["verdicts"]["request_changes"]), (2, 1))
        self.assertEqual(row["findings_by_severity"]["high"], 1)

    def test_review_quality_distinct_defects_and_overlap_for_two_reviews(self):
        self.write_task("T-review", role="review", status="done", inputs=["T-root"], result={"verdict": "request_changes", "comments": [
            {"path": "same.py", "line": 2, "issue": "shared defect"}]}, review_pass_index=1)
        self._review("T-review-2", pass_index=2, comments=[
            {"path": "same.py", "line": 2, "issue": "different wording"},
            {"path": "new.py", "line": 3, "issue": "new defect"}])
        row = scorecard.review_quality(self.root)["general"]
        self.assertEqual((row["distinct_defects"], row["overlap_share"], row["second_review_added"]), (2, .5, 1))

    def test_review_quality_groups_by_packet_version_with_pre_packet_bucket(self):
        self._review("T-review-packet", packet="p2")
        self.assertEqual(set(scorecard.review_quality(self.root, by="packet_version")), {"pre-packet", "p2"})

    def test_review_quality_findings_per_million_tokens_and_none_when_no_tokens(self):
        self._review("T-review-token", comments=[{"issue": "one"}], tokens=500_000)
        self.write_task("T-review-empty", role="review", inputs=["T-root"], result={"verdict": "approve"})
        by_packet = scorecard.review_quality(self.root, by="packet_version")
        self.assertGreater(by_packet["pre-packet"]["findings_per_million_tokens"], 0)
        self.write_task("T-only", role="review", inputs=["T-root"], result={"verdict": "approve", "packet_version": "empty"})
        self.assertIsNone(scorecard.review_quality(self.root, by="packet_version")["empty"]["findings_per_million_tokens"])
