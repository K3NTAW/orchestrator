"""orchestrator.serve: the per-user goal HTTP endpoint. Exercised entirely through starlette's TestClient with
orchestrator.goals.start/list_goals/status/stop patched -- no real git, no real Popen. ORCH_WORK and
ORCH_REPOS_TOML point at temp dirs so the per-slug lock and the repo config never touch this repo's own state."""
import asyncio, fcntl, inspect, json, subprocess, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_serve.py` needs this
from _harness import REPO, TMP  # noqa: F401

from starlette.testclient import TestClient

import orchestrator.goals
from orchestrator import serve

TOKEN = "s3cr3t-test-token"


class ServeTestCase(unittest.TestCase):
    def setUp(self):
        patch_token = mock.patch.dict("os.environ", {"ORCH_SERVICE_TOKEN": TOKEN})
        patch_token.start()
        self.addCleanup(patch_token.stop)

        self.work_dir = Path(tempfile.mkdtemp(prefix="orch-work-"))
        self.repo_dir = Path(tempfile.mkdtemp(prefix="orch-repo-"))
        self.repos_toml = Path(tempfile.mkdtemp(prefix="orch-repocfg-")) / "repos.toml"
        patch_env = mock.patch.dict("os.environ", {
            "ORCH_WORK": str(self.work_dir),
            "ORCH_REPOS_TOML": str(self.repos_toml),
        })
        patch_env.start()
        self.addCleanup(patch_env.stop)

        self._write_repos({"demo": {"path": str(self.repo_dir)}})
        self.client = TestClient(serve.create_app())

    def _write_repos(self, mapping):
        lines = []
        for slug, cfg in mapping.items():
            lines.append(f"[{slug}]")
            for k, v in cfg.items():
                lines.append(f"{k} = {json.dumps(v)}")
            lines.append("")
        self.repos_toml.write_text("\n".join(lines))

    def auth(self, token=TOKEN):
        return {"Authorization": f"Bearer {token}"}


class HealthAndAuth(ServeTestCase):
    def test_healthz_no_auth(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_rejects_missing_or_wrong_bearer(self):
        r = self.client.get("/goals")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"reason": "unauthorized"})

        r2 = self.client.get("/goals", headers=self.auth("wrong-token"))
        self.assertEqual(r2.status_code, 401)
        self.assertEqual(r2.json(), {"reason": "unauthorized"})

        r3 = self.client.get("/goals", headers={"Authorization": "Basic abc"})
        self.assertEqual(r3.status_code, 401)

    def test_non_ascii_bearer_401_not_500(self):
        # Headers arrive latin-1 decoded; a raw non-ASCII byte must not raise inside hmac.compare_digest.
        r = self.client.get("/goals", headers={"Authorization": b"Bearer \xe9"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"reason": "unauthorized"})


class CreateGoalValidation(ServeTestCase):
    def test_unknown_repo_404(self):
        r = self.client.post("/goals", json={"repo": "nope", "goal": "build the thing"}, headers=self.auth())
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"reason": "unknown repo"})

    def test_empty_goal_400(self):
        r = self.client.post("/goals", json={"repo": "demo", "goal": "   "}, headers=self.auth())
        self.assertEqual(r.status_code, 400)
        self.assertIn("reason", r.json())

        r2 = self.client.post("/goals", json={"repo": "demo"}, headers=self.auth())
        self.assertEqual(r2.status_code, 400)

        r3 = self.client.post("/goals", json={"goal": "x"}, headers=self.auth())
        self.assertEqual(r3.status_code, 400)

    def test_goal_too_long_400(self):
        goal = "x" * (serve.MAX_GOAL_CHARS + 1)
        r = self.client.post("/goals", json={"repo": "demo", "goal": goal}, headers=self.auth())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"reason": "goal too long"})

    def test_body_too_large_413(self):
        goal = "x" * (serve.MAX_BODY_BYTES + 1)
        body = json.dumps({"repo": "demo", "goal": goal}).encode()
        r = self.client.post("/goals", content=body,
                              headers={**self.auth(), "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json(), {"reason": "body too large"})

    def test_content_length_over_limit_rejects_without_reading_stream(self):
        fake_request = mock.Mock()
        fake_request.headers.get.return_value = str(serve.MAX_BODY_BYTES + 1)
        result = asyncio.run(serve._read_body_bounded(fake_request))
        self.assertIsNone(result)
        fake_request.stream.assert_not_called()

    def test_chunked_body_over_limit_aborts_after_one_extra_chunk(self):
        consumed = []

        async def gen():
            consumed.append(1)
            yield b"a" * (serve.MAX_BODY_BYTES - 10)
            consumed.append(2)
            yield b"a" * 1000  # pushes the running total over the limit
            consumed.append(3)
            yield b"a" * 1000  # must never be reached

        fake_request = mock.Mock()
        fake_request.headers.get.return_value = None
        fake_request.stream.return_value = gen()

        result = asyncio.run(serve._read_body_bounded(fake_request))
        self.assertIsNone(result)
        self.assertEqual(consumed, [1, 2])

    def test_invalid_json_body_400(self):
        r = self.client.post("/goals", content=b"{not valid json",
                              headers={**self.auth(), "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"reason": "invalid json body"})

    def test_bad_requester_type_400(self):
        r = self.client.post("/goals", json={"repo": "demo", "goal": "g", "requester": 123}, headers=self.auth())
        self.assertEqual(r.status_code, 400)

        r2 = self.client.post("/goals", json={"repo": "demo", "goal": "g", "account": 5}, headers=self.auth())
        self.assertEqual(r2.status_code, 400)


class CreateGoalLifecycle(ServeTestCase):
    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    def test_start_returns_goal_id_and_requester(self, mock_list, mock_start):
        mock_list.return_value = []
        mock_start.return_value = {"launched": True, "goal_id": "T-1001"}
        r = self.client.post("/goals", json={"repo": "demo", "goal": "build it", "requester": "alice"},
                             headers=self.auth())
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json(), {"goal_id": "T-1001", "repo": "demo", "status": "running", "requester": "alice"})
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        self.assertEqual(kwargs["requester"], "alice")
        self.assertEqual(kwargs["account_id"], "A")

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    def test_start_refusal_422(self, mock_list, mock_start):
        mock_list.return_value = []
        mock_start.return_value = {"launched": False, "reason": "target index has staged changes"}
        r = self.client.post("/goals", json={"repo": "demo", "goal": "build it"}, headers=self.auth())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json(), {"reason": "target index has staged changes"})

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    def test_start_refusal_reason_is_redacted_and_logged(self, mock_list, mock_start):
        mock_list.return_value = []
        mock_start.return_value = {"launched": False,
                                    "reason": "git add failed: fatal: https://u:SECRET@h/r.git"}
        with self.assertLogs("orchestrator.serve.access", level="ERROR") as logs:
            r = self.client.post("/goals", json={"repo": "demo", "goal": "build it"}, headers=self.auth())
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("SECRET", r.text)
        self.assertIn("***", r.json()["reason"])
        self.assertTrue(any("SECRET" not in entry and "***" in entry for entry in logs.output))

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    def test_start_refusal_reason_capped_at_300_chars(self, mock_list, mock_start):
        mock_list.return_value = []
        mock_start.return_value = {"launched": False, "reason": "x" * 1000}
        r = self.client.post("/goals", json={"repo": "demo", "goal": "build it"}, headers=self.auth())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(len(r.json()["reason"]), 300)

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    def test_second_start_409_while_running(self, mock_list, mock_start):
        mock_list.return_value = [{"goal_id": "T-99", "record_status": "running", "planner_alive": True}]
        r = self.client.post("/goals", json={"repo": "demo", "goal": "build it"}, headers=self.auth())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"reason": "goal already running", "goal_id": "T-99"})
        mock_start.assert_not_called()


class ListAndGetGoals(ServeTestCase):
    @mock.patch("orchestrator.goals.list_goals")
    def test_list_entries_carry_slug(self, mock_list):
        mock_list.return_value = [{"goal_id": "T-1", "record_status": "done"}]
        r = self.client.get("/goals?repo=demo", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"goals": [{"goal_id": "T-1", "record_status": "done", "repo": "demo"}]})

    @mock.patch("orchestrator.goals.list_goals")
    def test_list_unfiltered_fans_out_across_repos(self, mock_list):
        other_dir = Path(tempfile.mkdtemp(prefix="orch-repo2-"))
        self._write_repos({"demo": {"path": str(self.repo_dir)}, "other": {"path": str(other_dir)}})

        def fake_list(path):
            return [{"goal_id": "T-1"}] if path == str(self.repo_dir) else [{"goal_id": "T-2"}]
        mock_list.side_effect = fake_list

        r = self.client.get("/goals", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        out = r.json()["goals"]
        self.assertEqual({(g["repo"], g["goal_id"]) for g in out}, {("demo", "T-1"), ("other", "T-2")})

    def test_list_repo_not_configured_404(self):
        self._write_repos({"demo": {"path": str(self.repo_dir)},
                           "unpathed": {"git_url": "https://example.com/x.git"}})
        r = self.client.get("/goals?repo=unpathed", headers=self.auth())
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"reason": "repo not configured"})

    @mock.patch("orchestrator.goals.list_goals")
    def test_fan_out_isolates_one_failing_repo(self, mock_list):
        other_dir = Path(tempfile.mkdtemp(prefix="orch-repo2-"))
        self._write_repos({"demo": {"path": str(self.repo_dir)}, "other": {"path": str(other_dir)}})

        def fake_list(path):
            if path == str(other_dir):
                raise RuntimeError("boom")
            return [{"goal_id": "T-1"}]
        mock_list.side_effect = fake_list

        r = self.client.get("/goals", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        out = r.json()["goals"]
        self.assertIn({"goal_id": "T-1", "repo": "demo"}, out)
        self.assertIn({"repo": "other", "error": "unreadable"}, out)

    @mock.patch("orchestrator.goals.status")
    def test_get_goal_returns_single_entry_200(self, mock_status):
        mock_status.return_value = [{"goal_id": "T-1", "record_status": "done"}]
        r = self.client.get("/goals/demo/T-1", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"goal_id": "T-1", "record_status": "done", "repo": "demo"})

    @mock.patch("orchestrator.goals.status")
    def test_get_goal_unknown_404(self, mock_status):
        mock_status.return_value = []
        r = self.client.get("/goals/demo/T-404", headers=self.auth())
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"reason": "unknown goal"})


class ReposTomlUnreadable(ServeTestCase):
    def test_malformed_repos_toml_503_on_goal_routes(self):
        self.repos_toml.write_text("not [ valid toml")

        r = self.client.get("/goals", headers=self.auth())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"reason": "repos.toml unreadable"})

        r2 = self.client.post("/goals", json={"repo": "demo", "goal": "g"}, headers=self.auth())
        self.assertEqual(r2.status_code, 503)
        self.assertEqual(r2.json(), {"reason": "repos.toml unreadable"})

    def test_healthz_unaffected_by_malformed_repos_toml(self):
        self.repos_toml.write_text("not [ valid toml")
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_missing_repos_toml_is_503(self):
        self.repos_toml.unlink()

        r = self.client.get("/goals", headers=self.auth())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"reason": "service not configured"})

        r2 = self.client.post("/goals", json={"repo": "demo", "goal": "g"}, headers=self.auth())
        self.assertEqual(r2.status_code, 503)
        self.assertEqual(r2.json(), {"reason": "service not configured"})

    def test_healthz_unaffected_by_missing_repos_toml(self):
        self.repos_toml.unlink()
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)


class SafeReasonRedaction(unittest.TestCase):
    def test_safe_reason_redacts_bearer(self):
        reason = "refused: Authorization: Bearer abcDEF123.token-xyz was rejected upstream"
        redacted = serve._safe_reason(reason, "ctx")
        self.assertNotIn("abcDEF123", redacted)
        self.assertIn("***", redacted)

    def test_safe_reason_redacts_keyvalue_secret(self):
        reason = "git push failed: api_key=SECRETVALUE123 invalid"
        redacted = serve._safe_reason(reason, "ctx")
        self.assertNotIn("SECRETVALUE123", redacted)
        self.assertIn("***", redacted)


class CloneUnderLock(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.missing_path = str(self.work_dir / "not-cloned-yet")
        self._write_repos({"demo": {"path": str(self.repo_dir)},
                           "clonable": {"path": self.missing_path, "git_url": "https://example.com/repo.git"}})

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    @mock.patch("orchestrator.serve.subprocess.run")
    def test_clone_runs_under_lock_once_with_timeout(self, mock_run, mock_list, mock_start):
        mock_run.return_value = subprocess.CompletedProcess(["git", "clone"], 0, stdout="", stderr="")
        mock_list.return_value = []
        mock_start.return_value = {"launched": True, "goal_id": "T-2000"}
        r = self.client.post("/goals", json={"repo": "clonable", "goal": "g"}, headers=self.auth())
        self.assertEqual(r.status_code, 201, r.text)
        mock_run.assert_called_once()
        argv, kwargs = mock_run.call_args
        self.assertEqual(list(argv[0])[:2], ["git", "clone"])
        self.assertIn("timeout", kwargs)

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    @mock.patch("orchestrator.serve.subprocess.run")
    def test_clone_argv_has_double_dash(self, mock_run, mock_list, mock_start):
        mock_run.return_value = subprocess.CompletedProcess(["git", "clone"], 0, stdout="", stderr="")
        mock_list.return_value = []
        mock_start.return_value = {"launched": True, "goal_id": "T-2001"}
        r = self.client.post("/goals", json={"repo": "clonable", "goal": "g"}, headers=self.auth())
        self.assertEqual(r.status_code, 201, r.text)
        argv = list(mock_run.call_args[0][0])
        self.assertEqual(argv[:3], ["git", "clone", "--"])
        self.assertEqual(argv[3], "https://example.com/repo.git")

    @mock.patch("orchestrator.goals.start")
    @mock.patch("orchestrator.goals.list_goals")
    def test_repo_lock_serializes_requests(self, mock_list, mock_start):
        mock_list.return_value = []
        mock_start.return_value = {"launched": True, "goal_id": "T-3000"}

        lock_fh = open(serve._lock_path("demo"), "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = {}

        def do_post():
            result["response"] = self.client.post("/goals", json={"repo": "demo", "goal": "g"},
                                                    headers=self.auth())

        t = threading.Thread(target=do_post)
        t.start()
        try:
            t.join(timeout=0.5)
            self.assertTrue(t.is_alive(), "request should still be blocked on the held lock")
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["response"].status_code, 201, result["response"].text)

    @mock.patch("orchestrator.serve.subprocess.run")
    def test_clone_failure_502_does_not_echo_stderr(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            ["git", "clone"], 1, stdout="", stderr="fatal: https://user:SECRET@host/repo.git failed")
        r = self.client.post("/goals", json={"repo": "clonable", "goal": "g"}, headers=self.auth())
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("SECRET", r.text)
        self.assertEqual(r.json(), {"reason": "clone failed"})

    @mock.patch("orchestrator.serve.subprocess.run")
    def test_clone_timeout_504(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=serve.CLONE_TIMEOUT_S)
        r = self.client.post("/goals", json={"repo": "clonable", "goal": "g"}, headers=self.auth())
        self.assertEqual(r.status_code, 504)
        self.assertEqual(r.json(), {"reason": "clone timed out"})


class CancelGoal(ServeTestCase):
    @mock.patch("orchestrator.goals.stop")
    def test_cancel_marks_stopped(self, mock_stop):
        mock_stop.return_value = {"stopped": "T-1"}
        r = self.client.post("/goals/demo/T-1/cancel", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"goal_id": "T-1", "repo": "demo", "status": "stopped"})

    @mock.patch("orchestrator.goals.stop")
    def test_cancel_unknown_404(self, mock_stop):
        mock_stop.return_value = {"error": "unknown goal"}
        r = self.client.post("/goals/demo/T-404/cancel", headers=self.auth())
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"reason": "unknown goal"})

    @mock.patch("orchestrator.goals.stop")
    def test_cancel_returns_200_when_planner_already_gone(self, mock_stop):
        mock_stop.side_effect = ProcessLookupError()
        r = self.client.post("/goals/demo/T-1/cancel", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"goal_id": "T-1", "repo": "demo", "status": "stopped",
                                     "note": "planner already gone"})

    @mock.patch("orchestrator.goals.stop")
    def test_cancel_returns_200_on_permission_error(self, mock_stop):
        mock_stop.side_effect = PermissionError()
        r = self.client.post("/goals/demo/T-1/cancel", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"goal_id": "T-1", "repo": "demo", "status": "stopped",
                                     "note": "planner already gone"})


class ServeShape(ServeTestCase):
    def test_handlers_are_sync(self):
        app = serve.create_app()
        for route in app.routes:
            endpoint = route.endpoint
            self.assertFalse(inspect.iscoroutinefunction(endpoint), f"{endpoint} must be a plain def")

    def test_serve_imports_goals_as_module(self):
        self.assertIs(serve.goals, orchestrator.goals)


if __name__ == "__main__":
    unittest.main()
