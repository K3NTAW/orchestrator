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
        fixture.write_runs({"task": "T-route", "role": "execute"},
                           {"task": "T-route", "role": "jev_route", "baseline": "worker",
                            "hypothetical": "other", "signals": {"fit": .7}, "latency_ms": 5,
                            "usage": {"tokens": 3, "usd": .001}})
        with mock.patch.object(cli.scorecard, "STATE", fixture.root):
            text = self._scorecard_output("--routing")
            self.assertIn("agree\t1", text)
            self.assertIn("disagree\t1", text)
            self.assertIn("evidence verdict: insufficient", text)
            card = json.loads(self._scorecard_output("--routing", "--json"))
            self.assertEqual(card["groups"]["disagree"]["n"], 1)

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
