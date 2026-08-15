"""
snmp_util.py - SNMP access shared across vendor adapters. Shells out to the
system net-snmp tools (snmpget/snmpwalk) rather than a pip SNMP library, to
stay stdlib-only (subprocess is stdlib) per NexNOC's dev-stack constraints.

Install on the poller host: `sudo apt install snmp`
"""

from __future__ import annotations

import subprocess


class SnmpError(RuntimeError):
    pass


def snmp_get(host: str, community: str, oid: str, port: int = 161, timeout: float = 3.0,
             version: str = "2c") -> str:
    """Run snmpget for a single OID."""
    cmd = [
        "snmpget", f"-v{version}", "-c", community, "-t", str(timeout), "-r", "1",
        "-O", "qv", f"{host}:{port}", oid,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
    except FileNotFoundError as exc:
        raise SnmpError("snmpget not found - install net-snmp tools: apt install snmp") from exc
    except subprocess.TimeoutExpired as exc:
        raise SnmpError(f"snmpget timed out for {host} OID {oid}") from exc
    if result.returncode != 0:
        raise SnmpError(f"snmpget failed for {host} OID {oid}: {result.stderr.strip()}")
    return result.stdout.strip()


def snmp_walk(host: str, community: str, oid: str, port: int = 161, timeout: float = 5.0,
              version: str = "2c") -> list[str]:
    """Run snmpwalk starting at OID, return each line of output."""
    cmd = ["snmpwalk", f"-v{version}", "-c", community, "-t", str(timeout), f"{host}:{port}", oid]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except FileNotFoundError as exc:
        raise SnmpError("snmpwalk not found - install net-snmp tools: apt install snmp") from exc
    except subprocess.TimeoutExpired as exc:
        raise SnmpError(f"snmpwalk timed out for {host} OID {oid}") from exc
    if result.returncode != 0:
        raise SnmpError(f"snmpwalk failed for {host} OID {oid}: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def snmp_ping(host: str, community: str, port: int = 161, timeout: float = 3.0) -> bool:
    """Reachability check via the standard MIB-2 sysDescr OID (1.3.6.1.2.1.1.1.0) -
    every SNMP-speaking device answers this regardless of vendor, so it's a
    safe universal probe. Returns False on any SnmpError rather than raising,
    matching Driver.ping()'s contract."""
    try:
        snmp_get(host, community, "1.3.6.1.2.1.1.1.0", port=port, timeout=timeout)
        return True
    except SnmpError:
        return False
