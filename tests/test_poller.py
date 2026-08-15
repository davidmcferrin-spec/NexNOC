import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import poller as poller_mod  # noqa: E402
from db import Database  # noqa: E402
from poller import (  # noqa: E402
    HTTP_TIMEOUT_SECONDS,
    PollOutcome,
    _summarize_cycle,
    build_driver,
    cached_driver,
    devices_due,
    poll_loop,
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


class _Dev:
    def __init__(self, device_id):
        self.id = device_id


class TestDevicesDue(unittest.TestCase):
    def test_first_start_is_immediately_due(self):
        due = devices_due([_Dev(1)], {}, set(), now=1000.0, interval=30)
        self.assertEqual([d.id for d in due], [1])

    def test_skips_in_flight_and_respects_interval(self):
        devices = [_Dev(1), _Dev(2), _Dev(3)]
        last = {1: 100.0, 2: 100.0, 3: 100.0}
        self.assertEqual(
            [d.id for d in devices_due(devices, last, {2}, now=125.0, interval=30)],
            [],
        )
        self.assertEqual(
            [d.id for d in devices_due(devices, last, {2}, now=131.0, interval=30)],
            [1, 3],
        )


class TestDriverCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "t.db"))
        self.db.initialize()
        self.site = self.db.add_site("Chicago")
        self.device_id = self.db.add_device(
            site_id=self.site, name="CHI-X20-1", vendor="appear",
            mgmt_host="10.0.1.10",
        )
        poller_mod._driver_cache.clear()

    def tearDown(self):
        poller_mod._driver_cache.clear()
        self.tmpdir.cleanup()

    def test_reuses_instance(self):
        device = self.db.get_device(self.device_id)
        first = cached_driver(self.db, device)
        second = cached_driver(self.db, device)
        self.assertIs(first, second)

    def test_rebuilds_when_host_changes(self):
        device = self.db.get_device(self.device_id)
        first = cached_driver(self.db, device)
        self.db.update_device(self.device_id, mgmt_host="10.9.9.9")
        device = self.db.get_device(self.device_id)
        second = cached_driver(self.db, device)
        self.assertIsNot(first, second)

    def test_http_timeout_is_two_seconds(self):
        self.assertEqual(HTTP_TIMEOUT_SECONDS, 2.0)
        device = self.db.get_device(self.device_id)
        driver = build_driver(self.db, device)
        self.assertEqual(driver._client.timeout, 2.0)

    def test_rebuilds_when_snmp_community_value_rotates(self):
        os.environ["NEXNOC_TEST_COMM"] = "public"
        self.addCleanup(lambda: os.environ.pop("NEXNOC_TEST_COMM", None))
        device_id = self.db.add_device(
            site_id=self.site, name="CHI-NIMBRA-1", vendor="generic_snmp",
            mgmt_host="10.0.2.10", access_mode="direct_snmp",
            snmp_community_env="NEXNOC_TEST_COMM",
        )
        device = self.db.get_device(device_id)
        first = cached_driver(self.db, device)
        os.environ["NEXNOC_TEST_COMM"] = "rotated"
        second = cached_driver(self.db, device)
        self.assertIsNot(first, second)
        self.assertEqual(second.snmp_community, "rotated")

    def test_rebuilds_when_snmp_v3_secret_rotates(self):
        os.environ["NEXNOC_TEST_V3_USER"] = "monitor"
        os.environ["NEXNOC_TEST_V3_AUTH"] = "old-auth"
        os.environ["NEXNOC_TEST_V3_PRIV"] = "old-priv"
        self.addCleanup(lambda: os.environ.pop("NEXNOC_TEST_V3_USER", None))
        self.addCleanup(lambda: os.environ.pop("NEXNOC_TEST_V3_AUTH", None))
        self.addCleanup(lambda: os.environ.pop("NEXNOC_TEST_V3_PRIV", None))
        device_id = self.db.add_device(
            site_id=self.site, name="CHI-NIMBRA-V3", vendor="generic_snmp",
            mgmt_host="10.0.2.11", access_mode="direct_snmp",
            snmp_version="3",
            snmp_v3_user_env="NEXNOC_TEST_V3_USER",
            snmp_v3_auth_pass_env="NEXNOC_TEST_V3_AUTH",
            snmp_v3_priv_pass_env="NEXNOC_TEST_V3_PRIV",
        )
        device = self.db.get_device(device_id)
        first = cached_driver(self.db, device)
        os.environ["NEXNOC_TEST_V3_AUTH"] = "new-auth"
        second = cached_driver(self.db, device)
        self.assertIsNot(first, second)
        self.assertEqual(second.snmp_target.v3_auth_pass, "new-auth")


class TestPollScheduler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "t.db"))
        self.db.initialize()
        self.site = self.db.add_site("Chicago")

    async def asyncTearDown(self):
        poller_mod._driver_cache.clear()
        self.tmpdir.cleanup()

    async def _run_loop(self, interval, max_in_flight, tick, seconds):
        task = asyncio.create_task(poll_loop(
            self.db, interval,
            max_in_flight=max_in_flight, tick_seconds=tick,
        ))
        try:
            await asyncio.sleep(seconds)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_slow_device_does_not_block_fast(self):
        self.db.add_device(
            site_id=self.site, name="SLOW", vendor="appear", mgmt_host="10.0.0.1",
        )
        self.db.add_device(
            site_id=self.site, name="FAST", vendor="appear", mgmt_host="10.0.0.2",
        )
        started = []

        async def fake_poll(db, device):
            started.append(device.name)
            if device.name == "SLOW":
                await asyncio.sleep(0.45)
            else:
                await asyncio.sleep(0.02)
            return PollOutcome(device.name)

        with patch("poller.poll_device", fake_poll):
            await self._run_loop(interval=0.12, max_in_flight=8, tick=0.02, seconds=0.40)

        self.assertGreaterEqual(started.count("FAST"), 2)
        self.assertEqual(started.count("SLOW"), 1)

    async def test_in_flight_cap(self):
        for i in range(5):
            self.db.add_device(
                site_id=self.site, name=f"DEV-{i}", vendor="appear",
                mgmt_host=f"10.0.0.{i + 1}",
            )
        current = 0
        peak = 0

        async def fake_poll(db, device):
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.2)
            current -= 1
            return PollOutcome(device.name)

        with patch("poller.poll_device", fake_poll):
            await self._run_loop(interval=1.0, max_in_flight=2, tick=0.02, seconds=0.12)

        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
