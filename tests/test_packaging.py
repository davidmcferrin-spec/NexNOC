import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(rel):
    path = os.path.join(ROOT, rel)
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestPackaging(unittest.TestCase):
    def test_setup_script_exists_and_is_bash(self):
        text = _read("setup.sh")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("nexnoc-poller", text)
        self.assertIn("nexnoc-web", text)
        self.assertIn("apache2", text)
        self.assertIn("sqlite3", text)
        self.assertNotIn("pip install", text)

    def test_systemd_units_bind_loopback_and_use_env_file(self):
        poller = _read("systemd/nexnoc-poller.service")
        web = _read("systemd/nexnoc-web.service")
        self.assertIn("EnvironmentFile=-/etc/nexnoc/nexnoc.env", poller)
        self.assertIn("EnvironmentFile=-/etc/nexnoc/nexnoc.env", web)
        self.assertIn("User=nexnoc", poller)
        self.assertIn("User=nexnoc", web)
        self.assertIn("--db /var/lib/nexnoc/noc.db", poller)
        self.assertIn("--host 127.0.0.1", web)
        self.assertIn("--port 8080", web)
        self.assertNotIn("0.0.0.0", web)

    def test_apache_template_proxies_to_loopback(self):
        conf = _read("config/apache-nexnoc.conf")
        self.assertIn("@@SERVER_NAME@@", conf)
        self.assertIn("@@WEB_PORT@@", conf)
        self.assertIn("ProxyPass", conf)
        self.assertIn("127.0.0.1", conf)

    def test_env_example_matches_config_example_names(self):
        env = _read("config/nexnoc.env.example")
        cfg = _read("config.example.json")
        for name in (
            "CHI_X20_1_USER",
            "CHI_X20_1_PASS",
            "CHI_MX4_1_USER",
            "CHI_MX4_1_PASS",
            "NYC_X20_1_USER",
            "NYC_X20_1_PASS",
            "CHI_NIMBRA_1_SNMP_COMMUNITY",
        ):
            self.assertIn(name, env)
            self.assertIn(name, cfg)


if __name__ == "__main__":
    unittest.main()
