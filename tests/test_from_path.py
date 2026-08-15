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

    def test_dc_and_wdcw_are_distinct_sites(self):
        sites = {s["name"] for s in self.cfg["sites"]}
        self.assertIn("400 N. Capital St", sites)
        self.assertIn("WDCW TV Station", sites)
        self.assertNotIn("NewsNation DC", sites)
        self.assertNotIn("Washington DC - WDCW", sites)
        by_name = {s["name"]: s for s in self.cfg["sites"]}
        self.assertEqual(by_name["400 N. Capital St"]["city"], "Washington DC")
        self.assertEqual(by_name["WDCW TV Station"]["city"], "Washington DC")
        nn_dests = {
            f["dest_site"] for f in self.cfg["flows"]
            if f.get("dest_site") == "400 N. Capital St"
        }
        self.assertTrue(nn_dests)

    def test_path_is_not_a_device(self):
        names = {d["name"] for d in self.cfg["devices"]}
        self.assertNotIn("NY-HAI-9.245", names)
        self.assertNotIn("NY-HAI-HAI1012", names)
        self.assertNotIn("DC-HAI-40.109", names)
        self.assertNotIn("DC-X20-5", names)
        self.assertIn("NY-HAI-1030013", names)
        self.assertIn("DC-HAI-3010140", names)
        flow = next(f for f in self.cfg["flows"] if f.get("label") == "HAI 1011")
        self.assertEqual(flow["source_device"], "NY-HAI-1030013")
        self.assertEqual(flow["label"], "HAI 1011")
        self.assertEqual(flow["dest_label"], "10.70.9.218:5100")

    def test_haivision_stream_is_not_management(self):
        ny = next(d for d in self.cfg["devices"] if d["name"] == "NY-HAI-1030013")
        self.assertFalse(ny.get("mgmt_host"))
        self.assertFalse(ny.get("poll_enabled"))
        stream_ips = {"10.207.9.245", "10.70.9.218", "10.98.40.109", "10.115.19.20"}
        for d in self.cfg["devices"]:
            if d["vendor"] == "haivision":
                self.assertNotIn(d.get("mgmt_host"), stream_ips)

    def test_appear_uses_control_ip_not_stream(self):
        names = {d["name"] for d in self.cfg["devices"]}
        self.assertIn("DC-X20", names)
        self.assertIn("NY-X20", names)
        self.assertIn("CHI-X20", names)
        dc = next(d for d in self.cfg["devices"] if d["name"] == "DC-X20")
        self.assertEqual(dc["mgmt_host"], "10.98.5.232")
        self.assertNotEqual(dc["mgmt_host"], "10.98.9.117")
        ports = [p for p in self.cfg["ports"] if p["device"] == "DC-X20"]
        self.assertTrue(any(p["name"] == "10.98.9.117" and p["kind"] == "net" for p in ports))
        chi = next(d for d in self.cfg["devices"] if d["name"] == "CHI-X20")
        self.assertEqual(chi["mgmt_host"], "10.70.4.210")
        appear_hosts = {d["mgmt_host"] for d in self.cfg["devices"] if d["vendor"] == "appear"}
        self.assertNotIn("10.70.9.210", appear_hosts)
        self.assertNotIn("10.98.9.117", appear_hosts)

    def test_path_group_shares_encoder(self):
        names = {d["name"] for d in self.cfg["devices"]}
        self.assertNotIn("NY-HAI-HAI1012", names)
        flow = next(f for f in self.cfg["flows"] if f.get("label") == "HAI 1012")
        self.assertEqual(flow["source_device"], "NY-HAI-1030013")
        self.assertEqual(flow["source_port"], "In 2")
        self.assertEqual(flow["dest_label"], "10.70.9.218:5105")

    def test_credentials_live_on_device_records(self):
        for d in self.cfg["devices"]:
            self.assertTrue(d["api_username_env"].endswith("_USER"))
            self.assertTrue(d["api_password_env"].endswith("_PASS"))
        ny = next(d for d in self.cfg["devices"] if d["name"] == "NY-HAI-1030013")
        self.assertEqual(ny.get("api_username"), "admin")
        self.assertTrue(ny.get("api_password"))
        appear = next(d for d in self.cfg["devices"] if d["name"] == "DC-X20")
        self.assertEqual(appear.get("api_username"), "news")
        self.assertTrue(appear.get("api_password"))


if __name__ == "__main__":
    unittest.main()
