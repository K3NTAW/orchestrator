import _harness
import contextlib, io, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from orchestrator import cli, planner_context
from orchestrator.pool import encode_project_dir


class PlannerContext(unittest.TestCase):
    def _transcript(self, config_dir, root, name, usage, modified):
        directory = config_dir / "projects" / encode_project_dir(str(root))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps({"message": {"usage": usage}}) + "\n")
        os.utime(path, (modified, modified))

    def test_context_tokens_from_latest_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            self.assertIsNone(planner_context.context_tokens(config_dir, root))
            self._transcript(config_dir, root, "old.jsonl", {"input_tokens": 1}, 1)
            self._transcript(config_dir, root, "new.jsonl", {
                "input_tokens": 100, "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 3}, 2)
            self.assertEqual(123, planner_context.context_tokens(config_dir, root))

    def test_hook_message_over_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            cfg = {"planner": {"handover_context_tokens": 100}}
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root))
            self._transcript(config_dir, root, "session.jsonl", {"input_tokens": 99}, time.time())
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root))
            self._transcript(config_dir, root, "session.jsonl", {"input_tokens": 100}, time.time())
            expected = "Planner context is 100 tokens (threshold 100); hand over now: uv run orchestrator handover --reason context"
            self.assertEqual(expected, planner_context.hook_message(cfg, config_dir, root))
            with mock.patch.object(cli, "ROOT", root), \
                    mock.patch.object(cli, "pool_config", return_value=cfg), \
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}, clear=False), \
                    mock.patch.object(sys, "argv", ["orchestrator", "planner-context", "--hook"]), \
                    contextlib.redirect_stdout(output := io.StringIO()):
                cli.main()
            self.assertEqual(expected + "\n", output.getvalue())
            for tokens in (99, None):
                with self.subTest(tokens=tokens), \
                        mock.patch.object(planner_context, "context_tokens", return_value=tokens), \
                        mock.patch.object(cli, "pool_config", return_value=cfg), \
                        mock.patch.object(sys, "argv", ["orchestrator", "planner-context", "--hook"]), \
                        contextlib.redirect_stdout(output := io.StringIO()):
                    cli.main()
                self.assertEqual("", output.getvalue())
