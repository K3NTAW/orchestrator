import _harness
import re
import unittest
from pathlib import Path


class FixtureTests(unittest.TestCase):
    def test_fake_secret_has_no_literal_secret_shape_in_source(self):
        pattern = re.compile(
            r"(?i)(api[_-]?key|token|secret|password)\s*=\s*['\"][A-Za-z0-9/+_-]{12,}"
        )
        fixture_sources = (
            Path(__file__).with_name("test_evidence.py"),
            Path(__file__).with_name("test_jev_boundary.py"),
            Path(__file__).with_name("_fixtures.py"),
        )
        for source in fixture_sources:
            self.assertIsNone(pattern.search(source.read_text()), source)


if __name__ == "__main__":
    unittest.main()
