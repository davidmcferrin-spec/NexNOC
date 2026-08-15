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


class TestSnmpV3Args(unittest.TestCase):
    def test_snmp_get_v3_builds_usm_flags(self):
        from drivers.snmp_util import SnmpTarget

        fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
        captured = {}

        def run(cmd, **kwargs):
            captured["cmd"] = cmd
            return fake_result

        target = SnmpTarget(
            host="10.0.0.1", version="3", v3_user="noc",
            v3_sec_level="authPriv", v3_auth_proto="SHA", v3_auth_pass="auth-secret",
            v3_priv_proto="AES", v3_priv_pass="priv-secret",
        )
        with patch("drivers.snmp_util.subprocess.run", side_effect=run):
            snmp_get("10.0.0.1", target=target)
        cmd = captured["cmd"]
        self.assertIn("-v3", cmd)
        self.assertIn("-u", cmd)
        self.assertIn("noc", cmd)
        self.assertIn("-l", cmd)
        self.assertIn("authPriv", cmd)
        self.assertEqual(cmd[0], "snmpget")
        # argv may contain secrets; SnmpError messages must not
        with patch("drivers.snmp_util.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(SnmpError) as ctx:
                snmp_get("10.0.0.1", target=target)
        self.assertNotIn("auth-secret", str(ctx.exception))
        self.assertNotIn("priv-secret", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
