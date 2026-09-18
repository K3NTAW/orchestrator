"""orchestrator.serve: the per-user goal HTTP endpoint. Exercised entirely through starlette's TestClient with
orchestrator.goals.start/list_goals/status/stop patched -- no real git, no real Popen. ORCH_WORK and
ORCH_REPOS_TOML point at temp dirs so the per-slug lock and the repo config never touch this repo's own state."""
import inspect, json, subprocess, sys, tempfile, unittest
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
        self.assertTrue((self.work_dir / ".locks" / "clonable.lock").exists())

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
