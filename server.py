"""
server.py - Phase 2 dashboard for NexNOC.

Stdlib HTTP server (http.server). Serves the vanilla web/ frontend and a
JSON API over the same SQLite file the poller writes. Polling refresh on
the client - no WebSocket dependency. Inventory create/edit/delete plus
credential values (written to nexnoc.env, never to config.json or the DB).

Run (after bootstrap):
    python3 server.py --db noc.db --port 8080

Optionally bootstrap on start:
    python3 server.py --config config.json --db noc.db

Bind defaults to 127.0.0.1. GET / is the login page. Local + LDAP login
gates /dashboard and all writes. GET /kiosk, /api/state, and /api/time
stay anonymous so the wall board works without a session.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from audit import audit_log, audit_writable
from auth import load_session_user, next_url, parse_query, request_is_secure, token_from_cookie
from auth_api import AuthError, handle_auth
from db import Database, Device, utcnow_iso
from envfile import default_env_path
from inventory_api import SECRET_KEYS, default_pin_dir, device_secret_flags, handle as handle_inventory
from pins import BUILTIN_PINS

_PUBLIC_STATE_DROP = {
    "api_username_env", "api_password_env", "snmp_community_env",
    "snmp_v3_user_env", "snmp_v3_auth_pass_env", "snmp_v3_priv_pass_env",
    "nms_api_key_env",
    "api_username_set", "api_password_set", "snmp_community_set",
    "snmp_v3_user_set", "snmp_v3_auth_set", "snmp_v3_priv_set",
    "credentials_ready", "snmp_ready",
}

logger = logging.getLogger("nexnoc.server")

WEB_ROOT = Path(__file__).parent / "web"

# Dev default: Carto Dark Matter over the public CDN. Production sets
# map.local_tile_dir (or --tile-dir) and the dashboard serves XYZ tiles
# from disk at /tiles/{z}/{x}/{y}.png instead.
CDN_TILE_URL = "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png"
CDN_TILE_SUBDOMAINS = "abcd"
CDN_TILE_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    '&copy; <a href="https://carto.com/attributions">CARTO</a>'
)
_TILE_PATH = re.compile(r"^/tiles/(\d+)/(\d+)/(\d+)\.(png|jpg|jpeg|webp)$")

_SIGNAL_FROM_DEVICE = {
    "healthy": "up",
    "degraded": "degraded",
    "unreachable": "down",
    "decommissioned": "down",
}
_SIGNAL_RANK = {"down": 3, "degraded": 2, "unknown": 1, "up": 0}
_DEVICE_RANK = {
    "unreachable": 3,
    "decommissioned": 3,
    "degraded": 2,
    "unknown": 1,
    "healthy": 0,
}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _row_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def effective_signal_status(stored: Optional[str], device_status: Optional[str]) -> str:
    """Prefer an explicit signal status; otherwise derive from the host device.

    Phase 2 has no confirmed per-vendor signal APIs, so a signal left at
    `unknown` follows the device the poller already knows how to reach.
    An explicit up/degraded/down (set in config or a later phase) wins.
    """
    if stored and stored != "unknown":
        return stored
    return _SIGNAL_FROM_DEVICE.get(device_status or "", "unknown")


def _worst(statuses: list[str], rank: dict[str, int]) -> str:
    if not statuses:
        return "unknown"
    return max(statuses, key=lambda s: rank.get(s, 0))


def _count_by(items: list[str]) -> dict[str, int]:
    return dict(Counter(items))


def serialize_device(device: Device, site_name: str, city_name: str = "",
                     city_id: Optional[int] = None, env_path: Optional[Path] = None) -> dict:
    flags = device_secret_flags(device, env_path or default_env_path())
    return {
        "id": device.id,
        "site_id": device.site_id,
        "site_name": site_name,
        "city_id": city_id,
        "city_name": city_name or "",
        "name": device.name,
        "vendor": device.vendor,
        "device_role": device.device_role,
        "model": device.model,
        "firmware_version": device.firmware_version,
        "mgmt_host": device.mgmt_host,
        "access_mode": device.access_mode,
        "driver_override": device.driver_override,
        "control_driver": device.control_driver,
        "resolved_driver": device.resolved_driver,
        "status": device.status,
        "last_seen_at": device.last_seen_at,
        "last_error": device.last_error,
        "poll_enabled": device.poll_enabled,
        "api_port": device.api_port,
        "api_scheme": device.api_scheme,
        "api_verify_tls": device.api_verify_tls,
        "snmp_host": device.snmp_host,
        "snmp_port": device.snmp_port,
        "snmp_version": device.snmp_version,
        "snmp_enabled": device.snmp_enabled,
        "snmp_trap_enabled": device.snmp_trap_enabled,
        "snmp_v3_sec_level": device.snmp_v3_sec_level,
        "snmp_v3_auth_proto": device.snmp_v3_auth_proto,
        "snmp_v3_priv_proto": device.snmp_v3_priv_proto,
        **flags,
    }


def strip_public_state(payload: dict) -> dict:
    """Drop credential slot names/flags from the anonymous kiosk payload."""
    out = dict(payload)
    devices = []
    for device in out.get("devices") or []:
        devices.append({k: v for k, v in device.items() if k not in _PUBLIC_STATE_DROP})
    out["devices"] = devices
    return out


def _city_key_from_site(site: dict) -> str:
    if site.get("city_id"):
        return f"c:{site['city_id']}"
    name = (site.get("city_name") or site.get("city") or "").strip()
    if name:
        return f"n:{name.lower()}"
    return f"s:{site['id']}"


def _city_display_from_site(site: dict) -> str:
    return (site.get("city_name") or site.get("city") or site.get("name") or "").strip()


def _flow_source_city_key(flow: dict) -> str:
    if flow.get("source_city_id"):
        return f"c:{flow['source_city_id']}"
    name = (flow.get("source_city_name") or "").strip()
    if name:
        return f"n:{name.lower()}"
    return f"s:{flow['source_site_id']}"


def _flow_dest_city_key(flow: dict) -> Optional[str]:
    if flow.get("dest_city_id"):
        return f"c:{flow['dest_city_id']}"
    name = (flow.get("dest_city_name") or flow.get("dest_city_resolved") or "").strip()
    if name:
        return f"n:{name.lower()}"
    if flow.get("dest_site_id"):
        return f"s:{flow['dest_site_id']}"
    return None


def _serialize_flow(row: dict) -> dict:
    effective = effective_signal_status(row["status"], row["source_device_status"])
    dest_city = row.get("dest_city_resolved") or row.get("dest_city_name") or ""
    dest_name = (
        dest_city or row.get("dest_site_name") or row.get("dest_device_name")
        or row.get("dest_label") or ""
    )
    signal = row.get("signal_label") or row.get("source_port_name") or row.get("label") or ""
    flow = {
        "id": row["id"],
        "label": row["label"],
        "signal_label": signal,
        "direction": row["direction"] or "",
        "status": row["status"],
        "effective_status": effective,
        "last_status_change": row["last_status_change"],
        "last_polled_at": row["last_polled_at"],
        "source_device_id": row["source_device_id"],
        "source_device_name": row["source_device_name"],
        "source_device_status": row["source_device_status"],
        "source_device_vendor": row["source_device_vendor"],
        "source_site_id": row["source_site_id"],
        "source_site_name": row["source_site_name"],
        "source_site_lat": row["source_site_lat"],
        "source_site_lng": row["source_site_lng"],
        "source_city_id": row.get("source_city_id"),
        "source_city_name": row.get("source_city_name") or "",
        "source_port_id": row["source_port_id"],
        "source_port_name": row.get("source_port_name") or "",
        "source_port_kind": row.get("source_port_kind") or "",
        "dest_port_id": row.get("dest_port_id"),
        "origin_device_id": row.get("origin_device_id"),
        "origin_device_name": row.get("origin_device_name") or "",
        "origin_site_name": row.get("origin_site_name") or "",
        "origin_city_name": row.get("origin_city_name") or "",
        "origin_port_name": row.get("origin_port_name") or "",
        "dest_city_id": row.get("dest_city_id"),
        "dest_city_name": dest_city,
        "dest_city_lat": row.get("dest_city_lat"),
        "dest_city_lng": row.get("dest_city_lng"),
        "dest_site_id": row["dest_site_id"],
        "dest_site_name": row.get("dest_site_name") or "",
        "dest_site_lat": row.get("dest_site_lat"),
        "dest_site_lng": row.get("dest_site_lng"),
        "dest_device_id": row["dest_device_id"],
        "dest_device_name": row.get("dest_device_name") or "",
        "dest_port_name": row.get("dest_port_name") or "",
        "dest_label": row.get("dest_label") or "",
        "dest_display": dest_name,
    }
    flow["source_city_key"] = _flow_source_city_key(flow)
    flow["dest_city_key"] = _flow_dest_city_key(flow)
    return flow


def _build_cities(sites_out: list[dict], city_rows: list[dict],
                  devices_by_site: dict[int, list[Device]]) -> list[dict]:
    """One map node per city. Sites with no city stay their own node."""
    cities: dict[str, dict] = {}
    for row in city_rows:
        key = f"c:{row['id']}"
        cities[key] = {
            "id": key,
            "db_id": row["id"],
            "name": row["name"],
            "lat": row["lat"],
            "lng": row["lng"],
            "geo_source": row.get("geo_source") or "",
            "notes": row.get("notes") or "",
            "site_ids": [],
            "device_statuses": [],
        }
    for site in sites_out:
        key = _city_key_from_site(site)
        bucket = cities.get(key)
        if bucket is None:
            bucket = {
                "id": key,
                "db_id": site.get("city_id"),
                "name": _city_display_from_site(site),
                "lat": site.get("city_lat") or site.get("lat"),
                "lng": site.get("city_lng") or site.get("lng"),
                "site_ids": [],
                "device_statuses": [],
            }
            cities[key] = bucket
        bucket["site_ids"].append(site["id"])
        site_devices = devices_by_site.get(site["id"], [])
        bucket["device_statuses"].extend(d.status for d in site_devices)
        if bucket["lat"] is None:
            bucket["lat"] = site.get("lat")
            bucket["lng"] = site.get("lng")
        site["city_key"] = key
        site["city_name"] = bucket["name"]
    out = []
    for bucket in cities.values():
        statuses = bucket["device_statuses"]
        out.append({
            "id": bucket["id"],
            "db_id": bucket["db_id"],
            "name": bucket["name"],
            "lat": bucket["lat"],
            "lng": bucket["lng"],
            "status": _worst(statuses, _DEVICE_RANK) if statuses else "unknown",
            "site_count": len(bucket["site_ids"]),
            "site_ids": bucket["site_ids"],
            "device_count": len(statuses),
            "devices_by_status": _count_by(statuses),
            "notes": bucket.get("notes") or "",
            "geo_source": bucket.get("geo_source") or "",
        })
    out.sort(key=lambda c: c["name"])
    return out


def _aggregate_hops(flows_out: list[dict], cities_out: list[dict]) -> list[dict]:
    """One undirected trunk per city pair. Intra-city dests stay on the city panel."""
    city_by_id = {c["id"]: c for c in cities_out}
    buckets: dict[tuple[str, str], dict] = {}
    for flow in flows_out:
        src = flow.get("source_city_key")
        dst = flow.get("dest_city_key")
        if not src or not dst or src == dst:
            continue
        src_city = city_by_id.get(src)
        dst_city = city_by_id.get(dst)
        src_name = (src_city or {}).get("name") or flow.get("source_city_name") or flow["source_site_name"]
        dst_name = (dst_city or {}).get("name") or flow.get("dest_city_name") or flow.get("dest_site_name") or ""
        src_lat = (src_city or {}).get("lat") or flow.get("source_site_lat")
        src_lng = (src_city or {}).get("lng") or flow.get("source_site_lng")
        dst_lat = (
            (dst_city or {}).get("lat") or flow.get("dest_city_lat")
            or flow.get("dest_site_lat")
        )
        dst_lng = (
            (dst_city or {}).get("lng") or flow.get("dest_city_lng")
            or flow.get("dest_site_lng")
        )
        if src_lat is None or dst_lat is None:
            continue
        if src_name.lower() <= dst_name.lower():
            a_id, b_id = src, dst
            a_name, b_name = src_name, dst_name
            a_lat, a_lng, b_lat, b_lng = src_lat, src_lng, dst_lat, dst_lng
        else:
            a_id, b_id = dst, src
            a_name, b_name = dst_name, src_name
            a_lat, a_lng, b_lat, b_lng = dst_lat, dst_lng, src_lat, src_lng
        key = (a_id, b_id)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "id": f"{a_id}|{b_id}",
                "city_a_id": a_id,
                "city_b_id": b_id,
                "city_a_name": a_name,
                "city_b_name": b_name,
                "source_lat": a_lat,
                "source_lng": a_lng,
                "dest_lat": b_lat,
                "dest_lng": b_lng,
                "flow_ids": [],
                "statuses": [],
                "site_names": set(),
                "dir_counts": {},
                "dir_statuses": {},
            }
            buckets[key] = bucket
        bucket["flow_ids"].append(flow["id"])
        bucket["statuses"].append(flow["effective_status"])
        dkey = (src, dst, src_name, dst_name)
        bucket["dir_counts"][dkey] = bucket["dir_counts"].get(dkey, 0) + 1
        bucket["dir_statuses"].setdefault(dkey, []).append(flow["effective_status"])
        if flow.get("source_site_name"):
            bucket["site_names"].add(flow["source_site_name"])
        if flow.get("dest_site_name"):
            bucket["site_names"].add(flow["dest_site_name"])
    hops = []
    for bucket in buckets.values():
        directions = []
        for (src, dst, src_name, dst_name), count in bucket["dir_counts"].items():
            directions.append({
                "source_city_id": src,
                "dest_city_id": dst,
                "source_city_name": src_name,
                "dest_city_name": dst_name,
                "flow_count": count,
                "status": _worst(bucket["dir_statuses"][(src, dst, src_name, dst_name)], _SIGNAL_RANK),
            })
        directions.sort(key=lambda d: (d["source_city_name"], d["dest_city_name"]))
        hops.append({
            "id": bucket["id"],
            "city_a_id": bucket["city_a_id"],
            "city_b_id": bucket["city_b_id"],
            "city_a_name": bucket["city_a_name"],
            "city_b_name": bucket["city_b_name"],
            "source_city_id": bucket["city_a_id"],
            "dest_city_id": bucket["city_b_id"],
            "source_city_name": bucket["city_a_name"],
            "dest_city_name": bucket["city_b_name"],
            "source_site_name": bucket["city_a_name"],
            "dest_site_name": bucket["city_b_name"],
            "source_lat": bucket["source_lat"],
            "source_lng": bucket["source_lng"],
            "dest_lat": bucket["dest_lat"],
            "dest_lng": bucket["dest_lng"],
            "status": _worst(bucket["statuses"], _SIGNAL_RANK),
            "flow_count": len(bucket["flow_ids"]),
            "flows_by_status": _count_by(bucket["statuses"]),
            "site_names": sorted(bucket["site_names"]),
            "dest_site_names": sorted(bucket["site_names"]),
            "directions": directions,
        })
    hops.sort(key=lambda h: (h["city_a_name"], h["city_b_name"]))
    return hops


def server_clock() -> dict:
    """Authoritative wall-clock for the dashboard (NTP-backed OS time)."""
    return {
        "server_time_ms": int(time.time() * 1000),
        "server_time_utc": utcnow_iso(),
    }


def build_dashboard_state(db: Database, env_path: Optional[Path] = None) -> dict:
    """Single payload the frontend polls. Assembled here so tests can
    exercise status derivation without standing up HTTP."""
    secrets_path = env_path or default_env_path()
    sites_rows = [_row_dict(r) for r in db.list_sites()]
    site_by_id = {s["id"]: s for s in sites_rows}
    city_rows = [_row_dict(r) for r in db.list_cities()]
    devices = db.list_devices()
    trunks_rows = [_row_dict(r) for r in db.list_trunks()]
    signals_rows = [_row_dict(r) for r in db.list_signals()]
    flows_rows = [_row_dict(r) for r in db.list_flows()]
    ports_out = [_row_dict(r) for r in db.list_ports()]

    devices_out = [
        serialize_device(
            d,
            site_by_id.get(d.site_id, {}).get("name", ""),
            site_by_id.get(d.site_id, {}).get("city_name") or site_by_id.get(d.site_id, {}).get("city") or "",
            site_by_id.get(d.site_id, {}).get("city_id"),
            secrets_path,
        )
        for d in devices
    ]
    devices_by_site: dict[int, list[Device]] = {}
    for d in devices:
        devices_by_site.setdefault(d.site_id, []).append(d)

    signals_out = []
    signals_by_trunk: dict[Optional[int], list[dict]] = {}
    for row in signals_rows:
        effective = effective_signal_status(row["status"], row["device_status"])
        signal = {
            "id": row["id"],
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "device_status": row["device_status"],
            "device_vendor": row["device_vendor"],
            "site_id": row["site_id"],
            "site_name": row["site_name"],
            "trunk_id": row["trunk_id"],
            "trunk_label": row["trunk_label"],
            "source_label": row["source_label"],
            "destination_label": row["destination_label"],
            "direction": row["direction"] or "",
            "status": row["status"],
            "effective_status": effective,
            "last_status_change": row["last_status_change"],
            "last_polled_at": row["last_polled_at"],
        }
        signals_out.append(signal)
        signals_by_trunk.setdefault(row["trunk_id"], []).append(signal)

    flows_out = [_serialize_flow(row) for row in flows_rows]

    sites_out = []
    for site in sites_rows:
        site_devices = devices_by_site.get(site["id"], [])
        statuses = [d.status for d in site_devices]
        sites_out.append({
            "id": site["id"],
            "name": site["name"],
            "city": site.get("city") or "",
            "city_id": site.get("city_id"),
            "city_name": site.get("city_name") or site.get("city") or "",
            "city_lat": site.get("city_lat"),
            "city_lng": site.get("city_lng"),
            "lat": site["lat"],
            "lng": site["lng"],
            "address": site.get("address") or "",
            "geo_source": site.get("geo_source") or "",
            "pin_icon": site.get("pin_icon") or "building",
            "pin_color": site.get("pin_color") or "#6aa4ff",
            "pin_upload": site.get("pin_upload") or "",
            "notes": site.get("notes") or "",
            "status": _worst(statuses, _DEVICE_RANK) if site_devices else "unknown",
            "device_count": len(site_devices),
            "devices_by_status": _count_by(statuses),
        })

    cities_out = _build_cities(sites_out, city_rows, devices_by_site)
    hops_out = _aggregate_hops(flows_out, cities_out)

    trunks_out = []
    for trunk in trunks_rows:
        trunk_signals = signals_by_trunk.get(trunk["id"], [])
        effective_statuses = [s["effective_status"] for s in trunk_signals]
        trunks_out.append({
            "id": trunk["id"],
            "label": trunk["label"],
            "site_a_id": trunk["site_a_id"],
            "site_b_id": trunk["site_b_id"],
            "site_a_name": trunk["site_a_name"],
            "site_b_name": trunk["site_b_name"],
            "site_a_lat": trunk["site_a_lat"],
            "site_a_lng": trunk["site_a_lng"],
            "site_b_lat": trunk["site_b_lat"],
            "site_b_lng": trunk["site_b_lng"],
            "status": _worst(effective_statuses, _SIGNAL_RANK) if trunk_signals else "unknown",
            "signal_count": len(trunk_signals),
            "signals_by_status": _count_by(effective_statuses),
        })

    clock = server_clock()
    return {
        "generated_at": clock["server_time_utc"],
        "server_time_ms": clock["server_time_ms"],
        "server_time_utc": clock["server_time_utc"],
        "latest_poll_at": db.latest_poll_at(),
        "summary": {
            "sites": len(sites_out),
            "devices": len(devices_out),
            "devices_by_status": _count_by([d["status"] for d in devices_out]),
            "signals": len(signals_out),
            "signals_by_status": _count_by([s["effective_status"] for s in signals_out]),
            "flows": len(flows_out),
            "flows_by_status": _count_by([f["effective_status"] for f in flows_out]),
            "hops": len(hops_out),
            "cities": len(cities_out),
            "trunks": len(trunks_out),
        },
        "cities": cities_out,
        "sites": sites_out,
        "devices": devices_out,
        "ports": ports_out,
        "trunks": trunks_out,
        "signals": signals_out,
        "flows": flows_out,
        "hops": hops_out,
        "drivers": _driver_catalog(),
        "pins": list(BUILTIN_PINS),
    }


def build_device_detail(db: Database, device_id: int,
                        env_path: Optional[Path] = None) -> Optional[dict]:
    device = db.get_device(device_id)
    if device is None:
        return None
    site = db.get_site(device.site_id)
    site_name = site["name"] if site else ""
    city_name = ""
    city_id = None
    if site is not None:
        city_id = site["city_id"] if "city_id" in site.keys() else None
        if city_id:
            city = db.get_city(city_id)
            city_name = city["name"] if city else (site["city"] or "")
        else:
            city_name = site["city"] or ""
    modules = [_row_dict(r) for r in db.list_modules(device_id)]
    polls = [_row_dict(r) for r in db.recent_poll_history(device_id, limit=25)]
    traps = [_row_dict(r) for r in db.list_traps(device_id, limit=25)]
    ports = [_row_dict(r) for r in db.list_ports(device_id)]
    flows = [
        _serialize_flow(_row_dict(r))
        for r in db.list_flows()
        if r["source_device_id"] == device_id
        or r["dest_device_id"] == device_id
        or r["origin_device_id"] == device_id
    ]
    return {
        "device": serialize_device(device, site_name, city_name, city_id, env_path),
        "modules": modules,
        "ports": ports,
        "flows": flows,
        "recent_polls": polls,
        "recent_traps": traps,
    }


def load_map_section(path: Optional[str]) -> dict:
    """Read the optional top-level `map` object from a config JSON.

    Does not bootstrap inventory — the poller owns that. Missing or
    unreadable files yield defaults (CDN tiles).
    """
    if not path:
        return {}
    cfg_path = Path(path)
    if not cfg_path.is_file():
        logger.warning("map config %s not found — using tile defaults", path)
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("map config %s unreadable: %s", path, exc)
        return {}
    section = data.get("map")
    return section if isinstance(section, dict) else {}


def resolve_map_settings(map_cfg: Optional[dict] = None,
                         tile_dir: Optional[str] = None) -> dict:
    """Public tile settings for /api/state plus the on-disk tile root.

    If local_tile_dir / --tile-dir is set, the browser is pointed at
    /tiles/{z}/{x}/{y}.png and never at the CDN.
    """
    cfg = dict(map_cfg or {})
    local = (tile_dir or cfg.get("local_tile_dir") or "").strip()
    public = {
        "tile_url": cfg.get("tile_url") or CDN_TILE_URL,
        "tile_subdomains": cfg.get("tile_subdomains") or CDN_TILE_SUBDOMAINS,
        "tile_attribution": cfg.get("tile_attribution") or CDN_TILE_ATTRIBUTION,
        "min_zoom": int(cfg.get("min_zoom") if cfg.get("min_zoom") is not None else 3),
        "max_zoom": int(cfg.get("max_zoom") if cfg.get("max_zoom") is not None else 18),
        "source": "cdn",
    }
    if local:
        public["tile_url"] = "/tiles/{z}/{x}/{y}.png"
        public["tile_subdomains"] = ""
        public["source"] = "local"
        if cfg.get("max_zoom") is None:
            public["max_zoom"] = 8
        if cfg.get("tile_attribution"):
            public["tile_attribution"] = cfg["tile_attribution"]
    return {
        "public": public,
        "tile_dir": Path(local) if local else None,
    }


def _safe_tile_path(tile_dir: Path, z: int, x: int, y: int, ext: str) -> Optional[Path]:
    if not (0 <= z <= 22 and x >= 0 and y >= 0):
        return None
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        return None
    root = tile_dir.resolve()
    candidate = (root / str(z) / str(x) / f"{y}.{ext}").resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


def _driver_catalog() -> list[dict]:
    from drivers.base import driver_catalog
    from drivers.registry import DRIVER_REGISTRY
    return driver_catalog(DRIVER_REGISTRY)


_UPLOAD_PIN = re.compile(r"^/uploads/pins/([A-Za-z0-9._-]+)$")


def _safe_web_path(url_path: str) -> Optional[Path]:
    rel = url_path.lstrip("/")
    if rel in ("kiosk", "dashboard"):
        rel = "dashboard.html"
    elif not rel:
        rel = "index.html"
    candidate = (WEB_ROOT / rel).resolve()
    try:
        candidate.relative_to(WEB_ROOT.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


def make_handler(db: Database, map_settings: Optional[dict] = None,
                 env_path: Optional[Path] = None, pin_dir: Optional[Path] = None):
    settings = map_settings or resolve_map_settings()
    public_map = settings["public"]
    tile_dir = settings.get("tile_dir")
    secrets_path = Path(env_path) if env_path else default_env_path()
    uploads = Path(pin_dir) if pin_dir else default_pin_dir()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "NexNOC/0.2"

        def log_message(self, fmt: str, *args) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _current_user(self):
            token = token_from_cookie(self.headers.get("Cookie") or "")
            return load_session_user(db, token), token

        def _client_ip(self) -> str:
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return forwarded or self.address_string()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            user, token = self._current_user()

            if path == "/api/state":
                payload = build_dashboard_state(db, secrets_path)
                payload["map"] = public_map
                if not user:
                    payload = strip_public_state(payload)
                self._send_json(200, payload)
                return
            if path == "/api/time":
                self._send_json(200, server_clock())
                return
            tile_match = _TILE_PATH.match(path)
            if tile_match:
                if tile_dir is None:
                    self._send_json(404, {"error": "local tiles not configured"})
                    return
                z, x, y, ext = tile_match.groups()
                target = _safe_tile_path(tile_dir, int(z), int(x), int(y), ext)
                if target is None:
                    self.send_error(404, "tile not found")
                    return
                self._send_file(target, cache="public, max-age=86400")
                return
            if path.startswith("/api/auth/") or path.startswith("/api/admin"):
                query = parse_query(self.path)
                try:
                    status, payload, cookie = handle_auth(
                        db, "GET", path, query, user, token,
                        request_is_secure(self.headers), self._client_ip(),
                    )
                except AuthError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                    return
                except LookupError as exc:
                    self._send_json(404, {"error": str(exc)})
                    return
                self._send_json(status, payload, cookie=cookie)
                return
            if path.startswith("/api/devices/"):
                if not user:
                    self._send_json(401, {"error": "login required"})
                    return
                rest = path[len("/api/devices/"):]
                if rest.isdigit():
                    detail = build_device_detail(db, int(rest), secrets_path)
                    if detail is None:
                        self._send_json(404, {"error": "device not found"})
                    else:
                        self._send_json(200, detail)
                    return
                self._send_json(404, {"error": "not found"})
                return
            if path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return
            pin_match = _UPLOAD_PIN.match(path)
            if pin_match:
                target = (uploads / pin_match.group(1)).resolve()
                try:
                    target.relative_to(uploads.resolve())
                except ValueError:
                    self._send_json(404, {"error": "not found"})
                    return
                if not target.is_file():
                    self.send_error(404, "pin not found")
                    return
                self._send_file(target, cache="public, max-age=86400")
                return

            if path in ("/login", "/login.html"):
                if user and not user.get("must_change_password"):
                    self._redirect("/dashboard")
                else:
                    self._redirect("/?reason=password" if user else "/")
                return
            if path in ("/", "/index.html"):
                if user and not user.get("must_change_password"):
                    self._redirect("/dashboard")
                    return
                login = WEB_ROOT / "index.html"
                if login.is_file():
                    self._send_file(login)
                    return
            if path in ("/dashboard", "/dashboard.html"):
                if not user:
                    self._redirect(f"/?next={next_url(path)}")
                    return
                if user.get("must_change_password"):
                    self._redirect("/?reason=password")
                    return

            target = _safe_web_path(path)
            if target is None:
                self._send_json(404, {"error": "not found"})
                return
            self._send_file(target)

        def do_POST(self) -> None:
            self._mutate("POST")

        def do_PATCH(self) -> None:
            self._mutate("PATCH")

        def do_DELETE(self) -> None:
            self._mutate("DELETE")

        def _mutate(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if not path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return
            try:
                body = self._read_json()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            user, token = self._current_user()
            if path.startswith("/api/auth/") or path.startswith("/api/admin"):
                try:
                    status, payload, cookie = handle_auth(
                        db, method, path, body, user, token,
                        request_is_secure(self.headers), self._client_ip(),
                    )
                except AuthError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                    return
                except LookupError as exc:
                    self._send_json(404, {"error": str(exc)})
                    return
                self._send_json(status, payload, cookie=cookie)
                return
            if not user:
                self._send_json(401, {"error": "login required"})
                return
            if user.get("must_change_password"):
                self._send_json(403, {"error": "password change required"})
                return
            perms = user.get("permissions") or {}
            if not perms.get("manage_inventory"):
                self._send_json(403, {"error": "access denied"})
                return
            if any(key in body for key in SECRET_KEYS) and not perms.get("manage_credentials"):
                self._send_json(403, {"error": "access denied"})
                return
            if not audit_writable():
                self._send_json(500, {"error": "audit write failed"})
                return
            try:
                status, payload = handle_inventory(db, secrets_path, method, path, body, uploads)
            except LookupError as exc:
                status, payload = 404, {"error": str(exc)}
            except ValueError as exc:
                status, payload = 400, {"error": str(exc)}
            except OSError as exc:
                logger.exception("inventory write failed")
                status, payload = 500, {"error": f"could not write: {exc}"}
            ok = 200 <= status < 300
            if not audit_log(
                "inventory", user, self._client_ip(),
                {"method": method, "path": path}, ok=ok,
            ):
                self._send_json(500, {"error": "audit write failed"})
                return
            self._send_json(status, payload)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("body too large")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid JSON: {exc}") from exc
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _send_json(self, status: int, payload: dict, cookie: Optional[str] = None) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_file(self, path: Path, cache: str = "no-cache") -> None:
            data = path.read_bytes()
            ctype, _ = mimetypes.guess_type(str(path))
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return DashboardHandler


def serve(db: Database, host: str, port: int,
          map_settings: Optional[dict] = None,
          env_path: Optional[Path] = None,
          pin_dir: Optional[Path] = None) -> None:
    if not WEB_ROOT.is_dir():
        raise FileNotFoundError(f"web/ directory missing at {WEB_ROOT}")
    settings = map_settings or resolve_map_settings()
    handler = make_handler(db, settings, env_path, pin_dir)
    httpd = ThreadingHTTPServer((host, port), handler)
    src = settings["public"]["source"]
    tiles = settings["public"]["tile_url"]
    logger.info("NexNOC dashboard at http://%s:%d  (Ctrl-C to stop)", host, port)
    logger.info("Map tiles (%s): %s", src, tiles)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl-C)")
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="NexNOC - Phase 2 dashboard")
    parser.add_argument("--config", help="If set, bootstrap sites/devices/trunks/signals before serving")
    parser.add_argument("--map-config", dest="map_config",
                        help="JSON file to read map.tile_* / map.local_tile_dir from (no bootstrap)")
    parser.add_argument("--tile-dir", dest="tile_dir",
                        default=os.environ.get("NEXNOC_TILE_DIR", ""),
                        help="Local XYZ tile root; if set, /tiles/{z}/{x}/{y}.png is served from here")
    parser.add_argument("--db", default="noc.db", help="Path to SQLite DB file")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--env-file", dest="env_file",
                        default=os.environ.get("NEXNOC_ENV_FILE", ""),
                        help="Env file for credential values (default: /etc/nexnoc/nexnoc.env or config/nexnoc.env)")
    parser.add_argument("--pin-dir", dest="pin_dir",
                        default=os.environ.get("NEXNOC_PIN_DIR", ""),
                        help="Directory for uploaded site pin images")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    db = Database(args.db)
    db.initialize()

    if args.config:
        # Imported lazily so `python server.py --db noc.db` does not require
        # a config file, and so tests can import build_dashboard_state
        # without pulling in the poller.
        from poller import bootstrap, load_config
        bootstrap(db, load_config(args.config))

    map_cfg = load_map_section(args.map_config or args.config)
    map_settings = resolve_map_settings(map_cfg, args.tile_dir)
    env_path = Path(args.env_file) if args.env_file else default_env_path()
    pin_dir = Path(args.pin_dir) if args.pin_dir else default_pin_dir()

    try:
        serve(db, args.host, args.port, map_settings, env_path, pin_dir)
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
