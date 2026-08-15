import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database  # noqa: E402
from poller import bootstrap  # noqa: E402


def _config():
    return {
        "sites": [
            {"name": "Huntsville HQ", "city": "Huntsville, AL", "lat": 34.73, "lng": -86.58},
            {"name": "Chicago", "city": "Chicago, IL", "lat": 41.88, "lng": -87.63},
        ],
        "devices": [
            {
                "site": "Huntsville HQ",
                "name": "HSV-X20-1",
                "vendor": "appear",
                "mgmt_host": "10.0.0.10",
            },
        ],
        "trunks": [
            {"label": "HSV-CHI", "site_a": "Huntsville HQ", "site_b": "Chicago"},
        ],
        "signals": [
            {
                "device": "HSV-X20-1",
                "trunk": "HSV-CHI",
                "source_label": "src",
                "destination_label": "dst",
                "direction": "contribution",
            },
        ],
    }


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "test.db"))
        self.db.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_bootstrap_inserts_sites_devices_trunks_signals(self):
        bootstrap(self.db, _config())
        self.assertEqual(len(self.db.list_sites()), 2)
        self.assertEqual([d.name for d in self.db.list_devices()], ["HSV-X20-1"])
        self.assertEqual([t["label"] for t in self.db.list_trunks()], ["HSV-CHI"])
        signals = self.db.list_signals()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["source_label"], "src")
        self.assertEqual(signals[0]["trunk_label"], "HSV-CHI")
        flows = self.db.list_flows()
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["dest_site_name"], "Chicago")
        self.assertEqual(flows[0]["source_port_name"], "src")

    def test_bootstrap_is_idempotent(self):
        cfg = _config()
        bootstrap(self.db, cfg)
        bootstrap(self.db, cfg)
        self.assertEqual(len(self.db.list_sites()), 2)
        self.assertEqual(len(self.db.list_devices()), 1)
        self.assertEqual(len(self.db.list_trunks()), 1)
        self.assertEqual(len(self.db.list_signals()), 1)
        self.assertEqual(len(self.db.list_flows()), 1)

    def test_bootstrap_skips_unknown_site_on_device(self):
        cfg = _config()
        cfg["devices"].append({
            "site": "Mars",
            "name": "MARS-1",
            "vendor": "appear",
            "mgmt_host": "10.0.0.99",
        })
        bootstrap(self.db, cfg)
        self.assertEqual([d.name for d in self.db.list_devices()], ["HSV-X20-1"])

    def test_bootstrap_skips_unknown_trunk_endpoint(self):
        cfg = _config()
        cfg["trunks"].append({"label": "ghost", "site_a": "Chicago", "site_b": "Nowhere"})
        bootstrap(self.db, cfg)
        self.assertEqual([t["label"] for t in self.db.list_trunks()], ["HSV-CHI"])

    def test_bootstrap_skips_signal_with_unknown_device(self):
        cfg = _config()
        cfg["signals"].append({
            "device": "NO-SUCH",
            "trunk": "HSV-CHI",
            "source_label": "a",
            "destination_label": "b",
        })
        bootstrap(self.db, cfg)
        self.assertEqual(len(self.db.list_signals()), 1)

    def test_bootstrap_does_not_overwrite_existing_signal_status(self):
        bootstrap(self.db, _config())
        signal = self.db.list_signals()[0]
        self.db.set_signal_status(signal["id"], "down")
        bootstrap(self.db, _config())
        self.assertEqual(self.db.get_signal(signal["id"])["status"], "down")

    def test_bootstrap_explicit_flows_fan_out(self):
        cfg = _config()
        cfg["sites"].append({"name": "New York", "lat": 40.71, "lng": -74.01})
        cfg["ports"] = [
            {"device": "HSV-X20-1", "name": "SDI-1", "kind": "sdi_in"},
        ]
        cfg["flows"] = [
            {
                "label": "News → CHI",
                "source_device": "HSV-X20-1",
                "source_port": "SDI-1",
                "dest_site": "Chicago",
            },
            {
                "label": "News → NYC",
                "source_device": "HSV-X20-1",
                "source_port": "SDI-1",
                "dest_site": "New York",
            },
        ]
        bootstrap(self.db, cfg)
        bootstrap(self.db, cfg)
        flows = self.db.list_flows()
        self.assertEqual(len(flows), 2)
        dests = {f["dest_site_name"] for f in flows}
        self.assertEqual(dests, {"Chicago", "New York"})
        self.assertEqual(len(self.db.list_ports(self.db.list_devices()[0].id)), 1)

    def test_bootstrap_cities_and_two_sites(self):
        cfg = _config()
        cfg["cities"] = [{"name": "Chicago", "lat": 41.88, "lng": -87.63}]
        cfg["sites"].append({
            "name": "Chicago - Midway", "city": "Chicago", "lat": 41.79, "lng": -87.75,
        })
        cfg["sites"][1]["city"] = "Chicago"
        bootstrap(self.db, cfg)
        cities = self.db.list_cities()
        self.assertIn("Chicago", [c["name"] for c in cities])
        midway = self.db.get_site_by_name("Chicago - Midway")
        downtown = self.db.get_site_by_name("Chicago")
        self.assertEqual(midway["city_id"], downtown["city_id"])

    def test_bootstrap_empty_flows_skips_synthesize(self):
        cfg = _config()
        cfg["flows"] = []
        bootstrap(self.db, cfg)
        self.assertEqual(len(self.db.list_signals()), 1)
        self.assertEqual(len(self.db.list_flows()), 0)

    def test_bootstrap_pending_device_without_ip(self):
        cfg = _config()
        cfg["devices"].append({
            "site": "Chicago",
            "name": "CHI-HAI-PENDING",
            "vendor": "haivision",
            "mgmt_host": "",
            "poll_enabled": False,
            "api_username_env": "CHI_HAI_PENDING_USER",
            "api_password_env": "CHI_HAI_PENDING_PASS",
        })
        bootstrap(self.db, cfg)
        pending = next(d for d in self.db.list_devices() if d.name == "CHI-HAI-PENDING")
        self.assertEqual(pending.mgmt_host, "")
        self.assertFalse(pending.poll_enabled)


if __name__ == "__main__":
    unittest.main()
