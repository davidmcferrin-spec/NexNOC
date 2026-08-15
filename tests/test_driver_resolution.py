import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from drivers.base import Driver, DriverResolutionError, resolve_driver, _parse_version  # noqa: E402


# --- Fixture drivers for testing resolution logic in isolation, independent
# of the real Appear/Haivision/Net Insight drivers (which may themselves
# change over time) ---

class _FakeVendorDefaultDriver(Driver):
    driver_id = "fakevendor.default"
    vendor = "fakevendor"

    def ping(self) -> bool:
        return True


class _FakeVendorModelXDriver(Driver):
    driver_id = "fakevendor.model_x.default"
    vendor = "fakevendor"
    supported_models = ["Model X", "MX"]

    def ping(self) -> bool:
        return True


class _FakeVendorModelXNewFirmwareDriver(Driver):
    driver_id = "fakevendor.model_x.fw3plus"
    vendor = "fakevendor"
    supported_models = ["Model X"]
    firmware_min = "3.0.0"

    def ping(self) -> bool:
        return True


FIXTURE_REGISTRY = [
    _FakeVendorModelXNewFirmwareDriver,  # narrowest first - order matters for ties
    _FakeVendorModelXDriver,
    _FakeVendorDefaultDriver,
]


class TestVersionParsing(unittest.TestCase):
    def test_simple_dotted_version(self):
        self.assertEqual(_parse_version("2.4.1"), (2, 4, 1))

    def test_v_prefix_stripped(self):
        self.assertEqual(_parse_version("v2.4.1"), (2, 4, 1))

    def test_non_numeric_suffix_does_not_raise(self):
        # Not a strict semver parser - a '-rc3' style suffix just contributes
        # whatever digits it has rather than raising. Exact value isn't the
        # contract here; not-raising and staying comparable is.
        result = _parse_version("2.4.1-rc3")
        self.assertEqual(result[:2], (2, 4))
        self.assertIsInstance(result, tuple)

    def test_empty_string_does_not_raise(self):
        self.assertEqual(_parse_version(""), (0,))


class TestDriverApplies(unittest.TestCase):
    def test_default_driver_matches_anything(self):
        self.assertTrue(_FakeVendorDefaultDriver.applies_to(None, None))
        self.assertTrue(_FakeVendorDefaultDriver.applies_to("Anything", "9.9.9"))

    def test_default_driver_is_default_for_vendor(self):
        self.assertTrue(_FakeVendorDefaultDriver.is_default_for_vendor())
        self.assertFalse(_FakeVendorModelXDriver.is_default_for_vendor())

    def test_model_constrained_driver_matches_substring_case_insensitive(self):
        self.assertTrue(_FakeVendorModelXDriver.applies_to("model x pro", None))
        self.assertTrue(_FakeVendorModelXDriver.applies_to("MX-4000", None))
        self.assertFalse(_FakeVendorModelXDriver.applies_to("Model Y", None))

    def test_model_constrained_driver_requires_a_model(self):
        self.assertFalse(_FakeVendorModelXDriver.applies_to(None, None))

    def test_firmware_range_requires_firmware_version_present(self):
        self.assertFalse(_FakeVendorModelXNewFirmwareDriver.applies_to("Model X", None))

    def test_firmware_range_respects_min(self):
        self.assertTrue(_FakeVendorModelXNewFirmwareDriver.applies_to("Model X", "3.1.0"))
        self.assertTrue(_FakeVendorModelXNewFirmwareDriver.applies_to("Model X", "3.0.0"))
        self.assertFalse(_FakeVendorModelXNewFirmwareDriver.applies_to("Model X", "2.9.9"))


class TestResolveDriver(unittest.TestCase):
    def test_falls_back_to_default_when_no_model_given(self):
        driver = resolve_driver(FIXTURE_REGISTRY, vendor="fakevendor")
        self.assertEqual(driver.driver_id, "fakevendor.default")

    def test_falls_back_to_default_for_unrecognized_model(self):
        driver = resolve_driver(FIXTURE_REGISTRY, vendor="fakevendor", model="Model Z")
        self.assertEqual(driver.driver_id, "fakevendor.default")

    def test_picks_model_specific_driver_on_old_firmware(self):
        driver = resolve_driver(FIXTURE_REGISTRY, vendor="fakevendor", model="Model X", firmware_version="2.0.0")
        self.assertEqual(driver.driver_id, "fakevendor.model_x.default")

    def test_picks_firmware_specific_driver_over_generic_model_driver(self):
        driver = resolve_driver(FIXTURE_REGISTRY, vendor="fakevendor", model="Model X", firmware_version="3.5.0")
        # Both fakevendor.model_x.default and fakevendor.model_x.fw3plus match;
        # fw3plus is listed first in FIXTURE_REGISTRY so it wins per the
        # documented "first match in registry order" tie-break.
        self.assertEqual(driver.driver_id, "fakevendor.model_x.fw3plus")

    def test_explicit_override_wins_regardless_of_model(self):
        driver = resolve_driver(
            FIXTURE_REGISTRY, vendor="fakevendor", model="Model Z",
            driver_override="fakevendor.model_x.fw3plus",
        )
        self.assertEqual(driver.driver_id, "fakevendor.model_x.fw3plus")

    def test_explicit_override_unknown_id_raises_clear_error(self):
        with self.assertRaises(DriverResolutionError) as ctx:
            resolve_driver(FIXTURE_REGISTRY, vendor="fakevendor", driver_override="fakevendor.does_not_exist")
        self.assertIn("fakevendor.does_not_exist", str(ctx.exception))

    def test_unknown_vendor_raises_clear_error(self):
        with self.assertRaises(DriverResolutionError) as ctx:
            resolve_driver(FIXTURE_REGISTRY, vendor="totally_unknown_vendor")
        self.assertIn("totally_unknown_vendor", str(ctx.exception))

    def test_vendor_with_no_default_driver_and_no_match_raises(self):
        # A vendor whose only registered driver is model-constrained, with a
        # device that doesn't match it and has no default to fall back to.
        registry = [_FakeVendorModelXDriver]  # no default registered
        with self.assertRaises(DriverResolutionError):
            resolve_driver(registry, vendor="fakevendor", model="Model Z")


if __name__ == "__main__":
    unittest.main()
