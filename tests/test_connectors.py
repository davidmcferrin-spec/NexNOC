import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database  # noqa: E402
from drivers.appear import AppearXPlatformDriver  # noqa: E402
from drivers.haivision import HaivisionMakitoXDriver  # noqa: E402
from drivers.net_insight import NetInsightNimbraDriver  # noqa: E402
from inventory_api import stamp_device_connectors  # noqa: E402


class TestConnectorTemplates(unittest.TestCase):
    def test_appear_has_20_assignable_bncs(self):
        names = [c.name for c in AppearXPlatformDriver.connectors]
        self.assertEqual(len(names), 20)
        self.assertEqual(names[0], "BNC 1")
        self.assertEqual(names[-1], "BNC 20")
        self.assertTrue(all(c.capability == "assignable" for c in AppearXPlatformDriver.connectors))

    def test_haivision_has_4_assignable_bncs(self):
        self.assertEqual(len(HaivisionMakitoXDriver.connectors), 4)
        self.assertTrue(all(c.capability == "assignable" for c in HaivisionMakitoXDriver.connectors))

    def test_net_insight_has_no_bnc_template(self):
        self.assertEqual(tuple(NetInsightNimbraDriver.connectors), ())

    def test_stamp_skips_when_ports_already_exist(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(os.path.join(tmp.name, "t.db"))
        db.initialize()
        site = db.add_site("DC")
        device_id = db.add_device(
            site_id=site, name="DC-HAI-40", vendor="haivision", mgmt_host="10.1.1.1",
        )
        device = db.get_device(device_id)
        db.add_port(device_id, "In 1", kind="sdi_in")
        self.assertEqual(stamp_device_connectors(db, device), 0)
        self.assertEqual([p["name"] for p in db.list_ports(device_id)], ["In 1"])


if __name__ == "__main__":
    unittest.main()
