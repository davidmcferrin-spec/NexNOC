"""
snmp_util.py - SNMP GET/WALK via system net-snmp tools (stdlib subprocess).

Supports SNMPv1, v2c, and v3 (USM). Credential *values* are passed to
snmpget as argv and must never be logged. Install: `sudo apt install snmp`
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional

SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"

VALID_SNMP_VERSIONS = {"1", "2c", "3"}
VALID_SEC_LEVELS = {"noAuthNoPriv", "authNoPriv", "authPriv"}


class SnmpError(RuntimeError):
    pass


@dataclass
class SnmpTarget:
    host: str
    port: int = 161
    version: str = "2c"
    community: Optional[str] = None
    timeout: float = 3.0
    v3_user: Optional[str] = None
    v3_sec_level: str = "authPriv"
    v3_auth_proto: str = "SHA"
    v3_auth_pass: Optional[str] = None
    v3_priv_proto: str = "AES"
    v3_priv_pass: Optional[str] = None

    def configured(self) -> bool:
        if not (self.host or "").strip():
            return False
        if self.version not in VALID_SNMP_VERSIONS:
            return False
        if self.version == "3":
            return bool(self.v3_user)
        return bool(self.community)


def auth_args(target: SnmpTarget) -> list[str]:
    """net-snmp authentication flags. Caller must not log the returned list."""
    if target.version == "3":
        if not target.v3_user:
            raise SnmpError("SNMPv3 user is not set")
        level = target.v3_sec_level if target.v3_sec_level in VALID_SEC_LEVELS else "authPriv"
        args = ["-v3", "-u", target.v3_user, "-l", level]
        if level in ("authNoPriv", "authPriv"):
            args += ["-a", target.v3_auth_proto or "SHA", "-A", target.v3_auth_pass or ""]
        if level == "authPriv":
            args += ["-x", target.v3_priv_proto or "AES", "-X", target.v3_priv_pass or ""]
        return args
    ver = "1" if target.version == "1" else "2c"
    if not target.community:
        raise SnmpError(f"SNMP{ver} community is not set")
    return [f"-v{ver}", "-c", target.community]


def _run(tool: str, target: SnmpTarget, extra: list[str], hang: float) -> str:
    cmd = [
        tool, *auth_args(target),
        "-t", str(target.timeout), "-r", "1",
        "-O", "qv",
        f"{target.host}:{target.port}",
        *extra,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=hang)
    except FileNotFoundError as exc:
        raise SnmpError(f"{tool} not found - install net-snmp tools: apt install snmp") from exc
    except subprocess.TimeoutExpired as exc:
        raise SnmpError(f"{tool} timed out for {target.host}") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "failed").strip()
        raise SnmpError(f"{tool} failed for {target.host}: {err}")
    return result.stdout.strip()


def snmp_get(host: str, community: Optional[str] = None, oid: str = SYS_DESCR_OID,
             port: int = 161, timeout: float = 3.0, version: str = "2c",
             target: Optional[SnmpTarget] = None) -> str:
    """Run snmpget for a single OID. Legacy (host, community, oid) still works."""
    tgt = target or SnmpTarget(
        host=host, community=community, port=port, timeout=timeout, version=version,
    )
    return _run("snmpget", tgt, [oid], tgt.timeout + 2)


def snmp_walk(host: str, community: Optional[str] = None, oid: str = "1.3.6.1.2.1.1",
              port: int = 161, timeout: float = 5.0, version: str = "2c",
              target: Optional[SnmpTarget] = None) -> list[str]:
    tgt = target or SnmpTarget(
        host=host, community=community, port=port, timeout=timeout, version=version,
    )
    text = _run("snmpwalk", tgt, [oid], tgt.timeout + 5)
    return [line for line in text.splitlines() if line.strip()]


def snmp_ping(host: str, community: Optional[str] = None, port: int = 161,
              timeout: float = 3.0, version: str = "2c",
              target: Optional[SnmpTarget] = None) -> bool:
    """MIB-2 sysDescr reachability. False on any SnmpError (ping contract)."""
    try:
        snmp_get(host, community, SYS_DESCR_OID, port=port, timeout=timeout,
                 version=version, target=target)
        return True
    except SnmpError:
        return False
