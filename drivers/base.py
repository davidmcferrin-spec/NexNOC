"""
base.py - The Driver contract every hardware driver implements.

Key idea: a "vendor" is a rough family (Appear, Haivision, Net Insight), but
the thing that actually knows how to talk to a specific box is a **driver**,
matched on vendor + (optionally) model + firmware version range. Most
vendors will start with exactly one driver (the "default" for that vendor -
supported_models=None, no firmware range, matches anything). When a new
model or a firmware upgrade changes the API/SNMP surface enough to need
different handling, you add a new driver class narrower than the default
and register it - existing devices keep using the default until you either
update their `model`/`firmware_version` to match the new driver, or set an
explicit `driver_override` on that device's row.

This is the layer db.py and poller.py depend on - neither should ever
hardcode "if vendor == 'appear'" logic; they call resolve_driver() and use
whatever comes back.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InventoryItem:
    """One card/channel/stream discovered during collect()."""
    slot: str
    module_type: str = ""
    firmware_version: str = ""
    serial: str = ""
    status: str = "unknown"


@dataclass
class CollectResult:
    """Optional richer poll result. ping() stays the cheap reachability check;
    collect() fills modules / derived device_status when a driver has confirmed
    endpoints (Haivision /apis/*, Appear Prometheus)."""
    device_status: str = "healthy"   # healthy | degraded
    firmware_version: Optional[str] = None
    error: Optional[str] = None
    modules: list[InventoryItem] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    path: str
    ok: bool
    status_code: Optional[int] = None
    content_type: Optional[str] = None
    error: Optional[str] = None
    body_preview: Optional[str] = None


class DriverError(RuntimeError):
    """Base error for anything that goes wrong talking to a device."""


class DriverAuthError(DriverError):
    """Credentials wrong or not authorized."""


class DriverUnreachableError(DriverError):
    """Device/NMS not reachable at all (connection refused/timeout/DNS failure)."""


class DriverResolutionError(RuntimeError):
    """Raised by resolve_driver() when no driver matches, or an explicit
    driver_override doesn't exist. Distinct from DriverError (which is about
    talking to a device) - this is about picking which driver to use."""


def _parse_version(v: str) -> tuple:
    """Best-effort dotted-version parser for firmware range comparisons:
    '2.4.1' -> (2, 4, 1). Non-numeric segments are dropped rather than
    raising - firmware version strings are inconsistent across vendors
    (e.g. 'v2.4.1-rc3'), and a rough comparison is good enough for driver
    selection; exact semantics aren't load-bearing here."""
    parts = []
    for segment in v.replace("v", "").replace("V", "").split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts) if parts else (0,)


class Driver(ABC):
    """One instance per poll = one device. Constructed fresh each poll cycle
    by poller.py from the resolved driver class + Device row + resolved
    credentials - drivers are intentionally stateless/cheap to construct,
    not long-lived connections.

    Class-level attributes describe WHAT the driver handles (used for
    matching); instance methods describe HOW it talks to a device.
    """

    # --- identity & matching criteria (class-level, override in subclasses) ---
    driver_id: str              # unique, e.g. "appear.x_platform.default"
    vendor: str                 # must match a value in db.VALID_VENDORS
    supported_models: Optional[list[str]] = None   # substrings matched case-insensitively
                                                     # against Device.model; None = any model
    firmware_min: Optional[str] = None              # inclusive; None = no lower bound
    firmware_max: Optional[str] = None              # inclusive; None = no upper bound

    @classmethod
    def is_default_for_vendor(cls) -> bool:
        """A driver with no model/firmware constraints - the fallback used
        when nothing more specific matches. Every vendor should have exactly
        one of these registered; resolve_driver() will complain if not."""
        return cls.supported_models is None and cls.firmware_min is None and cls.firmware_max is None

    @classmethod
    def applies_to(cls, model: Optional[str], firmware_version: Optional[str]) -> bool:
        """Whether this driver is a valid (not necessarily best) match for
        a device with the given model/firmware. See resolve_driver() for how
        specificity is ranked when multiple drivers match."""
        if cls.supported_models is not None:
            if not model or not any(pat.lower() in model.lower() for pat in cls.supported_models):
                return False
        if cls.firmware_min is not None or cls.firmware_max is not None:
            if not firmware_version:
                return False
            fw = _parse_version(firmware_version)
            if cls.firmware_min is not None and fw < _parse_version(cls.firmware_min):
                return False
            if cls.firmware_max is not None and fw > _parse_version(cls.firmware_max):
                return False
        return True

    # --- instance behavior ---
    @abstractmethod
    def ping(self) -> bool:
        """Cheapest possible reachability check for this device. Must not
        raise - catch your own transport errors and return False."""
        raise NotImplementedError

    def discover(self, candidates: Optional[list[str]] = None) -> list[DiscoveryResult]:
        """Optional: probe for real API paths/OIDs. Default: unsupported.
        Override where a probe-based discovery approach makes sense."""
        raise NotImplementedError(f"{self.driver_id} driver does not implement discover()")

    def collect(self) -> Optional[CollectResult]:
        """Optional inventory/health snapshot. Default: nothing to collect.
        Must not be required for ping() to succeed."""
        return None


def resolve_driver(
    driver_registry: list[type],
    vendor: str,
    model: Optional[str] = None,
    firmware_version: Optional[str] = None,
    driver_override: Optional[str] = None,
) -> type:
    """Pick the driver class for a device.

    Resolution order:
      1. driver_override, if set - an explicit pin, always wins, raises if
         the named driver_id isn't in the registry (typo protection).
      2. The most specific registered driver for this vendor whose
         applies_to(model, firmware_version) is True - i.e. a driver with
         supported_models/firmware constraints, not the vendor's default.
      3. The vendor's default driver (is_default_for_vendor() == True).
      4. DriverResolutionError if none of the above produced a match.

    When multiple non-default drivers match (e.g. two firmware ranges that
    happen to overlap on a bad config), the first match in registry order
    wins - register narrower/newer drivers earlier if you need them to take
    priority. This is a deliberate simple rule, not smart conflict
    resolution; keep firmware ranges non-overlapping in practice.
    """
    if driver_override:
        for d in driver_registry:
            if d.driver_id == driver_override:
                return d
        known = sorted(d.driver_id for d in driver_registry)
        raise DriverResolutionError(
            f"driver_override {driver_override!r} not found in registry. Known driver ids: {known}"
        )

    vendor_drivers = [d for d in driver_registry if d.vendor == vendor]
    if not vendor_drivers:
        known_vendors = sorted({d.vendor for d in driver_registry})
        raise DriverResolutionError(
            f"No drivers registered for vendor {vendor!r}. Known vendors: {known_vendors}"
        )

    specific_matches = [
        d for d in vendor_drivers
        if not d.is_default_for_vendor() and d.applies_to(model, firmware_version)
    ]
    if specific_matches:
        return specific_matches[0]

    defaults = [d for d in vendor_drivers if d.is_default_for_vendor()]
    if defaults:
        return defaults[0]

    raise DriverResolutionError(
        f"No driver matches vendor={vendor!r} model={model!r} firmware={firmware_version!r}, "
        f"and no default driver is registered for vendor {vendor!r}. "
        f"Registered drivers for this vendor: {[d.driver_id for d in vendor_drivers]}"
    )
