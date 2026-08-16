import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import authenticate, user_payload  # noqa: E402
from auth_api import AuthError, handle_auth  # noqa: E402
from db import Database  # noqa: E402
from svc_util import (  # noqa: E402
    CONTROLLABLE_UNITS,
    SvcError,
    check_unit,
    control_service,
    list_services,
    parse_show,
    restart_service,
    service_logs,
)


class TestSvcParse(unittest.TestCase):
    def test_parse_show(self):
        info = parse_show(
            "ActiveState=active\nSubState=running\nUnitFileState=enabled\n"
            "Description=Dashboard\nMainPID=42\nNRestarts=1\n"
            "ActiveEnterTimestamp=Sat 2026-08-15\n"
        )
        self.assertEqual(info["active"], "active")
        self.assertEqual(info["sub"], "running")
        self.assertEqual(info["enabled"], "enabled")
        self.assertEqual(info["pid"], 42)
        self.assertEqual(info["restarts"], 1)

    def test_unknown_unit_rejected(self):
        with self.assertRaises(SvcError):
            check_unit("sshd")
        with self.assertRaises(SvcError):
            check_unit("apache2")
        with self.assertRaises(SvcError):
            check_unit("../nexnoc-web")


class TestSvcHelper(unittest.TestCase):
    def test_list_and_logs(self):
        def fake_run(args, timeout=15):
            if args[0] == "status":
                return f"ActiveState=active\nSubState=running\nDescription={args[1]}\nMainPID=9\n"
            if args[0] == "logs":
                return f"{args[1]} line 1\n"
            raise AssertionError(args)

        with patch("svc_util._run", side_effect=fake_run):
            listing = list_services()
            self.assertTrue(listing["available"])
            ids = [row["id"] for row in listing["services"]]
            self.assertEqual(ids, ["nexnoc-web", "nexnoc-poller", "nexnoc-trapd"])
            self.assertTrue(listing["services"][1]["controllable"])
            self.assertFalse(listing["services"][0]["controllable"])
            self.assertEqual(listing["services"][0]["active"], "active")
            self.assertEqual(service_logs("nexnoc-poller", 50), "nexnoc-poller line 1\n")

    def test_restart_web_does_not_wait(self):
        with patch("svc_util.subprocess.Popen") as popen:
            result = restart_service("nexnoc-web")
        self.assertTrue(result["restarting"])
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0][3:], ["restart", "nexnoc-web"])

    def test_restart_other_waits(self):
        with patch("svc_util._run", return_value="") as run:
            result = restart_service("nexnoc-poller")
        self.assertFalse(result["restarting"])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["restart", "nexnoc-poller"])

    def test_control_poller_and_trapd(self):
        self.assertEqual(CONTROLLABLE_UNITS, frozenset({"nexnoc-poller", "nexnoc-trapd"}))
        with patch("svc_util._run", return_value="") as run:
            result = control_service("nexnoc-poller", "stop")
        self.assertEqual(result["action"], "stop")
        self.assertEqual(run.call_args[0][0], ["stop", "nexnoc-poller"])
        with patch("svc_util._run", return_value="") as run:
            control_service("nexnoc-trapd", "disable")
        self.assertEqual(run.call_args[0][0], ["disable", "nexnoc-trapd"])

    def test_control_web_rejected(self):
        with self.assertRaises(SvcError):
            control_service("nexnoc-web", "stop")
        with self.assertRaises(SvcError):
            control_service("nexnoc-poller", "reload")


class TestSvcAdminApi(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "t.db"))
        self.db.initialize()
        admin = authenticate(self.db, "admin", "password")
        self.db.update_user(admin["user"]["id"], must_change_password=False)
        self.user = user_payload(self.db.get_user(admin["user"]["id"]), self.db.get_auth_settings())
        viewer = authenticate(self.db, "user", "password")
        self.db.update_user(viewer["user"]["id"], must_change_password=False)
        self.viewer = user_payload(self.db.get_user(viewer["user"]["id"]), self.db.get_auth_settings())

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_viewer_denied(self):
        with self.assertRaises(AuthError) as ctx:
            handle_auth(self.db, "GET", "/api/admin/services", {}, self.viewer, "t", False, "1.1.1.1")
        self.assertEqual(ctx.exception.status, 403)

    def test_admin_list_and_restart(self):
        listing = {"available": True, "error": None, "services": [
            {"id": "nexnoc-poller", "label": "poller", "active": "active"},
        ]}
        with patch("auth_api.list_services", return_value=listing):
            status, payload, _cookie = handle_auth(
                self.db, "GET", "/api/admin/services", {}, self.user, "t", False, "1.1.1.1",
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["services"][0]["id"], "nexnoc-poller")

        with patch("auth_api.service_logs", return_value="hello\n"):
            status, payload, _cookie = handle_auth(
                self.db, "GET", "/api/admin/services/nexnoc-poller/logs",
                {"lines": "80"}, self.user, "t", False, "1.1.1.1",
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["log"], "hello\n")

        with patch("auth_api.restart_service", return_value={"ok": True, "restarting": False}):
            status, payload, _cookie = handle_auth(
                self.db, "POST", "/api/admin/services/nexnoc-poller/restart",
                {}, self.user, "t", False, "1.1.1.1",
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        with patch("auth_api.restart_service", return_value={"ok": True, "restarting": True}):
            status, payload, _cookie = handle_auth(
                self.db, "POST", "/api/admin/services/nexnoc-web/restart",
                {}, self.user, "t", False, "1.1.1.1",
            )
        self.assertEqual(status, 202)

        with patch("auth_api.control_service", return_value={"ok": True, "action": "stop", "restarting": False}) as ctrl:
            status, payload, _cookie = handle_auth(
                self.db, "POST", "/api/admin/services/nexnoc-poller/stop",
                {}, self.user, "t", False, "1.1.1.1",
            )
        self.assertEqual(status, 200)
        ctrl.assert_called_once()
        self.assertEqual(ctrl.call_args[0][:2], ("nexnoc-poller", "stop"))

        with patch("auth_api.control_service", return_value={"ok": True, "action": "disable", "restarting": False}):
            status, payload, _cookie = handle_auth(
                self.db, "POST", "/api/admin/services/nexnoc-trapd/disable",
                {}, self.user, "t", False, "1.1.1.1",
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["action"], "disable")
