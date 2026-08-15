import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from drivers.prometheus_util import (  # noqa: E402
    looks_like_prometheus,
    parse_prometheus,
    sum_named,
)


SAMPLE = """
# HELP total_alarms Number of active alarms in the system.
# TYPE total_alarms gauge
total_alarms{config_id="abc",severity="critical",slot="5"} 2.000000
total_alarms{config_id="def",severity="major",slot="1"} 1.000000
# HELP apr_x_sdi_lock_status Whether or not lock is acquired
# TYPE apr_x_sdi_lock_status gauge
apr_x_sdi_lock_status{slot="3",config_label="DC ENC 1 (DC ENC 1)",direction="input"} 1
apr_x_sdi_lock_status{slot="5",config_label="Service: Slot 5-Enc.6 (DC to CHI SpyCam)",direction="input"} 0
memory_usage_ratio{slot="13"} 0.503852
"""


class TestPrometheusParser(unittest.TestCase):
    def test_looks_like_prometheus(self):
        self.assertTrue(looks_like_prometheus(SAMPLE))
        self.assertFalse(looks_like_prometheus("<html>nope</html>"))
        self.assertFalse(looks_like_prometheus(""))

    def test_parse_labels_and_values(self):
        samples = parse_prometheus(SAMPLE)
        by_name = {}
        for s in samples:
            by_name.setdefault(s.name, []).append(s)
        self.assertEqual(sum_named(samples, "total_alarms", severity="critical"), 2.0)
        self.assertEqual(sum_named(samples, "total_alarms", severity="major"), 1.0)
        locks = by_name["apr_x_sdi_lock_status"]
        self.assertEqual(len(locks), 2)
        spy = next(s for s in locks if s.labels.get("slot") == "5")
        self.assertEqual(spy.value, 0)
        self.assertIn("SpyCam", spy.labels["config_label"])

    def test_skips_malformed_lines(self):
        samples = parse_prometheus("not a metric\ncpu 1.5\n")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].name, "cpu")
        self.assertEqual(samples[0].value, 1.5)


if __name__ == "__main__":
    unittest.main()
