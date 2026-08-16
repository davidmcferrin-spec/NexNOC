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
from drivers.appear import interpret_appear_metrics  # noqa: E402
from drivers.base import CollectResult, DiscoveredFlow, DiscoveredPort  # noqa: E402
from drivers.prometheus_util import parse_prometheus  # noqa: E402
from poller import (  # noqa: E402
    HTTP_TIMEOUT_SECONDS,
    PollOutcome,
    _apply_snapshot,
    _summarize_cycle,
    build_driver,
    cached_driver,
    devices_due,
    poll_loop,
    setup_logging,
    should_bootstrap,
    snmp_target_for,
)


class TestBootstrapGate(unittest.TestCase):
    def test_import_only_when_empty_or_explicit(self):
        self.assertTrue(should_bootstrap(True, True))
        self.assertTrue(should_bootstrap(True, False))
        self.assertTrue(should_bootstrap(False, False))
        self.assertFalse(should_bootstrap(False, True))


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
            mgmt_host="10.0.1.10", snmp_community="public", snmp_enabled=False,
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
        device_id = self.db.add_device(
            site_id=self.site, name="CHI-NIMBRA-1", vendor="generic_snmp",
            mgmt_host="10.0.2.10", access_mode="direct_snmp",
            snmp_community="public",
        )
        device = self.db.get_device(device_id)
        first = cached_driver(self.db, device)
        self.db.update_device(device_id, snmp_community="rotated")
        device = self.db.get_device(device_id)
        second = cached_driver(self.db, device)
        self.assertIsNot(first, second)
        self.assertEqual(second.snmp_community, "rotated")

    def test_rebuilds_when_snmp_v3_secret_rotates(self):
        device_id = self.db.add_device(
            site_id=self.site, name="CHI-NIMBRA-V3", vendor="generic_snmp",
            mgmt_host="10.0.2.11", access_mode="direct_snmp",
            snmp_version="3",
            snmp_v3_user="monitor",
            snmp_v3_auth_pass="old-auth",
            snmp_v3_priv_pass="old-priv",
        )
        device = self.db.get_device(device_id)
        first = cached_driver(self.db, device)
        self.db.update_device(device_id, snmp_v3_auth_pass="new-auth")
        device = self.db.get_device(device_id)
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


class TestApplySnapshotDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "t.db"))
        self.db.initialize()
        self.site = self.db.add_site("Washington")
        self.device_id = self.db.add_device(
            site_id=self.site, name="DC-X20-1", vendor="appear",
            mgmt_host="10.0.1.10",
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def _device(self):
        return self.db.get_device(self.device_id)

    def test_creates_ports_and_draft_flows_once(self):
        snap = CollectResult(
            ports=[
                DiscoveredPort(
                    name="Slot 5-Enc.6 (DC to CHI SpyCam)", kind="sdi_in",
                    slot="cfg-spy", capability="input", direction="input",
                    status="down",
                ),
                DiscoveredPort(name="1/D1", kind="net", slot="net:1:D1", status="up"),
            ],
            flows=[
                DiscoveredFlow(
                    label="Slot 5-Enc.6 (DC to CHI SpyCam)",
                    dest_label="DC to CHI SpyCam",
                    port_slot="cfg-spy",
                    port_name="Slot 5-Enc.6 (DC to CHI SpyCam)",
                    signal_label="DC to CHI SpyCam",
                    status="down",
                ),
            ],
        )
        _apply_snapshot(self.db, self._device(), snap)
        _apply_snapshot(self.db, self._device(), snap)
        ports = self.db.list_ports(self.device_id)
        flows = [f for f in self.db.list_flows() if f["source_device_id"] == self.device_id]
        self.assertEqual(len(ports), 2)
        self.assertEqual(len(flows), 1)
        flow = flows[0]
        self.assertEqual(flow["dest_label"], "DC to CHI SpyCam")
        self.assertIsNone(flow["dest_site_id"])
        self.assertIsNone(flow["dest_device_id"])
        self.assertIsNone(flow["dest_city_id"])
        self.assertEqual(flow["status"], "down")
        self.assertEqual(self.db.find_port(self.device_id, "1/D1")["status"], "up")

    def test_later_poll_updates_status_not_destination(self):
        chi = self.db.add_site("Chicago")
        self.db.add_port(
            self.device_id, "Slot 5-Enc.6 (DC to CHI SpyCam)",
            kind="sdi_in", slot="cfg-spy",
        )
        self.db.add_flow(
            label="Slot 5-Enc.6 (DC to CHI SpyCam)",
            source_device_id=self.device_id,
            dest_site_id=chi,
            dest_label="placed",
            status="unknown",
        )
        snap = CollectResult(
            ports=[
                DiscoveredPort(
                    name="Slot 5-Enc.6 (DC to CHI SpyCam)", kind="sdi_in",
                    slot="cfg-spy", status="up",
                ),
            ],
            flows=[
                DiscoveredFlow(
                    label="Slot 5-Enc.6 (DC to CHI SpyCam)",
                    dest_label="DC to CHI SpyCam",
                    port_slot="cfg-spy",
                    status="up",
                ),
            ],
        )
        _apply_snapshot(self.db, self._device(), snap)
        flows = [f for f in self.db.list_flows() if f["source_device_id"] == self.device_id]
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["status"], "up")
        self.assertEqual(flows[0]["dest_site_id"], chi)
        self.assertEqual(flows[0]["dest_label"], "placed")
        self.assertEqual(self.db.find_port_by_slot(self.device_id, "cfg-spy")["status"], "up")

    def test_live_appear_fixtures_apply(self):
        fixture = Path(__file__).resolve().parent.parent / "API-Mibs" / "DC appear x20"
        if not (fixture / "product-metrics.txt").is_file():
            self.skipTest("Appear X20 scrape fixtures missing")
        body = "\n".join(
            (fixture / name).read_text(encoding="utf-8")
            for name in (
                "system-metrics.txt", "product-metrics.txt",
                "ipgateway-metrics.txt", "alarms-metrics.txt",
            )
        )
        snap = interpret_appear_metrics(parse_prometheus(body))
        _apply_snapshot(self.db, self._device(), snap)
        _apply_snapshot(self.db, self._device(), snap)
        ports = self.db.list_ports(self.device_id)
        flows = [f for f in self.db.list_flows() if f["source_device_id"] == self.device_id]
        self.assertEqual(len([p for p in ports if p["kind"].startswith("sdi")]), 12)
        self.assertEqual(len(flows), 12)
        self.assertTrue(all(f["dest_site_id"] is None for f in flows))
        self.assertTrue(any(f["dest_label"] == "DC to CHI SpyCam" for f in flows))


if __name__ == "__main__":
    unittest.main()
