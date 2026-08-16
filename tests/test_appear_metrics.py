import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from drivers.appear import interpret_appear_metrics  # noqa: E402
from drivers.prometheus_util import parse_prometheus  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "API-Mibs" / "DC appear x20"


def _load(*names: str) -> str:
    return "\n".join((FIXTURE / name).read_text(encoding="utf-8") for name in names)


class TestAppearLiveScrapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (FIXTURE / "alarms-metrics.txt").is_file():
            raise unittest.SkipTest("Appear X20 scrape fixtures missing")
        cls.body = _load(
            "system-metrics.txt",
            "product-metrics.txt",
            "ipgateway-metrics.txt",
            "alarms-metrics.txt",
        )
        cls.snap = interpret_appear_metrics(parse_prometheus(cls.body))

    def test_degraded_from_critical_alarms_and_unlocked_sdi(self):
        self.assertEqual(self.snap.device_status, "degraded")
        self.assertIn("critical", self.snap.error or "")
        self.assertIn("SDI unlocked", self.snap.error or "")

    def test_chassis_from_system_scrape(self):
        chassis = next(m for m in self.snap.modules if m.slot == "chassis")
        self.assertEqual(chassis.status, "healthy")
        self.assertIn("21C", chassis.module_type)
        self.assertIn("378W", chassis.module_type)
        self.assertIn("rpm", chassis.module_type)

    def test_sdi_services_from_product_scrape(self):
        by_slot = {m.slot: m for m in self.snap.modules}
        self.assertEqual(by_slot["3"].status, "healthy")
        self.assertEqual(by_slot["5"].status, "down")
        unlocked = [m for m in self.snap.modules if m.slot.startswith("5/") and m.status == "down"]
        self.assertTrue(unlocked)
        self.assertTrue(any(m.status == "healthy" and "1080i29.97" in m.module_type for m in self.snap.modules))

    def test_ipgateway_ports_from_scrape(self):
        d4 = next(m for m in self.snap.modules if m.slot.endswith("/D4") and "Management" in m.module_type)
        self.assertEqual(d4.status, "healthy")
        alarmed = [m for m in self.snap.modules if m.slot in ("1/D1", "1/D3", "2/D4")]
        self.assertTrue(alarmed)
        self.assertTrue(all(m.status == "down" for m in alarmed))

    def test_discovers_sdi_and_net_ports(self):
        sdi = [p for p in self.snap.ports if p.kind.startswith("sdi")]
        net = [p for p in self.snap.ports if p.kind in ("net", "mgmt")]
        self.assertEqual(len(sdi), 12)
        self.assertTrue(net)
        spy = next(p for p in sdi if "SpyCam" in p.name)
        self.assertEqual(spy.status, "down")
        self.assertEqual(spy.kind, "sdi_in")
        self.assertTrue(spy.slot)
        enc1 = next(p for p in sdi if p.name.startswith("DC ENC 1"))
        self.assertEqual(enc1.status, "up")
        self.assertTrue(any(p.name == "1/D4" and p.kind == "mgmt" for p in net))

    def test_draft_flows_have_dest_label_only(self):
        self.assertEqual(len(self.snap.flows), 12)
        spy = next(f for f in self.snap.flows if "SpyCam" in f.label)
        self.assertEqual(spy.dest_label, "DC to CHI SpyCam")
        self.assertEqual(spy.status, "down")
        self.assertTrue(spy.port_slot)
        enc = next(f for f in self.snap.flows if f.label.startswith("DC ENC 1"))
        self.assertEqual(enc.status, "up")
        self.assertEqual(enc.dest_label, "DC ENC 1")


if __name__ == "__main__":
    unittest.main()
