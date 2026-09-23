import _harness
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock
from contextlib import ExitStack

from orchestrator import env_policy, executor, goals, planner_shadow, spawn
from orchestrator.pool import Pool


class EnvPolicy(unittest.TestCase):
    def call(self, mode, base=None, extra=None, **settings):
        return env_policy.worker_env("execute", base=base or {}, extra=extra or {},
                                     cfg={"secrets": {"env_mode": mode, **settings}}, task_id="T-env")

    def test_active_mode_allowlists_and_keeps_extra(self):
        base = {"PATH": "roads", "ORCH_LABEL": "label", "ORCH_TOKEN": "blue",
                "OTHER": "green", "SERVICE_PASSWORD": "orange"}
        with mock.patch.object(env_policy.bus, "log_run") as log:
            env, stripped = self.call("active", base, {"SERVICE_PASSWORD": "purple"})
        self.assertEqual(env, {"PATH": "roads", "ORCH_LABEL": "label", "SERVICE_PASSWORD": "purple"})
        self.assertEqual(stripped, ["ORCH_TOKEN", "OTHER"])
        self.assertEqual(log.call_count, 1)
        with mock.patch.object(env_policy.bus, "log_run", side_effect=OSError("unavailable")):
            self.assertEqual(self.call("active", base)[1], ["ORCH_TOKEN", "OTHER", "SERVICE_PASSWORD"])
        self.assertEqual(base["SERVICE_PASSWORD"], "orange")

    def test_shadow_mode_only_reports(self):
        base = {"PATH": "roads", "OTHER": "blue", "ORCH_TOKEN": "green"}
        with mock.patch.object(env_policy.bus, "log_run") as log:
            env, stripped = self.call("shadow", base, {"EXPLICIT": "orange"})
        self.assertEqual(env, {**base, "EXPLICIT": "orange"})
        self.assertEqual(stripped, ["ORCH_TOKEN", "OTHER"])
        log.assert_called_once_with(role="env_policy", task="T-env", outcome="shadow",
                                    stripped=stripped, count=2)

    def test_off_mode_noop(self):
        with mock.patch.object(env_policy.bus, "log_run") as log, \
                mock.patch.object(env_policy, "fnmatchcase", side_effect=AssertionError):
            self.assertEqual(self.call("off", {"OTHER": "blue"}, {"NEW": "green"},
                                       env_passthrough=None), ({"OTHER": "blue", "NEW": "green"}, []))
        log.assert_not_called()

    def test_log_row_never_contains_values(self):
        for mode in ("shadow", "active"):
            with mock.patch.object(env_policy.bus, "log_run") as log:
                self.call(mode, {"ORCH_TOKEN": "cerulean", "PATH": "vermilion"}, {"NEW": "ochre"})
            row = json.dumps(log.call_args.kwargs)
            for value in ("cerulean", "vermilion", "ochre"):
                self.assertNotIn(value, row)

    def test_token_veto_and_exact_name_override(self):
        base = dict.fromkeys(["GIT_AUTHOR_NAME", "SSH_AUTH_SOCK", "ORCH_TOKEN", "ORCH_CREDENTIALS",
                              "ORCH_KEY", "ORCH_SECRET", "ORCH_PASSWORD", "ORCH_PASSWD", "ORCH_CREDENTIAL",
                              "ORCH_AUTH", "SERVICE_TOKEN"], "blue")
        with mock.patch.object(env_policy.bus, "log_run"):
            env, _ = self.call("active", base, execute={"SERVICE_TOKEN": "env:SOURCE"})
            self.assertEqual(set(env), {"GIT_AUTHOR_NAME", "SSH_AUTH_SOCK", "SERVICE_TOKEN"})
            env, _ = self.call("active", base, env_passthrough=["ORCH_*", "ORCH_TOKEN"])
            self.assertEqual(set(env), {"ORCH_TOKEN"})
            env, _ = self.call("active", base, env_passthrough=["SSH_*"])
            self.assertEqual(env, {})

    def test_extra_always_overrides_base_and_pool_toml_parses(self):
        cfg = tomllib.loads((_harness.REPO / ".orchestrator/pool.toml").read_text())
        self.assertEqual(cfg["secrets"]["env_mode"], "shadow")
        self.assertEqual(cfg["secrets"]["env_passthrough"], list(env_policy.DEFAULT_PASSTHROUGH))
        for mode in ("off", "shadow", "active"):
            with mock.patch.object(env_policy.bus, "log_run"):
                env, stripped = self.call(mode, {"ORCH_ROOT": "stale"}, {"ORCH_ROOT": "fresh"},
                                          env_passthrough=[])
            self.assertEqual(env, {"ORCH_ROOT": "fresh"})
            self.assertEqual(stripped, [])

    def test_foreign_root_receives_rows_without_touching_home(self):
        home_runs = _harness.TMP / ".orchestrator/runs"
        before = {path.name: path.read_bytes() for path in home_runs.glob("*.jsonl")}
        cfg = {"secrets": {"env_mode": "shadow"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".orchestrator"
            (state / "prompts").mkdir(parents=True)
            (state / "pool.toml").write_text(
                '[secrets]\nenv_mode = "shadow"\n[models]\nplanner = "example"\n'
                '[[claude_accounts]]\nid = "A"\nconfig_dir = "example"\n')
            (state / "prompts/planner.md").write_text("example system prompt")
            runs = state / "runs"
            runs.mkdir()
            log = runs / f"{date.today().isoformat()}.jsonl"
            log.write_text('{"existing": true}\n')
            with ExitStack() as stack:
                home_log = stack.enter_context(mock.patch.object(
                    env_policy.bus, "log_run", wraps=env_policy.bus.log_run))
                stack.enter_context(mock.patch.dict(os.environ, {"UNLISTED": "cerulean"}, clear=True))
                stack.enter_context(mock.patch.object(goals.subprocess, "run", return_value=mock.Mock(
                    stdout="T-123", stderr="", returncode=0)))
                stack.enter_context(mock.patch.object(goals, "trust_workspace"))
                stack.enter_context(mock.patch.object(goals, "_proc_start", return_value=1))
                stack.enter_context(mock.patch.object(goals, "Popen", return_value=mock.Mock(pid=42)))
                stack.enter_context(mock.patch.object(spawn, "render", return_value="example prompt"))
                stack.enter_context(mock.patch.object(spawn, "Pool", return_value=mock.Mock(cfg=cfg)))
                self.assertEqual(goals._create_goal_task(root, "ochre"), ("T-123", None))
                goals.launch_planner(root, "p", "A", 1, root / "planner.log", task_id="T-goal")
                result = planner_shadow.launch(
                    "p", model="m", account={"config_dir": "example"}, budget_usd=1,
                    log=root / "shadow.log", root=root, task_id="T-shadow")
                self.assertEqual(result["pid"], 42)
                for mode in ("active", "off"):
                    env_policy.worker_env("execute", base={"UNLISTED": "vermilion"}, extra={},
                                          cfg={"secrets": {"env_mode": mode}}, root=root, task_id="T-direct")
                home_log.assert_not_called()
            raw = log.read_text()
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(rows.pop(0), {"existing": True})
            self.assertEqual(len(rows), 4)
            self.assertEqual([row["outcome"] for row in rows], ["shadow"] * 3 + ["active"])
            self.assertEqual([row["task"] for row in rows[1:]], ["T-goal", "T-shadow", "T-direct"])
            self.assertTrue(rows[0]["task"].startswith("goal-create-"))
            for row in rows:
                self.assertEqual(row["role"], "env_policy")
                self.assertEqual(row["stripped"], ["UNLISTED"])
                self.assertEqual(row["count"], 1)
                self.assertEqual(row["attempt"], 1)
                self.assertIsNone(row["context"])
                self.assertIsInstance(row["ts"], float)
            for value in ("cerulean", "ochre", "vermilion"):
                self.assertNotIn(value, raw)
        self.assertEqual(before, {path.name: path.read_bytes() for path in home_runs.glob("*.jsonl")})

    def test_root_alias_uses_home_bus_and_foreign_write_failure_is_swallowed(self):
        with mock.patch.object(env_policy.bus, "log_run") as log:
            env_policy.worker_env("execute", base={}, extra={}, cfg={},
                                  root=_harness.TMP / ".orchestrator" / "..")
            log.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "unwritable"
            root.write_text("example")
            with mock.patch.object(env_policy.bus, "log_run") as log:
                env, stripped = env_policy.worker_env(
                    "execute", base={"UNLISTED": "blue"}, extra={}, cfg={}, root=root)
                self.assertEqual(env, {"UNLISTED": "blue"})
                self.assertEqual(stripped, ["UNLISTED"])
                log.assert_not_called()

    def test_all_spawn_paths_use_worker_env(self):
        marker = {"POLICY_RESULT": "blue"}
        pool = Pool()
        task = {"id": "T-env", "role": "execute", "tier": "sonnet", "complexity": 1,
                "worktree": str(_harness.TMP)}
        with ExitStack() as stack:
            policy = stack.enter_context(mock.patch.object(env_policy, "worker_env", return_value=(marker, [])))
            stack.enter_context(mock.patch.object(spawn, "trust_workspace"))
            stack.enter_context(mock.patch.object(spawn, "secrets_for_role", return_value={}))
            stack.enter_context(mock.patch.object(spawn, "resolve_secrets", return_value={"SERVICE_TOKEN": "green"}))
            stack.enter_context(mock.patch.object(spawn.shutil, "which", return_value="claude"))
            stack.enter_context(mock.patch.object(spawn, "worker_registry"))
            stack.enter_context(mock.patch.object(executor, "worker_registry"))
            popen = stack.enter_context(mock.patch.object(spawn.subprocess, "Popen", side_effect=RuntimeError("stop")))
            with self.assertRaisesRegex(RuntimeError, "stop"):
                spawn.run_claude(pool, pool.get("A"), task, "p", "m", "Read", 1, 10)
            self.assertIs(popen.call_args.kwargs["env"], marker)
            self.assertEqual(policy.call_args.kwargs["task_id"], "T-env")
            for args in (["p"], ["resume", "thread", "p"]):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    executor._run(pool, task, args, _harness.TMP, 10)
                self.assertIs(popen.call_args.kwargs["env"], marker)
                self.assertEqual(policy.call_args.args, ("execute",))
                self.assertEqual(policy.call_args.kwargs["extra"]["SERVICE_TOKEN"], "green")
                self.assertEqual(policy.call_args.kwargs["extra"]["ORCH_TASK_ID"], "T-env")
            run = stack.enter_context(mock.patch.object(goals.subprocess, "run", return_value=mock.Mock(
                stdout="T-123", stderr="", returncode=0)))
            goals._create_goal_task(_harness.TMP, "example goal")
            self.assertIs(run.call_args.kwargs["env"], marker)
            self.assertEqual(policy.call_args.kwargs["extra"]["ORCH_GOAL_TEXT"], "example goal")
            stack.enter_context(mock.patch.object(goals, "trust_workspace"))
            stack.enter_context(mock.patch.object(goals, "resolve_secrets", return_value={}))
            stack.enter_context(mock.patch.object(goals, "_proc_start", return_value=1))
            launch = stack.enter_context(mock.patch.object(goals, "Popen", return_value=mock.Mock(pid=42)))
            with tempfile.TemporaryDirectory() as directory:
                goals.launch_planner(_harness.TMP, "p", "A", 1, Path(directory) / "planner.log", task_id="T-goal")
                self.assertIs(launch.call_args.kwargs["env"], marker)
                self.assertEqual(policy.call_args.kwargs["task_id"], "T-goal")
                self.assertEqual(policy.call_args.kwargs["extra"]["ORCH_ROOT"], str(_harness.TMP.resolve()))
                planner_shadow.launch("p", model="m", account={"config_dir": directory}, budget_usd=1,
                                      log=Path(directory) / "shadow.log", root=_harness.TMP, task_id="T-shadow")
                self.assertIs(launch.call_args.kwargs["env"], marker)
                self.assertEqual(policy.call_args.kwargs["task_id"], "T-shadow")
                self.assertEqual(policy.call_args.kwargs["extra"]["ORCH_SHADOW"], "1")
            self.assertEqual(policy.call_count, 6)
