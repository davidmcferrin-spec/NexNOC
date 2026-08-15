import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from drivers.appear import AppearXPlatformDriver  # noqa: E402
from drivers.haivision import HaivisionMakitoXDriver  # noqa: E402
from drivers.base import DriverError, DriverAuthError, DriverUnreachableError  # noqa: E402
from drivers.http_util import JsonHttpClient  # noqa: E402


class _FakeDeviceHandler(BaseHTTPRequestHandler):
    """Minimal fake device: / returns 200, /api/status returns JSON,
    /secure requires Basic auth, /broken returns invalid JSON, /apidoc
    returns an HTML page (simulating Haivision's real on-device explorer)."""

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/apis/authentication":
            try:
                payload = json.loads(body.decode() or "{}")
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            if payload.get("username") == "admin" and payload.get("password") == "secret":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "session=makito-test")
                self.end_headers()
                self.wfile.write(b'{"username":"admin","uid":500}')
                return
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path == "/":
            self._send(200, b"ok", "text/plain")
        elif self.path == "/api/status":
            self._send(200, json.dumps({"health": "ok", "modules": 3}).encode(), "application/json")
        elif self.path == "/apis/status":
            self._send(200, json.dumps({
                "cardStatus": "OK",
                "firmwareVersion": "1.8.0-1",
                "serialNumber": "HAI-1",
                "cardType": "Makito X4 SDI Encoder",
            }).encode(), "application/json")
        elif self.path == "/apis/videnc":
            self._send(200, json.dumps({"data": [{"id": 0, "status": "RUNNING"}]}).encode(), "application/json")
        elif self.path == "/apis/audenc":
            self._send(200, json.dumps([]).encode(), "application/json")
        elif self.path == "/apis/streams":
            self._send(200, json.dumps([]).encode(), "application/json")
        elif self.path == "/apis/vidin":
            self._send(200, json.dumps([]).encode(), "application/json")
        elif self.path == "/prometheus/system/metrics":
            self._send(200, b"# TYPE memory_usage_ratio gauge\nmemory_usage_ratio{slot=\"1\"} 0.5\n", "text/plain")
        elif self.path == "/prometheus/product/metrics":
            text = (
                "# TYPE apr_x_sdi_lock_status gauge\n"
                "apr_x_sdi_lock_status{slot=\"3\",config_label=\"DC ENC 1\"} 1\n"
                "apr_x_sdi_lock_status{slot=\"5\",config_label=\"SpyCam\"} 0\n"
            )
            self._send(200, text.encode(), "text/plain")
        elif self.path == "/prometheus/alarms/metrics":
            self._send(200, b"# TYPE total_alarms gauge\ntotal_alarms{severity=\"critical\",slot=\"5\"} 2\n", "text/plain")
        elif self.path == "/prometheus/ipgateway/metrics":
            self._send(200, b"# TYPE port_rx_rate gauge\nport_rx_rate{slot=\"1\",connector=\"D4\"} 32792\n", "text/plain")
        elif self.path == "/apidoc":
            self._send(200, b"<html>API Explorer</html>", "text/html")
        elif self.path == "/secure":
            auth = self.headers.get("Authorization")
            if auth != "Basic YWRtaW46c2VjcmV0":  # admin:secret
                self.send_response(401)
                self.end_headers()
                return
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
        elif self.path == "/broken":
            self._send(200, b"not json{{{", "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)


class TestJsonHttpClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeDeviceHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def _client(self, **kwargs) -> JsonHttpClient:
        return JsonHttpClient(host="127.0.0.1", port=self.port, scheme="http", **kwargs)

    def test_ping_success(self):
        self.assertTrue(self._client().ping())

    def test_ping_unreachable_host(self):
        client = JsonHttpClient(host="127.0.0.1", port=1, scheme="http", timeout=0.5)
        self.assertFalse(client.ping())

    def test_get_json_success(self):
        self.assertEqual(self._client().get_json("/api/status"), {"health": "ok", "modules": 3})

    def test_get_json_404_raises(self):
        with self.assertRaises(DriverError):
            self._client().get_json("/notfound")

    def test_get_json_invalid_json_raises(self):
        with self.assertRaises(DriverError):
            self._client().get_json("/broken")

    def test_auth_required_without_credentials_raises_auth_error(self):
        with self.assertRaises(DriverAuthError):
            self._client().get_json("/secure")

    def test_auth_required_with_correct_credentials_succeeds(self):
        client = self._client(extra_headers={"Authorization": "Basic YWRtaW46c2VjcmV0"})
        self.assertEqual(client.get_json("/secure"), {"ok": True})

    def test_connection_refused_raises_unreachable(self):
        client = JsonHttpClient(host="127.0.0.1", port=1, scheme="http", timeout=0.2)
        with self.assertRaises(DriverUnreachableError):
            client.get_json("/")

    def test_discover_reports_ok_and_not_ok_paths(self):
        results = self._client().discover(["/api/status", "/notfound", "/"])
        by_path = {r.path: r for r in results}
        self.assertTrue(by_path["/api/status"].ok)
        self.assertFalse(by_path["/notfound"].ok)
        self.assertTrue(by_path["/"].ok)


class TestAppearDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeDeviceHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def test_driver_identity(self):
        self.assertEqual(AppearXPlatformDriver.driver_id, "appear.x_platform.default")
        self.assertEqual(AppearXPlatformDriver.vendor, "appear")
        self.assertTrue(AppearXPlatformDriver.is_default_for_vendor())
        self.assertIn("Prometheus", AppearXPlatformDriver.notes)

    def test_applies_to_any_model_and_firmware(self):
        # default driver: no constraints, matches anything including None
        self.assertTrue(AppearXPlatformDriver.applies_to(None, None))
        self.assertTrue(AppearXPlatformDriver.applies_to("X20", "3.1.0"))

    def test_ping(self):
        driver = AppearXPlatformDriver(host="127.0.0.1", port=self.port, scheme="http")
        self.assertTrue(driver.ping())

    def test_basic_auth_header_built_correctly(self):
        driver = AppearXPlatformDriver(host="127.0.0.1", port=self.port, scheme="http",
                                        username="admin", password="secret")
        self.assertEqual(driver.get_json("/secure"), {"ok": True})

    def test_discover_uses_default_candidates(self):
        driver = AppearXPlatformDriver(host="127.0.0.1", port=self.port, scheme="http")
        results = driver.discover()
        self.assertTrue(any(r.path == "/prometheus/system/metrics" and r.ok for r in results))
        self.assertTrue(any(r.path == "/prometheus/alarms/metrics" and r.ok for r in results))

    def test_collect_from_prometheus_metrics(self):
        driver = AppearXPlatformDriver(host="127.0.0.1", port=self.port, scheme="http")
        snap = driver.collect()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.device_status, "degraded")
        self.assertIn("critical", snap.error or "")
        slots = {m.slot: m for m in snap.modules}
        self.assertEqual(slots["5"].status, "down")
        self.assertEqual(slots["3"].status, "healthy")


class TestHaivisionDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeDeviceHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def test_driver_identity(self):
        self.assertEqual(HaivisionMakitoXDriver.driver_id, "haivision.makito_x.default")
        self.assertEqual(HaivisionMakitoXDriver.vendor, "haivision")
        self.assertTrue(HaivisionMakitoXDriver.is_default_for_vendor())
        self.assertIn("/apidoc", HaivisionMakitoXDriver.notes)

    def test_ping(self):
        driver = HaivisionMakitoXDriver(host="127.0.0.1", port=self.port, scheme="http")
        self.assertTrue(driver.ping())

    def test_apidoc_path_is_reachable_in_discovery(self):
        driver = HaivisionMakitoXDriver(host="127.0.0.1", port=self.port, scheme="http")
        results = driver.discover()
        by_path = {r.path: r for r in results}
        self.assertTrue(by_path["/apidoc"].ok)
        self.assertTrue(by_path["/apis/status"].ok)

    def test_collect_after_session_login(self):
        driver = HaivisionMakitoXDriver(
            host="127.0.0.1", port=self.port, scheme="http",
            username="admin", password="secret",
        )
        snap = driver.collect()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.device_status, "healthy")
        self.assertEqual(snap.firmware_version, "1.8.0-1")
        slots = [m.slot for m in snap.modules]
        self.assertIn("system", slots)
        self.assertTrue(any(s.startswith("videnc:") for s in slots))


if __name__ == "__main__":
    unittest.main()
