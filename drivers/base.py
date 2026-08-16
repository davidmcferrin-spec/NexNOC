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
from typing import Optional, Sequence


@dataclass(frozen=True)
class ConnectorSpec:
    """One physical BNC/SDI (or net/mgmt) connector the chassis ships with.

    capability:
      input      — fixed SDI in; direction cannot change
      output     — fixed SDI out
      assignable — operator (or a path) sets input / output / unused
    kind: sdi | net | mgmt  (physical family; SDI direction is capability)
    """
    name: str
    capability: str
    kind: str = "sdi"


def sdi_layout(count: int, capability: str = "assignable",
               prefix: str = "BNC") -> tuple[ConnectorSpec, ...]:
    return tuple(
        ConnectorSpec(name=f"{prefix} {i}", capability=capability, kind="sdi")
        for i in range(1, count + 1)
    )


@dataclass
class InventoryItem:
    """One card/channel/stream discovered during collect()."""
    slot: str
    module_type: str = ""
    firmware_version: str = ""
    serial: str = ""
    status: str = "unknown"


@dataclass
class DiscoveredPort:
    """Port seen during collect(). Poller upserts by name or slot; never deletes."""
    name: str
    kind: str = "other"
    slot: str = ""          # stable rematch key (Appear config_id, or net:slot:connector)
    capability: str = ""
    direction: str = ""
    status: str = "unknown"  # unknown | up | degraded | down


@dataclass
class DiscoveredFlow:
    """Draft source-side flow. Poller creates if missing; later polls update status only."""
    label: str
    dest_label: str
    port_slot: str = ""
    port_name: str = ""
    signal_label: str = ""
    status: str = "unknown"
    direction: str = ""


@dataclass
class CollectResult:
    """Optional richer poll result. ping() stays the cheap reachability check;
    collect() fills modules / derived device_status when a driver has confirmed
    endpoints (Haivision /apis/*, Appear Prometheus). ports/flows are optional
    upserts — dest city/site/device stay empty until an operator places them."""
    device_status: str = "healthy"   # healthy | degraded
    firmware_version: Optional[str] = None
    error: Optional[str] = None
    detail: Optional[str] = None     # extra poll_log lines (Appear Prometheus summary)
    modules: list[InventoryItem] = field(default_factory=list)
    ports: list[DiscoveredPort] = field(default_factory=list)
    flows: list[DiscoveredFlow] = field(default_factory=list)


# SNMPv2-Trap generic OIDs (snmpTraps). Shared by trapd + Driver.interpret_trap.
COLD_START = "1.3.6.1.6.3.1.1.5.1"
WARM_START = "1.3.6.1.6.3.1.1.5.2"
LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
LINK_UP = "1.3.6.1.6.3.1.1.5.4"
AUTH_FAIL = "1.3.6.1.6.3.1.1.5.5"


@dataclass
class TrapResult:
    """How a driver wants a received trap applied to device status.

    holds=True means a later healthy API/SNMP poll must not clear this
    (linkDown stays degraded until linkUp/coldStart/warmStart).
    device_status None = store the trap, do not change status.
    """
    device_status: Optional[str] = None  # healthy | degraded | None
    error: Optional[str] = None
    holds: bool = False


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
    `notes` is operator-facing documentation (catalog + inventory UI),
    not a matching field. See docs/DRIVERS.md.
    """

    # --- identity & matching criteria (class-level, override in subclasses) ---
    driver_id: str              # unique, e.g. "appear.x_platform.default"
    vendor: str                 # must match a value in db.VALID_VENDORS
    supported_models: Optional[list[str]] = None   # substrings matched case-insensitively
                                                     # against Device.model; None = any model
    firmware_min: Optional[str] = None              # inclusive (>=); None = no lower bound
    firmware_max: Optional[str] = None              # inclusive (<=); None = no upper bound
    notes: Optional[str] = None                     # operator-facing: what this driver covers
    connectors: Sequence[ConnectorSpec] = ()        # chassis BNC/SDI template stamped on create

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

    def snmp_ping(self, target) -> bool:
        """MIB-2 sysDescr reachability. Override only if a vendor needs a
        different cheap SNMP check. Must not raise."""
        from drivers.snmp_util import snmp_ping as _snmp_ping
        try:
            return bool(_snmp_ping(target.host, target=target))
        except Exception:  # noqa: BLE001
            return False

    def snmp_collect(self, target) -> Optional[CollectResult]:
        """Optional SNMP inventory/health. Default: sysDescr GET only.

        Do not invent vendor enterprise OIDs here — override on a driver
        after the device's own MIB is confirmed."""
        from drivers.snmp_util import SYS_DESCR_OID, SnmpError, snmp_get
        try:
            descr = snmp_get(target.host, target=target, oid=SYS_DESCR_OID)
        except SnmpError as exc:
            return CollectResult(device_status="degraded", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return CollectResult(device_status="degraded", error=str(exc))
        preview = (descr or "").strip().replace("\n", " ")[:80]
        return CollectResult(
            device_status="healthy",
            error=None,
            modules=[InventoryItem(
                slot="snmp", module_type="sysDescr",
                serial=preview, status="healthy",
            )],
        )

    @classmethod
    def interpret_trap(cls, trap_oid: str = "", varbinds=None,
                       generic_trap: Optional[int] = None) -> "TrapResult":
        """Map a trap onto status. Default: generic MIB-2 snmpTraps.

        Override to handle a vendor's enterprise OIDs once those are
        confirmed (do not invent them). Unknown OIDs are stored only."""
        del varbinds  # reserved for vendor overrides
        oid = trap_oid or ""
        gen = generic_trap
        if oid == LINK_DOWN or gen == 2:
            return TrapResult(
                device_status="degraded",
                error=f"SNMP trap {oid or 'linkDown'}",
                holds=True,
            )
        if oid == AUTH_FAIL or gen == 4:
            return TrapResult(
                device_status="degraded",
                error="SNMP authenticationFailure trap",
                holds=True,
            )
        if oid in (LINK_UP, COLD_START, WARM_START) or gen in (0, 1, 3):
            return TrapResult(device_status="healthy", holds=False)
        return TrapResult()


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


def driver_catalog(driver_registry: list[type]) -> list[dict]:
    """Public inventory of drivers: matching rules + BNC template."""
    out = []
    for cls in driver_registry:
        out.append({
            "driver_id": cls.driver_id,
            "vendor": cls.vendor,
            "supported_models": list(cls.supported_models) if cls.supported_models else None,
            "firmware_min": cls.firmware_min,
            "firmware_max": cls.firmware_max,
            "notes": cls.notes,
            "is_default": cls.is_default_for_vendor(),
            "connectors": [
                {"name": c.name, "capability": c.capability, "kind": c.kind}
                for c in (cls.connectors or ())
            ],
        })
    return out


def kind_for_connector(capability: str, direction: str = "", family: str = "sdi") -> str:
    """Map capability/direction onto the ports.kind CHECK values."""
    if family == "net":
        return "net"
    if family == "mgmt":
        return "mgmt"
    if capability == "input" or direction == "input":
        return "sdi_in"
    if capability == "output" or direction == "output":
        return "sdi_out"
    return "other"
