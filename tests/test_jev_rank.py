"""orchestrator.jev_rank.rank: batching questions across multiple jev.ask() calls, threshold filtering +
p-desc sort, and fail-open (jev.ask returns None -> items unchanged, p_relevant=None) -- all against a
monkeypatched jev.ask, never real network."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_jev_rank.py` doesn't add this dir itself
from _harness import TMP  # noqa: F401
from orchestrator import jev, jev_rank


class JevRankTests(unittest.TestCase):
    def setUp(self):
        self._orig_ask = jev.ask
        self.addCleanup(setattr, jev, "ask", self._orig_ask)

    def test_rank_batches_questions(self):
        items = [{"id": f"i{i}", "text": f"entry {i}"} for i in range(85)]
        calls = []

        def fake_ask(state, questions, **kw):
            calls.append(questions)
            self.assertEqual(state, "the goal")
            return {"answers": {qid: {"noul": 0.9} for qid in questions}}
        jev.ask = fake_ask

        result = jev_rank.rank(items, "the goal")

        self.assertEqual(len(calls), 3)  # 85 items / 40 per batch -> 40, 40, 5
        self.assertEqual([len(c) for c in calls], [40, 40, 5])
        for questions in calls:
            for qid, q in questions.items():
                self.assertEqual(q["type"], "noul")
                self.assertIn("relevant", q["instructions"])
                self.assertEqual(q["criteria"], next(it["text"] for it in items if it["id"] == qid))
        self.assertEqual(len(result), 85)
        self.assertEqual({it["id"] for it in result}, {it["id"] for it in items})

    def test_rank_applies_threshold(self):
        items = [{"id": "a", "text": "on topic"}, {"id": "b", "text": "way off"}, {"id": "c", "text": "borderline"}]
        answers = {"a": 0.9, "b": 0.1, "c": 0.35}

        def fake_ask(state, questions, **kw):
            return {"answers": {qid: {"noul": answers[qid]} for qid in questions}}
        jev.ask = fake_ask

        result = jev_rank.rank(items, "the goal", threshold=0.35)

        self.assertEqual([it["id"] for it in result], ["a", "c"])  # b dropped, sorted p desc
        self.assertEqual(result[0]["p_relevant"], 0.9)
        self.assertEqual(result[1]["p_relevant"], 0.35)

    def test_rank_fail_open(self):
        items = [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]
        jev.ask = lambda state, questions, **kw: None

        result = jev_rank.rank(items, "the goal")

        self.assertEqual(len(result), 2)
        self.assertEqual([it["id"] for it in result], ["a", "b"])  # original order preserved
        self.assertTrue(all(it["p_relevant"] is None for it in result))

    def test_rank_empty_items(self):
        jev.ask = lambda *a, **k: (_ for _ in ()).throw(AssertionError("ask must not be called for no items"))
        self.assertEqual(jev_rank.rank([], "the goal"), [])


if __name__ == "__main__":
    unittest.main()
