import _harness
import contextlib, io, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from orchestrator import cli, planner_context
from orchestrator.pool import encode_project_dir


class PlannerContext(unittest.TestCase):
    HEADER = ("Session compacted; continue in place. Planner mode: goals go through "
              "Skill(orchestrate); never edit source; see CLAUDE.md.")
    FOOTER = ("Full state: .orchestrator/plan.md; history: "
              ".orchestrator/plan-log.md (do not read by default).")

    def setUp(self):
        patcher = mock.patch.object(planner_context.subprocess, "Popen")
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def test_compact_brief_now_section(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / ".orchestrator" / "plan.md"
            plan.parent.mkdir()
            plan.write_text("# Plan\nold context\n## Now\ncurrent goal\n### Detail\nnext step\n## Other\nhistory\n")
            expected = f"{self.HEADER}\n## Now\ncurrent goal\n### Detail\nnext step\n\n{self.FOOTER}"
            self.assertEqual(planner_context.compact_brief(root), expected)
            with mock.patch.object(cli, "ROOT", root), \
                    mock.patch.object(sys, "argv", ["orchestrator", "planner-context", "--brief"]), \
                    contextlib.redirect_stdout(output := io.StringIO()):
                cli.main()
            self.assertEqual(output.getvalue(), expected + "\n")
            plan.write_text("# Plan\n## Now\ncurrent goal")
            self.assertEqual(planner_context.compact_brief(root),
                             f"{self.HEADER}\n## Now\ncurrent goal\n{self.FOOTER}")

    def test_compact_brief_fallback_and_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / ".orchestrator" / "plan.md"
            plan.parent.mkdir()
            missing = f"{self.HEADER}\nplan.md missing; run the resume skill."
            self.assertEqual(planner_context.compact_brief(root), missing)
            plan.write_text("# Plan\nshort")
            self.assertEqual(planner_context.compact_brief(root),
                             f"{self.HEADER}\n# Plan\nshort\n{self.FOOTER}")
            plan.write_text("# Plan\nfirst line\n" + "long line" * 20)
            self.assertEqual(planner_context.compact_brief(root, limit=25),
                             f"{self.HEADER}\n# Plan\nfirst line\n[cut; read .orchestrator/plan.md]\n{self.FOOTER}")
            with mock.patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertEqual(planner_context.compact_brief(root), missing)

    def _started(self, tokens):
        return f"Context {tokens} tokens: handover started (auto); checkpoint goes to .orchestrator/plan.md."

    def test_hook_runs_handover_once_per_band(self):
        with tempfile.TemporaryDirectory() as directory:
            root, config_dir = Path(directory) / "repo", Path(directory) / "config"
            cfg = {"planner": {"handover_context_tokens": 100000}}
            self._prompt_transcript(config_dir, root, 120000, 12)
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root, session_id="S"),
                             self._started(120000))
            self.assertEqual(self.popen.call_count, 1)
            args, kwargs = self.popen.call_args
            self.assertEqual(args[0], ["uv", "run", "orchestrator", "handover", "--reason", "context",
                                       "--session-id", "S"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["cwd"], str(root))
            self.assertTrue(Path(kwargs["stdout"].name).name.startswith("handover-"))
            self.assertEqual(Path(kwargs["stdout"].name).parent, root / ".orchestrator/runs")
            for tokens in (150000, 199999):
                self._prompt_transcript(config_dir, root, tokens, 12)
                self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, session_id="S"))
            self.assertEqual(self.popen.call_count, 1)
            self._prompt_transcript(config_dir, root, 200000, 12)
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root, session_id="S"),
                             self._started(200000))
            self.assertEqual(self.popen.call_count, 2)
            self._prompt_transcript(config_dir, root, 320000, 12)
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root, session_id="S"),
                             self._started(320000))
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, session_id="S"))
            self.assertEqual(self.popen.call_count, 3)
            state = json.loads((root / ".orchestrator/handover_state.json").read_text())
            self.assertEqual(state["auto_handover_bands"], {"S": 2})

    def test_hook_silent_below_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root, config_dir = Path(directory) / "repo", Path(directory) / "config"
            cfg = {"planner": {"handover_context_tokens": 100000}}
            self._prompt_transcript(config_dir, root, 99999, 12)
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, session_id="S"))
            self.popen.assert_not_called()
            self.assertFalse((root / ".orchestrator/handover_state.json").exists())

    def test_hook_silent_after_handover_in_same_band(self):
        with tempfile.TemporaryDirectory() as directory:
            root, config_dir = Path(directory) / "repo", Path(directory) / "config"
            cfg = {"planner": {"handover_context_tokens": 100000}}
            state = root / ".orchestrator/handover_state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"snapshot_hash": "h", "auto_handover_bands": {"S": 0}}))
            self._prompt_transcript(config_dir, root, 180000, 12)
            for _ in range(3):
                self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, session_id="S"))
            self.popen.assert_not_called()
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root, session_id="other"),
                             self._started(180000))
            data = json.loads(state.read_text())
            self.assertEqual(data, {"snapshot_hash": "h", "auto_handover_bands": {"S": 0, "other": 0}})

    def test_hook_reports_failure_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root, config_dir = Path(directory) / "repo", Path(directory) / "config"
            cfg = {"planner": {"handover_context_tokens": 100000}}
            self._prompt_transcript(config_dir, root, 150000, 12)
            self.popen.side_effect = FileNotFoundError("no uv\non PATH")
            message = planner_context.hook_message(cfg, config_dir, root, session_id="S")
            self.assertEqual(message, "Context 150000 tokens: auto handover failed (FileNotFoundError: no uv on PATH).")
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, session_id="S"))
            self.assertEqual(self.popen.call_count, 1)

    def test_plan_size_nag(self):
        with tempfile.TemporaryDirectory() as directory:
            root, config_dir = Path(directory) / "repo", Path(directory) / "config"
            plan = root / ".orchestrator/plan.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("é" * 21)
            cfg = {"planner": {"handover_context_tokens": 100, "plan_max_chars": 20}}
            nag = "plan.md is 21 chars (max 20): move history to .orchestrator/plan-log.md and keep ## Now current."
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root), nag)
            self._prompt_transcript(config_dir, root, 50, 12)
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root, session_id="S"), nag)
            self._prompt_transcript(config_dir, root, 200, 1)
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root, session_id="S"), nag)
            self._prompt_transcript(config_dir, root, 200, 12)
            lines = planner_context.hook_message(cfg, config_dir, root, session_id="S").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn(nag, lines)
            self.assertIn(self._started(200), lines)
            cfg["planner"]["handover_context_tokens"] = 0
            self.assertEqual(planner_context.hook_message(cfg, config_dir, root), nag)
            cfg["planner"]["plan_max_chars"] = 0
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root))
            plan.write_text("x" * 12000)
            self.assertIsNone(planner_context.hook_message({}, config_dir, root))
            plan.write_text("x" * 12001)
            self.assertIn("12001 chars (max 12000)", planner_context.hook_message({}, config_dir, root))

    def test_plan_size_nag_ignores_auto_handover(self):
        with tempfile.TemporaryDirectory() as directory:
            root, config_dir = Path(directory) / "repo", Path(directory) / "config"
            plan = root / ".orchestrator/plan.md"
            plan.parent.mkdir(parents=True)
            plan.write_text(
                "short plan\n\n"
                "## Auto-handover old\n" + "x" * 100 + "\n"
                "<!-- end auto-handover -->\n"
            )
            cfg = {"planner": {"handover_context_tokens": 0, "plan_max_chars": 20}}

            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root))

    def _prompt_transcript(self, config_dir, root, tokens, turns):
        path = config_dir / "projects" / encode_project_dir(str(root)) / "S.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [{"type": "user", "message": {"content": "continue"}} for _ in range(turns)]
        records += [{"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
                    for _ in range(12)]
        records.append({"type": "assistant", "message": {"usage": {"input_tokens": tokens}}})
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return path

    def test_hook_message_grace_turns_suppresses_early_handover(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            cfg = {"planner": {"handover_context_tokens": 100, "handover_grace_turns": 12}}
            transcript = self._prompt_transcript(config_dir, root, 200, 3)
            self.assertEqual(planner_context.user_turns(transcript), 3)
            self.assertEqual(planner_context.user_turns(transcript.parent / "missing"), 0)
            for kwargs in ({"transcript": transcript, "session_id": "S"}, {"session_id": "S"}):
                self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, **kwargs))
            self.assertIn("write the checkpoint", planner_context.hook_message(cfg, config_dir, root))
            self._prompt_transcript(config_dir, root, 200, 12)
            self.assertEqual(planner_context.user_turns(transcript), 12)
            message = planner_context.hook_message(cfg, config_dir, root, transcript=transcript, session_id="S")
            self.assertEqual(message, self._started(200))
            self.assertIn("S", self.popen.call_args[0][0])

    def test_hook_message_short_line_after_handover_for_this_session(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            cfg = {"planner": {"handover_context_tokens": 100}}
            transcript = self._prompt_transcript(config_dir, root, 2000, 12)
            message = planner_context.hook_message(cfg, config_dir, root, session_id="S")
            self.assertEqual(message, self._started(2000))
            self.assertEqual(len(message.splitlines()), 1)
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, session_id="S"))
            self.assertEqual(self._started(2000), planner_context.hook_message(
                cfg, config_dir, root, transcript=transcript, session_id="other"))

    def test_default_threshold_when_key_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            for tokens in (290000, 310000):
                with self.subTest(tokens=tokens):
                    self._prompt_transcript(config_dir, root, tokens, 12)
                    message = planner_context.hook_message({}, config_dir, root, session_id="S")
                    if tokens > 300000:
                        self.assertEqual(self._started(tokens), message)
                    else:
                        self.assertIsNone(message)
                    self.assertIsNone(planner_context.hook_message(
                        {"planner": {"handover_context_tokens": 0}}, config_dir, root, session_id="S"))

    def test_context_tokens_prefers_session_transcript_over_newest(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            sid = "current-session"
            self._transcript(config_dir, root, f"{sid}.jsonl", {
                "input_tokens": 10000, "cache_read_input_tokens": 20000,
                "cache_creation_input_tokens": 10000}, 1)
            self._transcript(config_dir, root, "other.jsonl", {"input_tokens": 190000}, 2)
            older = config_dir / "projects" / encode_project_dir(str(root)) / f"{sid}.jsonl"
            self.assertEqual(40000, planner_context.context_tokens(config_dir, root, session_id=sid))
            for transcript in (older, str(older)):
                with self.subTest(transcript=transcript):
                    self.assertEqual(40000, planner_context.context_tokens(
                        config_dir, root, transcript=transcript))
            self.assertEqual(40000, planner_context.context_tokens(
                config_dir, root, transcript=older, session_id="other"))
            self.assertEqual(190000, planner_context.context_tokens(config_dir, root))

    def test_fresh_session_reports_none_not_another_sessions_size(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir, root = Path(directory) / "config", Path(directory) / "repo"
            cfg = {"planner": {"handover_context_tokens": 100000}}
            self._transcript(config_dir, root, "fresh.jsonl", None, 1)
            self._transcript(config_dir, root, "other.jsonl", {"input_tokens": 190000}, 2)
            transcripts = config_dir / "projects" / encode_project_dir(str(root))
            for kwargs in ({"session_id": "fresh"}, {"session_id": "missing"},
                           {"transcript": transcripts / "fresh.jsonl"},
                           {"transcript": transcripts / "missing.jsonl"},
                           {"transcript": transcripts / "fresh.jsonl", "session_id": "other"},
                           {"transcript": transcripts / "missing.jsonl", "session_id": "other"}):
                with self.subTest(**kwargs):
                    self.assertIsNone(planner_context.context_tokens(config_dir, root, **kwargs))
                    self.assertIsNone(planner_context.hook_message(cfg, config_dir, root, **kwargs))

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
            cfg = {"planner": {"handover_context_tokens": 100, "handover_grace_turns": 0}}
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root))
            self._transcript(config_dir, root, "session.jsonl", {"input_tokens": 99}, time.time())
            self.assertIsNone(planner_context.hook_message(cfg, config_dir, root))
            self._transcript(config_dir, root, "session.jsonl", {"input_tokens": 100}, time.time())
            expected = ("Planner context is 100 tokens (threshold 100); write the checkpoint: "
                        "uv run orchestrator handover --reason context; then keep working, "
                        "the session compacts in place (auto-compact or /compact).")
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
