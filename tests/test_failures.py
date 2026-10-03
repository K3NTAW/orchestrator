"""Shared failure evidence must be usable without the pipeline driver."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO
from orchestrator import failures, gitutil, bus


class FailureEvidence(unittest.TestCase):
    def test_failures_module_has_no_daemon_import(self):
        source = (REPO / "orchestrator/failures.py").read_text()
        self.assertIsNone(re.search(r"^\s*(?:from\s+[^\n]*\bdaemon\b\s+import|from\s+\.\s+import\s+[^\n]*\bdaemon\b|import\s+[^\n]*\bdaemon\b)", source, re.M))

    def test_touch_areas_fail_closed_without_diff(self):
        for paths, lines in ((None, []), ([], None), (None, None)):
            with patch.object(gitutil, "changed_paths", return_value=paths), patch.object(gitutil, "_added_diff_lines", return_value=lines):
                self.assertEqual(failures.touch_areas({}, {}), {area: True for area in failures.DEFAULT_AREAS})

    def test_touch_areas_uses_configured_paths_and_patterns(self):
        cfg = {"review": {"areas": {"auth": {"paths": ["security/**"], "patterns": []},
                                     "data_deletion": {"paths": [], "patterns": ["erase"]}}}}
        with patch.object(gitutil, "changed_paths", return_value=["security/check.py"]), patch.object(gitutil, "_added_diff_lines", return_value=["erase(records)"]):
            self.assertEqual(failures.touch_areas({}, cfg), {"auth": True, "migrations": False, "interfaces": False, "data_deletion": True})

    def test_test_ids_reject_shell_and_traversal(self):
        self.assertEqual(failures.test_ids("FAILED tests/test_x.py::test_ok\nFAILED ../../evil.py\nFAILED $(id)"),
                         ["tests/test_x.py::test_ok"])
        ids, rejected = failures._test_ids_with_rejections(
            "FAILED tests/test_x.py::test_ok (missing: test not defined)\n"
            "FAILED --rootdir=/ ../x.py::t")
        self.assertEqual(ids, ["tests/test_x.py::test_ok"])
        self.assertEqual(set(rejected), {"--rootdir=/", "../x.py::t"})

    def test_gate_red_with_cooling_test_id_is_not_quota(self):
        task = {"id": "T-test", "hold_reason": "gate_red",
                "resume_hint": {"failures": "FAILED tests/test_gate_timeout.py::test_gate_timeout_ctx_wins_over_cooling_account (missing: test not defined)"}}
        self.assertEqual(failures.failure_kind(task, None), "code_defect")

        quota_task = {"id": "T-quota", "hold_reason": "codex usage limit",
                      "resume_hint": {"failures": "FAILED tests/test_gate_timeout.py::test_gate_timeout_ctx_wins_over_cooling_account"}}
        self.assertEqual(failures.failure_kind(quota_task, None), "quota")

    def test_review_request_changes_hold_is_code_defect(self):
        with tempfile.TemporaryDirectory(prefix="orch-failures-") as directory:
            root = Path(directory)
            with patch.object(bus, "STATE", root), patch.object(bus, "TASKS", root / "tasks"), \
                    patch.object(bus, "RUNS", root / "runs"):
                task = bus.create_task("held", "spec", ["passes"], ["x.py"], role="execute")
                bus.update(task["id"], status="held", hold_reason="review request_changes: T-review (request_changes)",
                           resume_hint={})
                review = bus.create_task("review", "review", ["reports"], ["x.py"], role="review",
                                         inputs=[task["id"]])
                bus.update(review["id"], status="done", result={"verdict": "request_changes",
                           "comments": [{"path": "x.py", "line": 3, "issue": "fix this code"}]})
                self.assertEqual(failures.failure_kind(bus.get(task["id"]), None), "code_defect")

                no_review = bus.create_task("held without review", "spec", ["passes"], ["x.py"], role="execute")
                bus.update(no_review["id"], status="held", hold_reason="review request_changes: T-missing",
                           resume_hint={})
                self.assertEqual(failures.failure_kind(bus.get(no_review["id"]), None), "unknown")

                invalid = bus.create_task("held invalid", "spec", ["passes"], ["x.py"], role="execute")
                bus.update(invalid["id"], status="held", hold_reason="review request_changes: T-invalid",
                           resume_hint={})
                invalid_review = bus.create_task("invalid review", "review", ["reports"], ["x.py"], role="review",
                                                inputs=[invalid["id"]])
                bus.update(invalid_review["id"], status="done", result={"verdict": "request_changes",
                           "comments": [{"path": "x.py", "line": 4,
                                          "issue": "the spec contradicts itself"}]})
                self.assertEqual(failures.failure_kind(bus.get(invalid["id"]), None), "invalid_spec")

    def test_failure_kind_incomplete_for_execute_incomplete_holds(self):
        self.enterContext(patch.object(bus, "read", return_value=[]))
        ordinary = "non-JSON output (rc=143): conflict"
        for reason in ("execute_incomplete: no commits", "execute_incomplete: uncommitted changes"):
            self.assertEqual(failures.failure_kind({"hold_reason": reason,
                                                     "resume_hint": {"partial_output": ordinary}}, None), "incomplete")
        cases = [("usage limit reached", "quota"), ("permission denied", "permissions"),
                 ("ModuleNotFoundError: missing", "environment")]
        for partial_output, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(failures.failure_kind({"hold_reason": "execute_incomplete: no commits",
                                                         "resume_hint": {"partial_output": partial_output}}, None), expected)
        self.assertEqual(failures.failure_kind({"hold_reason": "execute_incomplete: no commits"}, None), "incomplete")
        self.assertEqual(failures.failure_kind({"id": "T-incomplete-kind", "hold_reason": "gate_red",
                                                 "resume_hint": {"failures": "FAILED tests/test_x.py::test_x"}}, None),
                         "code_defect")

    def test_failure_signature_for_incomplete_uses_head_and_dirty_set(self):
        import hashlib
        import json
        self.enterContext(patch.object(bus, "read", return_value=[]))
        def task(head="h1", in_scope=None, out_scope=None):
            return {"id": "T-incomplete-signature", "hold_reason": "execute_incomplete: no commits", "pipeline": {"failure_kind": "incomplete"},
                    "resume_hint": {"head": head, "dirty_in_scope": in_scope or [],
                                     "dirty_out_of_scope": out_scope or []}}
        base = task(in_scope=["b.py", "a.py"], out_scope=["z.py"])
        self.assertEqual(failures.failure_signature(base),
                         failures.failure_signature(task(in_scope=["a.py", "b.py"], out_scope=["z.py"])))
        self.assertNotEqual(failures.failure_signature(base), failures.failure_signature(
            task(head="h2", in_scope=["b.py", "a.py"], out_scope=["z.py"])))
        self.assertNotEqual(failures.failure_signature(base), failures.failure_signature(task(in_scope=["a.py"])))
        self.assertEqual(failures.failure_signature(task(head=None)), failures.failure_signature(task(head="")))
        missing_head = task()
        missing_head["resume_hint"].pop("head")
        self.assertEqual(failures.failure_signature(missing_head), failures.failure_signature(task(head="")))
        gate = {"id": "T-gate-signature", "hold_reason": "gate_red", "pipeline": {"failure_kind": "code_defect"},
                "resume_hint": {"failures": "FAILED tests/test_x.py::test_x"}}
        legacy_payload = json.dumps({"kind": "code_defect", "tests": ["tests/test_x.py::test_x"],
                                     "comments": []}, separators=(",", ":"), sort_keys=True)
        self.assertEqual(failures.failure_signature(gate), hashlib.sha256(legacy_payload.encode()).hexdigest()[:12])
