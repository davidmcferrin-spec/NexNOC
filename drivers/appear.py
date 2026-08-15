"""
drivers/appear.py - Appear X Platform (X10/X20/X5/XM/XC5000/XC5100).

Confirmed from a live DC X20 (Prometheus text, not a public REST manual):

  Scrape URLs (confirmed):
    /prometheus/system/metrics
    /prometheus/product/metrics
    /prometheus/ipgateway/metrics
    /prometheus/alarms/metrics

  Metric names (confirmed):
    memory_usage_ratio, cpu_usage_ratio, storage_usage_ratio
    system_uptime_seconds, application_uptime_seconds
    power_consumption_watts, ambient_temperature_celsius, fan_speed_rpm
    total_alarms{severity,slot,config_id,...}
    apr_x_sdi_lock_status, apr_x_sdi_lock_loss_count, apr_x_sdi_bitrate
    apr_x_sdi_video_mode, apr_x_sdi_edh_error_count, apr_x_pid_ts_bytes
    port_rx_rate / port_tx_rate (IpGateway)

ping() GETs only /prometheus/system/metrics (cheap reachability).
collect() GETs all four scrapes and merges samples. JSON/HTTP sub-APIs
(MMI, IpGateway REST) remain unconfirmed. Phase 4 must not write to a
frame from this driver.
"""

from __future__ import annotations

from typing import Optional

from drivers.base import CollectResult, DiscoveryResult, Driver, InventoryItem, sdi_layout
from drivers.http_util import DEFAULT_TIMEOUT_SECONDS, JsonHttpClient
from drivers.prometheus_util import (
    looks_like_prometheus,
    parse_prometheus,
    sum_named,
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


class AppearXPlatformDriver(Driver):
    driver_id = "appear.x_platform.default"
    vendor = "appear"
    notes = (
        "Default Appear X Platform driver. Confirmed Prometheus scrapes on a "
        "live DC X20: /prometheus/{system,product,ipgateway,alarms}/metrics. "
        "Do not invent MMI/IpGateway JSON paths. Phase 4 must not write to a frame."
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
        samples = parse_prometheus(body)
        critical = sum_named(samples, "total_alarms", severity="critical")
        major = sum_named(samples, "total_alarms", severity="major")
        unlocked = [
            s for s in samples
            if s.name == "apr_x_sdi_lock_status" and s.value == 0
        ]
        if critical > 0:
            device_status = "degraded"
            error = f"{int(critical)} critical alarm(s)"
        elif unlocked:
            device_status = "degraded"
            error = f"{len(unlocked)} SDI input(s) unlocked"
        elif major > 0:
            device_status = "degraded"
            error = f"{int(major)} major alarm(s)"
        else:
            device_status = "healthy"
            error = None

        slots: dict[str, InventoryItem] = {}
        for s in samples:
            slot = s.labels.get("slot")
            if not slot:
                continue
            item = slots.setdefault(slot, InventoryItem(
                slot=slot, module_type="slot", status="unknown",
            ))
            if s.name == "apr_x_sdi_lock_status":
                if s.value == 0:
                    item.status = "down"
                elif item.status != "down":
                    item.status = "healthy"
            label = s.labels.get("config_label")
            if label and not item.module_type.startswith("Service"):
                item.module_type = label
        if not slots:
            for s in samples:
                if s.name == "memory_usage_ratio" and s.labels.get("slot"):
                    slots[s.labels["slot"]] = InventoryItem(
                        slot=s.labels["slot"], module_type="slot", status="unknown",
                    )

        return CollectResult(
            device_status=device_status,
            error=error,
            modules=sorted(slots.values(), key=lambda m: m.slot),
        )

    def get_json(self, path: str):
        return self._client.get_json(path)

    def post_json(self, path: str, body: dict):
        return self._client.post_json(path, body)
