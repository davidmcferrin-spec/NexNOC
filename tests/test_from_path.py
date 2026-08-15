import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "Example Docs" / "Newsnation Global Path Naming.xlsx"
_SPEC = importlib.util.spec_from_file_location(
    "from_path_xlsx", ROOT / "scripts" / "from_path_xlsx.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
build_inventory = _MOD.build_inventory


@unittest.skipUnless(XLSX.is_file(), "path spreadsheet not in the tree")
class TestFromPathXlsx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = build_inventory(XLSX)

    def test_includes_missing_ip_and_burbank(self):
        sites = {s["name"] for s in self.cfg["sites"]}
        self.assertIn("Burbank - CW", sites)
        pending = [d for d in self.cfg["devices"] if not d.get("mgmt_host")]
        self.assertTrue(pending, "rows without IPs should still become devices")
        for d in pending:
            self.assertFalse(d.get("poll_enabled"))
        dest_cities = {f["dest_city"] for f in self.cfg["flows"]}
        self.assertIn("Burbank", dest_cities)

    def test_env_names_only_no_secret_values(self):
        for d in self.cfg["devices"]:
            self.assertTrue(d["api_username_env"].endswith("_USER"))
            self.assertTrue(d["api_password_env"].endswith("_PASS"))
            self.assertNotIn("api_username", d)
            self.assertNotIn("api_password", d)


if __name__ == "__main__":
    unittest.main()
