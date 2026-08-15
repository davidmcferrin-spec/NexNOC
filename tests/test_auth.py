import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import (  # noqa: E402
    authenticate,
    effective_permissions,
    hash_password,
    load_session_user,
    next_url,
    verify_password,
)
from db import Database  # noqa: E402
from ldap_util import roles_from_ldap_groups  # noqa: E402


class TestPasswordsAndRoles(unittest.TestCase):
    def test_hash_roundtrip(self):
        stored = hash_password("password")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(verify_password("password", stored))
        self.assertFalse(verify_password("other", stored))

    def test_next_url_defaults_to_dashboard(self):
        self.assertEqual(next_url(""), "/dashboard")
        self.assertEqual(next_url("/"), "/dashboard")
        self.assertEqual(next_url("/login"), "/dashboard")
        self.assertEqual(next_url("/#map"), "/dashboard#map")
        self.assertEqual(next_url("/dashboard#links"), "/dashboard#links")
        self.assertEqual(next_url("https://evil"), "/dashboard")

    def test_role_or_and_overrides(self):
        perms = effective_permissions(["viewer", "operator"])
        self.assertTrue(perms["dashboard"])
        self.assertTrue(perms["manage_inventory"])
        self.assertFalse(perms["manage_users"])
        denied = effective_permissions(["admin"], {"manage_users": False})
        self.assertFalse(denied["manage_users"])
        granted = effective_permissions(["viewer"], {"manage_inventory": True})
        self.assertTrue(granted["manage_inventory"])


class TestSeedAndSession(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmpdir.name, "test.db"))
        self.db.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_seed_admin_and_user(self):
        names = {row["username"] for row in self.db.list_users()}
        self.assertEqual(names, {"admin", "user"})
        admin = authenticate(self.db, "admin", "password")
        self.assertTrue(admin["ok"])
        self.assertIn("admin", admin["user"]["roles"])
        self.assertTrue(admin["user"]["must_change_password"])
        user = authenticate(self.db, "user", "password")
        self.assertTrue(user["ok"])
        self.assertEqual(user["user"]["roles"], ["viewer"])

    def test_disabled_and_idle(self):
        row = self.db.get_user_by_username("user")
        self.db.update_user(row["id"], enabled=False)
        result = authenticate(self.db, "user", "password")
        self.assertFalse(result["ok"])
        self.db.update_user(row["id"], enabled=True, must_change_password=False)
        ok = authenticate(self.db, "user", "password")
        loaded = load_session_user(self.db, ok["token"])
        self.assertEqual(loaded["username"], "user")
        self.db.update_auth_settings(session_idle_minutes=5)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity = '2000-01-01T00:00:00.000Z' WHERE id = ?",
                (ok["token"],),
            )
        self.assertIsNone(load_session_user(self.db, ok["token"]))


class TestLdapGroups(unittest.TestCase):
    def test_group_cn_maps_roles(self):
        roles = roles_from_ldap_groups(
            ["CN=NexNOC-Operators,OU=Groups,DC=nexstar,DC=tv"],
            [{"name": "NexNOC-Operators", "roles": ["operator"]}],
        )
        self.assertEqual(roles, ["operator"])

    def test_ldap_login_ephemeral(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(os.path.join(tmp.name, "test.db"))
        db.initialize()
        db.update_auth_settings(ldap={
            "enabled": True,
            "host": "ldaps://ad.example.com",
            "port": 636,
            "bind_template": "{username}@nexstar.tv",
            "base_dn": "DC=nexstar,DC=tv",
            "ignore_cert": True,
            "allowed_groups": [{"name": "NexNOC-Viewers", "roles": ["viewer"]}],
        })
        fake = {
            "ok": True,
            "bind_dn": "alice@nexstar.tv",
            "member_of": ["CN=NexNOC-Viewers,OU=Groups,DC=nexstar,DC=tv"],
        }
        with patch("ldap_util.ldap_bind_user", return_value=fake):
            result = authenticate(db, "alice", "secret")
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("ephemeral"))
        self.assertEqual(result["user"]["roles"], ["viewer"])


if __name__ == "__main__":
    unittest.main()
