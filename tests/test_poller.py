import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database  # noqa: E402
from poller import (  # noqa: E402
    PollOutcome,
    _summarize_cycle,
    setup_logging,
    snmp_target_for,
)


class TestPollerLogging(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def test_log_file_and_verbose(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "poller.log")
        setup_logging(verbose=True, log_file=path)
        log = logging.getLogger("nexnoc.poller")
        log.debug("per-device line")
        log.info("cycle summary")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("per-device line", text)
        self.assertIn("cycle summary", text)

    def test_cycle_summary_counts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "poller.log")
        setup_logging(verbose=False, log_file=path)
        _summarize_cycle([
            PollOutcome("A", api=True, snmp=True),
            PollOutcome("B", api=False, snmp=True),
            PollOutcome("C", skipped=True, skip_reason="poll off"),
        ])
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("2 polled, 1 skipped", text)
        self.assertIn("api 1/2 ok", text)
        self.assertIn("snmp 2/2 ok", text)


class TestSnmpTarget(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "t.db"))
        self.db.initialize()
        self.site = self.db.add_site("Chicago")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_disabled_returns_none(self):
        device_id = self.db.add_device(
            site_id=self.site, name="CHI-X20-1", vendor="appear",
            mgmt_host="10.0.1.10", snmp_community_env="COMM", snmp_enabled=False,
        )
        self.assertIsNone(snmp_target_for(self.db.get_device(device_id)))

    def test_enabled_without_community_returns_none(self):
        device_id = self.db.add_device(
            site_id=self.site, name="CHI-X20-1", vendor="appear",
            mgmt_host="10.0.1.10", snmp_enabled=True,
        )
        self.assertIsNone(snmp_target_for(self.db.get_device(device_id)))


if __name__ == "__main__":
    unittest.main()
