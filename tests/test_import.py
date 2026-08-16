import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import UNASSIGNED_CITY_NAME, UNASSIGNED_SITE_NAME, Database  # noqa: E402
from inventory_api import (  # noqa: E402
    import_devices,
    normalize_vendor,
    parse_device_csv,
)


class TestParseDeviceCsv(unittest.TestCase):
    def test_requires_name_and_vendor(self):
        with self.assertRaises(ValueError):
            parse_device_csv("host,model\n10.0.0.1,X20\n")

    def test_aliases_and_skips_blank_rows(self):
        rows = parse_device_csv(
            "device,vendor,ip,model\n"
            "CHI-X20-1,Appear,10.0.1.10,X20\n"
            ",,,\n"
            "NYC-HAI-1,makito,10.0.2.10,\n"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "CHI-X20-1")
        self.assertEqual(rows[0]["mgmt_host"], "10.0.1.10")
        self.assertEqual(rows[1]["name"], "NYC-HAI-1")

    def test_normalize_vendor(self):
        self.assertEqual(normalize_vendor("Net Insight"), "net_insight")
        self.assertEqual(normalize_vendor("nimbra"), "net_insight")
        self.assertEqual(normalize_vendor("generic"), "generic_snmp")
        with self.assertRaises(ValueError):
            normalize_vendor("acme")


class TestImportDevices(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "t.db"))
        self.db.initialize()
        self.env = Path(self.tmpdir.name) / "nexnoc.env"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_ensure_unassigned_is_idempotent_and_unmapped(self):
        first = self.db.ensure_unassigned_site()
        second = self.db.ensure_unassigned_site()
        self.assertEqual(first, second)
        site = self.db.get_site(first)
        city = self.db.get_city_by_name(UNASSIGNED_CITY_NAME)
        self.assertEqual(site["name"], UNASSIGNED_SITE_NAME)
        self.assertIsNone(site["lat"])
        self.assertIsNone(city["lat"])

    def test_import_lands_in_unassigned(self):
        result = import_devices(self.db, self.env, (
            "name,vendor,mgmt_host\n"
            "CHI-X20-1,appear,10.0.1.10\n"
            "CHI-HAI-PENDING,haivision,\n"
        ))
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(result["errors"]), 0)
        holding = self.db.get_site(result["holding_site_id"])
        self.assertEqual(holding["name"], UNASSIGNED_SITE_NAME)
        placed = self.db.get_device_by_name("CHI-X20-1")
        pending = self.db.get_device_by_name("CHI-HAI-PENDING")
        self.assertEqual(placed.site_id, holding["id"])
        self.assertTrue(placed.poll_enabled)
        self.assertEqual(pending.mgmt_host, "")
        self.assertFalse(pending.poll_enabled)

    def test_matching_site_is_used(self):
        city_id = self.db.add_city("Chicago")
        site_id = self.db.add_site("Chicago - Wacker", city="Chicago", city_id=city_id)
        result = import_devices(self.db, self.env, (
            "name,vendor,mgmt_host,city,site\n"
            "CHI-X20-1,appear,10.0.1.10,Chicago,Chicago - Wacker\n"
        ))
        self.assertEqual(result["created"][0]["site_id"], site_id)
        self.assertEqual(self.db.get_device_by_name("CHI-X20-1").site_id, site_id)

    def test_unknown_site_stays_unassigned(self):
        result = import_devices(self.db, self.env, (
            "name,vendor,site\n"
            "ORPHAN-1,appear,No Such Building\n"
        ))
        device = self.db.get_device_by_name("ORPHAN-1")
        self.assertEqual(device.site_id, result["holding_site_id"])

    def test_reimport_fills_blanks_and_does_not_move(self):
        city_id = self.db.add_city("Chicago")
        site_id = self.db.add_site("Chicago - Wacker", city="Chicago", city_id=city_id)
        first = import_devices(self.db, self.env, "name,vendor,mgmt_host\nCHI-X20-1,appear,\n")
        self.db.update_device(first["created"][0]["id"], site_id=site_id)
        result = import_devices(self.db, self.env, (
            "name,vendor,mgmt_host,model,site\n"
            "CHI-X20-1,appear,10.0.1.10,X20,No Such Building\n"
        ))
        self.assertEqual(len(result["updated"]), 1)
        device = self.db.get_device_by_name("CHI-X20-1")
        self.assertEqual(device.site_id, site_id)
        self.assertEqual(device.mgmt_host, "10.0.1.10")
        self.assertEqual(device.model, "X20")

    def test_duplicate_host_is_an_error(self):
        import_devices(self.db, self.env, "name,vendor,mgmt_host\nCHI-X20-1,appear,10.0.1.10\n")
        result = import_devices(self.db, self.env, "name,vendor,mgmt_host\nCHI-X20-2,appear,10.0.1.10\n")
        self.assertEqual(len(result["created"]), 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("already in use", result["errors"][0]["error"])

    def test_unknown_vendor_is_an_error(self):
        result = import_devices(self.db, self.env, "name,vendor\nX-1,acme\n")
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("unknown vendor", result["errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
