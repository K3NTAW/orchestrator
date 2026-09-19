"""orchestrator.cli: `status`, `scorecard` and `pick` subcommands, plain-text and JSON output."""
import contextlib, io, json, os, sys, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_cli.py` doesn't add this dir itself
from _harness import REPO, TMP  # noqa: F401
from orchestrator import bus, cli
from orchestrator import pool as P


class Cli(unittest.TestCase):
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
