import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

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
        self.assertIsInstance(state["server_time_ms"], int)

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
        self.assertEqual(state["devices"][0].get("api_username") or "", "")
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

    def test_state_includes_dest_port_id_and_catalogs(self):
        port = self.db.add_port(self.dev, "BNC 1", kind="sdi_in", capability="assignable",
                                direction="input")
        dest_dev = self.db.add_device(
            site_id=self.chi, name="CHI-HAI-1", vendor="haivision", mgmt_host="10.0.0.30",
        )
        dest_port = self.db.add_port(dest_dev, "BNC 2", kind="sdi_out", capability="assignable",
                                     direction="output")
        self.db.add_flow(
            "News → CHI", self.dev, source_port_id=port, dest_site_id=self.chi,
            dest_device_id=dest_dev, dest_port_id=dest_port,
        )
        state = build_dashboard_state(self.db)
        flow = next(f for f in state["flows"] if f["label"] == "News → CHI")
        self.assertEqual(flow["dest_port_id"], dest_port)
        self.assertEqual(flow["dest_port_name"], "BNC 2")
        self.assertTrue(any(d["driver_id"].startswith("appear.") for d in state["drivers"]))
        appear = next(d for d in state["drivers"] if d["driver_id"].startswith("appear."))
        self.assertIn("notes", appear)
        self.assertTrue(appear["notes"])
        self.assertIn("firmware_min", appear)
        self.assertIn("firmware_max", appear)
        self.assertTrue(any(p["id"] == "building" for p in state["pins"]))
        hsv = next(s for s in state["sites"] if s["name"] == "Huntsville HQ")
        self.assertEqual(hsv["pin_icon"], "building")
        self.assertIn("pin_color", hsv)


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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestHttpServer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._prev_geocode = os.environ.get("NEXNOC_GEOCODE")
        self._prev_audit = os.environ.get("NEXNOC_AUDIT_FILE")
        os.environ["NEXNOC_GEOCODE"] = "0"
        os.environ["NEXNOC_AUDIT_FILE"] = os.path.join(self.tmpdir.name, "audit.jsonl")
        self.db = Database(os.path.join(self.tmpdir.name, "test.db"))
        self.db.initialize()
        for row in self.db.list_users():
            self.db.update_user(row["id"], must_change_password=False)
        site = self.db.add_site("Chicago")
        self.device_id = self.db.add_device(
            site_id=site, name="CHI-X20-1", vendor="appear", mgmt_host="10.0.1.10",
        )
        self.pin_dir = Path(self.tmpdir.name) / "pins"
        handler = make_handler(
            self.db,
            env_path=Path(self.tmpdir.name) / "nexnoc.env",
            pin_dir=self.pin_dir,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = self._login("admin", "password")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmpdir.cleanup()
        if self._prev_geocode is None:
            os.environ.pop("NEXNOC_GEOCODE", None)
        else:
            os.environ["NEXNOC_GEOCODE"] = self._prev_geocode
        if self._prev_audit is None:
            os.environ.pop("NEXNOC_AUDIT_FILE", None)
        else:
            os.environ["NEXNOC_AUDIT_FILE"] = self._prev_audit

    def _login(self, username, password):
        status, payload = self._send(
            "POST", "/api/auth/login",
            {"username": username, "password": password},
            auth=False, raw=True,
        )
        self.assertEqual(status, 200)
        raw = payload["_cookie"]
        return raw.split(";", 1)[0].strip()

    def _headers(self, auth=True, extra=None, cookie=None):
        headers = extra or {}
        token = cookie if cookie is not None else (self.cookie if auth else None)
        if token:
            headers["Cookie"] = token if "=" in token else f"nexnoc_session={token}"
        return headers

    def _get(self, path, auth=True, cookie=None, follow=True):
        req = Request(f"http://127.0.0.1:{self.port}{path}")
        for key, value in self._headers(auth=auth, cookie=cookie).items():
            req.add_header(key, value)
        opener = urlopen if follow else build_opener(_NoRedirect()).open
        try:
            with opener(req, timeout=3) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            body = exc.read()
            exc.close()
            if not follow and exc.code in (301, 302, 303, 307, 308):
                return exc.code, exc.headers, body
            raise

    def _send(self, method, path, payload=None, auth=True, cookie=None, raw=False):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in self._headers(auth=auth, cookie=cookie).items():
            req.add_header(key, value)
        try:
            with urlopen(req, timeout=3) as resp:
                parsed = json.loads(resp.read())
                if raw:
                    parsed["_cookie"] = resp.headers.get("Set-Cookie") or ""
                return resp.status, parsed
        except HTTPError as exc:
            body = exc.read()
            exc.close()
            parsed = {}
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"raw": body.decode("utf-8", "replace")}
            if raw:
                return exc.code, parsed
            raise AssertionError(f"{method} {path} -> {exc.code} {parsed}") from None

    def _audit_entries(self):
        path = os.environ["NEXNOC_AUDIT_FILE"]
        if not os.path.isfile(path):
            return []
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def test_api_state(self):
        status, headers, body = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)
        self.assertEqual(payload["summary"]["devices"], 1)
        self.assertEqual(payload["devices"][0]["name"], "CHI-X20-1")
        self.assertIsInstance(payload["server_time_ms"], int)
        self.assertGreater(payload["server_time_ms"], 1_700_000_000_000)

    def test_api_time(self):
        before = int(time.time() * 1000) - 2000
        status, headers, body = self._get("/api/time")
        after = int(time.time() * 1000) + 2000
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)
        self.assertIsInstance(payload["server_time_ms"], int)
        self.assertGreaterEqual(payload["server_time_ms"], before)
        self.assertLessEqual(payload["server_time_ms"], after)
        self.assertTrue(payload["server_time_utc"].endswith("Z"))

    def test_api_device_detail(self):
        self.db.record_poll(self.device_id, method="api", success=True, latency_ms=12)
        status, _, body = self._get(f"/api/devices/{self.device_id}")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["device"]["name"], "CHI-X20-1")
        self.assertEqual(len(payload["recent_polls"]), 1)
        self.assertEqual(payload["recent_traps"], [])

    def test_api_device_missing(self):
        with self.assertRaises(HTTPError) as ctx:
            self._get("/api/devices/999")
        self.assertEqual(ctx.exception.code, 404)

    def test_index_and_kiosk(self):
        status, headers, body = self._get("/dashboard")
        self.assertEqual(status, 200)
        self.assertIn(b"NexNOC", body)
        self.assertNotIn(b'data-view="edit"', body)
        self.assertIn(b'data-view="setup"', body)
        self.assertIn(b'id="setup-add-city"', body)
        self.assertIn(b'id="view-io"', body)
        self.assertNotIn(b'id="map-add-city"', body)
        self.assertIn(b'id="inv-new"', body)
        self.assertIn(b'id="link-new"', body)
        self.assertIn(b"server-time.js", body)
        self.assertIn(b'id="tz-overlay"', body)
        self.assertIn(b'id="zones-toggle"', body)
        self.assertIn(b"America/New_York", body)
        self.assertIn(b"drawer-closed", body)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        status, _, body = self._get("/kiosk", auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b"NexNOC", body)
        self.assertIn(b"app.js", body)
        status, _, login = self._get("/", auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", login)
        self.assertIn(b"login.css", login)
        self.assertIn(b'id="pw-form"', login)
        self.assertNotIn(b"app.js", login)
        self.assertNotIn(b'id="pw-modal"', body)
        status, headers, _ = self._get("/login", auth=False, follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")
        status, headers, _ = self._get("/", auth=True, follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/dashboard")
        status, headers, _ = self._get("/dashboard", auth=False, follow=False)
        self.assertEqual(status, 302)
        self.assertIn("/?next=", headers.get("Location", ""))

    def test_password_change_blocks_dashboard(self):
        row = self.db.get_user_by_username("admin")
        self.db.update_user(row["id"], must_change_password=True)
        status, headers, _ = self._get("/dashboard", follow=False)
        self.assertEqual(status, 302)
        self.assertIn("reason=password", headers.get("Location", ""))
        status, _, login = self._get("/", follow=False)
        self.assertEqual(status, 200)
        self.assertIn(b'id="pw-form"', login)
        self.assertNotIn(b"app.js", login)

    def test_static_js(self):
        status, headers, body = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"REFRESH_MS", body)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        status, _, clock = self._get("/server-time.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DashboardTime", clock)
        status, _, pins = self._get("/pins.js")
        self.assertEqual(status, 200)
        self.assertIn(b"NexNOCPins", pins)
        status, _, body = self._get("/vendor/leaflet/leaflet.js")
        self.assertEqual(status, 200)
        self.assertIn(b"Leaflet", body)
        status, _, page = self._get("/dashboard")
        self.assertIn(b"vendor/leaflet/leaflet.js", page)
        self.assertIn(b"/pins.js", page)
        self.assertIn(b"/server-time.js", page)
        self.assertIn(b"/kiosk", page)
        self.assertNotIn(b"map-data.js", page)
        status, _, login = self._get("/", auth=False)
        self.assertIn(b"login.css", login)
        self.assertNotIn(b"leaflet.js", login)

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
            "api_username": "admin",
            "api_password": "secret-from-portal",
            "poll_enabled": False,
        })
        self.assertEqual(status, 201)
        self.assertNotIn("api_password", device["device"])
        self.assertEqual(device["device"]["api_username"], "admin")
        self.assertTrue(device["device"]["api_username_set"])
        self.assertTrue(device["device"]["api_password_set"])
        device_id = device["device"]["id"]
        row = self.db.get_device(device_id)
        self.assertEqual(row.api_username, "admin")
        self.assertEqual(row.api_password, "secret-from-portal")
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

    def test_failed_inventory_write_audits_ok_false(self):
        status, payload = self._send("POST", "/api/cities", {}, raw=True)
        self.assertEqual(status, 400)
        self.assertIn("required", payload.get("error", ""))
        inventory = [e for e in self._audit_entries() if e.get("action") == "inventory"]
        self.assertTrue(inventory)
        self.assertFalse(inventory[-1]["ok"])
        self.assertEqual(inventory[-1]["method"], "POST")
        self.assertEqual(inventory[-1]["path"], "/api/cities")

        status, created = self._send("POST", "/api/cities", {"name": "Audit City"})
        self.assertEqual(status, 201)
        inventory = [e for e in self._audit_entries() if e.get("action") == "inventory"]
        self.assertTrue(inventory[-1]["ok"])

    def test_fixed_input_cannot_patch_to_sdi_out(self):
        port_id = self.db.add_port(
            self.device_id, "SDI IN 1", kind="sdi_in",
            capability="input", direction="input",
        )
        for body in ({"kind": "sdi_out"}, {"kind": "sdi_out", "direction": "output"}):
            status, payload = self._send(
                "PATCH", f"/api/ports/{port_id}", body, raw=True,
            )
            self.assertEqual(status, 400, body)
            self.assertIn("fixed input", payload.get("error", ""))
            row = self.db.get_port(port_id)
            self.assertEqual(row["kind"], "sdi_in")
            self.assertEqual(row["capability"], "input")
            self.assertEqual(row["direction"], "input")

    def test_assignable_direction_patch_still_sets_kind(self):
        port_id = self.db.add_port(
            self.device_id, "BNC X", kind="other",
            capability="assignable", direction="unused",
        )
        status, payload = self._send(
            "PATCH", f"/api/ports/{port_id}", {"direction": "output"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["port"]["direction"], "output")
        self.assertEqual(payload["port"]["kind"], "sdi_out")

    def test_duplicate_mgmt_host_rejected(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, payload = self._send("POST", "/api/devices", {
            "name": "CHI-DUP-B",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.0.1.10",
        }, raw=True)
        self.assertEqual(status, 400)
        self.assertIn("already used", payload.get("error", ""))

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

    def test_bulk_patch_and_delete(self):
        site_id = self.db.get_device(self.device_id).site_id
        other = self.db.add_site("New York")
        status, created = self._send("POST", "/api/devices", {
            "name": "NY-HAI-1",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.0.2.10",
            "poll_enabled": False,
        })
        self.assertEqual(status, 201)
        other_id = created["device"]["id"]
        status, result = self._send("POST", "/api/devices/bulk", {
            "ids": [self.device_id, other_id],
            "patch": {"site_id": other, "poll_enabled": "true"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(sorted(result["updated"]), sorted([self.device_id, other_id]))
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.db.get_device(self.device_id).site_id, other)
        self.assertTrue(self.db.get_device(other_id).poll_enabled)

        status, f1 = self._send("POST", "/api/flows", {
            "label": "HAI A", "source_device_id": self.device_id, "dest_label": "x",
        })
        status, f2 = self._send("POST", "/api/flows", {
            "label": "HAI B", "source_device_id": other_id, "dest_label": "y",
        })
        status, deleted = self._send("POST", "/api/flows/bulk", {
            "ids": [f1["flow"]["id"], f2["flow"]["id"]],
            "delete": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(deleted["deleted"]), 2)
        self.assertIsNone(self.db.get_flow(f1["flow"]["id"]))

    def test_bulk_merge_devices(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, extra = self._send("POST", "/api/devices", {
            "name": "CHI-X20-DUP",
            "vendor": "appear",
            "site_id": site_id,
            "mgmt_host": "",
        })
        self.assertEqual(status, 201)
        extra_id = extra["device"]["id"]
        status, result = self._send("POST", "/api/devices/bulk", {
            "ids": [self.device_id, extra_id],
            "merge_into": self.device_id,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["kept"], self.device_id)
        self.assertEqual(result["merged"], [extra_id])
        self.assertIsNone(self.db.get_device(extra_id))
        self.assertIsNotNone(self.db.get_device(self.device_id))

    def test_bulk_requires_ids(self):
        with self.assertRaises(AssertionError) as ctx:
            self._send("POST", "/api/devices/bulk", {"patch": {"poll_enabled": True}})
        self.assertIn("400", str(ctx.exception))

    def test_create_device_stamps_driver_connectors(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, created = self._send("POST", "/api/devices", {
            "name": "CHI-HAI-NEW",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.9.9.40",
            "poll_enabled": False,
        })
        self.assertEqual(status, 201)
        ports = self.db.list_ports(created["device"]["id"])
        self.assertEqual(len(ports), 4)
        self.assertEqual({p["name"] for p in ports}, {"BNC 1", "BNC 2", "BNC 3", "BNC 4"})
        self.assertTrue(all(p["capability"] == "assignable" for p in ports))
        self.assertTrue(all(p["direction"] == "unused" for p in ports))

        status, appear = self._send("POST", "/api/devices", {
            "name": "CHI-X20-NEW",
            "vendor": "appear",
            "site_id": site_id,
            "mgmt_host": "10.9.9.41",
            "poll_enabled": False,
        })
        self.assertEqual(status, 201)
        self.assertEqual(len(self.db.list_ports(appear["device"]["id"])), 20)

    def test_flow_assigns_bnc_direction(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, created = self._send("POST", "/api/devices", {
            "name": "DC-HAI-40",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.9.9.50",
            "poll_enabled": False,
        })
        device_id = created["device"]["id"]
        bnc = next(p for p in self.db.list_ports(device_id) if p["name"] == "BNC 1")
        status, flow = self._send("POST", "/api/flows", {
            "label": "HAI 1011",
            "signal_label": "HAI 1011",
            "source_device_id": device_id,
            "source_port_id": bnc["id"],
            "dest_site_id": site_id,
        })
        self.assertEqual(status, 201)
        row = self.db.get_port(bnc["id"])
        self.assertEqual(row["direction"], "input")
        self.assertEqual(row["kind"], "sdi_in")

    def test_site_address_patch_preserves_manual_coords(self):
        status, site = self._send("POST", "/api/sites", {
            "name": "400 N Capitol", "lat": 38.89, "lng": -77.01,
        })
        self.assertEqual(status, 201)
        self.assertEqual(site["site"]["geo_source"], "manual")
        site_id = site["site"]["id"]
        with patch("inventory_api.geocode_or_none", return_value={
            "lat": 1.0, "lng": 2.0, "display_name": "elsewhere", "source": "geocode",
        }):
            status, updated = self._send("PATCH", f"/api/sites/{site_id}", {
                "address": "401 N Capitol St NE",
            })
        self.assertEqual(status, 200)
        self.assertAlmostEqual(updated["site"]["lat"], 38.89)
        self.assertAlmostEqual(updated["site"]["lng"], -77.01)
        self.assertEqual(updated["site"]["geo_source"], "manual")
        self.assertEqual(updated["site"]["address"], "401 N Capitol St NE")

    def test_site_address_patch_geocodes_when_not_manual(self):
        status, site = self._send("POST", "/api/sites", {"name": "Unplaced"})
        self.assertEqual(status, 201)
        site_id = site["site"]["id"]
        with patch("inventory_api.geocode_or_none", return_value={
            "lat": 41.8, "lng": -87.6, "display_name": "Chicago", "source": "geocode",
        }):
            status, updated = self._send("PATCH", f"/api/sites/{site_id}", {
                "address": "233 S Wacker",
            })
        self.assertEqual(status, 200)
        self.assertAlmostEqual(updated["site"]["lat"], 41.8)
        self.assertEqual(updated["site"]["geo_source"], "geocode")

    def test_failed_flow_create_does_not_assign_ports(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, created = self._send("POST", "/api/devices", {
            "name": "DC-HAI-FAIL",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.9.9.51",
            "poll_enabled": False,
        })
        device_id = created["device"]["id"]
        bnc = next(p for p in self.db.list_ports(device_id) if p["name"] == "BNC 1")
        with self.assertRaises(AssertionError) as ctx:
            self._send("POST", "/api/flows", {
                "label": "orphan",
                "source_device_id": device_id,
                "source_port_id": bnc["id"],
            })
        self.assertIn("400", str(ctx.exception))
        row = self.db.get_port(bnc["id"])
        self.assertEqual(row["direction"], "unused")
        self.assertEqual(len(self.db.list_flows()), 0)

    def test_flow_delete_and_move_release_assignable_bnc(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, created = self._send("POST", "/api/devices", {
            "name": "DC-HAI-REL",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.9.9.52",
            "poll_enabled": False,
        })
        device_id = created["device"]["id"]
        ports = {p["name"]: p for p in self.db.list_ports(device_id)}
        bnc1, bnc2 = ports["BNC 1"], ports["BNC 2"]
        status, flow = self._send("POST", "/api/flows", {
            "label": "HAI 1011",
            "source_device_id": device_id,
            "source_port_id": bnc1["id"],
            "dest_site_id": site_id,
        })
        self.assertEqual(status, 201)
        self.assertEqual(self.db.get_port(bnc1["id"])["direction"], "input")
        status, _ = self._send("PATCH", f"/api/flows/{flow['flow']['id']}", {
            "source_port_id": bnc2["id"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.db.get_port(bnc1["id"])["direction"], "unused")
        self.assertEqual(self.db.get_port(bnc2["id"])["direction"], "input")
        status, _ = self._send("DELETE", f"/api/flows/{flow['flow']['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(self.db.get_port(bnc2["id"])["direction"], "unused")

    def test_shared_source_port_stays_assigned(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, created = self._send("POST", "/api/devices", {
            "name": "DC-HAI-FAN",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.9.9.53",
            "poll_enabled": False,
        })
        device_id = created["device"]["id"]
        bnc = next(p for p in self.db.list_ports(device_id) if p["name"] == "BNC 1")
        first = self._send("POST", "/api/flows", {
            "label": "News → CHI",
            "source_device_id": device_id,
            "source_port_id": bnc["id"],
            "dest_site_id": site_id,
        })[1]
        nyc = self._send("POST", "/api/sites", {
            "name": "New York Fan", "lat": 40.7, "lng": -74.0,
        })[1]["site"]["id"]
        self._send("POST", "/api/flows", {
            "label": "News → NYC",
            "source_device_id": device_id,
            "source_port_id": bnc["id"],
            "dest_site_id": nyc,
        })
        self._send("DELETE", f"/api/flows/{first['flow']['id']}")
        self.assertEqual(self.db.get_port(bnc["id"])["direction"], "input")

    def test_distribution_flow_keeps_source_as_output(self):
        site_id = self.db.get_device(self.device_id).site_id
        status, created = self._send("POST", "/api/devices", {
            "name": "CHI-NIM-OUT",
            "vendor": "haivision",
            "site_id": site_id,
            "mgmt_host": "10.9.9.54",
            "poll_enabled": False,
        })
        device_id = created["device"]["id"]
        bnc = next(p for p in self.db.list_ports(device_id) if p["name"] == "BNC 1")
        status, flow = self._send("POST", "/api/flows", {
            "label": "News via CHI",
            "source_device_id": device_id,
            "source_port_id": bnc["id"],
            "origin_device_id": self.device_id,
            "dest_site_id": site_id,
            "direction": "distribution",
        })
        self.assertEqual(status, 201)
        row = self.db.get_port(bnc["id"])
        self.assertEqual(row["direction"], "output")
        self.assertEqual(row["kind"], "sdi_out")
        self.assertEqual(flow["flow"]["origin_device_id"], self.device_id)

    def test_existing_output_hop_is_not_rewritten_to_input(self):
        site_id = self.db.get_device(self.device_id).site_id
        out_id = self.db.add_port(
            self.device_id, "out-NYC", kind="sdi_out",
            capability="assignable", direction="output",
        )
        status, _ = self._send("POST", "/api/flows", {
            "label": "forward",
            "source_device_id": self.device_id,
            "source_port_id": out_id,
            "dest_site_id": site_id,
        })
        self.assertEqual(status, 201)
        row = self.db.get_port(out_id)
        self.assertEqual(row["direction"], "output")
        self.assertEqual(row["kind"], "sdi_out")

    def test_pin_upload_and_geocode_endpoint(self):
        status, site = self._send("POST", "/api/sites", {
            "name": "400 N Capitol", "lat": 38.89, "lng": -77.01,
        })
        self.assertEqual(status, 201)
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        status, result = self._send("POST", f"/api/sites/{site['site']['id']}/pin", {
            "filename": "pin.png",
            "data": png,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["site"]["pin_icon"], "upload")
        stored = result["site"]["pin_upload"]
        self.assertTrue(stored.endswith("pin.png"))
        get_status, headers, body = self._get(f"/uploads/pins/{stored}")
        self.assertEqual(get_status, 200)
        self.assertEqual(body[:4], b"\x89PNG")

        status, none_hit = self._send("POST", "/api/geocode", {"query": "Washington", "kind": "city"})
        self.assertEqual(status, 200)
        self.assertIsNone(none_hit["hit"])
        with patch("inventory_api.geocode", return_value={
            "lat": 38.9, "lng": -77.0, "display_name": "Washington", "source": "geocode",
        }):
            status, hit = self._send("POST", "/api/geocode", {"query": "Washington", "kind": "city"})
        self.assertEqual(status, 200)
        self.assertEqual(hit["hit"]["lat"], 38.9)

    def test_delete_city_blocked_via_api(self):
        status, city = self._send("POST", "/api/cities", {
            "name": "Blocked City", "lat": 1.0, "lng": 2.0,
        })
        self.assertEqual(status, 201)
        status, _site = self._send("POST", "/api/sites", {
            "name": "A Building",
            "city_id": city["city"]["id"],
            "city": "Blocked City",
            "lat": 1.0,
            "lng": 2.0,
        })
        self.assertEqual(status, 201)
        with self.assertRaises(AssertionError) as ctx:
            self._send("DELETE", f"/api/cities/{city['city']['id']}")
        self.assertIn("400", str(ctx.exception))
        self.assertIn("site", str(ctx.exception).lower())

    def test_anon_state_strips_secret_flags(self):
        status, _, body = self._get("/api/state", auth=False)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        device = payload["devices"][0]
        self.assertNotIn("api_username", device)
        self.assertNotIn("api_password", device)
        self.assertNotIn("credentials_ready", device)
        self.assertIn("mgmt_host", device)
        authed = json.loads(self._get("/api/state")[2])
        self.assertIn("credentials_ready", authed["devices"][0])

    def test_anon_cannot_write_or_read_device_detail(self):
        status, payload = self._send("POST", "/api/cities", {"name": "Nope"}, auth=False, raw=True)
        self.assertEqual(status, 401)
        with self.assertRaises(HTTPError) as ctx:
            self._get("/api/devices/%s" % self.device_id, auth=False)
        self.assertEqual(ctx.exception.code, 401)

    def test_viewer_cannot_write(self):
        cookie = self._login("user", "password")
        status, payload = self._send(
            "POST", "/api/cities", {"name": "Viewer City"}, cookie=cookie, raw=True,
        )
        self.assertEqual(status, 403)
        me = self._send("GET", "/api/auth/me", cookie=cookie)[1]
        self.assertEqual(me["user"]["roles"], ["viewer"])
        self.assertFalse(me["user"]["permissions"]["manage_inventory"])

    def test_bad_login_and_me(self):
        status, payload = self._send(
            "POST", "/api/auth/login",
            {"username": "admin", "password": "wrong"},
            auth=False, raw=True,
        )
        self.assertEqual(status, 401)
        status, me = self._send("GET", "/api/auth/me")
        self.assertEqual(status, 200)
        self.assertEqual(me["user"]["username"], "admin")
        self.assertTrue(me["user"]["permissions"]["manage_users"])


if __name__ == "__main__":
    unittest.main()
