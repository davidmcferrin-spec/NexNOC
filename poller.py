"""
poller.py - Phase 1 foundation service for NexNOC.

Responsibilities:
  1. Bootstrap: load config.json, upsert sites/devices/trunks/signals/ports/flows
     into the DB (idempotent - safe to re-run after editing config.json to add
     a device or path). If config has no `flows` key, one flow is derived
     per legacy signal so the map still has directed hops.
  2. Poll loop: every `poll_interval_seconds`, concurrently hit every enabled
     device via its vendor adapter's ping(), record results to poll_log, and
     update devices.status.
  3. Retry/backoff: a single failed poll does not immediately mark a device
     unreachable - avoids flapping status on a dropped packet. Marks
     'unreachable' only after `consecutive_failures_threshold` misses in a row.

Run:
    python3 poller.py --config config.json --db /var/lib/nexnoc/noc.db

Bootstrap only (no polling), e.g. after editing config.json:
    python3 poller.py --config config.json --db noc.db --bootstrap-only

Discover real API paths against one HTTP-based device (Appear, Haivision)
before relying on this for real health/inventory data:
    python3 poller.py --discover HSV-X20-1 --config config.json --db noc.db
(Net Insight direct-SNMP devices don't support discover() - see drivers/net_insight.py)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from db import Database, Device
from drivers.base import CollectResult, Driver, DriverResolutionError, resolve_driver
from drivers.registry import DRIVER_REGISTRY
from drivers.snmp_util import SnmpError, SnmpTarget, snmp_ping
from envfile import default_env_path, get_value, upsert_values

logger = logging.getLogger("nexnoc.poller")

CONSECUTIVE_FAILURES_THRESHOLD = 3  # misses in a row before status flips to 'unreachable'

# Old importer names → current site names. Applied on bootstrap so leftover
# rows (e.g. "Washington DC - WDCW") collapse into the canonical site.
SITE_ALIASES = {
    "Washington DC - WDCW": "WDCW TV Station",
    "NewsNation DC": "400 N. Capital St",
    "Washington DC - NN": "400 N. Capital St",
    "Washington DC": "400 N. Capital St",
}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config.example.json to config.json and edit it."
        )
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def bootstrap(db: Database, config: dict) -> None:
    """Idempotently sync sites/devices/trunks/signals from config.json into
    the DB. Existing rows are matched by unique name (sites/devices), trunk
    label, or (device, source, destination) for signals; new ones are
    inserted. Does NOT delete rows removed from config.json - that's a
    deliberate manual action so a typo can't silently nuke poll history."""
    existing_cities = _bootstrap_cities(db, config)
    existing_sites = {row["name"]: row["id"] for row in db.list_sites()}
    _bootstrap_site_aliases(db, config, existing_sites)
    for site_cfg in config.get("sites", []):
        city_name = site_cfg.get("city") or ""
        city_id = existing_cities.get(city_name) if city_name else None
        if site_cfg["name"] not in existing_sites:
            site_id = db.add_site(
                name=site_cfg["name"],
                city=city_name,
                city_id=city_id,
                lat=site_cfg.get("lat"),
                lng=site_cfg.get("lng"),
            )
            existing_sites[site_cfg["name"]] = site_id
            logger.info("Added site %r (id=%d) in city %r", site_cfg["name"], site_id, city_name or None)
        else:
            site_id = existing_sites[site_cfg["name"]]
            if city_id is not None:
                row = db.get_site(site_id)
                if row is not None and row["city_id"] is None:
                    db.set_site_city(row["id"], city_id, city_name)
            updates = {}
            if site_cfg.get("lat") is not None:
                updates["lat"] = site_cfg["lat"]
            if site_cfg.get("lng") is not None:
                updates["lng"] = site_cfg["lng"]
            if updates:
                db.update_site(site_id, **updates)

    existing_devices = {d.name: d for d in db.list_devices(include_decommissioned=True)}
    for dev_cfg in config.get("devices", []):
        if dev_cfg["name"] in existing_devices:
            continue
        site_id = existing_sites.get(dev_cfg["site"])
        if site_id is None:
            logger.error(
                "Device %r references unknown site %r - skipping. Add the site to config.json first.",
                dev_cfg["name"], dev_cfg["site"],
            )
            continue
        try:
            device_id = db.add_device(
                site_id=site_id,
                name=dev_cfg["name"],
                vendor=dev_cfg["vendor"],
                mgmt_host=dev_cfg["mgmt_host"],
                device_role=dev_cfg.get("device_role", ""),
                model=dev_cfg.get("model", ""),
                firmware_version=dev_cfg.get("firmware_version", ""),
                access_mode=dev_cfg.get("access_mode", "direct_api"),
                driver_override=dev_cfg.get("driver_override"),
                api_port=dev_cfg.get("api_port", 443),
                api_scheme=dev_cfg.get("api_scheme", "https"),
                api_verify_tls=dev_cfg.get("api_verify_tls", False),
                api_username_env=dev_cfg.get("api_username_env"),
                api_password_env=dev_cfg.get("api_password_env"),
                snmp_community_env=dev_cfg.get("snmp_community_env"),
                snmp_host=dev_cfg.get("snmp_host"),
                snmp_port=dev_cfg.get("snmp_port", 161),
                snmp_version=dev_cfg.get("snmp_version", "2c"),
                snmp_enabled=dev_cfg.get("snmp_enabled"),
                snmp_trap_enabled=dev_cfg.get("snmp_trap_enabled", True),
                snmp_v3_user_env=dev_cfg.get("snmp_v3_user_env"),
                snmp_v3_sec_level=dev_cfg.get("snmp_v3_sec_level", "authPriv"),
                snmp_v3_auth_proto=dev_cfg.get("snmp_v3_auth_proto", "SHA"),
                snmp_v3_auth_pass_env=dev_cfg.get("snmp_v3_auth_pass_env"),
                snmp_v3_priv_proto=dev_cfg.get("snmp_v3_priv_proto", "AES"),
                snmp_v3_priv_pass_env=dev_cfg.get("snmp_v3_priv_pass_env"),
                nms_host=dev_cfg.get("nms_host"),
                nms_port=dev_cfg.get("nms_port"),
                nms_api_key_env=dev_cfg.get("nms_api_key_env"),
                nms_device_ref=dev_cfg.get("nms_device_ref"),
                poll_enabled=dev_cfg.get(
                    "poll_enabled",
                    bool(str(dev_cfg.get("mgmt_host") or "").strip()),
                ),
            )
        except ValueError as exc:
            logger.error("Device %r config invalid, skipping: %s", dev_cfg["name"], exc)
            continue
        logger.info("Added device %r (id=%d, vendor=%s) at site %r",
                     dev_cfg["name"], device_id, dev_cfg["vendor"], dev_cfg["site"])

    _bootstrap_trunks_and_signals(db, config, existing_sites)
    _bootstrap_ports_and_flows(db, config, existing_sites, existing_cities)
    _bootstrap_device_secrets(config)


def _bootstrap_trunks_and_signals(db: Database, config: dict, existing_sites: dict) -> None:
    """Idempotently insert trunks and signals from config.json.
    Matched on trunk label and on (device, source_label, destination_label).
    Does not update existing rows - same rule as devices, so a typo cannot
    overwrite live status written by a later poller/phase."""
    existing_devices = {d.name: d for d in db.list_devices(include_decommissioned=True)}

    for trunk_cfg in config.get("trunks", []):
        label = trunk_cfg.get("label")
        if not label:
            logger.error("Trunk config missing label - skipping: %s", trunk_cfg)
            continue
        if db.get_trunk_by_label(label) is not None:
            continue
        site_a_id = existing_sites.get(trunk_cfg.get("site_a"))
        site_b_id = existing_sites.get(trunk_cfg.get("site_b"))
        if site_a_id is None or site_b_id is None:
            logger.error(
                "Trunk %r references unknown site %r / %r - skipping.",
                label, trunk_cfg.get("site_a"), trunk_cfg.get("site_b"),
            )
            continue
        try:
            trunk_id = db.add_trunk(site_a_id, site_b_id, label)
        except ValueError as exc:
            logger.error("Trunk %r invalid, skipping: %s", label, exc)
            continue
        logger.info("Added trunk %r (id=%d)", label, trunk_id)

    for sig_cfg in config.get("signals", []):
        device = existing_devices.get(sig_cfg.get("device"))
        if device is None:
            logger.error(
                "Signal %r -> %r references unknown device %r - skipping.",
                sig_cfg.get("source_label"), sig_cfg.get("destination_label"),
                sig_cfg.get("device"),
            )
            continue
        source = sig_cfg.get("source_label")
        dest = sig_cfg.get("destination_label")
        if not source or not dest:
            logger.error("Signal config missing source_label/destination_label - skipping: %s", sig_cfg)
            continue
        if db.find_signal(device.id, source, dest) is not None:
            continue
        trunk_id = None
        trunk_label = sig_cfg.get("trunk")
        if trunk_label:
            trunk = db.get_trunk_by_label(trunk_label)
            if trunk is None:
                logger.error(
                    "Signal %r -> %r references unknown trunk %r - skipping.",
                    source, dest, trunk_label,
                )
                continue
            trunk_id = trunk["id"]
        try:
            signal_id = db.add_signal(
                device_id=device.id,
                source_label=source,
                destination_label=dest,
                trunk_id=trunk_id,
                direction=sig_cfg.get("direction", ""),
                status=sig_cfg.get("status", "unknown"),
            )
        except ValueError as exc:
            logger.error("Signal %r -> %r invalid, skipping: %s", source, dest, exc)
            continue
        logger.info("Added signal %r -> %r (id=%d)", source, dest, signal_id)


def _ensure_city(db: Database, name: str, lat=None, lng=None) -> Optional[int]:
    if not name:
        return None
    existing = db.get_city_by_name(name)
    if existing is not None:
        return existing["id"]
    city_id = db.add_city(name, lat=lat, lng=lng)
    logger.info("Added city %r (id=%d)", name, city_id)
    return city_id


def _bootstrap_cities(db: Database, config: dict) -> dict:
    """Cities are first-class: many sites can share one. Explicit `cities`
    in config win; otherwise unique site.city strings are created."""
    existing = {row["name"]: row["id"] for row in db.list_cities()}
    for city_cfg in config.get("cities", []):
        name = city_cfg.get("name")
        if not name:
            continue
        if name in existing:
            continue
        existing[name] = _ensure_city(
            db, name, lat=city_cfg.get("lat"), lng=city_cfg.get("lng"),
        )
    for site_cfg in config.get("sites", []):
        name = site_cfg.get("city") or ""
        if name and name not in existing:
            existing[name] = _ensure_city(
                db, name, lat=site_cfg.get("lat"), lng=site_cfg.get("lng"),
            )
    return existing


def _bootstrap_device_secrets(config: dict) -> None:
    """Copy api_username / api_password from config.json into the env file.

    Values stay on the device records in config (and later in SQL). The
    poller still resolves them through env names. Never logs the values.
    """
    updates: dict[str, str] = {}
    for dev_cfg in config.get("devices", []):
        user_env = dev_cfg.get("api_username_env")
        pass_env = dev_cfg.get("api_password_env")
        if user_env and dev_cfg.get("api_username"):
            updates[user_env] = str(dev_cfg["api_username"])
        if pass_env and dev_cfg.get("api_password"):
            updates[pass_env] = str(dev_cfg["api_password"])
    if not updates:
        return
    upsert_values(default_env_path(), updates)
    logger.info("Loaded %d credential values from config.json into env file", len(updates))


def _bootstrap_site_aliases(db: Database, config: dict, existing_sites: dict) -> None:
    """Rename or merge leftover site names onto the canonical config names."""
    wanted = {site.get("name") for site in config.get("sites", []) if site.get("name")}
    for old_name, new_name in SITE_ALIASES.items():
        if new_name not in wanted:
            continue
        old_id = existing_sites.get(old_name)
        if old_id is None:
            continue
        new_id = existing_sites.get(new_name)
        if new_id is None:
            db.update_site(old_id, name=new_name)
            existing_sites[new_name] = old_id
            del existing_sites[old_name]
            logger.info("Renamed site %r -> %r", old_name, new_name)
            continue
        if new_id == old_id:
            continue
        db.merge_sites(old_id, new_id)
        del existing_sites[old_name]
        logger.info("Merged site %r into %r", old_name, new_name)


def _guess_port_kind(name: str) -> str:
    lower = (name or "").lower()
    if "sdi" in lower or "in" == lower or lower.startswith("in-"):
        if "out" in lower:
            return "sdi_out"
        return "sdi_in"
    if any(token in lower for token in ("hevc", "enc", "10g", "nic", "eth", "sfp")):
        return "net"
    if "mgmt" in lower or "management" in lower:
        return "mgmt"
    if "out" in lower:
        return "sdi_out"
    return "other"


def _ensure_port(db: Database, device_id: int, name: str, kind: str = "", slot: str = "") -> int:
    existing = db.find_port(device_id, name)
    if existing is not None:
        return existing["id"]
    return db.add_port(device_id, name, kind=kind or _guess_port_kind(name), slot=slot)


def _bootstrap_ports_and_flows(db: Database, config: dict, existing_sites: dict,
                               existing_cities: Optional[dict] = None) -> None:
    """Idempotently insert ports and flows. Explicit `flows` in config are
    the inventory of record. If config has no flows key, derive one flow
    per legacy signal so an existing trunks/signals config still draws
    directed site-to-site hops (including fan-out once rewritten as flows)."""
    existing_devices = {d.name: d for d in db.list_devices(include_decommissioned=True)}

    for port_cfg in config.get("ports", []):
        device = existing_devices.get(port_cfg.get("device"))
        if device is None:
            logger.error("Port %r references unknown device %r - skipping.",
                         port_cfg.get("name"), port_cfg.get("device"))
            continue
        name = port_cfg.get("name")
        if not name:
            logger.error("Port config missing name - skipping: %s", port_cfg)
            continue
        if db.find_port(device.id, name) is not None:
            continue
        try:
            port_id = db.add_port(
                device.id,
                name,
                kind=port_cfg.get("kind") or _guess_port_kind(name),
                slot=port_cfg.get("slot", ""),
            )
        except ValueError as exc:
            logger.error("Port %r on %r invalid, skipping: %s", name, device.name, exc)
            continue
        logger.info("Added port %r on %s (id=%d)", name, device.name, port_id)

    for dev_cfg in config.get("devices", []):
        device = existing_devices.get(dev_cfg.get("name"))
        if device is None:
            continue
        for port_cfg in dev_cfg.get("ports", []):
            name = port_cfg.get("name") if isinstance(port_cfg, dict) else str(port_cfg)
            if not name:
                continue
            kind = port_cfg.get("kind", "") if isinstance(port_cfg, dict) else ""
            slot = port_cfg.get("slot", "") if isinstance(port_cfg, dict) else ""
            if db.find_port(device.id, name) is None:
                try:
                    _ensure_port(db, device.id, name, kind=kind, slot=slot)
                    logger.info("Added port %r on %s", name, device.name)
                except ValueError as exc:
                    logger.error("Port %r on %r invalid, skipping: %s", name, device.name, exc)

    cities = existing_cities or {row["name"]: row["id"] for row in db.list_cities()}
    if "flows" in config:
        for flow_cfg in config.get("flows") or []:
            _insert_flow_from_config(db, flow_cfg, existing_devices, existing_sites, cities)
    else:
        _synthesize_flows_from_signals(db, existing_devices, cities)


def _city_id_for_site(db: Database, site_id: Optional[int], cities: dict) -> Optional[int]:
    if site_id is None:
        return None
    site = db.get_site(site_id)
    if site is None:
        return None
    if site["city_id"]:
        return site["city_id"]
    return cities.get(site["city"] or "")


def _insert_flow_from_config(db: Database, flow_cfg: dict, existing_devices: dict,
                             existing_sites: dict, existing_cities: dict) -> None:
    source = existing_devices.get(flow_cfg.get("source_device"))
    if source is None:
        logger.error("Flow %r references unknown source_device %r - skipping.",
                     flow_cfg.get("label") or flow_cfg.get("signal"), flow_cfg.get("source_device"))
        return
    dest_device = None
    dest_name = flow_cfg.get("dest_device")
    if dest_name:
        dest_device = existing_devices.get(dest_name)
        if dest_device is None:
            logger.error("Flow %r references unknown dest_device %r - skipping.",
                         flow_cfg.get("label"), dest_name)
            return
    dest_site_id = existing_sites.get(flow_cfg.get("dest_site")) if flow_cfg.get("dest_site") else None
    if dest_site_id is None and dest_device is not None:
        dest_site_id = dest_device.site_id
    dest_city_id = existing_cities.get(flow_cfg.get("dest_city")) if flow_cfg.get("dest_city") else None
    if dest_city_id is None:
        dest_city_id = _city_id_for_site(db, dest_site_id, existing_cities)
    dest_label = flow_cfg.get("dest_label") or ""
    signal_label = flow_cfg.get("signal") or flow_cfg.get("signal_label") or ""
    dest_hint = flow_cfg.get("dest_city") or flow_cfg.get("dest_site") or dest_name or dest_label
    label = flow_cfg.get("label") or f"{signal_label or source.name} → {dest_hint}"
    dest_device_id = dest_device.id if dest_device else None
    if db.find_flow(source.id, label, dest_site_id, dest_device_id, dest_label,
                    dest_city_id=dest_city_id) is not None:
        return
    source_port_id = None
    port_name = flow_cfg.get("source_port")
    if port_name:
        source_port_id = _ensure_port(
            db, source.id, port_name,
            kind=flow_cfg.get("source_port_kind", ""),
        )
    dest_port_id = None
    dest_port_name = flow_cfg.get("dest_port")
    if dest_port_name and dest_device is not None:
        dest_port_id = _ensure_port(
            db, dest_device.id, dest_port_name,
            kind=flow_cfg.get("dest_port_kind", ""),
        )
    origin_device_id = None
    origin_port_id = None
    origin_name = flow_cfg.get("origin_device")
    if origin_name:
        origin = existing_devices.get(origin_name)
        if origin is None:
            logger.error("Flow %r references unknown origin_device %r - skipping origin.",
                         label, origin_name)
        else:
            origin_device_id = origin.id
            origin_port = flow_cfg.get("origin_port")
            if origin_port:
                origin_port_id = _ensure_port(db, origin.id, origin_port)
    try:
        flow_id = db.add_flow(
            label=label,
            source_device_id=source.id,
            source_port_id=source_port_id,
            dest_site_id=dest_site_id,
            dest_device_id=dest_device_id,
            dest_port_id=dest_port_id,
            dest_label=dest_label,
            direction=flow_cfg.get("direction", ""),
            status=flow_cfg.get("status", "unknown"),
            signal_label=signal_label,
            dest_city_id=dest_city_id,
            origin_device_id=origin_device_id,
            origin_port_id=origin_port_id,
        )
    except ValueError as exc:
        logger.error("Flow %r invalid, skipping: %s", label, exc)
        return
    logger.info("Added flow %r (id=%d)", label, flow_id)


def _synthesize_flows_from_signals(db: Database, existing_devices: dict,
                                  existing_cities: Optional[dict] = None) -> None:
    """One flow per legacy signal. Dest site is the trunk's other end so
    the map can draw a directed hop without a flows block in config."""
    trunks = {row["id"]: row for row in db.list_trunks()}
    for sig in db.list_signals():
        dest_site_id = None
        trunk = trunks.get(sig["trunk_id"]) if sig["trunk_id"] else None
        if trunk is not None:
            dest_site_id = (
                trunk["site_b_id"] if trunk["site_a_id"] == sig["site_id"]
                else trunk["site_a_id"]
            )
        dest_label = sig["destination_label"] or ""
        dest_device_id = None
        maybe_dest = dest_label.split(" / ")[0].strip()
        dest_dev = existing_devices.get(maybe_dest)
        if dest_dev is not None:
            dest_device_id = dest_dev.id
            if dest_site_id is None:
                dest_site_id = dest_dev.site_id
        cities = existing_cities or {row["name"]: row["id"] for row in db.list_cities()}
        dest_city_id = _city_id_for_site(db, dest_site_id, cities)
        label = f"{sig['source_label']} → {dest_label}"
        if db.find_flow(sig["device_id"], label, dest_site_id, dest_device_id, dest_label,
                        dest_city_id=dest_city_id) is not None:
            continue
        source_port_id = None
        source = sig["source_label"] or ""
        port_name = source.split(" / ")[-1].strip() if " / " in source else source
        if port_name:
            source_port_id = _ensure_port(db, sig["device_id"], port_name)
        try:
            flow_id = db.add_flow(
                label=label,
                source_device_id=sig["device_id"],
                source_port_id=source_port_id,
                dest_site_id=dest_site_id,
                dest_device_id=dest_device_id,
                dest_label=dest_label,
                direction=sig["direction"] or "",
                status=sig["status"] or "unknown",
                signal_label=port_name,
                dest_city_id=dest_city_id,
            )
        except ValueError as exc:
            logger.error("Synthesized flow from signal %s invalid, skipping: %s", sig["id"], exc)
            continue
        logger.info("Derived flow %r from signal %d (id=%d)", label, sig["id"], flow_id)


def resolve_env(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    value = os.environ.get(name)
    if value:
        return value
    value = get_value(default_env_path(), name)
    if not value:
        logger.warning("Env var %s is not set", name)
    return value


def snmp_target_for(device: Device) -> Optional[SnmpTarget]:
    """Build a GET target when SNMP is enabled or this box is SNMP-primary."""
    use = device.snmp_enabled or device.access_mode == "direct_snmp"
    if not use:
        return None
    host = (device.snmp_host or device.mgmt_host or "").strip()
    if not host:
        return None
    version = device.snmp_version or "2c"
    if version == "3":
        target = SnmpTarget(
            host=host,
            port=device.snmp_port or 161,
            version="3",
            v3_user=resolve_env(device.snmp_v3_user_env),
            v3_sec_level=device.snmp_v3_sec_level or "authPriv",
            v3_auth_proto=device.snmp_v3_auth_proto or "SHA",
            v3_auth_pass=resolve_env(device.snmp_v3_auth_pass_env),
            v3_priv_proto=device.snmp_v3_priv_proto or "AES",
            v3_priv_pass=resolve_env(device.snmp_v3_priv_pass_env),
        )
    else:
        target = SnmpTarget(
            host=host,
            port=device.snmp_port or 161,
            version=version,
            community=resolve_env(device.snmp_community_env),
        )
    if not target.configured():
        return None
    return target


def build_driver(db: Database, device: Device) -> Driver:
    """Resolve and construct the right driver for a device (vendor + model +
    firmware_version, or an explicit driver_override), resolving credentials
    from the environment variables named in its DB row. Records which
    driver_id was resolved back onto the device row (informational).

    Raises DriverResolutionError if no driver matches - see
    drivers/base.py:resolve_driver() for the matching rules."""
    driver_cls = resolve_driver(
        DRIVER_REGISTRY,
        vendor=device.vendor,
        model=device.model,
        firmware_version=device.firmware_version,
        driver_override=device.driver_override,
    )
    db.set_resolved_driver(device.id, driver_cls.driver_id)

    if device.vendor in ("net_insight", "generic_snmp"):
        target = snmp_target_for(device)
        return driver_cls(
            host=device.snmp_host or device.mgmt_host,
            snmp_community=resolve_env(device.snmp_community_env),
            snmp_port=device.snmp_port,
            access_mode=device.access_mode,
            snmp_target=target,
        )

    # appear / haivision - both HTTP/JSON with username+password. If a
    # future vendor's driver needs different constructor args, branch here
    # (or better: give drivers a uniform from_device(device, creds)
    # classmethod once there's a second HTTP-shaped vendor with different
    # needs - not worth the indirection for two vendors today).
    return driver_cls(
        host=device.mgmt_host,
        port=device.api_port,
        scheme=device.api_scheme,
        username=resolve_env(device.api_username_env),
        password=resolve_env(device.api_password_env),
        verify_tls=device.api_verify_tls,
    )


# Track consecutive failures in memory - resets on process restart, which is
# fine: a restart is a reasonable point to give every device a clean slate.
_consecutive_failures: dict[int, int] = {}


def _poll_method_for(device: Device) -> str:
    return {"direct_api": "api", "direct_snmp": "snmp", "via_nms": "nms"}[device.access_mode]


async def poll_device(db: Database, device: Device) -> None:
    if not device.poll_enabled:
        return
    if not (device.mgmt_host or "").strip() and not (device.snmp_host or "").strip():
        logger.info("Skipping %r — no management IP (edit in the portal)", device.name)
        return

    method = _poll_method_for(device)
    loop = asyncio.get_running_loop()
    has_api = device.access_mode in ("direct_api", "via_nms")
    snmp_target = snmp_target_for(device)

    api_ok: Optional[bool] = None
    snmp_ok: Optional[bool] = None
    driver = None
    snapshot = None

    if has_api:
        try:
            driver = build_driver(db, device)
        except DriverResolutionError as exc:
            logger.error("Cannot poll device %r: %s", device.name, exc)
            db.record_poll(device.id, method=method, success=False, error_message=str(exc))
            api_ok = False
        except NotImplementedError as exc:
            logger.error("Device %r: %s", device.name, exc)
            db.record_poll(device.id, method=method, success=False, error_message=str(exc))
            api_ok = False
        else:
            try:
                api_ok = await loop.run_in_executor(None, driver.ping)
            except NotImplementedError as exc:
                logger.error("Device %r: %s", device.name, exc)
                db.record_poll(device.id, method=method, success=False, error_message=str(exc))
                api_ok = False
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected error polling device %r via API", device.name)
                db.record_poll(device.id, method=method, success=False, error_message=str(exc))
                api_ok = False
            else:
                db.record_poll(device.id, method=method, success=bool(api_ok),
                                error_message=None if api_ok else "API ping failed")
                if api_ok:
                    try:
                        snapshot = await loop.run_in_executor(None, driver.collect)
                    except Exception:  # noqa: BLE001
                        logger.exception("collect() failed for device %r after successful ping", device.name)

    if device.access_mode == "direct_snmp" and not has_api:
        try:
            driver = build_driver(db, device)
            snmp_ok = await loop.run_in_executor(None, driver.ping)
        except DriverResolutionError as exc:
            logger.error("Cannot poll device %r: %s", device.name, exc)
            db.record_poll(device.id, method="snmp", success=False, error_message=str(exc))
            snmp_ok = False
        except NotImplementedError as exc:
            logger.error("Device %r: %s", device.name, exc)
            db.record_poll(device.id, method="snmp", success=False, error_message=str(exc))
            snmp_ok = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error polling device %r via SNMP", device.name)
            db.record_poll(device.id, method="snmp", success=False, error_message=str(exc))
            snmp_ok = False
        else:
            db.record_poll(device.id, method="snmp", success=bool(snmp_ok),
                            error_message=None if snmp_ok else "SNMP ping failed")
    elif snmp_target is not None:
        try:
            snmp_ok = await loop.run_in_executor(None, lambda: snmp_ping(target=snmp_target, host=snmp_target.host))
        except Exception as exc:  # noqa: BLE001
            logger.exception("SNMP GET failed for device %r", device.name)
            snmp_ok = False
            db.record_poll(device.id, method="snmp", success=False, error_message=str(exc))
        else:
            db.record_poll(device.id, method="snmp", success=bool(snmp_ok),
                            error_message=None if snmp_ok else "SNMP ping failed")

    if api_ok is None and snmp_ok is None:
        return

    both_fail = (api_ok is False and snmp_ok is not True) or (snmp_ok is False and api_ok is not True)
    if api_ok is True and snmp_ok is False:
        _consecutive_failures[device.id] = 0
        db.set_device_status(device.id, "degraded", error="API up, SNMP GET failed")
        if snapshot is not None:
            _apply_snapshot(db, device, snapshot)
        return
    if snmp_ok is True and api_ok is False:
        _consecutive_failures[device.id] = 0
        db.set_device_status(device.id, "degraded", error="SNMP up, API ping failed")
        return
    if both_fail and api_ok is not True and snmp_ok is not True:
        _handle_poll_result(db, device, success=False)
        return
    _handle_poll_result(db, device, success=True, snapshot=snapshot)


def _apply_snapshot(db: Database, device: Device, snapshot: CollectResult) -> None:
    if snapshot.firmware_version and snapshot.firmware_version != device.firmware_version:
        db.set_device_firmware(device.id, snapshot.firmware_version)
    for item in snapshot.modules:
        db.upsert_module(
            device.id,
            slot=item.slot,
            module_type=item.module_type,
            firmware_version=item.firmware_version,
            serial=item.serial,
            status=item.status,
        )


def _handle_poll_result(db: Database, device: Device, success: bool,
                        snapshot: Optional[CollectResult] = None) -> None:
    if success:
        _consecutive_failures[device.id] = 0
        if snapshot is not None:
            _apply_snapshot(db, device, snapshot)
            status = snapshot.device_status if snapshot.device_status in ("healthy", "degraded") else "healthy"
            if device.status != status:
                logger.info("Device %r -> %s", device.name, status)
            db.set_device_status(device.id, status, error=snapshot.error)
            return
        if device.status != "healthy":
            logger.info("Device %r recovered -> healthy", device.name)
        db.set_device_status(device.id, "healthy")
        return

    _consecutive_failures[device.id] = _consecutive_failures.get(device.id, 0) + 1
    misses = _consecutive_failures[device.id]
    if misses >= CONSECUTIVE_FAILURES_THRESHOLD:
        if device.status != "unreachable":
            logger.warning("Device %r unreachable after %d consecutive failed polls", device.name, misses)
        db.set_device_status(device.id, "unreachable", error=f"{misses} consecutive failed polls")
    else:
        logger.debug("Device %r poll failed (%d/%d before marking unreachable)",
                     device.name, misses, CONSECUTIVE_FAILURES_THRESHOLD)


async def poll_loop(db: Database, interval_seconds: int) -> None:
    logger.info("Starting poll loop, interval=%ds", interval_seconds)
    while True:
        devices = db.list_devices()
        if not devices:
            logger.warning("No devices in database - nothing to poll. Run bootstrap first.")
        else:
            await asyncio.gather(*(poll_device(db, d) for d in devices))
        await asyncio.sleep(interval_seconds)


def run_discovery(db: Database, device_name: str) -> None:
    devices = {d.name: d for d in db.list_devices(include_decommissioned=True)}
    device = devices.get(device_name)
    if device is None:
        print(f"No device named {device_name!r} in the database. Known devices: {sorted(devices)}", file=sys.stderr)
        sys.exit(1)
    try:
        driver = build_driver(db, device)
    except DriverResolutionError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    print(f"Probing candidate API paths against {device.name} "
          f"({device.vendor}, driver={driver.driver_id}, {device.mgmt_host})...\n")
    try:
        results = driver.discover()
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    for result in results:
        marker = "OK " if result.ok else "-- "
        if result.error:
            print(f"{marker}{result.path:24s} ERROR: {result.error}")
        else:
            print(f"{marker}{result.path:24s} HTTP {result.status_code}  {result.content_type or ''}")
            if result.ok and result.body_preview:
                print(f"      {result.body_preview!r}")
    print("\nPaths marked OK responded with a non-error HTTP status - inspect the "
          "body to confirm it's actually a usable API endpoint (vs. a generic "
          "web UI page) before wiring it into the poller.")


def main() -> None:
    parser = argparse.ArgumentParser(description="NexNOC - Phase 1 poller/bootstrap")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--db", default="noc.db", help="Path to SQLite DB file")
    parser.add_argument("--bootstrap-only", action="store_true", help="Sync config into DB and exit")
    parser.add_argument("--discover", metavar="DEVICE_NAME", help="Probe candidate API paths against one device and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    db = Database(args.db)
    db.initialize()

    config = load_config(args.config)
    bootstrap(db, config)

    if args.discover:
        run_discovery(db, args.discover)
        return

    if args.bootstrap_only:
        logger.info("Bootstrap complete, exiting (--bootstrap-only)")
        return

    interval = config.get("poll_interval_seconds", 30)
    try:
        asyncio.run(poll_loop(db, interval))
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl-C)")


if __name__ == "__main__":
    main()
