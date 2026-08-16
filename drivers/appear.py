"""
drivers/appear.py - Appear X Platform (X10/X20/X5/XM/XC5000/XC5100).

Confirmed from a live DC X20 (Prometheus text, not a public REST manual):

  Scrape URLs (confirmed):
    /prometheus/system/metrics
    /prometheus/product/metrics
    /prometheus/ipgateway/metrics
    /prometheus/alarms/metrics

  Metric names (confirmed on those scrapes):
    memory_usage_ratio, cpu_usage_ratio, storage_usage_ratio
    system_uptime_seconds, application_uptime_seconds
    power_consumption_watts, ambient_temperature_celsius, fan_speed_rpm
    total_alarms{severity,slot,config_id,config_label,connector,...}
    apr_x_sdi_lock_status, apr_x_sdi_lock_loss_count, apr_x_sdi_bitrate
    apr_x_sdi_video_mode, apr_x_sdi_edh_error_count, apr_x_pid_ts_bytes
    port_rx_rate / port_tx_rate / port_rx_bits / port_tx_bits
    backplane_rx_rate / backplane_tx_rate

ping() GETs only /prometheus/system/metrics (cheap reachability).
collect() GETs all four scrapes and interprets the confirmed samples.
JSON/HTTP sub-APIs (MMI, IpGateway REST) remain unconfirmed — do not invent them.
"""

from __future__ import annotations

from typing import Optional

from drivers.base import (
    CollectResult,
    DiscoveredFlow,
    DiscoveredPort,
    DiscoveryResult,
    Driver,
    InventoryItem,
    sdi_layout,
)
from drivers.http_util import DEFAULT_TIMEOUT_SECONDS, JsonHttpClient
from drivers.prometheus_util import (
    PromSample,
    first_value,
    looks_like_prometheus,
    parse_prometheus,
)

# Confirmed scrape paths on the DC X20. Each family is a separate document;
# collect() fetches all four and merges. ping() uses only the first.
PROMETHEUS_PATHS = [
    "/prometheus/system/metrics",
    "/prometheus/product/metrics",
    "/prometheus/ipgateway/metrics",
    "/prometheus/alarms/metrics",
]
PING_PATH = PROMETHEUS_PATHS[0]
DISCOVERY_CANDIDATES = list(PROMETHEUS_PATHS)

# From apr_x_sdi_video_mode HELP on the live DC X20 product scrape.
SDI_VIDEO_MODES = {
    0: "off",
    1: "480i30", 2: "480i29.97", 3: "480p60", 4: "480p59.94",
    5: "576i25", 6: "576p50",
    7: "720p24", 8: "720p23.98", 9: "720p25", 10: "720p30",
    11: "720p29.97", 12: "720p50", 13: "720p60", 14: "720p59.94",
    15: "1080p24", 16: "1080p23.98", 17: "1080p25", 18: "1080p30",
    19: "1080p29.97", 20: "1080i25", 21: "1080i30", 22: "1080i29.97",
    23: "1080p60", 24: "1080p59.94",
    25: "2160p24", 26: "2160p23.98", 27: "2160p25", 28: "2160p30",
    29: "2160p29.97", 30: "2160p50", 31: "2160p60", 32: "2160p59.94",
}


def _samples_named(samples: list[PromSample], name: str) -> list[PromSample]:
    return [s for s in samples if s.name == name]


def _video_mode(value: float) -> str:
    return SDI_VIDEO_MODES.get(int(value), f"mode {int(value)}")


def _short_service(label: str, config_id: str) -> str:
    text = (label or "").replace("Service: ", "").strip()
    if "Enc." in text:
        tail = text.split("Enc.", 1)[1]
        num = tail.split()[0].strip("()")
        return f"Enc.{num}"
    if text:
        return text.split("(")[0].strip()[:28] or (config_id[:8] or "sdi")
    return config_id[:8] or "sdi"


def _service_names(label: str, config_id: str) -> tuple[str, str]:
    """(port/flow name, leftover dest_label). Does not parse city names."""
    text = (label or "").replace("Service: ", "").strip()
    dest = ""
    open_paren = text.rfind("(")
    close_paren = text.rfind(")")
    if open_paren != -1 and close_paren > open_paren:
        dest = text[open_paren + 1:close_paren].strip()
    name = text or (config_id[:8] if config_id else "SDI")
    return name, dest or name


def _prom_io(labels: dict) -> str:
    direction = (labels.get("direction") or "").strip()
    return direction if direction in ("input", "output") else ""


def _fmt_bps(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}Mb/s"
    if value >= 1000:
        return f"{value / 1000:.1f}kb/s"
    return f"{int(value)}b/s"


def _fmt_ratio(value: float) -> str:
    return f"{int(round(value * 100))}%"


def interpret_appear_metrics(samples: list[PromSample]) -> CollectResult:
    """Turn confirmed X20 Prometheus samples into device status + modules."""
    alarms = [s for s in _samples_named(samples, "total_alarms") if s.value > 0]
    critical = [s for s in alarms if s.labels.get("severity") == "critical"]
    major = [s for s in alarms if s.labels.get("severity") == "major"]
    warning = [s for s in alarms if s.labels.get("severity") == "warning"]
    alarm_slots: dict[str, str] = {}
    for s in critical:
        slot = s.labels.get("slot")
        if slot:
            alarm_slots[slot] = "down"
    for s in major:
        slot = s.labels.get("slot")
        if slot and alarm_slots.get(slot) != "down":
            alarm_slots[slot] = "down"

    locks = _samples_named(samples, "apr_x_sdi_lock_status")
    bitrates = {
        (s.labels.get("config_id") or s.labels.get("config_label") or ""): s.value
        for s in _samples_named(samples, "apr_x_sdi_bitrate")
    }
    modes = {
        (s.labels.get("config_id") or s.labels.get("config_label") or ""): s.value
        for s in _samples_named(samples, "apr_x_sdi_video_mode")
    }
    unlocked = [s for s in locks if s.value == 0]

    modules: dict[str, InventoryItem] = {}
    discovered_ports: dict[str, DiscoveredPort] = {}
    discovered_flows: list[DiscoveredFlow] = []

    watts = first_value(samples, "power_consumption_watts")
    temp = first_value(samples, "ambient_temperature_celsius")
    fans = _samples_named(samples, "fan_speed_rpm")
    stopped_fans = [s for s in fans if s.value <= 0]
    if watts is not None or temp is not None or fans:
        fan_txt = ""
        if fans:
            speeds = [int(s.value) for s in fans]
            fan_txt = f" · fans {min(speeds)}-{max(speeds)} rpm"
        chassis_bits = []
        if temp is not None:
            chassis_bits.append(f"{int(temp)}C")
        if watts is not None:
            chassis_bits.append(f"{int(watts)}W")
        modules["chassis"] = InventoryItem(
            slot="chassis",
            module_type="".join(chassis_bits) + fan_txt,
            status="down" if stopped_fans else "healthy",
        )

    for s in _samples_named(samples, "memory_usage_ratio"):
        slot = s.labels.get("slot")
        if not slot:
            continue
        cpu = next((c.value for c in _samples_named(samples, "cpu_usage_ratio")
                    if c.labels.get("slot") == slot), None)
        parts = [f"mem {_fmt_ratio(s.value)}"]
        if cpu is not None:
            parts.append(f"cpu {_fmt_ratio(cpu)}")
        status = alarm_slots.get(slot, "unknown")
        modules[slot] = InventoryItem(
            slot=slot, module_type=" · ".join(parts), status=status,
        )

    for s in locks:
        slot = s.labels.get("slot") or "?"
        cid = s.labels.get("config_id") or ""
        label = s.labels.get("config_label") or ""
        key = f"{slot}/{_short_service(label, cid)}"
        locked = s.value != 0
        rate = bitrates.get(cid, bitrates.get(label))
        mode = modes.get(cid, modes.get(label))
        bits = [label.replace("Service: ", "") or f"SDI {slot}"]
        if mode is not None:
            bits.append(_video_mode(mode))
        if rate is not None:
            bits.append(f"{int(rate)} Mb/s" if rate else "0 Mb/s")
        modules[key] = InventoryItem(
            slot=key,
            module_type=" · ".join(bits),
            status="healthy" if locked else "down",
        )
        card = modules.get(slot)
        if card is not None:
            if not locked:
                card.status = "down"
            elif card.status == "unknown":
                card.status = "healthy"
        elif slot not in modules:
            modules[slot] = InventoryItem(
                slot=slot,
                module_type=label or "sdi",
                status="healthy" if locked else "down",
            )
        name, dest = _service_names(label, cid)
        port_slot = cid or f"sdi:{slot}:{name}"
        io = _prom_io(s.labels)
        port_status = "up" if locked else "down"
        discovered_ports[port_slot] = DiscoveredPort(
            name=name,
            kind="sdi_out" if io == "output" else "sdi_in",
            slot=port_slot,
            capability=io,
            direction=io,
            status=port_status,
        )
        discovered_flows.append(DiscoveredFlow(
            label=name,
            dest_label=dest,
            port_slot=port_slot,
            port_name=name,
            signal_label=dest,
            status=port_status,
        ))

    ports = {}
    for s in _samples_named(samples, "port_rx_rate") + _samples_named(samples, "port_tx_rate"):
        slot = s.labels.get("slot") or "?"
        conn = s.labels.get("connector") or "?"
        ports.setdefault((slot, conn), {
            "label": s.labels.get("config_label") or conn,
            "rx": 0.0,
            "tx": 0.0,
        })
        if s.name == "port_rx_rate":
            ports[(slot, conn)]["rx"] = s.value
        else:
            ports[(slot, conn)]["tx"] = s.value
    alarm_ports = {
        (s.labels.get("slot"), s.labels.get("connector"))
        for s in critical + major
        if s.labels.get("connector")
    }
    for (slot, conn), info in ports.items():
        key = f"{slot}/{conn}"
        alarmed = (slot, conn) in alarm_ports
        live = info["rx"] > 0 or info["tx"] > 0
        status = "down" if alarmed else ("healthy" if live else "unknown")
        modules[key] = InventoryItem(
            slot=key,
            module_type=f"{info['label']} · rx {_fmt_bps(info['rx'])} · tx {_fmt_bps(info['tx'])}",
            status=status,
        )
        card = modules.get(slot)
        if card is not None and alarmed:
            card.status = "down"
        label = info["label"] or conn
        port_status = "down" if alarmed else ("up" if live else "unknown")
        kind = "mgmt" if "management" in label.lower() else "net"
        discovered_ports[f"net:{slot}:{conn}"] = DiscoveredPort(
            name=key,
            kind=kind,
            slot=f"net:{slot}:{conn}",
            status=port_status,
        )

    parts = []
    if critical:
        parts.append(f"{len(critical)} critical")
    if major:
        parts.append(f"{len(major)} major")
    if warning and not (critical or major):
        parts.append(f"{len(warning)} warning")
    if unlocked:
        parts.append(f"{len(unlocked)} SDI unlocked")
    if stopped_fans:
        parts.append(f"{len(stopped_fans)} fan(s) stopped")
    degraded = bool(critical or major or unlocked or stopped_fans)
    error = "; ".join(parts) if parts else None
    if error and (critical or major):
        labeled = []
        for s in (critical + major)[:6]:
            where = s.labels.get("config_label") or s.labels.get("connector") or ""
            slot = s.labels.get("slot") or "?"
            labeled.append(f"slot {slot}" + (f" {where}" if where else ""))
        if labeled:
            error = f"{error} ({', '.join(labeled)})"

    detail_lines = []
    if error:
        detail_lines.append(error)
    if temp is not None or watts is not None:
        detail_lines.append(
            f"chassis {int(temp) if temp is not None else '?'}C "
            f"{int(watts) if watts is not None else '?'}W"
        )
    if unlocked:
        names = [s.labels.get("config_label") or s.labels.get("slot") or "sdi" for s in unlocked[:8]]
        detail_lines.append("unlocked: " + ", ".join(names))

    return CollectResult(
        device_status="degraded" if degraded else "healthy",
        error=error if degraded else None,
        detail="\n".join(detail_lines) if detail_lines else None,
        modules=sorted(modules.values(), key=lambda m: (m.slot != "chassis", m.slot)),
        ports=list(discovered_ports.values()),
        flows=discovered_flows,
    )


class AppearXPlatformDriver(Driver):
    driver_id = "appear.x_platform.default"
    vendor = "appear"
    notes = (
        "Appear X Platform via confirmed Prometheus scrapes on a live DC X20: "
        "/prometheus/{system,product,ipgateway,alarms}/metrics. Alarms, SDI lock, "
        "video mode, bitrate, and IP-gateway port rates drive status. Collect "
        "upserts SDI/net ports and dest_label-only draft flows — no dest city. "
        "Do not invent MMI/IpGateway JSON paths."
    )
    # Modular frame: BNCs are operator-assignable until card layout is confirmed.
    connectors = sdi_layout(20, "assignable")

    def __init__(self, host: str, port: int = 443, scheme: str = "https",
                 username: Optional[str] = None, password: Optional[str] = None,
                 verify_tls: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        headers = {}
        if username is not None and password is not None:
            from base64 import b64encode
            token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        self._client = JsonHttpClient(
            host=host, port=port, scheme=scheme, verify_tls=verify_tls,
            timeout=timeout, extra_headers=headers,
        )

    def ping(self) -> bool:
        """Reachability only — one confirmed scrape, not the full collect set."""
        try:
            body = self._client.get_text(PING_PATH)
            return looks_like_prometheus(body)
        except Exception:  # noqa: BLE001 - ping must not raise
            return False

    def discover(self, candidates: Optional[list[str]] = None) -> list[DiscoveryResult]:
        return self._client.discover(candidates or DISCOVERY_CANDIDATES)

    def _fetch_metrics(self) -> Optional[str]:
        """GET every confirmed scrape and concatenate Prometheus text.
        Returns None if none of the four paths answered with metrics."""
        chunks: list[str] = []
        for path in PROMETHEUS_PATHS:
            try:
                body = self._client.get_text(path)
            except Exception:  # noqa: BLE001
                continue
            if looks_like_prometheus(body):
                chunks.append(body)
        if not chunks:
            return None
        return "\n".join(chunks)

    def collect(self) -> Optional[CollectResult]:
        body = self._fetch_metrics()
        if body is None:
            return None
        return interpret_appear_metrics(parse_prometheus(body))

    def get_json(self, path: str):
        return self._client.get_json(path)

    def post_json(self, path: str, body: dict):
        return self._client.post_json(path, body)
