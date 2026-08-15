"""
trapd.py - SNMP trap receiver for NexNOC.

Listens for SNMPv1 and SNMPv2c traps (UDP). SNMPv3 traps are accepted via
snmptrapd's traphandle (see config/snmptrapd.nexnoc.conf) which posts a
JSON line to this process's --notify-socket, or invoke:

    python3 trapd.py --db noc.db --from-snmptrapd

Community strings are used only to match a device; they are never stored
or logged. Bind port 162 requires CAP_NET_BIND_SERVICE (the systemd unit
sets this). For unprivileged dev: --port 1162.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from typing import Optional

from db import Database
from envfile import default_env_path, get_value
from poller import resolve_env, setup_logging

logger = logging.getLogger("nexnoc.trapd")

# SNMPv2-Trap generic OIDs (snmpTraps)
COLD_START = "1.3.6.1.6.3.1.1.5.1"
WARM_START = "1.3.6.1.6.3.1.1.5.2"
LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
LINK_UP = "1.3.6.1.6.3.1.1.5.4"
AUTH_FAIL = "1.3.6.1.6.3.1.1.5.5"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

GENERIC_NAMES = {
    0: COLD_START,
    1: WARM_START,
    2: LINK_DOWN,
    3: LINK_UP,
    4: AUTH_FAIL,
}


class BerError(ValueError):
    pass


def _read_len(data: bytes, i: int) -> tuple[int, int]:
    if i >= len(data):
        raise BerError("truncated length")
    first = data[i]
    i += 1
    if first < 0x80:
        return first, i
    n = first & 0x7F
    if n == 0 or i + n > len(data):
        raise BerError("bad length")
    value = int.from_bytes(data[i:i + n], "big")
    return value, i + n


def _tlv(data: bytes, i: int) -> tuple[int, bytes, int]:
    if i >= len(data):
        raise BerError("truncated tag")
    tag = data[i]
    length, j = _read_len(data, i + 1)
    end = j + length
    if end > len(data):
        raise BerError("truncated value")
    return tag, data[j:end], end


def _decode_int(raw: bytes) -> int:
    if not raw:
        return 0
    return int.from_bytes(raw, "big", signed=True)


def _decode_oid(raw: bytes) -> str:
    if not raw:
        return ""
    first = raw[0]
    parts = [first // 40, first % 40]
    acc = 0
    for b in raw[1:]:
        acc = (acc << 7) | (b & 0x7F)
        if not (b & 0x80):
            parts.append(acc)
            acc = 0
    return ".".join(str(p) for p in parts)


def _decode_ip(raw: bytes) -> str:
    if len(raw) == 4:
        return ".".join(str(b) for b in raw)
    return raw.hex()


def decode_snmp_message(data: bytes) -> dict:
    """Decode SNMPv1 Trap (PDU 0xA4) or SNMPv2c Trap (PDU 0xA7)."""
    tag, body, _ = _tlv(data, 0)
    if tag != 0x30:
        raise BerError(f"not a SEQUENCE (tag {tag:#x})")
    i = 0
    _, ver_raw, i = _tlv(body, i)
    version_n = _decode_int(ver_raw)
    _, comm_raw, i = _tlv(body, i)
    try:
        community = comm_raw.decode("ascii", "replace")
    except Exception:
        community = ""
    pdu_tag, pdu, _ = _tlv(body, i)
    result = {
        "version": {0: "1", 1: "2c"}.get(version_n, str(version_n)),
        "community": community,
        "trap_oid": "",
        "generic_trap": None,
        "varbinds": [],
        "agent_addr": "",
    }
    if pdu_tag == 0xA4:
        j = 0
        _, ent, j = _tlv(pdu, j)
        _, addr, j = _tlv(pdu, j)
        _, gen, j = _tlv(pdu, j)
        _, spec, j = _tlv(pdu, j)
        _, _ts, j = _tlv(pdu, j)
        result["agent_addr"] = _decode_ip(addr)
        result["generic_trap"] = _decode_int(gen)
        spec_n = _decode_int(spec)
        result["trap_oid"] = GENERIC_NAMES.get(result["generic_trap"], _decode_oid(ent))
        if result["generic_trap"] == 6:
            result["trap_oid"] = f"{_decode_oid(ent)}.0.{spec_n}"
        _, vbseq, _ = _tlv(pdu, j)
        result["varbinds"] = _decode_varbinds(vbseq)
    elif pdu_tag == 0xA7:
        j = 0
        _, _rid, j = _tlv(pdu, j)
        _, _es, j = _tlv(pdu, j)
        _, _ei, j = _tlv(pdu, j)
        _, vbseq, _ = _tlv(pdu, j)
        vbs = _decode_varbinds(vbseq)
        result["varbinds"] = vbs
        if len(vbs) >= 2:
            result["trap_oid"] = vbs[1][1]
        if result["trap_oid"] == LINK_DOWN:
            result["generic_trap"] = 2
        elif result["trap_oid"] == LINK_UP:
            result["generic_trap"] = 3
        elif result["trap_oid"] == COLD_START:
            result["generic_trap"] = 0
        elif result["trap_oid"] == WARM_START:
            result["generic_trap"] = 1
    else:
        raise BerError(f"unsupported PDU tag {pdu_tag:#x}")
    return result


def _decode_varbinds(seq: bytes) -> list[tuple[str, str]]:
    out = []
    i = 0
    while i < len(seq):
        tag, item, i = _tlv(seq, i)
        if tag != 0x30:
            continue
        k = 0
        _, oid_raw, k = _tlv(item, k)
        val_tag, val_raw, _ = _tlv(item, k)
        oid = _decode_oid(oid_raw)
        if val_tag in (0x06,):
            value = _decode_oid(val_raw)
        elif val_tag in (0x02, 0x41, 0x43):
            value = str(_decode_int(val_raw))
        elif val_tag == 0x04:
            value = val_raw.decode("utf-8", "replace")
        elif val_tag == 0x40:
            value = _decode_ip(val_raw)
        else:
            value = val_raw.hex()
        out.append((oid, value))
    return out


def encode_snmpv2c_trap(community: str, trap_oid: str,
                        varbinds: Optional[list[tuple[str, str]]] = None) -> bytes:
    """Minimal SNMPv2c trap for tests (sysUpTime + snmpTrapOID)."""

    def lenb(n: int) -> bytes:
        if n < 0x80:
            return bytes([n])
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(raw)]) + raw

    def tlv(tag: int, val: bytes) -> bytes:
        return bytes([tag]) + lenb(len(val)) + val

    def enc_int(n: int) -> bytes:
        if n == 0:
            raw = b"\x00"
        else:
            length = (n.bit_length() + 8) // 8
            raw = n.to_bytes(length, "big", signed=True)
            if raw[0] & 0x80:
                raw = b"\x00" + raw
        return tlv(0x02, raw)

    def enc_oid(oid: str) -> bytes:
        parts = [int(p) for p in oid.split(".") if p]
        raw = bytes([40 * parts[0] + parts[1]])
        for p in parts[2:]:
            stack = [p & 0x7F]
            p >>= 7
            while p:
                stack.append(0x80 | (p & 0x7F))
                p >>= 7
            raw += bytes(reversed(stack))
        return tlv(0x06, raw)

    def enc_str(s: str) -> bytes:
        return tlv(0x04, s.encode("ascii"))

    vbs = [(SYS_UPTIME, enc_int(0)), ("1.3.6.1.6.3.1.1.4.1.0", enc_oid(trap_oid))]
    for oid, val in varbinds or []:
        vbs.append((oid, enc_str(val)))
    vb_body = b"".join(tlv(0x30, enc_oid(oid) + val) for oid, val in vbs)
    pdu = tlv(0xA7, enc_int(1) + enc_int(0) + enc_int(0) + tlv(0x30, vb_body))
    return tlv(0x30, enc_int(1) + enc_str(community) + pdu)


def _community_matches(device, community: str, env_path) -> bool:
    if (device.snmp_version or "2c") == "3":
        return True
    expected = os.environ.get(device.snmp_community_env or "") or get_value(
        env_path, device.snmp_community_env or "",
    )
    if not expected:
        expected = resolve_env(device.snmp_community_env)
    if not expected:
        return True
    return community == expected


def apply_trap(db: Database, source_ip: str, decoded: dict,
               env_path=None) -> Optional[int]:
    env_path = env_path or default_env_path()
    device = db.find_device_by_mgmt_host(source_ip)
    if device is None and decoded.get("agent_addr"):
        device = db.find_device_by_mgmt_host(decoded["agent_addr"])
    matched = False
    if device is not None and device.snmp_trap_enabled:
        if _community_matches(device, decoded.get("community") or "", env_path):
            matched = True
    trap_id = db.add_trap(
        source_ip=source_ip,
        version=decoded.get("version"),
        trap_oid=decoded.get("trap_oid") or "",
        generic_trap=decoded.get("generic_trap"),
        varbinds_json=json.dumps(decoded.get("varbinds") or []),
        device_id=device.id if device and matched else (device.id if device else None),
        matched=matched,
    )
    if not matched or device is None:
        logger.info("Trap from %s oid=%s unmatched", source_ip, decoded.get("trap_oid"))
        return trap_id
    oid = decoded.get("trap_oid") or ""
    if oid == LINK_DOWN or decoded.get("generic_trap") == 2:
        db.set_device_status(device.id, "degraded", error=f"SNMP trap {oid or 'linkDown'}")
        logger.warning("Trap linkDown from %s (%s)", device.name, source_ip)
    elif oid == AUTH_FAIL or decoded.get("generic_trap") == 4:
        db.set_device_status(device.id, "degraded", error="SNMP authenticationFailure trap")
        logger.warning("Trap authFailure from %s (%s)", device.name, source_ip)
    elif oid in (LINK_UP, COLD_START, WARM_START) or decoded.get("generic_trap") in (0, 1, 3):
        if device.status in ("unreachable", "degraded", "unknown"):
            db.set_device_status(device.id, "healthy")
        logger.info("Trap %s from %s (%s)", oid, device.name, source_ip)
    else:
        logger.info("Trap %s from %s (%s)", oid, device.name, source_ip)
    return trap_id


def handle_datagram(db: Database, data: bytes, source_ip: str) -> None:
    try:
        decoded = decode_snmp_message(data)
    except BerError as exc:
        logger.debug("Ignoring non-SNMP UDP from %s: %s", source_ip, exc)
        return
    # Community is used only to match a device; add_trap never stores it.
    apply_trap(db, source_ip, decoded)


def handle_snmptrapd_stdin(db: Database) -> None:
    """snmptrapd traphandle: first line hostname, rest OID value pairs.

    Typical:
        hostname
        ip
        uptime oid
        oid value
    """
    lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    if len(lines) < 2:
        return
    source_ip = lines[1]
    trap_oid = ""
    varbinds = []
    if len(lines) >= 3:
        parts = lines[2].split()
        trap_oid = parts[-1] if parts else ""
    for line in lines[3:]:
        bits = line.split(None, 1)
        if len(bits) == 2:
            varbinds.append((bits[0], bits[1]))
    apply_trap(db, source_ip, {
        "version": "3",
        "community": "",
        "trap_oid": trap_oid,
        "generic_trap": None,
        "varbinds": varbinds,
        "agent_addr": source_ip,
    })


def serve(db: Database, host: str, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    logger.info("NexNOC trap listener on udp://%s:%d", host, port)
    while True:
        data, addr = sock.recvfrom(65535)
        try:
            handle_datagram(db, data, addr[0])
        except Exception:  # noqa: BLE001
            logger.exception("trap handler failed from %s", addr[0])


def main() -> None:
    parser = argparse.ArgumentParser(description="NexNOC SNMP trap receiver")
    parser.add_argument("--db", default="noc.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("NEXNOC_TRAP_PORT", "162")))
    parser.add_argument("--from-snmptrapd", action="store_true",
                        help="Read one snmptrapd traphandle payload from stdin and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    db = Database(args.db)
    db.initialize()
    if args.from_snmptrapd:
        handle_snmptrapd_stdin(db)
        return
    serve(db, args.host, args.port)


if __name__ == "__main__":
    main()
