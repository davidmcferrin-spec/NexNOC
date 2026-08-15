"""
drivers/net_insight.py - Net Insight Nimbra (MSR, Edge, 600/1000/400/680
series nodes).

This is the vendor's DEFAULT driver. STATUS: structurally different from
Appear/Haivision - this is not "find the right HTTP path," it's "this
vendor's per-device management is SNMP, full stop." Per Net Insight's own
product docs:

  - Individual Nimbra nodes are managed via CLI, web GUI, or SNMP v1/v2c/v3
    directly (device-local). There is no confirmed per-device JSON/REST API.
  - A northbound REST API exists, but it belongs to "Nimbra Vision" - Net
    Insight's separate central NMS/orchestration product - not to each node.
    If you run Nimbra Vision, device status/topology/provisioning can go
    through Vision's REST API instead of polling each node directly; if you
    don't run Vision, SNMP-per-node is the only confirmed path.
  - Nimbra devices serve their own downloadable enterprise MIB from the
    device's web GUI (Control Networks -> SNMP -> MIB specifications, per
    the Nimbra Element Manager manual) - pull that from a real device to
    get real OIDs rather than guessing. Standard MIB-2 OIDs (sysDescr etc.)
    work universally for basic reachability in the meantime.

This driver therefore implements ping() via SNMP by default (access_mode
'direct_snmp' in the devices table) and does NOT implement discover() for
direct mode - there's no HTTP path-probing to do. If you set access_mode to
'via_nms' and point nms_host/nms_port at a Nimbra Vision server, that needs
its own driver (a Vision-specific one, not this class) once you have
Vision's actual REST reference (not public - same "ask the vendor / capture
from the Vision web UI" situation as Appear). This class raises
NotImplementedError for via_nms rather than pretending to support it.
"""

from __future__ import annotations

from typing import Optional

from drivers.base import DiscoveryResult, Driver
from drivers.snmp_util import SnmpTarget, snmp_ping


class NetInsightNimbraDriver(Driver):
    driver_id = "net_insight.nimbra.default"
    vendor = "net_insight"
    notes = (
        "Default Nimbra per-node SNMP driver. via_nms (Nimbra Vision REST) is "
        "not implemented. Pull the device enterprise MIB before adding vendor OIDs."
    )
    # Default for the vendor - no model/firmware constraints.

    def __init__(self, host: str, snmp_community: Optional[str] = None,
                 snmp_port: int = 161, snmp_timeout: float = 3.0,
                 access_mode: str = "direct_snmp",
                 snmp_target: Optional[SnmpTarget] = None):
        self.host = host
        self.snmp_community = snmp_community
        self.snmp_port = snmp_port
        self.snmp_timeout = snmp_timeout
        self.access_mode = access_mode
        self.snmp_target = snmp_target

    def ping(self) -> bool:
        if self.access_mode != "direct_snmp":
            raise NotImplementedError(
                "NetInsightNimbraDriver access_mode 'via_nms' has no client implementation yet - "
                "Nimbra Vision's northbound REST API reference isn't confirmed. See module docstring."
            )
        if self.snmp_target is not None:
            return snmp_ping(self.snmp_target.host, target=self.snmp_target)
        if not self.snmp_community:
            return False
        return snmp_ping(self.host, self.snmp_community, port=self.snmp_port, timeout=self.snmp_timeout)

    def discover(self, candidates: Optional[list[str]] = None) -> list[DiscoveryResult]:
        # No HTTP-path discovery for direct SNMP devices - see module docstring
        # for how to get real OIDs (pull the device's own MIB from its web GUI).
        raise NotImplementedError(
            "Net Insight direct-SNMP devices have no HTTP API to probe. "
            "Pull the device's enterprise MIB from its own web GUI "
            "(Control Networks -> SNMP -> MIB specifications) for real OIDs, "
            "or use snmp_walk() from drivers/snmp_util.py to explore the MIB tree directly."
        )
