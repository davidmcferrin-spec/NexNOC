import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database  # noqa: E402
from envfile import upsert_values  # noqa: E402
from trapd import (  # noqa: E402
    LINK_DOWN,
    apply_trap,
    decode_snmp_message,
    encode_snmpv2c_trap,
    handle_datagram,
)


class TestSnmpTraps(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "test.db"))
        self.db.initialize()
        self.env_path = Path(self.tmpdir.name) / "nexnoc.env"
        self.site = self.db.add_site("Chicago")
        self.device_id = self.db.add_device(
            site_id=self.site, name="CHI-X20-1", vendor="appear",
            mgmt_host="10.1.1.10", snmp_community_env="CHI_X20_1_SNMP",
            snmp_enabled=True, snmp_trap_enabled=True,
        )
        upsert_values(self.env_path, {"CHI_X20_1_SNMP": "secret-comm"})

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_v2c_roundtrip_decode(self):
        pkt = encode_snmpv2c_trap("secret-comm", LINK_DOWN)
        decoded = decode_snmp_message(pkt)
        self.assertEqual(decoded["version"], "2c")
        self.assertEqual(decoded["trap_oid"], LINK_DOWN)
        self.assertEqual(decoded["community"], "secret-comm")

    def test_linkdown_matches_device_and_sets_degraded(self):
        pkt = encode_snmpv2c_trap("secret-comm", LINK_DOWN)
        decoded = decode_snmp_message(pkt)
        apply_trap(self.db, "10.1.1.10", decoded, env_path=self.env_path)
        device = self.db.get_device(self.device_id)
        self.assertEqual(device.status, "degraded")
        traps = self.db.list_traps(device_id=self.device_id)
        self.assertEqual(len(traps), 1)
        self.assertTrue(traps[0]["matched"])
        blob = traps[0]["varbinds_json"] or ""
        self.assertNotIn("secret-comm", blob)

    def test_handle_datagram_does_not_persist_community(self):
        pkt = encode_snmpv2c_trap("secret-comm", LINK_DOWN)
        handle_datagram(self.db, pkt, "10.1.1.10")
        traps = self.db.list_traps()
        self.assertEqual(len(traps), 1)
        row = dict(traps[0])
        self.assertNotIn("community", row)
        self.assertNotIn("secret-comm", str(row))


if __name__ == "__main__":
    unittest.main()
