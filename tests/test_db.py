import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database  # noqa: E402


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        self.db = Database(self.db_path)
        self.db.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_initialize_is_idempotent(self):
        self.db.initialize()
        self.db.initialize()

    def test_add_and_list_sites(self):
        site_id = self.db.add_site("Huntsville HQ", city="Huntsville, AL", lat=34.73, lng=-86.58)
        sites = self.db.list_sites()
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]["id"], site_id)
        self.assertEqual(sites[0]["name"], "Huntsville HQ")

    def test_add_site_duplicate_name_rejected(self):
        self.db.add_site("Chicago")
        with self.assertRaises(Exception):
            with self.db.connect() as conn:
                conn.execute("INSERT INTO sites (name) VALUES ('Chicago')")

    def test_add_device_requires_valid_site(self):
        with self.assertRaises(Exception):
            self.db.add_device(site_id=999, name="GHOST-1", vendor="appear", mgmt_host="10.0.0.1")

    def test_add_device_rejects_unknown_vendor(self):
        site_id = self.db.add_site("Chicago")
        with self.assertRaises(ValueError):
            self.db.add_device(site_id=site_id, name="X-1", vendor="acme_corp", mgmt_host="10.0.0.1")

    def test_add_device_rejects_unknown_access_mode(self):
        site_id = self.db.add_site("Chicago")
        with self.assertRaises(ValueError):
            self.db.add_device(site_id=site_id, name="X-1", vendor="appear",
                                mgmt_host="10.0.0.1", access_mode="telepathy")

    def test_add_and_get_appear_device(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(
            site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10",
            model="X20", api_username_env="CHI_USER", api_password_env="CHI_PASS",
        )
        device = self.db.get_device(device_id)
        self.assertIsNotNone(device)
        self.assertEqual(device.name, "CHI-X20-1")
        self.assertEqual(device.vendor, "appear")
        self.assertEqual(device.access_mode, "direct_api")
        self.assertEqual(device.status, "unknown")
        self.assertFalse(device.api_verify_tls)

    def test_add_haivision_device(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(
            site_id=site_id, name="CHI-MX4-1", vendor="haivision", mgmt_host="10.0.1.20",
            device_role="encoder", model="Makito X4",
        )
        device = self.db.get_device(device_id)
        self.assertEqual(device.vendor, "haivision")
        self.assertEqual(device.device_role, "encoder")

    def test_add_net_insight_device_direct_snmp(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(
            site_id=site_id, name="CHI-NIMBRA-1", vendor="net_insight", mgmt_host="10.0.1.30",
            access_mode="direct_snmp", snmp_community_env="CHI_NIMBRA_SNMP",
        )
        device = self.db.get_device(device_id)
        self.assertEqual(device.vendor, "net_insight")
        self.assertEqual(device.access_mode, "direct_snmp")
        # snmp_host defaults to mgmt_host when not explicitly set
        self.assertEqual(device.snmp_host, "10.0.1.30")

    def test_add_net_insight_device_via_nms(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(
            site_id=site_id, name="CHI-NIMBRA-VISION-1", vendor="net_insight", mgmt_host="10.0.1.31",
            access_mode="via_nms", nms_host="10.0.1.5", nms_port=8443,
            nms_api_key_env="NIMBRA_VISION_KEY", nms_device_ref="node-42",
        )
        device = self.db.get_device(device_id)
        self.assertEqual(device.access_mode, "via_nms")
        self.assertEqual(device.nms_host, "10.0.1.5")
        self.assertEqual(device.nms_device_ref, "node-42")

    def test_device_name_must_be_unique(self):
        site_id = self.db.add_site("Chicago")
        self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        with self.assertRaises(Exception):
            self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.11")

    def test_set_device_firmware(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-MX4-1", vendor="haivision", mgmt_host="10.0.1.20")
        self.db.set_device_firmware(device_id, "1.8.0-1")
        self.assertEqual(self.db.get_device(device_id).firmware_version, "1.8.0-1")

    def test_set_device_status_healthy_updates_last_seen(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.set_device_status(device_id, "healthy")
        device = self.db.get_device(device_id)
        self.assertEqual(device.status, "healthy")
        self.assertIsNotNone(device.last_seen_at)

    def test_set_device_status_unreachable_does_not_touch_last_seen(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.set_device_status(device_id, "healthy")
        first_seen = self.db.get_device(device_id).last_seen_at
        self.db.set_device_status(device_id, "unreachable", error="timeout")
        device = self.db.get_device(device_id)
        self.assertEqual(device.status, "unreachable")
        self.assertEqual(device.last_error, "timeout")
        self.assertEqual(device.last_seen_at, first_seen)

    def test_set_device_status_rejects_invalid_value(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        with self.assertRaises(ValueError):
            self.db.set_device_status(device_id, "definitely_not_a_real_status")

    def test_list_devices_excludes_decommissioned_by_default(self):
        site_id = self.db.add_site("Chicago")
        active_id = self.db.add_device(site_id=site_id, name="ACTIVE-1", vendor="appear", mgmt_host="10.0.1.10")
        decom_id = self.db.add_device(site_id=site_id, name="DECOM-1", vendor="appear", mgmt_host="10.0.1.11")
        self.db.set_device_status(decom_id, "decommissioned")

        active_only = self.db.list_devices()
        self.assertEqual([d.id for d in active_only], [active_id])

        with_decom = self.db.list_devices(include_decommissioned=True)
        self.assertEqual(sorted(d.id for d in with_decom), sorted([active_id, decom_id]))

    def test_list_devices_filters_by_vendor(self):
        site_id = self.db.add_site("Chicago")
        self.db.add_device(site_id=site_id, name="APPEAR-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.add_device(site_id=site_id, name="HAIV-1", vendor="haivision", mgmt_host="10.0.1.20")
        appear_only = self.db.list_devices(vendor="appear")
        self.assertEqual([d.name for d in appear_only], ["APPEAR-1"])

    def test_remove_device_cascades_poll_log(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.record_poll(device_id, method="api", success=True, latency_ms=42)
        self.assertEqual(len(self.db.recent_poll_history(device_id)), 1)
        self.db.remove_device(device_id)
        self.assertIsNone(self.db.get_device(device_id))
        self.assertEqual(len(self.db.recent_poll_history(device_id)), 0)

    def test_upsert_module_updates_existing_row(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.upsert_module(device_id, slot="1", module_type="SLx100", firmware_version="1.0")
        self.db.upsert_module(device_id, slot="1", module_type="SLx100", firmware_version="1.1")
        modules = self.db.list_modules(device_id)
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["firmware_version"], "1.1")

    def test_record_poll_accepts_all_methods(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        for method in ("api", "snmp", "nms"):
            self.db.record_poll(device_id, method=method, success=True)
        self.assertEqual(len(self.db.recent_poll_history(device_id)), 3)

    def test_record_poll_rejects_invalid_method(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        with self.assertRaises(ValueError):
            self.db.record_poll(device_id, method="carrier_pigeon", success=True)

    def test_config_snapshot_round_trip(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.add_config_snapshot(device_id, config_hash="abc123", config_json='{"foo": "bar"}')
        latest = self.db.latest_config_snapshot(device_id)
        self.assertEqual(latest["config_hash"], "abc123")

    def test_initialize_bad_schema_path_raises(self):
        bad_db = Database(os.path.join(self.tmpdir.name, "other.db"), schema_path="/nonexistent/schema.sql")
        with self.assertRaises(FileNotFoundError):
            bad_db.initialize()

    def test_get_site_by_name(self):
        self.db.add_site("Chicago")
        self.assertIsNotNone(self.db.get_site_by_name("Chicago"))
        self.assertIsNone(self.db.get_site_by_name("Nowhere"))

    def test_add_and_list_trunks(self):
        a = self.db.add_site("Huntsville HQ", lat=34.73, lng=-86.58)
        b = self.db.add_site("Chicago", lat=41.88, lng=-87.63)
        trunk_id = self.db.add_trunk(a, b, "HSV-CHI contribution")
        trunks = self.db.list_trunks()
        self.assertEqual(len(trunks), 1)
        self.assertEqual(trunks[0]["id"], trunk_id)
        self.assertEqual(trunks[0]["site_a_name"], "Huntsville HQ")
        self.assertEqual(trunks[0]["site_b_name"], "Chicago")
        self.assertEqual(self.db.get_trunk_by_label("HSV-CHI contribution")["id"], trunk_id)

    def test_add_trunk_rejects_same_site(self):
        site_id = self.db.add_site("Chicago")
        with self.assertRaises(ValueError):
            self.db.add_trunk(site_id, site_id, "loop")

    def test_add_and_list_signals(self):
        a = self.db.add_site("Huntsville HQ")
        b = self.db.add_site("Chicago")
        trunk_id = self.db.add_trunk(a, b, "HSV-CHI")
        device_id = self.db.add_device(site_id=a, name="HSV-X20-1", vendor="appear", mgmt_host="10.0.0.10")
        signal_id = self.db.add_signal(
            device_id, "src", "dst", trunk_id=trunk_id, direction="contribution",
        )
        signals = self.db.list_signals()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["id"], signal_id)
        self.assertEqual(signals[0]["device_name"], "HSV-X20-1")
        self.assertEqual(signals[0]["trunk_label"], "HSV-CHI")
        self.assertEqual(signals[0]["site_name"], "Huntsville HQ")
        found = self.db.find_signal(device_id, "src", "dst")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], signal_id)

    def test_add_signal_rejects_invalid_status(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        with self.assertRaises(ValueError):
            self.db.add_signal(device_id, "src", "dst", status="on_fire")

    def test_set_signal_status(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        signal_id = self.db.add_signal(device_id, "src", "dst")
        self.db.set_signal_status(signal_id, "up")
        row = self.db.get_signal(signal_id)
        self.assertEqual(row["status"], "up")
        self.assertIsNotNone(row["last_status_change"])

    def test_add_and_list_ports_and_flows(self):
        hsv = self.db.add_site("Huntsville HQ", lat=34.73, lng=-86.58)
        chi = self.db.add_site("Chicago", lat=41.88, lng=-87.63)
        nyc = self.db.add_site("New York", lat=40.71, lng=-74.01)
        src = self.db.add_device(site_id=hsv, name="HSV-X20-1", vendor="appear", mgmt_host="10.0.0.10")
        dst = self.db.add_device(site_id=chi, name="CHI-NIMBRA-1", vendor="net_insight", mgmt_host="10.0.1.30")
        port_id = self.db.add_port(src, "SDI-1", kind="sdi_in", slot="1")
        self.assertEqual(self.db.find_port(src, "SDI-1")["id"], port_id)
        chi_id = self.db.add_flow(
            "News → CHI", src, source_port_id=port_id, dest_site_id=chi,
            dest_device_id=dst, direction="contribution",
        )
        nyc_id = self.db.add_flow(
            "News → NYC", src, source_port_id=port_id, dest_site_id=nyc,
            dest_label="NYC / program in", direction="contribution",
        )
        flows = self.db.list_flows()
        self.assertEqual(len(flows), 2)
        self.assertEqual({f["id"] for f in flows}, {chi_id, nyc_id})
        self.assertEqual(flows[0]["source_port_name"], "SDI-1")
        found = self.db.find_flow(src, "News → CHI", chi, dst, "")
        self.assertEqual(found["id"], chi_id)
        self.db.set_flow_status(chi_id, "degraded")
        self.assertEqual(self.db.find_flow(src, "News → CHI", chi, dst, "")["status"], "degraded")

    def test_add_flow_requires_destination(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        with self.assertRaises(ValueError):
            self.db.add_flow("nowhere", device_id)

    def test_add_flow_dest_city_only(self):
        hsv = self.db.add_site("Huntsville HQ")
        city = self.db.add_city("New York", lat=40.71, lng=-74.01)
        device_id = self.db.add_device(site_id=hsv, name="HSV-X20-1", vendor="appear", mgmt_host="10.0.0.10")
        flow_id = self.db.add_flow(
            "News → NYC", device_id, dest_city_id=city, signal_label="News", dest_label="NYC / program in",
        )
        row = self.db.find_flow(device_id, "News → NYC", None, None, "NYC / program in", dest_city_id=city)
        self.assertEqual(row["id"], flow_id)
        self.assertEqual(self.db.list_flows()[0]["dest_city_resolved"], "New York")

    def test_add_port_rejects_bad_kind(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        with self.assertRaises(ValueError):
            self.db.add_port(device_id, "X", kind="hdmi")

    def test_latest_poll_at_empty_and_set(self):
        self.assertIsNone(self.db.latest_poll_at())
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10")
        self.db.record_poll(device_id, method="api", success=True)
        self.assertIsNotNone(self.db.latest_poll_at())

    def test_pending_device_empty_host_and_update(self):
        site_id = self.db.add_site("Atlanta - CW")
        device_id = self.db.add_device(
            site_id=site_id, name="ATL-HAI-HAI1161", vendor="haivision",
            mgmt_host="", poll_enabled=False,
            api_username_env="ATL_HAI_HAI1161_USER",
        )
        device = self.db.get_device(device_id)
        self.assertEqual(device.mgmt_host, "")
        self.assertFalse(device.poll_enabled)
        self.db.update_device(device_id, mgmt_host="10.1.2.3", poll_enabled=True)
        device = self.db.get_device(device_id)
        self.assertEqual(device.mgmt_host, "10.1.2.3")
        self.assertTrue(device.poll_enabled)

    def test_duplicate_mgmt_host_rejected(self):
        site_id = self.db.add_site("Chicago")
        self.db.add_device(
            site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10",
        )
        with self.assertRaises(ValueError) as ctx:
            self.db.add_device(
                site_id=site_id, name="CHI-X20-2", vendor="appear", mgmt_host="10.0.1.10",
            )
        self.assertIn("10.0.1.10", str(ctx.exception))

    def test_empty_mgmt_host_allowed_many_times(self):
        site_id = self.db.add_site("Atlanta - CW")
        a = self.db.add_device(
            site_id=site_id, name="ATL-HAI-A", vendor="haivision",
            mgmt_host="", poll_enabled=False,
        )
        b = self.db.add_device(
            site_id=site_id, name="ATL-HAI-B", vendor="haivision",
            mgmt_host="", poll_enabled=False,
        )
        self.assertNotEqual(a, b)
        self.assertEqual(self.db.get_device(a).mgmt_host, "")
        self.assertEqual(self.db.get_device(b).mgmt_host, "")

    def test_snmp_enabled_defaults_from_community(self):
        site_id = self.db.add_site("Chicago")
        device_id = self.db.add_device(
            site_id=site_id, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10",
            snmp_community_env="CHI_X20_1_SNMP",
        )
        device = self.db.get_device(device_id)
        self.assertTrue(device.snmp_enabled)
        self.assertEqual(device.snmp_version, "2c")


if __name__ == "__main__":
    unittest.main()
