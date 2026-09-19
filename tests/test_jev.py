"""jev.ask()'s fail-open behaviour (disabled, retry-then-None, budget exhaustion), redact()'s token stripping,
and state truncation -- all against a monkeypatched urllib.request.urlopen, never real network."""
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

    def tearDown(self):
        jev._cfg = self._orig_cfg
        jev._api_key = self._orig_api_key
        jev.urllib.request.urlopen = self._orig_urlopen
        jev.time.sleep = self._orig_sleep
        # jev-<date>.jsonl lives in the same shared .orchestrator/runs/ dir bus.log_run() writes to (one TMP
        # root for the whole test process, per _harness.py); other modules' tests glob("*.jsonl") in there and
        # assume the lexicographically-last file is their own run, so nothing jev-prefixed can survive this test.
        self._clean_jev_files()

    @staticmethod
    def _clean_jev_files():
        if jev.STATE_FILE.exists():
            jev.STATE_FILE.unlink()
        for f in jev.RUNS_DIR.glob("jev-*.jsonl"):
            f.unlink()

    def test_disabled_returns_none_without_network(self):
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


if __name__ == "__main__":
    unittest.main()
