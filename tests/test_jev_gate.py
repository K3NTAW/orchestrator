import _harness
"""orchestrator/jev_gate.py: the PreToolUse gate that asks Jev whether a worker's proposed tool call is
needed/redundant/destructive. jev.ask() is monkeypatched throughout (same pattern as test_jev.py) -- never
real network. Covers the transcript reader's error correlation, gate_mode="log" never blocking, gate_mode="block"
blocking a confident redundant call and allowing a needed one, the protected-call allowlist, a missing
transcript, and the shell hook exiting 0 for a Planner session (no task id)."""
import contextlib, io, json, sys, threading, unittest
import hashlib, os, subprocess, tempfile, tomllib
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_jev_gate.py` doesn't add this dir itself
from _harness import REPO, TMP, hook
from orchestrator import bus, jev, jev_gate

BLOCK_CFG = {"enabled": True, "gate_mode": "block"}
LOG_CFG = {"enabled": True, "gate_mode": "log"}


def answers(needed=(0.9, 0.9), redundant=(0.05, 0.9), destructive=(0.0, 0.9)):
    def a(pair):
        return {"noul": pair[0], "confidence": pair[1]}
    return {"answers": {"needed": a(needed), "redundant": a(redundant), "destructive": a(destructive)}}


class JevGateTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = jev_gate._cfg
        self._orig_ask = jev.ask
        self._clean_log()

    def tearDown(self):
        jev_gate._cfg = self._orig_cfg
        jev.ask = self._orig_ask
        self._clean_log()

    @staticmethod
    def _clean_log():
        if jev_gate.GATE_LOG.exists():
            jev_gate.GATE_LOG.unlink()

    def _log_lines(self):
        if not jev_gate.GATE_LOG.exists():
            return []
        return [json.loads(l) for l in jev_gate.GATE_LOG.read_text().splitlines() if l.strip()]

    def _task(self):
        return bus.create_task("do x", "spec text", ["tests pass"], ["src/**"], role="execute")

    def _run(self, payload, task_id):
        import os
        old = os.environ.get("ORCH_TASK_ID")
        os.environ["ORCH_TASK_ID"] = task_id
        try:
            return jev_gate.run(payload)
        finally:
            if old is None:
                os.environ.pop("ORCH_TASK_ID", None)
            else:
                os.environ["ORCH_TASK_ID"] = old

    # -- transcript reader -------------------------------------------------

    def _sample_fixture(self, rate=0.0):
        directory = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, value in (("STATE", root), ("GATE_LOG", root / "runs/jev/gate.jsonl")):
            replacement = patch.object(jev_gate, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        jev_gate._cfg = lambda: {"enabled": True, "gate_mode": "sample", "sample_rate": rate,
                                 "block_repeats": False}
        target = root / "source.py"
        target.write_text("original")
        payload = {"session_id": "sample-test", "tool_name": "Read",
                   "tool_input": {"file_path": str(target)}}
        return self._task()["id"], payload, target

    def test_sample_mode_is_deterministic(self):
        task, payload, _ = self._sample_fixture(0.1)
        payload["tool_name"] = "Edit"
        expected = [int(hashlib.sha1(f"sample-test{i}".encode()).hexdigest(), 16) % 1000 < 100
                    for i in range(50)]
        self.assertTrue(any(expected))
        self.assertFalse(all(expected))
        sequences = []
        for _ in range(2):
            with patch.object(jev, "ask", return_value=answers()) as ask:
                for index in range(50):
                    payload["tool_input"]["offset"] = index
                    self.assertEqual(self._run(payload, task), 0)
                self.assertEqual(ask.call_count, sum(expected))
            sequences.append([row["sampled"] for row in self._log_lines()])
            (jev_gate.STATE / "runs/jev/session-sample-test.json").unlink()
            self._clean_log()
        self.assertEqual(sequences, [expected, expected])

    def test_unsampled_call_makes_no_request(self):
        task, payload, _ = self._sample_fixture()
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")) as ask:
            self.assertEqual(self._run(payload, task), 0)
            ask.assert_not_called()
        row, = self._log_lines()
        self.assertFalse(row["sampled"])
        self.assertFalse(row["scored"])
        self.assertEqual(row["latency_ms"], 0)

    def test_repeat_detected_without_network(self):
        task, payload, target = self._sample_fixture()
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")) as ask:
            for _ in range(2):
                self.assertEqual(self._run(payload, task), 0)
            ask.assert_not_called()
        rows = self._log_lines()
        self.assertEqual([r["repeat"] for r in rows], [False, True])
        state = json.loads((jev_gate.STATE / "runs/jev/session-sample-test.json").read_text())
        self.assertEqual(state[rows[0]["input_hash"]], {"count": 2, "mtime": target.stat().st_mtime_ns})
        payload["session_id"] = "another-session"
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self._run(payload, task), 0)
        self.assertFalse(self._log_lines()[-1]["repeat"])

    def test_repeat_cleared_when_mtime_changes(self):
        task, payload, target = self._sample_fixture()
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self._run(payload, task), 0)
            stat = target.stat()
            target.write_text("changed")
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual(self._run(payload, task), 0)
            self.assertEqual(self._run(payload, task), 0)
        self.assertEqual([r["repeat"] for r in self._log_lines()], [False, False, True])

    def test_record_call_serialises_concurrent_writes(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            target = root / "source.py"
            target.write_text("original")
            with patch.object(jev_gate, "STATE", root):
                barrier = threading.Barrier(2)

                def record():
                    barrier.wait()
                    return jev_gate._record_call("concurrent", "Read", {"file_path": str(target)})

                threads = [threading.Thread(target=record) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                state = json.loads((root / "runs/jev/session-concurrent.json").read_text())
            self.assertEqual(len(state), 1)
            self.assertEqual(next(iter(state.values()))["count"], 2)

    def test_corrupt_session_file_warns_and_recovers(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            session = root / "runs/jev/session-corrupt.json"
            session.parent.mkdir(parents=True)
            session.write_text("{not json")
            target = root / "source.py"
            target.write_text("original")
            with patch.object(jev_gate, "STATE", root), contextlib.redirect_stderr(io.StringIO()) as err:
                jev_gate._record_call("corrupt", "Read", {"file_path": str(target)})
            self.assertEqual(err.getvalue().count("corrupt session state"), 1)
            state = json.loads(session.read_text())
            self.assertEqual(next(iter(state.values()))["count"], 1)

    def test_target_mtime_uses_unredacted_path(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            target = Path(directory) / ("a" * 40)
            target.write_text("original")
            tool_input = {"file_path": str(target)}
            logged_target = jev_gate._target("Read", tool_input)
            self.assertNotEqual(logged_target, str(target))
            self.assertEqual(jev_gate._target_mtime(jev_gate._target_path("Read", tool_input)),
                             target.stat().st_mtime_ns)

    def test_rows_have_target_hash_and_flags(self):
        task, payload, target = self._sample_fixture(1.0)
        with patch.object(jev, "ask", return_value=answers()):
            for tool in ("Read", "Edit", "Write", "Grep", "Glob", "Bash"):
                value = {"command": "echo " + "x " * 100} if tool == "Bash" else {"path": str(target), "pattern": "x"}
                payload.update(tool_name=tool, tool_input=value)
                self.assertEqual(self._run(payload, task), 0)
                payload["tool_input"] = dict(reversed(list(value.items())))
                self.assertEqual(self._run(payload, task), 0)
        rows = self._log_lines()
        for first, second in zip(rows[::2], rows[1::2]):
            self.assertEqual(first["tool_target"], ("echo " + "x " * 100)[:80] if first["tool"] == "Bash" else str(target))
            self.assertRegex(first["input_hash"], r"^[0-9a-f]{40}$")
            self.assertEqual(first["input_hash"], second["input_hash"])
            self.assertFalse(first["repeat"])
            self.assertTrue(second["repeat"])
            expected_first = first["tool"] != "Read"
            expected_second = first["tool"] not in ("Read", "Grep", "Glob")
            self.assertEqual((first["sampled"], first["scored"]),
                             (expected_first, expected_first))
            self.assertEqual((second["sampled"], second["scored"]),
                             (expected_second, expected_second))
        with patch.object(jev, "redact", return_value="[redacted]") as redact:
            self.assertEqual(jev_gate._target("Read", {"file_path": "secret"}), "[redacted]")
            redact.assert_called_once_with("secret")

    def test_block_repeats_denies(self):
        task, payload, _ = self._sample_fixture()
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self._run(payload, task), 0)
            jev_gate._cfg = lambda: {**BLOCK_CFG, "block_repeats": True}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(self._run(payload, task), 2)
        self.assertIn("identical read already made this session; use the earlier result", err.getvalue())
        self.assertTrue(self._log_lines()[-1]["blocked"])
        self.assertFalse(self._log_lines()[-1]["scored"])
        jev_gate._cfg = lambda: {**BLOCK_CFG, "block_repeats": False}
        with patch.object(jev, "ask", return_value=answers()):
            self.assertEqual(self._run(payload, task), 0)

    def test_hook_honours_sample_defaults(self):
        config = tomllib.loads((REPO / ".orchestrator/pool.toml").read_text())["jev"]
        self.assertEqual((config["gate_mode"], config["sample_rate"], config["block_repeats"]),
                         ("sample", 0.1, False))
        task = self._task()["id"]
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            state = root / ".orchestrator"
            (state / "tasks").mkdir(parents=True)
            (state / "pool.toml").write_text('[jev]\nenabled = true\ngate_mode = "sample"\nsample_rate = 0.1\nblock_repeats = false\n')
            (state / "tasks" / f"{task}.json").write_text(json.dumps({"id": task, "role": "execute"}))
            bin_dir = root / ".venv/bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").symlink_to(sys.executable)
            # Loaded in the hook's interpreter: replace Jev before any scoring can reach the network.
            (root / "sitecustomize.py").write_text(
                'from orchestrator import jev\n'
                'def fake_ask(*args):\n'
                f'    with open({str(root / "requests")!r}, "a") as fh: fh.write("called\\n")\n'
                f'    return {answers()!r}\n'
                'jev.ask = fake_ask\n')
            flags = [int(hashlib.sha1(f"hook-sample{i}".encode()).hexdigest(), 16) % 1000 < 100
                     for i in range(50)]
            count = flags.index(True) + 2
            for _ in range(count):
                result = hook("jev-gate.sh", {"session_id": "hook-sample", "tool_name": "Edit",
                              "tool_input": {"file_path": str(state / "pool.toml"), "offset": _}}, cwd=root,
                              env={"ORCH_ROOT": str(root), "ORCH_TASK_ID": task,
                                   "PYTHONPATH": os.pathsep.join((str(root), str(REPO)))})
                self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in (state / "runs/jev/gate.jsonl").read_text().splitlines()]
            self.assertEqual([r["sampled"] for r in rows], flags[:count])
            self.assertEqual((root / "requests").read_text().splitlines(), ["called"] * sum(flags[:count]))
            self.assertTrue(all(r["mode"] == "sample" and not r["blocked"] for r in rows))
            self.assertTrue(all(not r["repeat"] for r in rows))

    def test_transcript_reader_correlates_errors(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a.py"}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}]}}),
            "not json at all {{{",
            json.dumps({"type": "other", "no": "message"}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "fail", "is_error": True}]}}),
        ]
        path = TMP / "transcript.jsonl"
        path.write_text("\n".join(lines) + "\n")

        recent = jev_gate.read_transcript(str(path))
        self.assertEqual([r["name"] for r in recent], ["Read", "Bash"])
        self.assertEqual([r["error"] for r in recent], [False, True])
        self.assertIn("/a.py", recent[0]["input"])

    def test_missing_transcript_allows(self):
        self.assertEqual(jev_gate.read_transcript(str(TMP / "no-such-transcript.jsonl")), [])

        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: answers()  # needed call, no missing-transcript block
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/x.py"},
                   "transcript_path": str(TMP / "no-such-transcript.jsonl"), "session_id": "s1"}
        self.assertEqual(self._run(payload, task["id"]), 0)

    # -- gate_mode ----------------------------------------------------------

    def test_block_without_confidence_uses_strict_thresholds(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        for key, probability, blocked in (("redundant", 0.92, True), ("redundant", 0.919, False),
                                           ("needed", 0.08, True), ("needed", 0.081, False)):
            with self.subTest(key=key, probability=probability):
                value = {key: {"p": probability, "confidence": None}}
                self.assertEqual(jev_gate.decide(value, "block"), (blocked, key if blocked else None))
                self.assertEqual(jev_gate.decide(value, "log"), (False, None))
        jev_gate._cfg = lambda: {**BLOCK_CFG, "block_redundant_p_noconf": 0.99,
                                "block_needed_p_noconf": 0.01}
        self.assertEqual(jev_gate.decide({"redundant": {"p": 0.95}}, "block"), (False, None))
        self.assertEqual(jev_gate.decide({"needed": {"p": 0.05}}, "block"), (False, None))

    def test_block_with_confidence_uses_default_thresholds(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        for key, probability, blocked in (("redundant", 0.85, True), ("redundant", 0.849, False),
                                           ("needed", 0.15, True), ("needed", 0.151, False)):
            with self.subTest(key=key, probability=probability):
                value = {key: {"p": probability, "confidence": 0.6}}
                self.assertEqual(jev_gate.decide(value, "block"), (blocked, key if blocked else None))
                value[key]["confidence"] = 0.59
                self.assertEqual(jev_gate.decide(value, "block"), (False, None))
        jev_gate._cfg = lambda: {**BLOCK_CFG, "block_redundant_p": 0.99, "block_needed_p": 0.01}
        for key, probability in (("redundant", 0.95), ("needed", 0.05)):
            self.assertEqual(jev_gate.decide({key: {"p": probability, "confidence": 0.9}}, "block"),
                             (False, None))

    def test_rule_logged(self):
        jev_gate._cfg = lambda: LOG_CFG
        for confidence, probability, rule in ((None, 0.92, "noconf"), (0.6, 0.85, "conf"),
                                               (None, 0.9, "none"), (0.59, 0.99, "none")):
            self._clean_log()
            response = answers(redundant=(probability, confidence))
            if confidence is None:
                del response["answers"]["redundant"]["confidence"]
            jev.ask = lambda state, questions: response
            self.assertEqual(self._run({"tool_name": "Bash", "tool_input": {"command": "echo x"}},
                                       self._task()["id"]), 0)
            self.assertEqual(self._log_lines()[0]["rule"], rule)
        self._clean_log()
        jev.ask = lambda state, questions: None
        self._run({"tool_name": "Bash", "tool_input": {"command": "echo x"}}, self._task()["id"])
        self.assertEqual(self._log_lines()[0]["rule"], "none")

    def test_log_mode_never_blocks(self):
        jev_gate._cfg = lambda: LOG_CFG
        jev.ask = lambda state, questions: answers(needed=(0.05, 0.9), redundant=(0.95, 0.9))
        task = self._task()
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo x"}, "transcript_path": "", "session_id": "s1"}

        rc = self._run(payload, task["id"])
        self.assertEqual(rc, 0)
        entries = self._log_lines()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["blocked"])
        self.assertTrue(entries[0]["scored"])
        self.assertEqual(entries[0]["mode"], "log")

    def test_block_mode_blocks_redundant_read(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: answers(needed=(0.5, 0.9), redundant=(0.95, 0.9))
        task = self._task()
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo x"}, "transcript_path": "", "session_id": "s1"}

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run(payload, task["id"])
        self.assertEqual(rc, 2)
        self.assertIn("jev-gate", err.getvalue())
        self.assertIn("Bash", err.getvalue())
        entries = self._log_lines()
        self.assertTrue(entries[0]["blocked"])

    def test_block_mode_allows_needed_call(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: answers(needed=(0.9, 0.9), redundant=(0.05, 0.9))
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/y.py"}, "transcript_path": "", "session_id": "s1"}

        rc = self._run(payload, task["id"])
        self.assertEqual(rc, 0)
        entries = self._log_lines()
        self.assertFalse(entries[0]["blocked"])

    def test_ask_none_never_blocks(self):
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = lambda state, questions: None
        task = self._task()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/z.py"}, "transcript_path": "", "session_id": "s1"}

        rc = self._run(payload, task["id"])
        self.assertEqual(rc, 0)
        entries = self._log_lines()
        self.assertFalse(entries[0]["scored"])
        self.assertFalse(entries[0]["blocked"])

    # -- protected calls ------------------------------------------------------

    def test_protected_calls_never_block(self):
        def boom(state, questions):
            raise AssertionError("jev.ask must not be called for a protected call")
        jev_gate._cfg = lambda: BLOCK_CFG
        jev.ask = boom
        task = self._task()

        protected_payloads = [
            {"tool_name": "Bash", "tool_input": {"command": "bash .claude/hooks/tests-green.sh"}, "session_id": "s1"},
            {"tool_name": "Bash", "tool_input": {"command": "git commit -m wip"}, "session_id": "s1"},
            {"tool_name": "Bash", "tool_input": {"command": "git add -A"}, "session_id": "s1"},
            {"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "s1"},
            {"tool_name": "bus_post_result", "tool_input": {"task": "T-1"}, "session_id": "s1"},
        ]
        for payload in protected_payloads:
            self.assertEqual(self._run(payload, task["id"]), 0, payload)

    def test_hook_exits_zero_without_task(self):
        out = hook("jev-gate.sh", {"tool_name": "Read", "tool_input": {"file_path": "/x.py"}},
                    cwd=TMP, env={"ORCH_TASK_ID": ""})
        self.assertEqual(out.returncode, 0)

    def test_disabled_exits_before_network_imports(self):
        code = '''
import builtins, io, os
original = builtins.__import__
attempted = []
def sentinel(name, *args, **kwargs):
    if name.startswith("urllib") or name == "jev" or "jev" in (args[2] if len(args) > 2 and args[2] else ()):
        attempted.append(name)
        raise AssertionError("network import before gate check")
    return original(name, *args, **kwargs)
builtins.__import__ = sentinel
from orchestrator import jev_gate
jev_gate.P.config = lambda: {"jev": {"enabled": False}}
os.environ["ORCH_TASK_ID"] = "T-test"
assert jev_gate.main([]) == 0
assert attempted == []
assert not any(k.startswith("urllib") for k in jev_gate.sys.modules)
jev_gate._cfg.cache_clear()
jev_gate.P.config = lambda: {"jev": {"enabled": True, "gate_roles": ["execute"]}}
jev_gate.load_task = lambda _: {"role": "review"}
jev_gate.read_transcript = lambda _: (_ for _ in ()).throw(AssertionError("transcript read"))
assert jev_gate.run({"tool_name": "Read", "tool_input": {"file_path": "/x"}}) == 0
assert attempted == []
'''
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_hook_single_invocation(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/x.py"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".orchestrator").mkdir()
            bin_dir = root / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            counter, received = root / "counter", root / "received"
            spy = '#!/bin/bash\nprintf "%s\\n" "$*" >> "$COUNTER"\ncat > "$RECEIVED"\nexit "${SPY_RC:-0}"\n'
            for executable in (bin_dir / "python", bin_dir / "uv"):
                executable.write_text(spy)
                executable.chmod(0o755)
            env = {"ORCH_ROOT": str(root), "ORCH_TASK_ID": "T-test", "COUNTER": str(counter),
                   "RECEIVED": str(received), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
            for fallback in (False, True):
                if fallback:
                    (bin_dir / "python").unlink()
                for rc in (0, 2, 1):
                    counter.unlink(missing_ok=True)
                    out = hook("jev-gate.sh", payload, env={**env, "SPY_RC": str(rc)})
                    self.assertEqual(out.returncode, 2 if rc == 2 else 0)
                    self.assertEqual(counter.read_text().splitlines(),
                                     [("run python " if fallback else "") + "-m orchestrator.jev_gate"])
                    self.assertEqual(json.loads(received.read_text()), payload)

    def test_startup_ms_logged(self):
        jev_gate._cfg = lambda: LOG_CFG
        jev.ask = lambda state, questions: answers()
        with patch.dict(os.environ, {"ORCH_JEV_STARTED_AT": str(jev_gate.time.time() - 0.1)}):
            self.assertEqual(self._run({"tool_name": "Bash", "tool_input": {"command": "echo x"}},
                                       self._task()["id"]), 0)
        entry = self._log_lines()[0]
        self.assertGreaterEqual(entry["startup_ms"], 100)
        self.assertGreaterEqual(entry["latency_ms"], 0)

    def test_skip_list(self):
        self.assertEqual(jev_gate.SKIP_RULES, ("empty_input", "bare_glob"))
        jev_gate._cfg = lambda: LOG_CFG
        with patch.object(jev, "ask", side_effect=AssertionError("unexpected scoring")), \
                patch.object(jev_gate, "read_transcript", side_effect=AssertionError("unexpected transcript")):
            for name, value in (("Read", {}), ("Bash", None), ("Glob", {"pattern": "**/*.py"})):
                self.assertEqual(self._run({"tool_name": name, "tool_input": value}, "T-test"), 0)
        self.assertEqual(self._log_lines(), [])
        self.assertFalse(jev_gate.should_skip("Glob", {"pattern": "*.py", "path": "/src"}))
        self.assertFalse(jev_gate.should_skip("Grep", {"pattern": "TODO"}))

    def test_config_cached(self):
        self._orig_cfg.cache_clear()
        try:
            with patch.object(jev_gate.P, "config", return_value={"jev": LOG_CFG}) as config:
                self.assertEqual(jev_gate._cfg(), LOG_CFG)
                self.assertEqual(jev_gate._cfg(), LOG_CFG)
                config.assert_called_once_with()
        finally:
            self._orig_cfg.cache_clear()

    def test_block_repeats_still_reaches_block_path_with_economy(self):
        task, payload, target = self._sample_fixture(1.0)
        jev_gate._cfg = lambda: {**BLOCK_CFG, "block_repeats": True}
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")):
            for tool in ("Read", "Grep", "Glob"):
                with self.subTest(tool=tool):
                    payload.update(session_id="block-" + tool, tool_name=tool,
                                   tool_input={"path": str(target), "pattern": "original"})
                    self.assertEqual(self._run(payload, task), 0)
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(self._run(payload, task), 2)
                    self.assertIn("identical read already made", err.getvalue())
                    row = self._log_lines()[-1]
                    self.assertTrue(row["blocked"])
                    self.assertTrue(row["would_suppress"])
                    self.assertFalse(row["scored"])

    def test_block_path_row_carries_economy_fields(self):
        task, payload, target = self._sample_fixture()
        with patch.object(jev, "ask", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self._run(payload, task), 0)
            # Exact-repeat state compares mtime; economy also notices size changes.
            stat = target.stat()
            target.write_text("changed and longer")
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            jev_gate._cfg = lambda: {**BLOCK_CFG, "block_repeats": True}
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self._run(payload, task), 2)
        row = self._log_lines()[-1]
        self.assertTrue(row["blocked"])
        self.assertTrue(row["repeat"])
        self.assertEqual(row["read_kind"], "repeated_read_changed")
        self.assertEqual(row["tokens_estimate"], target.stat().st_size // 4)
        self.assertFalse(row["would_suppress"])
        self.assertFalse(row["scored"])

    def test_first_reads_skip_jev_sample_and_log_read_kind(self):
        task, payload, _target = self._sample_fixture(1.0)
        with patch.object(jev, "ask", side_effect=AssertionError("first read must not call Jev")) as ask:
            self.assertEqual(self._run(payload, task), 0)
        ask.assert_not_called()
        row, = self._log_lines()
        self.assertEqual(row["read_kind"], "first_read")
        self.assertFalse(row["sampled"])
        self.assertFalse(row["would_suppress"])

    def test_gate_never_denies_on_would_suppress(self):
        task, payload, _target = self._sample_fixture(1.0)
        with patch.object(jev, "ask", side_effect=AssertionError("deterministic repeat must not call Jev")):
            self.assertEqual(self._run(payload, task), 0)
            self.assertEqual(self._run(payload, task), 0)
        row = self._log_lines()[-1]
        self.assertEqual(row["read_kind"], "repeated_read_unchanged")
        self.assertTrue(row["would_suppress"])
        self.assertFalse(row["blocked"])


if __name__ == "__main__":
    unittest.main()
