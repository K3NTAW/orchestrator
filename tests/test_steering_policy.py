import _harness
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator import steering_policy as policy


def stale_fixture(paths=(), risk="none"):
    return {"stale_paths": list(paths), "risk": risk, "risk_reasons": [],
            "signals": {"overlapping_merge": []}, "moved_count": len(paths), "graph": "absent"}


class SteeringPolicyTests(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "T-policy", "parent": "T-goal", "status": "running",
                     "scope": ["orchestrator/work.py", "lib/work.py", "app/main.py"],
                     "pipeline": {}, "constraints": {}}
        self.inputs = dict(tasks={self.task["id"]: self.task},
                           registry_doc={"last_event_at": 950}, stale_evidence=stale_fixture(),
                           gate_history=[], cfg={}, critical=False, now=1000)

    def evaluate(self, **overrides):
        return policy.evaluate(self.task, **{**self.inputs, **overrides})

    def test_triggers_in_order_and_actions(self):
        with patch.object(policy, "changed_paths", return_value=["a", "b", "c", "d"]), \
                patch.object(policy, "no_commits", return_value=True):
            cases = [
                ({"stale_evidence": stale_fixture(["orchestrator/auth.py"], "high"),
                  "registry_doc": {"last_event_at": 0}, "gate_history": ["same", "same"]},
                 "security_concern", "cancel", "orchestrator/auth.py"),
                ({"stale_evidence": stale_fixture(["lib/api.py"], "medium"),
                  "registry_doc": {"last_event_at": 0}},
                 "dependency_changed", "steer", "lib/api.py"),
                ({"registry_doc": {"last_event_at": 0}}, "stuck", "steer", "no registry event"),
                ({"gate_history": ["same", "same"]}, "out_of_scope", "steer", "a, b, c, d"),
            ]
            for inputs, trigger, action, detail in cases:
                with self.subTest(trigger=trigger):
                    result = self.evaluate(**inputs)
                    self.assertEqual((result["trigger"], result["action"]), (trigger, action))
                    self.assertIn(trigger, result["message"])
                    self.assertIn(detail, result["message"])
        with patch.object(policy, "changed_paths", return_value=[]):
            result = self.evaluate(gate_history=["older", "check failed", "check failed"])
            self.assertEqual(result["trigger"], "repeated_failure")
            self.assertEqual(result["message"], "repeated_failure: check failed")
            self.assertEqual(result["action"], "steer")
            self.assertEqual(self.evaluate(gate_history=["same", "different"])["action"], "continue")
            self.assertEqual(self.evaluate(stale_evidence=stale_fixture(["elsewhere.py"], "high"))["action"], "continue")

    def test_min_interval_and_critical_path_rule(self):
        self.assertEqual(policy.DEFAULTS["min_interval_s"], 1800)
        evidence = stale_fixture(["orchestrator/auth.py"], "high")
        with patch.object(policy, "changed_paths", return_value=[]), \
                patch.object(policy, "no_commits", return_value=True) as guard:
            self.assertEqual(self.evaluate(stale_evidence=evidence, critical=True)["action"], "steer")
            guard.assert_not_called()
            self.assertEqual(self.evaluate(stale_evidence=evidence)["action"], "cancel")
            guard.return_value = False
            self.assertEqual(self.evaluate(stale_evidence=evidence)["action"], "steer")
            guard.side_effect = subprocess.TimeoutExpired("git", 5)
            result = self.evaluate(stale_evidence=evidence)
            self.assertEqual(result["action"], "steer")
            self.assertIn("git_unavailable", result["reasons"])

    def test_git_status_rules_timeout_and_ignored_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            self.task["worktree"] = directory
            output = ("R  lib/new name.py\0old name.py\0?? stray.py\0 M stray.py\0"
                      "?? __pycache__/x\0?? x.pyc\0?? .orchestrator/state\0"
                      "?? .venv/lib/x\0?? node_modules/x\0?? .git/config\0?? app/main.py\0")
            with patch.object(policy.subprocess, "run", return_value=Mock(returncode=0, stdout=output)) as run:
                self.assertEqual(policy.changed_paths(self.task), ["app/main.py", "lib/new name.py", "stray.py"])
                run.assert_called_once_with(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=directory,
                                            capture_output=True, text=True, timeout=5, check=False)
                self.assertEqual(self.evaluate(cfg={"steering": {"out_of_scope_events": 1}})["action"], "continue")
                result = self.evaluate(cfg={"steering": {"out_of_scope_events": 0}})
                self.assertEqual(result["message"], "out_of_scope: stray.py")
            for failure in [subprocess.TimeoutExpired("git", 5), OSError("unavailable")]:
                with patch.object(policy.subprocess, "run", side_effect=failure):
                    result = self.evaluate()
                    self.assertEqual(result["action"], "continue")
                    self.assertIn("git_unavailable", result["reasons"])
            with patch.object(policy.subprocess, "run", return_value=Mock(returncode=1)):
                self.assertIn("git_unavailable", self.evaluate()["reasons"])
            with patch.object(policy.subprocess, "run", side_effect=[Mock(returncode=0, stdout="base\n"),
                                                                      Mock(returncode=0, stdout="0\n")]) as run:
                self.assertTrue(policy.no_commits(self.task))
                self.assertEqual(run.call_args_list[-1].args[0], ["git", "rev-list", "--count", "base..HEAD"])
        self.assertIn("git_unavailable", self.evaluate()["reasons"])
        self.assertTrue(policy.matches("lib/sub/file.py", ["lib/"]))
        self.assertTrue(policy.matches("test/a.py", ["test/*.py"]))
        self.assertFalse(policy.matches("library/a", ["lib"]))

    def test_missing_inputs_errors_and_lineage(self):
        for key in policy.STALE_KEYS:
            evidence = stale_fixture()
            del evidence[key]
            self.assertEqual(self.evaluate(stale_evidence=evidence)["reasons"], ["missing_input"])
        with patch.object(policy, "changed_paths", side_effect=RuntimeError("unexpected")):
            self.assertEqual(self.evaluate()["reasons"], ["evaluation_error"])
        root = {"id": "root", "pipeline": {"gate_reds": 2}, "constraints": {"failure_signature": "first"}}
        middle = {"id": "middle", "constraints": {"fix_round_for": "root"}}
        leaf = {"id": "leaf", "pipeline": {"gate_reds": 1},
                "constraints": {"fix_round_for": "middle", "failure_signature": "last"}}
        self.assertEqual(policy.gate_history(leaf, {"root": root, "middle": middle}), ["first", "last"])

    def test_real_bus_shape_derives_packet_read_scope(self):
        self.assertNotIn("read_scope", self.task)
        with tempfile.TemporaryDirectory() as directory:
            wt = Path(directory)
            (wt / "app").mkdir()
            (wt / "shared").mkdir()
            (wt / "shared/api.py").write_text("value = 1\n")
            (wt / "app/main.py").write_text("from shared.api import value\n")
            self.task.update(worktree=directory, scope=["app/main.py"])
            self.assertEqual(policy.read_scope(self.task), ["app/", "shared/api.py", "tests/"])
            with patch.object(policy, "changed_paths", return_value=[]):
                result = self.evaluate(stale_evidence=stale_fixture(["shared/api.py"], "high"))
                self.assertEqual(result["trigger"], "dependency_changed")
                self.assertEqual(self.evaluate(registry_doc={"last_event_at": 0})["trigger"], "stuck")
            subprocess.run(["git", "init", "-q", directory], check=True, capture_output=True)
            (wt / "docs/newdir").mkdir(parents=True)
            (wt / "docs/newdir/page.md").write_text("example\n")
            self.task["scope"] += ["docs/newdir/*.md", "shared/api.py"]
            result = self.evaluate(cfg={"steering": {"out_of_scope_events": 0}})
            self.assertEqual(result["action"], "continue")
            self.assertIn("docs/newdir/page.md", policy.changed_paths(self.task))

    def test_shared_read_scope_skips_outside_paths(self):
        from orchestrator import spawn
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wt = root / "worktree"
            wt.mkdir()
            (root / "outside.py").write_text("value = 1\n")
            (wt / "linked.py").symlink_to(root / "outside.py")
            (wt / "app").mkdir()
            (wt / "app/main.py").write_text("import linked\n")
            task = {"scope": ["../outside.py", str(root / "outside.py"), "linked.py", "app/main.py"],
                    "worktree": str(wt)}
            self.assertEqual(policy.safe_scope(task), ["app/main.py"])
            self.assertEqual(policy.read_scope(task), ["app/", "tests/"])
            with patch.object(policy, "read_scope", side_effect=RuntimeError("shared helper reached")) as shared:
                with self.assertRaisesRegex(RuntimeError, "shared helper reached"):
                    spawn._packet_body(task, wt, cfg={})
                shared.assert_called_once_with(task, wt)

    def test_safe_scope_without_worktree_does_not_use_cwd(self):
        entries = ["../outside.py", "/synthetic/absolute.py", "app/main.py"]
        with patch.object(Path, "resolve", side_effect=AssertionError("must not resolve against cwd")):
            self.assertEqual(policy.safe_scope({"scope": entries}), entries)
            self.assertEqual(policy.safe_scope({"scope": entries, "worktree": None}), entries)
