import _harness
"""orchestrator.cli: `status`, `scorecard` and `pick` subcommands, plain-text and JSON output."""
import contextlib, io, json, os, sys, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_cli.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import bus, cli
from orchestrator import pool as P


class Cli(unittest.TestCase):
    def test_workers_cli_lists_active(self):
        from orchestrator import worker_registry as registry
        task = bus.create_task("cli worker", "s", ["a"], ["x.py"])
        tid = task["id"]
        self.addCleanup(registry._path(tid).unlink, missing_ok=True)
        self.addCleanup(registry._path(tid, events=True).unlink, missing_ok=True)
        registry.upsert(tid, status="running", role="execute")
        for _ in range(25):
            registry.event(tid, "stage", stage="execute")
        def output(*args):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["orchestrator", "workers", *args]), contextlib.redirect_stdout(out):
                cli.main()
            return out.getvalue()
        self.assertIn(tid, [r["task"] for r in json.loads(output("--json"))])
        detail = json.loads(output("--task", tid, "--json"))
        self.assertEqual(detail["task"], tid)
        self.assertEqual(len(detail["events"]), 20)
        self.assertIn("task\trole\tmodel\tprovider", output())
        registry.finish(tid, "done")
        self.assertNotIn(tid, [r["task"] for r in json.loads(output("--json"))])
        self.assertIn(tid, [r["task"] for r in json.loads(output("--all", "--json"))])

    def test_memory_search_cli(self):
        import shutil
        source = Path(__file__).resolve().parents[1] / ".orchestrator" / "memory"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / ".orchestrator" / "memory"
            target.mkdir(parents=True)
            for name in ("decisions.md", "gotchas.md", "architecture.md", "model-notes.md"):
                shutil.copy2(source / name, target / name)
            with mock.patch.object(cli, "ROOT", root), mock.patch.object(sys, "argv", [
                    "orchestrator", "memory", "migrate"]), contextlib.redirect_stdout(io.StringIO()):
                cli.main()
            output = io.StringIO()
            with mock.patch.object(cli, "ROOT", root), mock.patch.object(sys, "argv", [
                    "orchestrator", "memory", "search", "daemon", "--json"]), contextlib.redirect_stdout(output):
                cli.main()
            self.assertTrue(json.loads(output.getvalue()))

    def test_cli_roadmap_status_writes_json(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            config = root / ".orchestrator/pool.toml"
            config.parent.mkdir(parents=True)
            config.write_text("")
            output = io.StringIO()
            with mock.patch.object(cli, "ROOT", root), \
                    mock.patch.object(sys, "argv", ["orchestrator", "roadmap-status", "--write"]), \
                    contextlib.redirect_stdout(output):
                cli.main()
            path = root / ".orchestrator/roadmap-status.json"
            report = json.loads(path.read_text())
            self.assertIn("summary", report)
            self.assertEqual(sum(report["summary"].values()), len(report["requirements"]))
            self.assertIn(str(path), output.getvalue())

    def test_scorecard_parallelism_flag(self):
        from orchestrator import scorecard
        with tempfile.TemporaryDirectory() as directory:
            for args in (["--parallelism", "--goal", "G"], ["--parallelism", "--json"]):
                output = io.StringIO()
                with mock.patch.object(scorecard, "STATE", Path(directory)), \
                        mock.patch.object(sys, "argv", ["orchestrator", "scorecard", *args]), \
                        contextlib.redirect_stdout(output):
                    cli.main()
                if "--json" in args:
                    self.assertIn("totals", json.loads(output.getvalue()))
                else:
                    self.assertIn("max_concurrent_executors", output.getvalue())
                    self.assertIn("undefined", output.getvalue())
            for args in (["--efficiency"], ["--economics"], ["--routing"], ["--reviews"],
                         ["--planner"], ["--by", "executor"]):
                with mock.patch.object(sys, "argv", ["orchestrator", "scorecard", "--parallelism", *args]), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)

    def test_scorecard_overhead_cli(self):
        from orchestrator import overhead
        rows = [{"goal_id": "T-1", "orchestration_tokens": 10, "execution_tokens": 20,
                 "amplification": .5, "orchestration_usd": 1, "goal_usd": 3, "cost_share": 1 / 3,
                 "orchestration_s": 2, "goal_s": 5, "latency_share": .4,
                 "roles": {}, "unattributed_tokens": 0}]
        with mock.patch.object(overhead, "report", return_value=rows):
            self.assertIn("amplification", self._scorecard_output("--overhead"))
            parsed = json.loads(self._scorecard_output("--overhead", "--goal", "T-1", "--json"))
            self.assertEqual(parsed[0]["goal_id"], "T-1")

    def test_jev_diagnose_summary(self):
        from orchestrator import jev
        execute = bus.create_task("diagnose execute", "s", ["a"], ["x"], role="execute")["id"]
        review = bus.create_task("diagnose review", "s", ["a"], ["x"], role="review")["id"]
        stamp = datetime(2026, 9, 20, 12).timestamp()
        rows = [
            {"ts": stamp, "task": execute, "tool": "Read", "scored": True, "sampled": True,
             "p_needed": 0.1, "repeat": False, "latency_ms": 100, "startup_ms": 10},
            {"ts": stamp, "task": review, "tool": "Bash", "scored": True, "sampled": True,
             "p_needed": 0.9, "repeat": False, "latency_ms": 300, "startup_ms": 30},
            {"ts": stamp, "task": review, "tool": "Read", "scored": False, "sampled": False,
             "p_needed": None, "repeat": True, "blocked": True, "latency_ms": 0},
            {"ts": 0, "task": execute, "tool": "Write", "scored": True, "p_needed": 0.1},
        ]
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            gate = root / ".orchestrator/runs/jev/gate.jsonl"
            gate.parent.mkdir(parents=True)
            gate.write_text("\n".join(json.dumps(row) for row in rows) + "\ninvalid json\n")
            export = root / "labels.jsonl"
            out = io.StringIO()
            with mock.patch.object(cli, "ROOT", root), mock.patch.object(sys, "argv", [
                "orchestrator", "jev", "diagnose", "--since", "2026-09-19", "--export", str(export), "--n", "1"
            ]), mock.patch.object(jev, "ask", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(out):
                cli.main()
            lines = out.getvalue().splitlines()
            self.assertIn("calls=3 scored=2 sampled_share=66.7%", lines)
            self.assertIn("blocked=1", lines)
            self.assertIn("latency_ms_p50=200.0 latency_ms_p95=290.0", lines[2])
            self.assertIn("startup_ms_p50=20.0", lines[2])
            self.assertIn("network_ms_p50=", lines[2])
            tools = json.loads(next(line.removeprefix("by_tool=") for line in lines if line.startswith("by_tool=")))
            roles = json.loads(next(line.removeprefix("by_role=") for line in lines if line.startswith("by_role=")))
            self.assertEqual(set(tools), {"Read", "Bash"})
            self.assertEqual((tools["Read"]["calls"], tools["Read"]["scored"], tools["Read"]["waste_pct"]), (2, 1, 100.0))
            self.assertEqual(roles["execute"]["waste_pct"], 100.0)
            self.assertEqual(roles["review"]["waste_pct"], 0.0)
            self.assertEqual(roles["review"]["repeat_pct"], 50.0)
            self.assertIn(f"exported 1 rows to {export}", lines)
            labelled, = [json.loads(line) for line in export.read_text().splitlines()]
            self.assertEqual(labelled.pop("label"), "")
            self.assertIn(labelled.pop("role"), {"execute", "review"})
            self.assertIn(labelled, rows[:2])

    def _scorecard_fixture(self):
        import test_scorecard
        fixture = test_scorecard.Scorecard()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write_task("T-0244", role="execute", parent="T-0240")
        fixture.write_task("T-0245", role="review", parent="T-0241")
        fixture.write_runs(
            {"task": "T-0244", "role": "execute", "usd": 2, "turns": 11},
            {"task": "T-0245", "role": "review", "usd": 1},
        )
        fixture.write_gate(
            {"task": "T-0244", "scored": True, "p_needed": 0.1},
            {"task": "T-0245", "scored": True, "p_needed": 0.9, "blocked": True},
            {"task": "T-0245", "scored": True, "p_needed": 0.9},
            {"task": "T-0245", "scored": False, "p_needed": 0.1},
            {"task": "T-9999", "scored": True, "p_needed": 0.1},
        )
        return fixture

    def _scorecard_output(self, *args):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["orchestrator", "scorecard", *args]), contextlib.redirect_stdout(out):
            cli.main()
        return out.getvalue()

    def _scorecard_text_rows(self, fixture, by):
        build = getattr(cli.scorecard, "by_" + by)
        with mock.patch.object(cli.scorecard, "STATE", fixture.root), mock.patch.object(
            cli.scorecard, "by_" + by, side_effect=lambda *args, **kwargs: build(root=fixture.root)
        ), mock.patch.object(cli.scorecard, "planner_footer", return_value="planner: -"):
            output = self._scorecard_output("--by", by)
        lines = output.splitlines()
        header = lines[0].split("\t")
        return header, {line.split("\t")[0]: dict(zip(header, line.split("\t")))
                        for line in lines[1:] if "\t" in line}

    def test_scorecard_by_task_text_has_gate_columns(self):
        fixture = self._scorecard_fixture()
        header, rows = self._scorecard_text_rows(fixture, "task")
        self.assertEqual(header[-4:], ["calls", "waste_pct", "blocked", "turns"])
        self.assertEqual([rows["T-0244"][key] for key in header[-4:]], ["1", "100.0", "0", "11"])
        self.assertEqual([rows["total"][key] for key in header[-4:]], ["3", "33.3", "1", "11"])
        self.assertEqual(rows["T-0245"]["turns"], "-")
        fixture.write_gate({"task": "T-0244", "scored": False, "blocked": True})
        _, rows = self._scorecard_text_rows(fixture, "task")
        self.assertEqual([rows["total"][key] for key in header[-4:]], ["0", "-", "1", "11"])
        (fixture.root / "runs" / "jev" / "gate.jsonl").unlink()
        _, rows = self._scorecard_text_rows(fixture, "task")
        for tid in ("T-0244", "T-0245", "total"):
            self.assertEqual([rows[tid][key] for key in header[-4:-1]], ["-", "-", "-"])

    def test_scorecard_by_goal_text_has_waste_and_turns(self):
        fixture = self._scorecard_fixture()
        header, rows = self._scorecard_text_rows(fixture, "goal")
        self.assertEqual(header[-3:], ["calls", "waste_pct", "turns"])
        self.assertEqual([rows["T-0240"][key] for key in header[-3:]], ["1", "100.0", "11"])
        self.assertEqual([rows["total"][key] for key in header[-3:]], ["3", "33.3", "11"])
        self.assertEqual(rows["T-0241"]["turns"], "-")
        (fixture.root / "runs" / "jev" / "gate.jsonl").unlink()
        _, rows = self._scorecard_text_rows(fixture, "goal")
        self.assertEqual([rows["T-0241"][key] for key in header[-3:]], ["-", "-", "-"])
        self.assertEqual(rows["total"]["waste_pct"], "-")

    def test_scorecard_goal_footer(self):
        fixture = self._scorecard_fixture()
        fixture.write_task("T-0240", status="done", result={"pr": "https://github.com/acme/repo/pull/24"})
        build = cli.scorecard.by_goal
        with mock.patch.object(cli.scorecard, "STATE", fixture.root), mock.patch.object(
            cli.scorecard, "by_goal", side_effect=lambda *args, **kwargs: build(root=fixture.root)
        ):
            output = self._scorecard_output("--by", "goal")
        self.assertIn("tokens per accepted goal: 0 over 1 goals (usd 2.0)", output)

    def test_cli_scorecard_scheduling_and_strategies_text_and_json(self):
        from orchestrator import sched_scorecard, strategy
        scheduling = {"scheduler": {"n": 2}}
        strategies = {("serial", "bug", "small"): {"n": 1}}
        with mock.patch.object(sched_scorecard, "build", return_value=scheduling), \
                mock.patch.object(sched_scorecard, "format", return_value="scheduling card"), \
                mock.patch.object(strategy, "scorecard", return_value=strategies), \
                mock.patch.object(strategy, "format", return_value="strategy card"):
            self.assertEqual(self._scorecard_output("--scheduling"), "scheduling card\n")
            self.assertEqual(json.loads(self._scorecard_output("--scheduling", "--json")), scheduling)
            self.assertEqual(self._scorecard_output("--strategies"), "strategy card\n")
            self.assertEqual(json.loads(self._scorecard_output("--strategies", "--json")),
                             {"serial/bug/small": {"n": 1}})
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["orchestrator", "scorecard", "--scheduling", "--strategies"]), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.main()
        self.assertIn("--scheduling, --strategies", err.getvalue())

    def test_cli_explain_prints_decision_rows(self):
        from orchestrator import decision_log
        rows = [{"ts": 1, "kind": "routing", "subject": "T-1", "selected": "a", "reason": "fit",
                 "rejected": [], "outcomes": []}]
        with mock.patch.object(decision_log, "explain", return_value=rows), \
                mock.patch.object(decision_log, "format_explain", return_value="explanation"):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["orchestrator", "explain", "T-1"]), contextlib.redirect_stdout(out):
                cli.main()
            self.assertEqual(out.getvalue(), "explanation\n")
            with mock.patch.object(sys, "argv", ["orchestrator", "explain", "T-1", "--json"]), contextlib.redirect_stdout(out := io.StringIO()):
                cli.main()
            self.assertEqual(json.loads(out.getvalue()), rows)

    def test_cli_promotion_report(self):
        from orchestrator import promotion
        rows = [{"feature": "scheduler", "recommendation": "stay", "mode": "shadow", "n": 0}]
        with mock.patch.object(promotion, "report", return_value=rows), \
                mock.patch.object(promotion, "format_report", return_value="scheduler: stay"):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["orchestrator", "promotion"]), contextlib.redirect_stdout(out):
                cli.main()
            self.assertEqual(out.getvalue(), "scheduler: stay\n")
            with mock.patch.object(sys, "argv", ["orchestrator", "promotion", "--json"]), contextlib.redirect_stdout(out := io.StringIO()):
                cli.main()
            self.assertEqual(json.loads(out.getvalue()), rows)

    def test_cli_scorecard_efficiency_text_and_json(self):
        fixture = self._scorecard_fixture()
        with mock.patch.object(cli.scorecard, "STATE", fixture.root):
            text = self._scorecard_output("--efficiency", "--by", "band")
            self.assertIn("n/a", text)
            self.assertIn("Amplification=", text)
            self.assertIn("unknown", text)
            card = json.loads(self._scorecard_output("--efficiency", "--json"))
            self.assertIsNone(card["by"])
            self.assertEqual(card["tokens"], 0)
            self.assertIn("unknown", card["groups"])

    def test_cli_scorecard_economics_text_and_json(self):
        fixture = self._scorecard_fixture()
        fixture.write_task("T-econ", executor="cheap", status="done", merged_into="goal/G",
                           pipeline={"first_green_at": "2026-01-01T00:00:00Z", "gate_reds": 0})
        fixture.write_runs({"task": "T-econ", "role": "execute", "executor": "cheap",
                            "input_tokens": 10, "usd": 1})
        with mock.patch.object(cli.scorecard, "STATE", fixture.root):
            text = self._scorecard_output("--economics")
            self.assertIn("cost_to_accepted", text)
            self.assertIn("cheap", text)
            card = json.loads(self._scorecard_output("--economics", "--by", "band", "--json"))
            self.assertIn("cheap/1-3", card)

    def test_cli_scorecard_routing_text_and_json(self):
        fixture = self._scorecard_fixture()
        fixture.write_task("T-route", executor="worker", merged_into="main",
                           pipeline={"first_green_at": 1, "gate_reds": 0}, lineage_fix_rounds=0)
        fixture.write_task("T-route-agree", executor="worker", status="failed",
                           pipeline={"gate_reds": 1}, lineage_fix_rounds=0)
        fixture.write_runs({"task": "T-route", "role": "execute"},
                           {"task": "T-route", "role": "jev_route", "baseline": "worker",
                            "hypothetical": "other", "signals": {"fit": .7}, "latency_ms": 5,
                            "usage": {"tokens": 3, "usd": .001}},
                           {"task": "T-route-agree", "role": "execute"},
                           {"task": "T-route-agree", "role": "jev_route", "baseline": "worker",
                            "hypothetical": "worker", "signals": {"fit": .4}, "latency_ms": 5,
                            "usage": {"tokens": 3, "usd": .001}})
        with mock.patch.object(cli.scorecard, "STATE", fixture.root):
            text = self._scorecard_output("--routing")
            rows = [line.split("\t") for line in text.splitlines()]
            self.assertEqual([row[:2] for row in rows if row[0] == "agree"], [["agree", "1"]])
            self.assertEqual([row[:2] for row in rows if row[0] == "disagree"], [["disagree", "1"]])
            self.assertIn("evidence verdict: insufficient", text)
            card = json.loads(self._scorecard_output("--routing", "--json"))
            self.assertEqual(card["groups"]["agree"]["n"], 1)
            self.assertEqual(card["groups"]["disagree"]["n"], 1)

    def test_cli_scorecard_reviews_text_and_json(self):
        fixture = self._scorecard_fixture()
        fixture.write_task("T-reviewed", executor="worker")
        fixture.write_task("T-review-quality", role="review", status="done", inputs=["T-reviewed"],
                           result={"verdict": "approve", "comments": []})
        fixture.write_runs({"task": "T-review-quality", "role": "review", "total_tokens": 10})
        with mock.patch.object(cli.scorecard, "STATE", fixture.root):
            self.assertIn("findings_per_review", self._scorecard_output("--reviews"))
            self.assertEqual(json.loads(self._scorecard_output("--reviews", "--json"))["general"]["n_reviews"], 2)

    def test_cli_scorecard_rejects_two_modes_and_names_by_modes(self):
        for modes in (("--economics", "--efficiency"),
                      ("--economics", "--routing"),
                      ("--efficiency", "--routing")):
            err = io.StringIO()
            with self.subTest(modes=modes), mock.patch.object(
                sys, "argv", ["orchestrator", "scorecard", *modes]
            ), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("choose one of --economics, --efficiency, --routing", err.getvalue())

        for by in ("band", "class", "role"):
            err = io.StringIO()
            with self.subTest(by=by), mock.patch.object(
                sys, "argv", ["orchestrator", "scorecard", "--by", by]
            ), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("default supports --by executor|tier|task|goal", err.getvalue())

    def test_cli_scorecard_by_validation_per_mode(self):
        cases = (
            ((), "default", "executor|tier|task|goal", ("packet_version", "reviewed_executor")),
            (("--efficiency",), "--efficiency", "goal|executor|band|class|role",
             ("packet_version", "reviewed_executor", "tier", "task")),
            (("--economics",), "--economics", "executor|band|class", ("packet_version", "role")),
            (("--routing",), "--routing", "none (omit --by)", ("packet_version", "executor")),
            (("--reviews",), "--reviews", "role|packet_version|tier|band|reviewed_executor",
             ("executor", "goal", "class", "task")),
        )
        for flags, mode, allowed, invalid in cases:
            for by in invalid:
                err = io.StringIO()
                with self.subTest(mode=mode, by=by), mock.patch.object(
                    sys, "argv", ["orchestrator", "scorecard", *flags, "--by", by]
                ), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    cli.main()
                self.assertEqual(cm.exception.code, 2)
                self.assertIn(f"{mode} supports --by {allowed}", err.getvalue())
        fixture = self._scorecard_fixture()
        with mock.patch.object(cli.scorecard, "STATE", fixture.root):
            card = json.loads(self._scorecard_output("--reviews", "--by", "packet_version", "--json"))
        self.assertIn("pre-packet", card)

    def test_scorecard_default_output_unchanged(self):
        with mock.patch.object(cli.scorecard, "build", return_value={}), mock.patch.object(cli.scorecard, "scores", return_value={}):
            self.assertEqual(self._scorecard_output(), "id\tmerged\tfailed\trounds_avg\twall_s\tusd\thits\tscore\n")

    def test_status_plain_and_json(self):
        sys.argv = ["orchestrator", "status", "--plain"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("A\tutil="))
        self.assertTrue(lines[2].startswith("codex\tavailable="))

        sys.argv = ["orchestrator", "status"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        parsed = json.loads(out.getvalue())
        self.assertEqual(set(parsed.keys()), {"accounts", "executors", "codex", "queue"})
        self.assertEqual((len(parsed["executors"]), sum(e["enabled"] for e in parsed["executors"])), (7, 4))

    def test_scorecard_table_and_json(self):
        bus.create_task("cli-scored", "s", ["a"], ["x.py"], role="execute", complexity=3)  # ensures >=1 row exists
        bus.post_result(bus.read(role="execute")[-1]["id"], {"summary": "ok"})
        sys.argv = ["orchestrator", "scorecard"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("id\tmerged\tfailed\trounds_avg\twall_s\tusd\thits\tscore"))
        self.assertGreaterEqual(len(lines), 1)

        sys.argv = ["orchestrator", "scorecard", "--json"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        json.loads(out.getvalue())  # valid JSON

    def test_planner_runs_summary_prints_on_empty_ledger(self):
        from orchestrator import planner_runs as PR
        PR._runs_path().unlink(missing_ok=True)
        sys.argv = ["orchestrator", "planner-runs", "--summary"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        line = out.getvalue().strip()
        self.assertIn("decisions=0", line)
        self.assertIn("jev_scored=0", line)
        self.assertIn("agreement_rate=None", line)

    def test_planner_runs_default_prints_summary(self):
        """T-0232 review item 4: `orchestrator planner-runs` with no flag prints the same summary line as
        `--summary` -- the flag is still accepted, but no longer required."""
        from orchestrator import planner_runs as PR
        PR._runs_path().unlink(missing_ok=True)
        sys.argv = ["orchestrator", "planner-runs"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main()
        line = out.getvalue().strip()
        self.assertIn("decisions=0", line)
        self.assertIn("jev_scored=0", line)
        self.assertIn("agreement_rate=None", line)
        self.assertIn("mean_confidence=None", line)

    def test_cost_ignores_jev_lines(self):
        bus.RUNS.mkdir(parents=True, exist_ok=True)
        marker = f"cli-cost-jev-{time.time()}"
        with open(bus.RUNS / f"{time.strftime('%Y-%m-%d')}.jsonl", "a") as fh:
            fh.write(json.dumps({"task": marker, "role": "execute", "input_tokens": 7, "output_tokens": 0,
                                  "cache_read_input_tokens": 0}) + "\n")
            # a jev usage line: no "role" key at all -- must not be counted anywhere, not even under "?"
            fh.write(json.dumps({"ts": time.time(), "caller": "noul", "input_tokens": 999999,
                                  "model": "jev-latest", "latency_ms": 1.0, "ok": True}) + "\n")
        agg = cli.cost("task")
        self.assertEqual(agg[marker]["input_tokens"], 7)
        self.assertEqual(agg[marker]["runs"], 1)
        self.assertNotIn(999999, [v.get("input_tokens") for v in agg.values()])

    def test_pick_tallies_then_prints_account_and_config_dir(self):
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True)
        with mock.patch.object(cli.Pool, "tally_planner") as tally:
            sys.argv = ["orchestrator", "pick", "scout"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main()
            tally.assert_called_once()
        acct_id, config_dir = out.getvalue().strip().split("\t")
        self.assertIn(acct_id, {"A", "B"})
        self.assertEqual(config_dir, os.path.expanduser(P.Pool().get(acct_id).config_dir))
        self.assertNotIn("~", config_dir)

    def test_pick_planner_model_flag(self):
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True)
        pl = P.Pool()
        legacy = io.StringIO()
        with mock.patch.object(cli, "Pool", return_value=pl), \
                mock.patch.object(pl, "tally_planner"), \
                mock.patch.object(sys, "argv", ["orchestrator", "pick", "planner"]), \
                contextlib.redirect_stdout(legacy):
            cli.main()
        self.assertEqual(len(legacy.getvalue().strip().splitlines()), 1)

        shadow = io.StringIO()
        with mock.patch.object(cli, "Pool", return_value=pl), \
                mock.patch.object(pl, "tally_planner"), \
                mock.patch.object(sys, "argv", ["orchestrator", "pick", "planner", "--model"]), \
                contextlib.redirect_stdout(shadow):
            cli.main()
        lines = shadow.getvalue().strip().splitlines()
        self.assertEqual(lines[1].split("\t")[:2], ["model", pl.cfg["models"]["planner"]])
        self.assertIn("shadow", lines[1])

        pl.cfg.setdefault("planner", {}).setdefault("routing", {})["mode"] = "active"
        active = io.StringIO()
        with mock.patch.object(cli, "Pool", return_value=pl), \
                mock.patch.object(pl, "tally_planner"), \
                mock.patch.object(sys, "argv", ["orchestrator", "pick", "planner", "--model"]), \
                contextlib.redirect_stdout(active):
            cli.main()
        model_line = active.getvalue().strip().splitlines()[1]
        self.assertEqual(model_line.split("\t")[:2], ["model", pl.cfg["models"]["opus"]])
        self.assertIn("active", model_line)

    def test_claude_md_planner_wording(self):
        first_line = (REPO / "CLAUDE.md").read_text().splitlines()[0]
        self.assertNotIn("Planner (Fable 5.1)", first_line)
        self.assertIn("[planner.routing]", first_line)
        self.assertIn("`orchestrator pick planner --model`", first_line)

    def test_pick_exits_3_when_none(self):
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True)
        pl = P.Pool()
        for acct_id in ("A", "B"):
            pl.cooldown(pl.get(acct_id), 3600)
        sys.argv = ["orchestrator", "pick", "scout"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("hold: no account with headroom", err.getvalue())

    def test_pick_tally_failure_prints_one_stderr_line_and_proceeds(self):
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True)
        with mock.patch.object(cli.Pool, "tally_planner", side_effect=RuntimeError("boom")):
            sys.argv = ["orchestrator", "pick", "scout"]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main()
        self.assertEqual(err.getvalue().strip(), "pick: planner tally failed: boom")
        acct_id, config_dir = out.getvalue().strip().split("\t")
        self.assertIn(acct_id, {"A", "B"})

    def test_pick_planner_end_to_end_real_transcript(self):
        """No tally mock: a real .jsonl under a fake config_dir, run through the actual pick code path."""
        P.PERSIST.unlink(missing_ok=True); P.PLANNER_USAGE.unlink(missing_ok=True)
        fake_home = TMP / "fake-home-cli-e2e"
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            proj_dir = (fake_home / ".claude-a" / "projects" / P.encode_project_dir(str(P.ROOT.resolve())))
            proj_dir.mkdir(parents=True)
            ts = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            line = json.dumps({"type": "assistant", "timestamp": ts, "message": {"usage": {
                "input_tokens": 40, "output_tokens": 10, "cache_read_input_tokens": 0}}}) + "\n"
            (proj_dir / "e2e.jsonl").write_text(line)
            sys.argv = ["orchestrator", "pick", "planner"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main()
            acct_id, config_dir = out.getvalue().strip().split("\t")
            self.assertNotIn("~", config_dir)
            self.assertEqual(config_dir, os.path.expanduser("~/.claude-a"))
            self.assertEqual(acct_id, "A")
            self.assertEqual(P.Pool().get("A").planner_day_tokens, 50)


if __name__ == "__main__":
    unittest.main()
