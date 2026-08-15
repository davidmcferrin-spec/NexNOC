import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from drivers.net_insight import NetInsightNimbraDriver  # noqa: E402
from drivers.snmp_util import SnmpError, snmp_get, snmp_ping  # noqa: E402


class TestSnmpHelpers(unittest.TestCase):
    def test_snmp_get_raises_if_binary_missing(self):
        with patch("drivers.snmp_util.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(SnmpError):
                snmp_get("127.0.0.1", "public", "1.3.6.1.2.1.1.1.0")

    def test_snmp_get_raises_on_nonzero_exit(self):
        fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Timeout: No Response")
        with patch("drivers.snmp_util.subprocess.run", return_value=fake_result):
            with self.assertRaises(SnmpError):
                snmp_get("127.0.0.1", "public", "1.3.6.1.2.1.1.1.0")

    def test_snmp_get_returns_stripped_output(self):
        fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="  Nimbra MSR 600  \n", stderr="")
        with patch("drivers.snmp_util.subprocess.run", return_value=fake_result):
            value = snmp_get("127.0.0.1", "public", "1.3.6.1.2.1.1.1.0")
        self.assertEqual(value, "Nimbra MSR 600")

    def test_snmp_ping_true_on_success(self):
        fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="Nimbra MSR 600", stderr="")
        with patch("drivers.snmp_util.subprocess.run", return_value=fake_result):
            self.assertTrue(snmp_ping("127.0.0.1", "public"))

    def test_snmp_ping_false_on_failure(self):
        fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Timeout")
        with patch("drivers.snmp_util.subprocess.run", return_value=fake_result):
            self.assertFalse(snmp_ping("127.0.0.1", "public"))


class TestNetInsightDriver(unittest.TestCase):
    def test_driver_identity(self):
        self.assertEqual(NetInsightNimbraDriver.driver_id, "net_insight.nimbra.default")
        self.assertEqual(NetInsightNimbraDriver.vendor, "net_insight")
        self.assertTrue(NetInsightNimbraDriver.is_default_for_vendor())

    def test_ping_direct_snmp_success(self):
        driver = NetInsightNimbraDriver(host="127.0.0.1", snmp_community="public")
        fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="Nimbra MSR 600", stderr="")
        with patch("drivers.snmp_util.subprocess.run", return_value=fake_result):
            self.assertTrue(driver.ping())

    def test_ping_direct_snmp_failure(self):
        driver = NetInsightNimbraDriver(host="127.0.0.1", snmp_community="public")
        fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Timeout")
        with patch("drivers.snmp_util.subprocess.run", return_value=fake_result):
            self.assertFalse(driver.ping())

    def test_ping_without_community_returns_false_not_raise(self):
        driver = NetInsightNimbraDriver(host="127.0.0.1", snmp_community=None)
        self.assertFalse(driver.ping())

    def test_ping_via_nms_raises_not_implemented(self):
        driver = NetInsightNimbraDriver(host="127.0.0.1", snmp_community="public", access_mode="via_nms")
        with self.assertRaises(NotImplementedError):
            driver.ping()

    def test_discover_raises_not_implemented(self):
        driver = NetInsightNimbraDriver(host="127.0.0.1", snmp_community="public")
        with self.assertRaises(NotImplementedError):
            driver.discover()


if __name__ == "__main__":
    unittest.main()
