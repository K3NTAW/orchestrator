import _harness
import json
import unittest

from orchestrator import evidence
from _fixtures import fake_secret


class EvidenceTests(unittest.TestCase):
    def test_make_ids_are_content_addressed_and_redacted(self):
        token_name = "API_" + "TOKEN"
        token_value = fake_secret("evidence")
        raw = token_name + "=" + token_value + "\ndef useful(): pass"
        first = evidence.make("source_chunk", "x.py:1", raw, provenance="repo", observed_at=1)
        again = evidence.make("source_chunk", "x.py:1", raw, provenance="repo", observed_at=2)
        changed = evidence.make("source_chunk", "x.py:1", raw + "!", provenance="repo")
        self.assertEqual(first.id, again.id)
        self.assertNotEqual(first.id, changed.id)
        pool = evidence.EvidencePool("redaction")
        stored = pool.add(first)
        self.assertIn("[REDACTED]", stored.content)
        self.assertNotIn(token_value, pool.path.read_text())

    def test_pool_add_is_idempotent_and_fresh_by_commit(self):
        ev = evidence.make("source_chunk", "x.py", "x", commit="abc", provenance="repo")
        pool = evidence.EvidencePool("idempotent")
        self.assertEqual(pool.add(ev), pool.add(ev))
        self.assertEqual(1, len(pool.path.read_text().splitlines()))
        self.assertTrue(evidence.fresh(ev, "abc"))
        self.assertFalse(pool.fresh(ev, "def"))
        self.assertEqual([ev.id], pool.stale_ids("def"))


if __name__ == "__main__":
    unittest.main()
