import _harness  # noqa: F401
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import jev, jev_skills


def records():
    return {name: {"version": version} for name, version in (("s/a", "1"), ("s/b", "2"), ("s/c", "3"))}


def answer(value):
    return {"noul": value}


def response(ids, values=None):
    values = values or {}
    answers = {}
    for skill_id in ids:
        for name in jev_skills.QUESTION_NAMES:
            answers[f"{skill_id}:{name}"] = answer(values.get(name, .7 if name != "duplicates" else .1))
    return {"answers": answers, "usage": {"input_tokens": 10}}


class JevSkillsTests(unittest.TestCase):
    def run_classify(self, ids, ask, cfg=None):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(jev_skills, "STATE", Path(root)), \
                mock.patch.object(jev_skills.skills_registry, "load", return_value={"skills": records()}), \
                mock.patch.object(jev_skills.skills_registry, "render", side_effect=lambda item, level: f"L1 {item}"), \
                mock.patch.object(jev_skills, "_repo_head", return_value="head"):
            return jev_skills.classify({"id": "T-1", "title": "task", "spec": "spec", "scope": ["x.py"]},
                                       "execute", ids, cfg=cfg or {}, ask=ask)

    def test_only_ambiguous_skills_are_sent_in_one_batch(self):
        calls = []
        def ask(state, questions, **kwargs):
            calls.append((state, questions, kwargs))
            return response(["s/a", "s/b"])
        result = self.run_classify(["s/a", "s/b"], ask)
        self.assertEqual(len(calls), 1)
        self.assertEqual([row["id"] for row in calls[0][0]["skills"]], ["s/a", "s/b"])
        self.assertEqual(result["batch_size"], 2)

    def test_selection_rule_thresholds(self):
        def ask(state, questions, **kwargs):
            values = response(["s/a", "s/b"])
            values["answers"]["s/a:relevant"] = answer(.59)
            values["answers"]["s/b:duplicates"] = answer(.49)
            values["answers"]["s/b:worth_cost"] = answer(.5)
            return values
        result = self.run_classify(["s/a", "s/b"], ask)
        self.assertFalse(result["decisions"]["s/a"]["select"])
        self.assertTrue(result["decisions"]["s/b"]["select"])

    def test_unavailable_jev_keeps_bucket_excluded(self):
        result = self.run_classify(["s/a"], lambda *args, **kwargs: None)
        self.assertEqual(result["source"], "none")
        self.assertFalse(any(row.get("select") for row in result["decisions"].values()))

    def test_cache_key_invalidates_on_skill_version_or_head_change(self):
        task = {"id": "T", "title": "x", "spec": "y", "scope": ["z"]}
        first = jev_skills._cache_key(task, {"s/a": {"version": "1"}}, ["s/a"])
        with mock.patch.object(jev_skills, "_repo_head", return_value="other"):
            head = jev_skills._cache_key(task, {"s/a": {"version": "1"}}, ["s/a"])
        version = jev_skills._cache_key(task, {"s/a": {"version": "2"}}, ["s/a"])
        self.assertEqual(len({first, head, version}), 3)

    def test_boundary_drops_undeclared_fields_and_never_carries_content(self):
        captured = {}
        original_cfg = jev._cfg
        with mock.patch.object(jev, "_cfg", return_value={**original_cfg(), "enabled": True}), \
                mock.patch.object(jev, "_api_key", return_value="key"), \
                mock.patch.object(jev, "_day_tokens_used", return_value=0), \
                mock.patch.object(jev.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'{"answers": {}}'
            jev.ask({"title": "x", "packet": "forbidden", "content": "secret"}, {}, site="skills")
            captured.update(json.loads(opened.call_args.args[0].data)["state"] and
                            json.loads(json.loads(opened.call_args.args[0].data)["state"]))
        self.assertEqual(captured, {"title": "x"})

    def test_batch_truncation_order_and_not_sent(self):
        result = self.run_classify(["s/a", "s/b", "s/c"],
                                   lambda state, questions, **kwargs: response(["s/a", "s/b"]),
                                   {"skills": {"jev_max_batch": 2}})
        self.assertEqual(result["decisions"]["s/c"]["status"], "not_sent")
        self.assertEqual(result["batch_size"], 2)

    def test_no_import_cycle_between_executor_and_skill_router(self):
        code = "import orchestrator.executor; import orchestrator.skill_router"
        result = subprocess.run([sys.executable, "-c", code], cwd=_harness.REPO,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
