"""MIB-2 SNMP reachability for boxes that have no vendor API driver yet."""
from __future__ import annotations

from typing import Optional

from drivers.base import Driver
from drivers.snmp_util import SnmpTarget, snmp_ping


class GenericSnmpDriver(Driver):
    driver_id = "generic.snmp.default"
    vendor = "generic_snmp"
    notes = (
        "MIB-2 sysDescr reachability for boxes with no vendor API driver yet. "
        "Override with a real vendor driver once endpoints or OIDs are confirmed."
    )

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
        if self.snmp_target is not None:
            return snmp_ping(self.snmp_target.host, target=self.snmp_target)
        if not self.snmp_community:
            return False
        return snmp_ping(self.host, self.snmp_community, port=self.snmp_port,
                         timeout=self.snmp_timeout)
