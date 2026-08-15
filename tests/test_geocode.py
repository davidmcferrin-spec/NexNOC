import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from geocode import GeocodeError, geocode, geocode_or_none  # noqa: E402


class TestGeocode(unittest.TestCase):
    def test_fake_fetch_parses_nominatim_hit(self):
        payload = json.dumps([
            {"lat": "38.9072", "lon": "-77.0369", "display_name": "Washington, DC"},
        ]).encode("utf-8")
        seen = {}

        def fetch(url):
            seen["url"] = url
            return payload

        hit = geocode("Washington", kind="city", fetch=fetch)
        self.assertEqual(hit["lat"], 38.9072)
        self.assertEqual(hit["lng"], -77.0369)
        self.assertIn("Washington", hit["display_name"])
        self.assertEqual(hit["source"], "geocode")
        self.assertIn("nominatim.openstreetmap.org", seen["url"])
        self.assertIn("class=place", seen["url"])

    def test_empty_list_is_none(self):
        hit = geocode("nowhere-xyz", fetch=lambda url: b"[]")
        self.assertIsNone(hit)

    def test_bad_json_raises(self):
        with self.assertRaises(GeocodeError):
            geocode("x", fetch=lambda url: b"not-json")

    def test_empty_query_raises(self):
        with self.assertRaises(ValueError):
            geocode("  ")

    def test_disabled_env_skips_network(self):
        prev = os.environ.get("NEXNOC_GEOCODE")
        os.environ["NEXNOC_GEOCODE"] = "0"
        try:
            self.assertIsNone(geocode("Chicago"))
            hit = geocode(
                "Chicago",
                fetch=lambda url: b'[{"lat":"41.8","lon":"-87.6"}]',
            )
            self.assertEqual(hit["lat"], 41.8)
        finally:
            if prev is None:
                os.environ.pop("NEXNOC_GEOCODE", None)
            else:
                os.environ["NEXNOC_GEOCODE"] = prev

    def test_geocode_or_none_swallows_errors(self):
        self.assertIsNone(geocode_or_none("x", fetch=lambda url: b"nope"))


if __name__ == "__main__":
    unittest.main()
