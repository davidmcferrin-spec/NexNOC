import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from envfile import get_value, is_set, upsert_values  # noqa: E402


class TestEnvfile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "nexnoc.env"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_upsert_preserves_comments_and_other_keys(self):
        self.path.write_text("# keep me\nFOO=one\nBAR=two\n", encoding="utf-8")
        upsert_values(self.path, {"FOO": "replaced", "BAZ": "new"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# keep me", text)
        self.assertIn("FOO=replaced", text)
        self.assertIn("BAR=two", text)
        self.assertIn("BAZ=new", text)
        self.assertEqual(get_value(self.path, "FOO"), "replaced")

    def test_is_set_checks_environ_then_file(self):
        upsert_values(self.path, {"PORTAL_USER": "admin"})
        self.assertTrue(is_set(self.path, "PORTAL_USER"))
        self.assertFalse(is_set(self.path, "MISSING_KEY"))

    def test_rejects_invalid_key(self):
        with self.assertRaises(ValueError):
            upsert_values(self.path, {"not a key": "x"})


if __name__ == "__main__":
    unittest.main()
