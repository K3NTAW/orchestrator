"""orchestrator.cli: `status`, `scorecard` and `pick` subcommands, plain-text and JSON output."""
import contextlib, io, json, sys, unittest
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

    def test_pick_tallies_then_prints_account_and_config_dir(self):
        P.PERSIST.unlink(missing_ok=True)
        with mock.patch.object(cli.Pool, "tally_planner") as tally:
            sys.argv = ["orchestrator", "pick", "scout"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main()
            tally.assert_called_once()
        acct_id, config_dir = out.getvalue().strip().split("\t")
        self.assertIn(acct_id, {"A", "B"})
        self.assertEqual(config_dir, P.Pool().get(acct_id).config_dir)

    def test_pick_exits_3_when_none(self):
        P.PERSIST.unlink(missing_ok=True)
        pl = P.Pool()
        for acct_id in ("A", "B"):
            pl.cooldown(pl.get(acct_id), 3600)
        sys.argv = ["orchestrator", "pick", "scout"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("hold: no account with headroom", out.getvalue())


if __name__ == "__main__":
    unittest.main()
