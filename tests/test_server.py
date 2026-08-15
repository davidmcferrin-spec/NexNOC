import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import Database  # noqa: E402
from server import (  # noqa: E402
    CDN_TILE_URL,
    build_dashboard_state,
    effective_signal_status,
    make_handler,
    resolve_map_settings,
)


class TestEffectiveStatus(unittest.TestCase):
    def test_explicit_status_wins(self):
        self.assertEqual(effective_signal_status("down", "healthy"), "down")
        self.assertEqual(effective_signal_status("up", "unreachable"), "up")

    def test_unknown_follows_device(self):
        self.assertEqual(effective_signal_status("unknown", "healthy"), "up")
        self.assertEqual(effective_signal_status("unknown", "degraded"), "degraded")
        self.assertEqual(effective_signal_status("unknown", "unreachable"), "down")
        self.assertEqual(effective_signal_status("unknown", "unknown"), "unknown")
        self.assertEqual(effective_signal_status(None, "healthy"), "up")


class TestDashboardState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "test.db"))
        self.db.initialize()
        self.hsv = self.db.add_site("Huntsville HQ", lat=34.73, lng=-86.58)
        self.chi = self.db.add_site("Chicago", lat=41.88, lng=-87.63)
        self.dev = self.db.add_device(
            site_id=self.hsv, name="HSV-X20-1", vendor="appear", mgmt_host="10.0.0.10",
        )
        self.trunk = self.db.add_trunk(self.hsv, self.chi, "HSV-CHI")
        self.db.add_signal(self.dev, "src", "dst", trunk_id=self.trunk, direction="contribution")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_empty_db_state(self):
        empty = Database(os.path.join(self.tmpdir.name, "empty.db"))
        empty.initialize()
        state = build_dashboard_state(empty)
        self.assertEqual(state["summary"]["sites"], 0)
        self.assertEqual(state["summary"]["devices"], 0)
        self.assertEqual(state["signals"], [])
        self.assertEqual(state["flows"], [])
        self.assertEqual(state["hops"], [])
        self.assertEqual(state["cities"], [])
        self.assertIsNone(state["latest_poll_at"])

    def test_derived_trunk_and_site_status(self):
        self.db.set_device_status(self.dev, "healthy")
        state = build_dashboard_state(self.db)
        self.assertEqual(state["signals"][0]["effective_status"], "up")
        self.assertEqual(state["trunks"][0]["status"], "up")
        self.assertEqual(state["sites"][1]["name"], "Huntsville HQ")
        hsv = next(s for s in state["sites"] if s["name"] == "Huntsville HQ")
        self.assertEqual(hsv["status"], "healthy")
        self.assertEqual(state["devices"][0]["site_name"], "Huntsville HQ")
        self.assertNotIn("api_password", state["devices"][0])
        self.assertNotIn("api_username", state["devices"][0])
        self.assertIn("api_password_env", state["devices"][0])
        self.assertFalse(state["devices"][0]["credentials_ready"])

    def test_worst_signal_wins_on_trunk(self):
        self.db.set_device_status(self.dev, "healthy")
        other = self.db.add_device(
            site_id=self.hsv, name="HSV-MX4-1", vendor="haivision", mgmt_host="10.0.0.20",
        )
        sid = self.db.add_signal(other, "enc", "chi", trunk_id=self.trunk)
        self.db.set_signal_status(sid, "down")
        state = build_dashboard_state(self.db)
        self.assertEqual(state["trunks"][0]["status"], "down")
        self.assertEqual(state["trunks"][0]["signal_count"], 2)

    def test_flow_hops_fan_out_and_worst_status(self):
        nyc = self.db.add_site("New York", lat=40.71, lng=-74.01)
        port = self.db.add_port(self.dev, "SDI-1", kind="sdi_in")
        chi = self.db.add_flow(
            "News → CHI", self.dev, source_port_id=port, dest_site_id=self.chi,
        )
        self.db.add_flow(
            "News → NYC", self.dev, source_port_id=port, dest_site_id=nyc,
        )
        self.db.set_device_status(self.dev, "healthy")
        self.db.set_flow_status(chi, "down")
        state = build_dashboard_state(self.db)
        self.assertEqual(len(state["flows"]), 2)
        self.assertEqual(len(state["hops"]), 2)
        chi_hop = next(h for h in state["hops"] if "Chicago" in (h["city_a_name"], h["city_b_name"]))
        nyc_hop = next(h for h in state["hops"] if "New York" in (h["city_a_name"], h["city_b_name"]))
        self.assertEqual(chi_hop["flow_count"], 1)
        self.assertEqual(chi_hop["status"], "down")
        self.assertEqual(nyc_hop["status"], "up")
        self.assertIn("|", chi_hop["id"])
        self.assertEqual(len(chi_hop["directions"]), 1)

    def test_two_sites_one_city_one_map_node(self):
        chi_city = self.db.add_city("Chicago", lat=41.88, lng=-87.63)
        wacker = self.db.add_site("Chicago - Wacker", city="Chicago", city_id=chi_city,
                                  lat=41.89, lng=-87.64)
        midway = self.db.add_site("Chicago - Midway", city="Chicago", city_id=chi_city,
                                  lat=41.79, lng=-87.75)
        hsv_city = self.db.add_city("Huntsville", lat=34.73, lng=-86.58)
        self.db.set_site_city(self.hsv, hsv_city, "Huntsville")
        port = self.db.add_port(self.dev, "SDI-2", kind="sdi_in")
        self.db.add_flow(
            "News → Wacker", self.dev, source_port_id=port, dest_site_id=wacker,
            dest_city_id=chi_city, signal_label="News",
        )
        self.db.add_flow(
            "Weather → Midway", self.dev, source_port_id=port, dest_site_id=midway,
            dest_city_id=chi_city, signal_label="Weather",
        )
        state = build_dashboard_state(self.db)
        chicago = next(c for c in state["cities"] if c["name"] == "Chicago")
        self.assertEqual(chicago["site_count"], 2)
        chi_hops = [h for h in state["hops"] if "Chicago" in (h["city_a_name"], h["city_b_name"])]
        self.assertEqual(len(chi_hops), 1)
        self.assertEqual(chi_hops[0]["flow_count"], 2)
        self.assertEqual(sorted(chi_hops[0]["site_names"]),
                         ["Chicago - Midway", "Chicago - Wacker", "Huntsville HQ"])

    def test_bidirectional_flows_one_trunk(self):
        nyc = self.db.add_site("New York", lat=40.71, lng=-74.01)
        nyc_dev = self.db.add_device(
            site_id=nyc, name="NYC-X20-1", vendor="appear", mgmt_host="10.0.2.10",
        )
        out = self.db.add_port(self.dev, "SDI-out", kind="sdi_in")
        back = self.db.add_port(nyc_dev, "SDI-1", kind="sdi_in")
        self.db.add_flow("east", self.dev, source_port_id=out, dest_site_id=nyc)
        self.db.add_flow("west", nyc_dev, source_port_id=back, dest_site_id=self.hsv)
        state = build_dashboard_state(self.db)
        pair = [h for h in state["hops"]
                if "New York" in (h["city_a_name"], h["city_b_name"])
                and "Huntsville HQ" in (h["city_a_name"], h["city_b_name"])]
        self.assertEqual(len(pair), 1)
        self.assertEqual(pair[0]["flow_count"], 2)
        self.assertEqual(len(pair[0]["directions"]), 2)

    def test_output_keeps_foreign_origin(self):
        nyc = self.db.add_site("New York", lat=40.71, lng=-74.01)
        chi_dev = self.db.add_device(
            site_id=self.chi, name="CHI-NIMBRA-1", vendor="net_insight", mgmt_host="10.0.1.30",
        )
        out_port = self.db.add_port(chi_dev, "out-NYC", kind="sdi_out")
        origin_port = self.db.add_port(self.dev, "SDI-1b", kind="sdi_in")
        self.db.add_flow(
            "News via CHI", chi_dev, source_port_id=out_port, dest_site_id=nyc,
            signal_label="News", origin_device_id=self.dev, origin_port_id=origin_port,
        )
        state = build_dashboard_state(self.db)
        flow = next(f for f in state["flows"] if f["signal_label"] == "News")
        self.assertEqual(flow["source_device_name"], "CHI-NIMBRA-1")
        self.assertEqual(flow["origin_device_name"], "HSV-X20-1")
        self.assertEqual(flow["origin_port_name"], "SDI-1b")


class TestMapSettings(unittest.TestCase):
    def test_cdn_defaults(self):
        settings = resolve_map_settings()
        self.assertEqual(settings["public"]["source"], "cdn")
        self.assertEqual(settings["public"]["tile_url"], CDN_TILE_URL)
        self.assertIsNone(settings["tile_dir"])

    def test_local_dir_switches_url(self):
        settings = resolve_map_settings({"local_tile_dir": "/var/lib/nexnoc/tiles"})
        self.assertEqual(settings["public"]["source"], "local")
        self.assertEqual(settings["public"]["tile_url"], "/tiles/{z}/{x}/{y}.png")
        self.assertEqual(settings["public"]["tile_subdomains"], "")
        self.assertEqual(settings["tile_dir"], Path("/var/lib/nexnoc/tiles"))


class TestHttpServer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "test.db"))
        self.db.initialize()
        site = self.db.add_site("Chicago")
        self.device_id = self.db.add_device(
            site_id=site, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10",
        )
        handler = make_handler(self.db, env_path=Path(self.tmpdir.name) / "nexnoc.env")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmpdir.cleanup()

    def _get(self, path):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            exc.read()
            exc.close()
            raise

    def _send(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            body = exc.read()
            exc.close()
            parsed = {}
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"raw": body.decode("utf-8", "replace")}
            raise AssertionError(f"{method} {path} -> {exc.code} {parsed}") from None

    def test_api_state(self):
        status, headers, body = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)
        self.assertEqual(payload["summary"]["devices"], 1)
        self.assertEqual(payload["devices"][0]["name"], "CHI-X20-1")

    def test_api_device_detail(self):
        self.db.record_poll(self.device_id, method="api", success=True, latency_ms=12)
        status, _, body = self._get(f"/api/devices/{self.device_id}")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["device"]["name"], "CHI-X20-1")
        self.assertEqual(len(payload["recent_polls"]), 1)

    def test_api_device_missing(self):
        with self.assertRaises(HTTPError) as ctx:
            self._get("/api/devices/999")
        self.assertEqual(ctx.exception.code, 404)

    def test_index_and_kiosk(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"NexNOC", body)
        self.assertNotIn(b'data-view="edit"', body)
        self.assertIn(b'id="inv-new"', body)
        self.assertIn(b'id="link-new"', body)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        status, _, body = self._get("/kiosk")
        self.assertEqual(status, 200)
        self.assertIn(b"NexNOC", body)

    def test_static_js(self):
        status, headers, body = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"REFRESH_MS", body)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        status, _, body = self._get("/vendor/leaflet/leaflet.js")
        self.assertEqual(status, 200)
        self.assertIn(b"Leaflet", body)
        status, _, page = self._get("/")
        self.assertIn(b"vendor/leaflet/leaflet.js", page)
        self.assertIn(b"/kiosk", page)
        self.assertNotIn(b"map-data.js", page)

    def test_api_state_includes_cdn_map_defaults(self):
        status, _, body = self._get("/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["map"]["source"], "cdn")
        self.assertEqual(payload["map"]["tile_url"], CDN_TILE_URL)

    def test_local_tiles_served(self):
        tiledir = os.path.join(self.tmpdir.name, "tiles")
        os.makedirs(os.path.join(tiledir, "3", "1"))
        png = os.path.join(tiledir, "3", "1", "2.png")
        with open(png, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        settings = resolve_map_settings({"local_tile_dir": tiledir})
        handler = make_handler(self.db, settings)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        status, headers, body = self._get("/tiles/3/1/2.png")
        self.assertEqual(status, 200)
        self.assertEqual(body[:4], b"\x89PNG")
        self.assertIn("image/png", headers.get("Content-Type", ""))
        payload = json.loads(self._get("/api/state")[2])
        self.assertEqual(payload["map"]["source"], "local")
        self.assertEqual(payload["map"]["tile_url"], "/tiles/{z}/{x}/{y}.png")
        with self.assertRaises(HTTPError) as ctx:
            self._get("/tiles/3/1/99.png")
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_api_404(self):
        with self.assertRaises(HTTPError) as ctx:
            self._get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_path_traversal_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            self._get("/../schema.sql")
        self.assertEqual(ctx.exception.code, 404)

    def test_create_and_patch_device_with_credentials(self):
        status, created = self._send("POST", "/api/cities", {
            "name": "Atlanta", "lat": 33.75, "lng": -84.39,
        })
        self.assertEqual(status, 201)
        city_id = created["city"]["id"]
        status, site = self._send("POST", "/api/sites", {
            "name": "Atlanta - CW", "city_id": city_id, "city": "Atlanta",
        })
        self.assertEqual(status, 201)
        status, device = self._send("POST", "/api/devices", {
            "name": "ATL-HAI-PENDING",
            "vendor": "haivision",
            "site_id": site["site"]["id"],
            "mgmt_host": "",
            "api_username_env": "ATL_HAI_PENDING_USER",
            "api_password_env": "ATL_HAI_PENDING_PASS",
            "api_username": "admin",
            "api_password": "secret-from-portal",
            "poll_enabled": False,
        })
        self.assertEqual(status, 201)
        self.assertNotIn("api_password", device["device"])
        self.assertTrue(device["device"]["api_username_set"])
        self.assertTrue(device["device"]["api_password_set"])
        env_text = (Path(self.tmpdir.name) / "nexnoc.env").read_text(encoding="utf-8")
        self.assertIn("ATL_HAI_PENDING_USER=admin", env_text)
        self.assertIn("ATL_HAI_PENDING_PASS=secret-from-portal", env_text)
        device_id = device["device"]["id"]
        status, _patched = self._send("PATCH", f"/api/devices/{device_id}", {
            "mgmt_host": "10.9.9.9",
            "poll_enabled": True,
        })
        self.assertEqual(status, 200)
        row = self.db.get_device(device_id)
        self.assertEqual(row.mgmt_host, "10.9.9.9")
        self.assertTrue(row.poll_enabled)
        payload = json.loads(self._get("/api/state")[2])
        listed = next(d for d in payload["devices"] if d["name"] == "ATL-HAI-PENDING")
        self.assertTrue(listed["credentials_ready"])
        self.assertNotIn("api_password", listed)
        self.assertNotIn("secret-from-portal", json.dumps(payload))

    def test_duplicate_mgmt_host_rejected(self):
        site_id = self.db.get_device(self.device_id).site_id
        data = json.dumps({
            "name": "CHI-DUP-B",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.0.1.10",
        }).encode("utf-8")
        req = Request(
            f"http://127.0.0.1:{self.port}/api/devices", data=data, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn(b"already used", ctx.exception.read())

    def test_create_flow_and_delete(self):
        status, flow = self._send("POST", "/api/flows", {
            "label": "HAI 9999",
            "signal_label": "Test Path",
            "source_device_id": self.device_id,
            "dest_label": "Burbank",
        })
        self.assertEqual(status, 201)
        flow_id = flow["flow"]["id"]
        status, _ = self._send("DELETE", f"/api/flows/{flow_id}")
        self.assertEqual(status, 200)
        self.assertIsNone(self.db.get_flow(flow_id))


if __name__ == "__main__":
    unittest.main()
