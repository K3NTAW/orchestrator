import _harness
import json, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from orchestrator import decision_log, memory_scorecard, memory_store


class MemoryScorecard(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.state = self.repo / ".orchestrator"
        (self.state / "tasks").mkdir(parents=True)

    def task(self, task_id, **fields):
        row = {"id": task_id, "created_at": time.time(), "scope": ["fallback.py"],
               "constraints": {}, "merged_into": "main", "result": {"summary": ""}, **fields}
        (self.state / "tasks" / f"{task_id}.json").write_text(json.dumps(row))
        return row

    def retrieval(self, task_id, mode="active", selected=(), legacy=(), candidates=(), tiered=10, legacy_tokens=20):
        decision_log.record("retrieval", task_id, candidates=list(candidates), hard_constraints={},
                            deterministic={}, selected=list(selected), reason="fixture", mode=mode,
                            extra={"legacy_ids": list(legacy), "tokens_tiered": tiered,
                                   "tokens_legacy": legacy_tokens}, root=self.state)

    def test_precision_and_downstream_use_from_rows_and_outcomes(self):
        self.task("T-1", changed_files=["orchestrator/router.py"],
                  result={"summary": "Applied the cache policy."})
        memory_store.add(memory_store.Record(id="router", title="Router choice", files=["orchestrator/router.py"]), self.repo)
        memory_store.add(memory_store.Record(id="cache", title="Cache policy"), self.repo)
        self.retrieval("T-1", selected=["router", "cache"], candidates=[{"id": "router"}, {"id": "cache"}], tiered=100)
        card = memory_scorecard.build(self.state)
        row = card["rows"][0]
        self.assertEqual((row["records_retrieved"], row["records_presented"], row["used"]), (2, 2, 2))
        self.assertEqual(card["by_mode"]["active"]["precision"], 1.0)
        self.assertEqual(card["by_mode"]["active"]["utility_per_token"], 20.0)

    def test_repeated_and_irrelevant_counted(self):
        self.task("T-1", parent="G-1", changed_files=[])
        self.task("T-2", parent="G-1", changed_files=[])
        memory_store.add(memory_store.Record(id="unused", title="Unrelated fact"), self.repo)
        self.retrieval("T-1", selected=["unused"], candidates=[{"id": "unused"}])
        self.retrieval("T-2", selected=["unused"], candidates=[{"id": "unused"}])
        card = memory_scorecard.build(self.state)
        self.assertEqual(card["by_mode"]["active"]["irrelevant"], 2)
        self.assertEqual(card["by_mode"]["active"]["repeated_retrieval"], 1)

    def test_changed_paths_fallback_order_and_mode_conditional_tokens(self):
        worktree = self.repo / "wt"
        worktree.mkdir()
        self.task("T-active", worktree=str(worktree), changed_files=["merged.py"], scope=["scope.py"])
        self.task("T-shadow", changed_files=["legacy.py"], scope=["scope.py"])
        self.task("T-scope", scope=["only_scope.py"])
        self.retrieval("T-active", selected=["a"], candidates=[{"id": "a", "files": ["live.py"]}], tiered=11, legacy_tokens=91)
        self.retrieval("T-shadow", mode="shadow", legacy=["b"], candidates=[{"id": "b", "files": ["legacy.py"]}], tiered=92, legacy_tokens=12)
        self.retrieval("T-scope", selected=["c"], candidates=[{"id": "c", "files": ["only_scope.py"]}], tiered=13)
        with mock.patch.object(memory_scorecard.gitutil, "changed_paths", return_value=["live.py"]):
            card = memory_scorecard.build(self.state)
        rows = {row["task"]: row for row in card["rows"]}
        self.assertEqual((rows["T-active"]["changed_path_source"], rows["T-active"]["memory_tokens"]), ("worktree", 11))
        self.assertEqual((rows["T-shadow"]["changed_path_source"], rows["T-shadow"]["memory_tokens"]), ("changed_files", 12))
        self.assertEqual(rows["T-scope"]["changed_path_source"], "scope")


if __name__ == "__main__":
    unittest.main()
