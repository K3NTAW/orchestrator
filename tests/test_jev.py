"""jev.ask()'s fail-open behaviour (disabled, retry-then-None, budget exhaustion), redact()'s token stripping,
state truncation, the on-disk jev_state.json lock/atomic-write/corruption handling, and the cached API key --
all against a monkeypatched urllib.request.urlopen, never real network."""
import json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -m unittest tests/test_jev.py` doesn't add this dir itself
from _harness import TMP
from orchestrator import jev


class FakeResp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


ENABLED_CFG = {"enabled": True, "model": "jev-latest", "timeout_s": 5.0,
               "daily_budget_tokens": 20_000_000, "max_state_chars": 100_000}


class JevTests(unittest.TestCase):
    def setUp(self):
        self._clean_jev_files()
        self._orig_cfg = jev._cfg
        self._orig_api_key = jev._api_key
        self._orig_urlopen = jev.urllib.request.urlopen
        self._orig_sleep = jev.time.sleep
        self._orig_tally_cache = dict(jev._tally_cache)
        self._orig_api_key_cache = dict(jev._api_key_cache)

    def tearDown(self):
        jev._cfg = self._orig_cfg
        jev._api_key = self._orig_api_key
        jev.urllib.request.urlopen = self._orig_urlopen
        jev.time.sleep = self._orig_sleep
        jev._tally_cache.clear(); jev._tally_cache.update(self._orig_tally_cache)
        jev._api_key_cache.clear(); jev._api_key_cache.update(self._orig_api_key_cache)
        # jev/<date>.jsonl lives in its own runs/jev/ subdir (never runs/*.jsonl -- that's the shared dir
        # bus.log_run() writes worker runs to, which other modules' tests glob and assume they own).
        self._clean_jev_files()

    @staticmethod
    def _clean_jev_files():
        if jev.STATE_FILE.exists():
            jev.STATE_FILE.unlink()
        if jev.STATE_LOCK_FILE.exists():
            jev.STATE_LOCK_FILE.unlink()
        if jev.RUNS_DIR.exists():
            for f in jev.RUNS_DIR.glob("*.jsonl"):
                f.unlink()
        jev._tally_cache["day"] = None
        jev._tally_cache["tokens"] = 0

    def test_disabled_returns_none_without_network(self):
        jev._cfg = lambda: {**ENABLED_CFG, "enabled": False}
        jev._api_key = lambda: "fake-key"

        def boom(*a, **k):
            raise AssertionError("urlopen must not be called when jev is disabled")
        jev.urllib.request.urlopen = boom
        self.assertIsNone(jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}}))

    def test_ask_parses_answers(self):
        jev._cfg = lambda: ENABLED_CFG
        jev._api_key = lambda: "fake-key"
        response = {"answers": {"q": {"type": "noul", "noul": 0.87, "confidence": 0.9}},
                    "usage": {"input_tokens": 123, "output_tokens": 4}}
        jev.urllib.request.urlopen = lambda req, timeout=None: FakeResp(response)

        result = jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}})
        self.assertEqual(result, response)
        self.assertEqual(jev._day_tokens_used(), 123)

    def test_retry_then_none_on_429(self):
        jev._cfg = lambda: ENABLED_CFG
        jev._api_key = lambda: "fake-key"
        jev.time.sleep = lambda s: None
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(jev.URL, 429, "rate limited", {}, None)
        jev.urllib.request.urlopen = flaky

        result = jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}})
        self.assertIsNone(result)
        self.assertEqual(calls["n"], 2)

    def test_redact_strips_tokens(self):
        fake_token = "sk-" + "a1b2c3d4e5" * 3
        text = f"here is a secret: {fake_token} do not log it"
        redacted = jev.redact(text)
        self.assertNotIn(fake_token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_budget_exhausted_returns_none(self):
        jev._cfg = lambda: {**ENABLED_CFG, "daily_budget_tokens": 10}
        jev._api_key = lambda: "fake-key"
        jev.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        jev.STATE_FILE.write_text(json.dumps({"day": jev._today(), "tokens": 999}))

        def boom(*a, **k):
            raise AssertionError("urlopen must not be called once the daily budget is exhausted")
        jev.urllib.request.urlopen = boom

        self.assertIsNone(jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}}))

    def test_state_truncated(self):
        jev._cfg = lambda: {**ENABLED_CFG, "max_state_chars": 50}
        jev._api_key = lambda: "fake-key"
        captured = {}

        def capture(req, timeout=None):
            captured["state"] = json.loads(req.data.decode())["state"]
            return FakeResp({"answers": {}, "usage": {"input_tokens": 0}})
        jev.urllib.request.urlopen = capture

        long_state = "hello world " * 20
        jev.ask(long_state, {"q": {"type": "noul", "instructions": "is this fine?"}})
        self.assertEqual(captured["state"], long_state[:50])
        self.assertLessEqual(len(captured["state"]), 50)

    def test_timeout_from_config(self):
        jev._cfg = lambda: {**ENABLED_CFG, "timeout_s": 42}
        jev._api_key = lambda: "fake-key"
        captured = {}

        def capture(req, timeout=None):
            captured["timeout"] = timeout
            return FakeResp({"answers": {}, "usage": {"input_tokens": 0}})
        jev.urllib.request.urlopen = capture

        jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}})
        self.assertEqual(captured["timeout"], 42)

        captured.clear()
        jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}}, timeout_s=7)
        self.assertEqual(captured["timeout"], 7)

    def test_budget_zero_blocks(self):
        jev._cfg = lambda: {**ENABLED_CFG, "daily_budget_tokens": 0}
        jev._api_key = lambda: "fake-key"

        def boom(*a, **k):
            raise AssertionError("urlopen must not be called when daily_budget_tokens is 0")
        jev.urllib.request.urlopen = boom

        self.assertIsNone(jev.ask("some state", {"q": {"type": "noul", "instructions": "is this fine?"}}))

    def test_state_lock_and_atomic_write(self):
        jev._add_day_tokens(100)
        self.assertEqual(jev._day_tokens_used(), 100)
        on_disk = json.loads(jev.STATE_FILE.read_text())
        self.assertEqual(on_disk, {"day": jev._today(), "tokens": 100})

        jev._add_day_tokens(50)
        self.assertEqual(jev._day_tokens_used(), 150)

        # a corrupt on-disk file must not reset the process's tally to 0: _add_day_tokens keeps accumulating
        # from the last known-good in-memory value instead of trusting the unparseable file.
        jev.STATE_FILE.write_text("{not json")
        self.assertEqual(jev._day_tokens_used(), 150)
        jev._add_day_tokens(25)
        self.assertEqual(jev._day_tokens_used(), 175)
        on_disk = json.loads(jev.STATE_FILE.read_text())
        self.assertEqual(on_disk, {"day": jev._today(), "tokens": 175})

    def test_api_key_cached(self):
        calls = {"n": 0}

        def fake_secrets_for_role(role):
            calls["n"] += 1
            return {"TYPESAFE_API_KEY": "fake-key"}
        orig = jev.spawn.secrets_for_role
        jev.spawn.secrets_for_role = fake_secrets_for_role
        jev._api_key_cache["value"] = None
        jev._api_key_cache["resolved_at"] = None
        try:
            self.assertEqual(jev._api_key(), "fake-key")
            self.assertEqual(jev._api_key(), "fake-key")
            self.assertEqual(calls["n"], 1)  # second call within the TTL window reuses the cache

            jev._api_key_cache["resolved_at"] -= jev.API_KEY_TTL_S + 1  # simulate the cache going stale
            self.assertEqual(jev._api_key(), "fake-key")
            self.assertEqual(calls["n"], 2)
        finally:
            jev.spawn.secrets_for_role = orig

    def test_redacted_before_send(self):
        jev._cfg = lambda: {**ENABLED_CFG, "max_state_chars": 20}
        jev._api_key = lambda: "fake-key"
        fake_token = "sk-" + "a1b2c3d4e5" * 3
        # the token starts at index 10 and runs past the 20-char truncation cutoff: if truncation happened
        # before redaction, the surviving fragment ("sk-a1b2c3d") would be too short to match the token
        # pattern and would leak un-redacted.
        long_state = "x" * 10 + fake_token + "y" * 50
        captured = {}

        def capture(req, timeout=None):
            captured["state"] = json.loads(req.data.decode())["state"]
            return FakeResp({"answers": {}, "usage": {"input_tokens": 0}})
        jev.urllib.request.urlopen = capture

        jev.ask(long_state, {"q": {"type": "noul", "instructions": "is this fine?"}})
        self.assertNotIn(fake_token, captured["state"])
        self.assertNotIn("a1b2c3d", captured["state"])


class JevVotesTests(unittest.TestCase):
    def test_votes_averaged(self):
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory, \
                patch.object(jev, "RUNS_DIR", Path(directory)), \
                patch.object(jev, "_cfg", return_value={**ENABLED_CFG, "votes": 3}), \
                patch.object(jev, "_api_key", return_value="fake-key"), \
                patch.object(jev, "_day_tokens_used", return_value=0), \
                patch.object(jev, "_add_day_tokens") as tally:
            questions = {"needed": {"type": "noul", "instructions": "needed?"},
                         "redundant": {"type": "noul", "instructions": "redundant?"}}
            response = {"answers": {
                "needed_1": {"noul": 0.3}, "needed_2": {"noul": 0.6}, "needed_3": {"noul": 0.9},
                "redundant_1": {"noul": 0.1, "confidence": 0.6},
                "redundant_2": {"noul": 0.2, "confidence": 0.9},
                "redundant_3": {"noul": 0.3, "confidence": 0.9}}, "usage": {"input_tokens": 123}}
            with patch.object(jev.urllib.request, "urlopen", return_value=FakeResp(response)) as request:
                result = jev.ask("state", questions)
            request.assert_called_once()
            sent = json.loads(request.call_args.args[0].data)
            self.assertEqual(sent["questions"], {f"{key}_{i}": value for key, value in questions.items()
                                                for i in range(1, 4)})
            self.assertAlmostEqual(result["answers"]["needed"]["noul"], 0.6)
            self.assertIsNone(result["answers"]["needed"]["confidence"])
            self.assertAlmostEqual(result["answers"]["redundant"]["noul"], 0.2)
            self.assertAlmostEqual(result["answers"]["redundant"]["confidence"], 0.8)
            tally.assert_called_once_with(123)
            entry = json.loads((Path(directory) / f"{jev._today()}.jsonl").read_text())
            self.assertEqual(entry["votes"], 3)

    def test_missing_confidence_stays_none(self):
        from unittest.mock import patch
        with patch.object(jev, "_cfg", return_value=ENABLED_CFG), \
                patch.object(jev, "_api_key", return_value="fake-key"), \
                patch.object(jev, "_day_tokens_used", return_value=0), \
                patch.object(jev, "_add_day_tokens"), patch.object(jev, "_log_usage"), \
                patch.object(jev.urllib.request, "urlopen",
                             side_effect=lambda *a, **k: FakeResp({"answers": {"q": {"noul": 0.95}}})):
            self.assertIsNone(jev.ask("state", {"q": {"type": "noul"}})["answers"]["q"]["confidence"])
            self.assertEqual(jev.noul("state", "needed?"), 0.95)


if __name__ == "__main__":
    unittest.main()
