"""Write API for cities / sites / devices / ports / flows.

Device credentials are stored on the device row. Secret values are never
returned in responses — only set/ready flags (and api_username / SNMP
user, which are logins rather than passwords).
"""
from __future__ import annotations

import base64
import csv
import io
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from db import (
    DEVICE_SECRET_COLUMNS,
    UNASSIGNED_CITY_NAME,
    UNASSIGNED_SITE_NAME,
    VALID_ACCESS_MODES,
    VALID_VENDORS,
    Database,
)
from geocode import GeocodeError, geocode, geocode_or_none
from pins import DEFAULT_PIN_COLOR, DEFAULT_PIN_ICON, valid_pin_color, valid_pin_icon

SECRET_KEYS = {
    "api_username", "api_password", "snmp_community",
    "snmp_v3_user", "snmp_v3_auth_pass", "snmp_v3_priv_pass", "nms_api_key",
}

_PIN_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_PIN_EXT = {".png", ".svg", ".jpg", ".jpeg", ".webp"}
_PIN_MAX = 200_000


def default_pin_dir() -> Path:
    env = os.environ.get("NEXNOC_PIN_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "uploads" / "pins"


def _opt_int(value):
    if value is None or value == "":
        return None
    return int(value)


def _opt_float(value):
    if value is None or value == "":
        return None
    return float(value)


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _bulk_ids(body: dict) -> list[int]:
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw:
        raise ValueError("ids must be a non-empty list")
    ids = []
    for item in raw:
        try:
            ids.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid id {item!r}") from exc
    return ids


def _filled(value) -> bool:
    return bool(value and str(value).strip())


def device_secret_flags(device, env_path: Path | None = None) -> dict:
    return {
        "api_username": device.api_username or "",
        "snmp_v3_user": device.snmp_v3_user or "",
        "api_username_set": _filled(device.api_username),
        "api_password_set": _filled(device.api_password),
        "snmp_community_set": _filled(device.snmp_community),
        "snmp_v3_user_set": _filled(device.snmp_v3_user),
        "snmp_v3_auth_set": _filled(device.snmp_v3_auth_pass),
        "snmp_v3_priv_set": _filled(device.snmp_v3_priv_pass),
        "credentials_ready": _filled(device.api_username) and _filled(device.api_password),
        "snmp_ready": _filled(device.snmp_community) or _filled(device.snmp_v3_user),
    }


def _fill_geo(query: str, kind: str, lat, lng, geo_source: str) -> tuple:
    """If coords missing, geocode. Explicit coords default to manual."""
    geo_source = (geo_source or "").strip()
    if lat is not None and lng is not None:
        return lat, lng, geo_source or "manual"
    if not query:
        return lat, lng, geo_source
    hit = geocode_or_none(query, kind=kind)
    if not hit:
        return lat, lng, geo_source
    return hit["lat"], hit["lng"], "geocode"


def _same_coord(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) < 1e-6


def _operator_moved_pin(body: dict, existing) -> bool:
    """True when the PATCH includes lat/lng that differ from the stored pin."""
    if "lat" not in body and "lng" not in body:
        return False
    new_lat = _opt_float(body["lat"]) if "lat" in body else existing["lat"]
    new_lng = _opt_float(body["lng"]) if "lng" in body else existing["lng"]
    return not (
        _same_coord(new_lat, existing["lat"])
        and _same_coord(new_lng, existing["lng"])
    )


def stamp_device_connectors(db: Database, device) -> int:
    """Create BNC/SDI rows from the resolved driver when the device has none."""
    from drivers.base import DriverResolutionError, kind_for_connector, resolve_driver
    from drivers.registry import DRIVER_REGISTRY

    if db.list_ports(device.id):
        return 0
    try:
        cls = resolve_driver(
            DRIVER_REGISTRY,
            device.vendor,
            device.model,
            device.firmware_version,
            device.driver_override,
        )
    except DriverResolutionError:
        return 0
    added = 0
    for spec in cls.connectors or ():
        direction = "unused" if spec.capability == "assignable" else spec.capability
        kind = kind_for_connector(spec.capability, direction, spec.kind)
        db.add_port(
            device.id, spec.name, kind=kind,
            capability=spec.capability, direction=direction,
        )
        added += 1
    return added


def _port_is_output(port) -> bool:
    if port is None:
        return False
    return (
        (port["capability"] or "") == "output"
        or (port["direction"] or "") == "output"
        or (port["kind"] or "") == "sdi_out"
    )


def source_port_direction(db: Database, source_port_id: Optional[int],
                          origin_device_id: Optional[int]) -> str:
    """Encode hops use an input; forwarding/distribution hops keep an output."""
    if origin_device_id:
        return "output"
    if source_port_id is None:
        return "input"
    return "output" if _port_is_output(db.get_port(source_port_id)) else "input"


def check_port_direction(db: Database, port_id: Optional[int], direction: str) -> None:
    """Raise if a fixed port cannot take this direction. Does not write."""
    if port_id is None:
        return
    port = db.get_port(port_id)
    if port is None:
        return
    cap = port["capability"] or ""
    if cap in ("input", "output") and direction != cap:
        raise ValueError(f"{port['name']} is a fixed {cap} and cannot be {direction}")


def apply_port_direction(db: Database, port_id: Optional[int], direction: str) -> None:
    if port_id is None:
        return
    port = db.get_port(port_id)
    if port is None:
        return
    check_port_direction(db, port_id, direction)
    cap = port["capability"] or ""
    if cap == "assignable" or (not cap and port["kind"] in ("other", "sdi_in", "sdi_out")):
        family = "net" if port["kind"] == "net" else "mgmt" if port["kind"] == "mgmt" else "sdi"
        from drivers.base import kind_for_connector
        kind = kind_for_connector(cap or direction, direction, family)
        db.update_port(port_id, direction=direction, kind=kind, capability=cap or "assignable")


def release_port_if_unused(db: Database, port_id: Optional[int],
                           except_flow_id: Optional[int] = None) -> None:
    """Return an assignable BNC to unused when no remaining flow uses it."""
    if port_id is None:
        return
    if db.count_flows_for_port(port_id, except_flow_id=except_flow_id):
        return
    port = db.get_port(port_id)
    if port is None:
        return
    if (port["capability"] or "") in ("input", "output"):
        return
    apply_port_direction(db, port_id, "unused")


def _is_holding(name) -> bool:
    return (name or "").strip().lower() == UNASSIGNED_SITE_NAME.lower()


def _refuse_holding_change(row, body: Optional[dict] = None, *, deleting: bool = False) -> None:
    if not _is_holding(row["name"]):
        return
    if deleting:
        raise ValueError("Unassigned is the import holding bin and cannot be deleted")
    if body and "name" in body and (body.get("name") or "").strip() != row["name"]:
        raise ValueError("Unassigned cannot be renamed")


def _city_payload(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {
        "id": row["id"],
        "name": row["name"],
        "lat": row["lat"],
        "lng": row["lng"],
        "geo_source": row["geo_source"] if "geo_source" in keys else "",
        "notes": row["notes"] or "",
        "holding": _is_holding(row["name"]),
    }


def _site_payload(row) -> dict:
    data = dict(row)
    data.setdefault("address", "")
    data.setdefault("geo_source", "")
    data.setdefault("pin_icon", DEFAULT_PIN_ICON)
    data.setdefault("pin_color", DEFAULT_PIN_COLOR)
    data.setdefault("pin_upload", None)
    data["holding"] = _is_holding(data.get("name"))
    return data


def _handle_cities(db: Database, method: str, item_id: Optional[int], body: dict):
    if method == "POST":
        if not body.get("name"):
            raise ValueError("city name is required")
        lat, lng, geo_source = _fill_geo(
            body["name"], "city",
            _opt_float(body.get("lat")), _opt_float(body.get("lng")),
            body.get("geo_source") or "",
        )
        city_id = db.add_city(
            name=body["name"],
            lat=lat,
            lng=lng,
            notes=body.get("notes") or "",
            geo_source=geo_source,
        )
        return 201, {"city": _city_payload(db.get_city(city_id))}
    if item_id is None:
        raise LookupError("city id required")
    if db.get_city(item_id) is None:
        raise LookupError("city not found")
    if method == "PATCH":
        existing = db.get_city(item_id)
        _refuse_holding_change(existing, body)
        fields = {}
        for key in ("name", "notes", "geo_source"):
            if key in body:
                fields[key] = body[key]
        if "lat" in body:
            fields["lat"] = _opt_float(body["lat"])
        if "lng" in body:
            fields["lng"] = _opt_float(body["lng"])
        if "lat" in fields and "lng" in fields and fields.get("geo_source") is None:
            existing = db.get_city(item_id)
            if existing and existing["geo_source"] != "manual":
                fields["geo_source"] = "manual"
        db.update_city(item_id, **fields)
        return 200, {"city": _city_payload(db.get_city(item_id))}
    if method == "DELETE":
        _refuse_holding_change(db.get_city(item_id), deleting=True)
        db.delete_city(item_id)
        return 200, {"deleted": item_id}
    raise LookupError("not found")


def _pin_fields(body: dict) -> dict:
    fields = {}
    if "pin_icon" in body:
        icon = body["pin_icon"] or DEFAULT_PIN_ICON
        if not valid_pin_icon(icon):
            raise ValueError(f"unknown pin_icon {icon!r}")
        fields["pin_icon"] = icon
    if "pin_color" in body:
        color = body["pin_color"] or DEFAULT_PIN_COLOR
        if not valid_pin_color(color):
            raise ValueError(f"invalid pin_color {color!r}")
        fields["pin_color"] = color
    if "pin_upload" in body:
        fields["pin_upload"] = body["pin_upload"] or None
    return fields


def _handle_sites(db: Database, method: str, item_id: Optional[int], body: dict):
    if method == "POST":
        if not body.get("name"):
            raise ValueError("site name is required")
        address = body.get("address") or ""
        query = address or body["name"]
        kind = "address" if address else "city"
        lat, lng, geo_source = _fill_geo(
            query, kind,
            _opt_float(body.get("lat")), _opt_float(body.get("lng")),
            body.get("geo_source") or "",
        )
        pins = _pin_fields(body)
        site_id = db.add_site(
            name=body["name"],
            city=body.get("city") or "",
            city_id=_opt_int(body.get("city_id")),
            address=address,
            lat=lat,
            lng=lng,
            notes=body.get("notes") or "",
            geo_source=geo_source,
            pin_icon=pins.get("pin_icon", DEFAULT_PIN_ICON),
            pin_color=pins.get("pin_color", DEFAULT_PIN_COLOR),
            pin_upload=pins.get("pin_upload"),
        )
        return 201, {"site": _site_payload(db.get_site(site_id))}
    if item_id is None:
        raise LookupError("site id required")
    existing = db.get_site(item_id)
    if existing is None:
        raise LookupError("site not found")
    if method == "PATCH":
        _refuse_holding_change(existing, body)
        fields = {}
        for key in ("name", "city", "notes", "address", "geo_source"):
            if key in body:
                fields[key] = body[key]
        if "city_id" in body:
            fields["city_id"] = _opt_int(body["city_id"])
        if "lat" in body:
            fields["lat"] = _opt_float(body["lat"])
        if "lng" in body:
            fields["lng"] = _opt_float(body["lng"])
        new_address = (body["address"] if "address" in body else existing["address"]) or ""
        address_changed = "address" in body and new_address.strip() != (existing["address"] or "").strip()
        # A new address relocates the pin unless the operator also typed lat/lng.
        if address_changed and new_address.strip() and not _operator_moved_pin(body, existing):
            hit = geocode_or_none(new_address, kind="address")
            if hit:
                fields["lat"] = hit["lat"]
                fields["lng"] = hit["lng"]
                fields["geo_source"] = "geocode"
        fields.update(_pin_fields(body))
        db.update_site(item_id, **fields)
        return 200, {"site": _site_payload(db.get_site(item_id))}
    if method == "DELETE":
        _refuse_holding_change(existing, deleting=True)
        try:
            db.delete_site(item_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Move or delete this site's devices (and trunks) first."
            ) from exc
        return 200, {"deleted": item_id}
    raise LookupError("not found")


def save_site_pin(db: Database, site_id: int, body: dict,
                  pin_dir: Optional[Path] = None) -> dict:
    if db.get_site(site_id) is None:
        raise LookupError("site not found")
    raw_name = (body.get("filename") or "pin.png").split("/")[-1].split("\\")[-1]
    if not _PIN_NAME.match(raw_name) or Path(raw_name).suffix.lower() not in _PIN_EXT:
        raise ValueError("pin filename must be png/svg/jpg/webp")
    b64 = body.get("data") or ""
    if isinstance(b64, str) and "," in b64 and b64.strip().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        blob = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("pin data must be base64") from exc
    if not blob or len(blob) > _PIN_MAX:
        raise ValueError(f"pin must be between 1 and {_PIN_MAX} bytes")
    dest_dir = pin_dir or default_pin_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{site_id}_{raw_name}"
    (dest_dir / stored).write_bytes(blob)
    db.update_site(site_id, pin_icon="upload", pin_upload=stored)
    return {"site": _site_payload(db.get_site(site_id))}


def _device_fields(body: dict) -> dict:
    fields = {}
    for key in (
        "site_id", "name", "vendor", "device_role", "model", "firmware_version",
        "mgmt_host", "access_mode", "driver_override", "control_driver",
        "api_port", "api_scheme", "api_verify_tls",
        "api_username", "api_password",
        "snmp_host", "snmp_port", "snmp_community", "snmp_version",
        "snmp_enabled", "snmp_trap_enabled",
        "snmp_v3_user", "snmp_v3_sec_level", "snmp_v3_auth_proto",
        "snmp_v3_auth_pass", "snmp_v3_priv_proto", "snmp_v3_priv_pass",
        "nms_host", "nms_port", "nms_api_key", "nms_device_ref",
        "poll_enabled",
    ):
        if key not in body:
            continue
        value = body[key]
        if key in {"site_id", "api_port", "snmp_port", "nms_port"}:
            value = _opt_int(value) if key != "site_id" else int(value)
        if key in {"api_verify_tls", "poll_enabled", "snmp_enabled", "snmp_trap_enabled"}:
            value = _as_bool(value)
        if key in DEVICE_SECRET_COLUMNS and (value is None or value == ""):
            continue
        fields[key] = value
    return fields


def _handle_devices(db: Database, env_path: Path, method: str,
                    item_id: Optional[int], body: dict):
    if method == "POST":
        if not body.get("name") or not body.get("vendor") or body.get("site_id") is None:
            raise ValueError("device name, vendor, and site_id are required")
        host = body.get("mgmt_host")
        if host is None:
            host = ""
        poll = body.get("poll_enabled")
        if poll is None:
            poll = bool(str(host).strip())
        device_id = db.add_device(
            site_id=int(body["site_id"]),
            name=body["name"],
            vendor=body["vendor"],
            mgmt_host=host,
            device_role=body.get("device_role") or "",
            model=body.get("model") or "",
            firmware_version=body.get("firmware_version") or "",
            access_mode=body.get("access_mode") or "direct_api",
            driver_override=body.get("driver_override") or None,
            control_driver=body.get("control_driver") or None,
            api_port=int(body.get("api_port") or 443),
            api_scheme=body.get("api_scheme") or "https",
            api_verify_tls=bool(body.get("api_verify_tls", False)),
            api_username=body.get("api_username"),
            api_password=body.get("api_password"),
            snmp_community=body.get("snmp_community"),
            snmp_host=body.get("snmp_host"),
            snmp_port=int(body.get("snmp_port") or 161),
            snmp_version=body.get("snmp_version") or "2c",
            snmp_enabled=body.get("snmp_enabled"),
            snmp_trap_enabled=body.get("snmp_trap_enabled", True),
            snmp_v3_user=body.get("snmp_v3_user"),
            snmp_v3_sec_level=body.get("snmp_v3_sec_level") or "authPriv",
            snmp_v3_auth_proto=body.get("snmp_v3_auth_proto") or "SHA",
            snmp_v3_auth_pass=body.get("snmp_v3_auth_pass"),
            snmp_v3_priv_proto=body.get("snmp_v3_priv_proto") or "AES",
            snmp_v3_priv_pass=body.get("snmp_v3_priv_pass"),
            nms_host=body.get("nms_host"),
            nms_port=_opt_int(body.get("nms_port")),
            nms_api_key=body.get("nms_api_key"),
            nms_device_ref=body.get("nms_device_ref"),
            poll_enabled=bool(poll),
        )
        device = db.get_device(device_id)
        if body.get("snmp_enabled") is not False and any(
            body.get(key) for key in ("snmp_community", "snmp_v3_user")
        ):
            db.update_device(device_id, snmp_enabled=True)
        stamp_device_connectors(db, device)
        device = db.get_device(device_id)
        return 201, {"device": {"id": device.id, "name": device.name, **device_secret_flags(device)}}
    if item_id is None:
        raise LookupError("device id required")
    device = db.get_device(item_id)
    if device is None:
        raise LookupError("device not found")
    if method == "PATCH":
        fields = _device_fields(body)
        if fields:
            db.update_device(item_id, **fields)
        if "snmp_enabled" not in fields and body.get("snmp_enabled") is not False and any(
            body.get(key) for key in ("snmp_community", "snmp_v3_user")
        ):
            db.update_device(item_id, snmp_enabled=True)
        device = db.get_device(item_id)
        return 200, {"device": {"id": device.id, "name": device.name, **device_secret_flags(device)}}
    if method == "DELETE":
        db.remove_device(item_id)
        return 200, {"deleted": item_id}
    raise LookupError("not found")


def _handle_ports(db: Database, method: str, item_id: Optional[int], body: dict):
    if method == "POST":
        if body.get("device_id") is None or not body.get("name"):
            raise ValueError("port device_id and name are required")
        capability = body.get("capability") or ""
        direction = body.get("direction") or ""
        kind = body.get("kind") or "other"
        if capability and not body.get("kind"):
            from drivers.base import kind_for_connector
            kind = kind_for_connector(capability, direction or capability)
        port_id = db.add_port(
            device_id=int(body["device_id"]),
            name=body["name"],
            kind=kind,
            slot=body.get("slot") or "",
            capability=capability,
            direction=direction,
        )
        return 201, {"port": dict(db.get_port(port_id))}
    if item_id is None:
        raise LookupError("port id required")
    if db.get_port(item_id) is None:
        raise LookupError("port not found")
    if method == "PATCH":
        fields = {}
        for key in ("name", "kind", "slot", "status", "capability", "direction"):
            if key in body:
                fields[key] = body[key]
        if "device_id" in body:
            fields["device_id"] = int(body["device_id"])
        implied = fields.get("direction")
        if implied is None and fields.get("kind") == "sdi_out":
            implied = "output"
        elif implied is None and fields.get("kind") == "sdi_in":
            implied = "input"
        if implied is not None:
            check_port_direction(db, item_id, implied)
        if "direction" in fields and "kind" not in fields:
            apply_port_direction(db, item_id, fields["direction"])
            fields.pop("direction", None)
            if not fields:
                return 200, {"port": dict(db.get_port(item_id))}
        db.update_port(item_id, **fields)
        return 200, {"port": dict(db.get_port(item_id))}
    if method == "DELETE":
        db.delete_port(item_id)
        return 200, {"deleted": item_id}
    raise LookupError("not found")


def _handle_flows(db: Database, method: str, item_id: Optional[int], body: dict):
    if method == "POST":
        if not body.get("label") or body.get("source_device_id") is None:
            raise ValueError("flow label and source_device_id are required")
        source_port_id = _opt_int(body.get("source_port_id"))
        dest_port_id = _opt_int(body.get("dest_port_id"))
        origin_device_id = _opt_int(body.get("origin_device_id"))
        src_dir = source_port_direction(db, source_port_id, origin_device_id)
        check_port_direction(db, source_port_id, src_dir)
        check_port_direction(db, dest_port_id, "output")
        flow_id = db.add_flow(
            label=body["label"],
            source_device_id=int(body["source_device_id"]),
            source_port_id=source_port_id,
            dest_site_id=_opt_int(body.get("dest_site_id")),
            dest_device_id=_opt_int(body.get("dest_device_id")),
            dest_port_id=dest_port_id,
            dest_label=body.get("dest_label") or "",
            direction=body.get("direction") or "",
            signal_label=body.get("signal_label") or "",
            dest_city_id=_opt_int(body.get("dest_city_id")),
            origin_device_id=origin_device_id,
            origin_port_id=_opt_int(body.get("origin_port_id")),
        )
        apply_port_direction(db, source_port_id, src_dir)
        apply_port_direction(db, dest_port_id, "output")
        return 201, {"flow": dict(db.get_flow(flow_id))}
    if item_id is None:
        raise LookupError("flow id required")
    existing = db.get_flow(item_id)
    if existing is None:
        raise LookupError("flow not found")
    if method == "PATCH":
        fields = {}
        for key in ("label", "signal_label", "dest_label", "direction", "status"):
            if key in body:
                fields[key] = body[key]
        for key in (
            "source_device_id", "source_port_id", "origin_device_id", "origin_port_id",
            "dest_city_id", "dest_site_id", "dest_device_id", "dest_port_id",
        ):
            if key in body:
                fields[key] = _opt_int(body[key])
        new_src = fields["source_port_id"] if "source_port_id" in fields else existing["source_port_id"]
        new_dst = fields["dest_port_id"] if "dest_port_id" in fields else existing["dest_port_id"]
        origin = (
            fields["origin_device_id"] if "origin_device_id" in fields
            else existing["origin_device_id"]
        )
        src_dir = source_port_direction(db, new_src, origin)
        if "source_port_id" in fields or "origin_device_id" in fields:
            check_port_direction(db, new_src, src_dir)
        if "dest_port_id" in fields:
            check_port_direction(db, new_dst, "output")
        db.update_flow(item_id, **fields)
        if "source_port_id" in fields or "origin_device_id" in fields:
            apply_port_direction(db, new_src, src_dir)
        if "dest_port_id" in fields:
            apply_port_direction(db, new_dst, "output")
        if existing["source_port_id"] != new_src:
            release_port_if_unused(db, existing["source_port_id"])
        if existing["dest_port_id"] != new_dst:
            release_port_if_unused(db, existing["dest_port_id"])
        return 200, {"flow": dict(db.get_flow(item_id))}
    if method == "DELETE":
        db.delete_flow(item_id)
        release_port_if_unused(db, existing["source_port_id"])
        release_port_if_unused(db, existing["dest_port_id"])
        return 200, {"deleted": item_id}
    raise LookupError("not found")


CSV_MAX_ROWS = 500
_CSV_ALIASES = {
    "name": "name",
    "device": "name",
    "vendor": "vendor",
    "mgmt_host": "mgmt_host",
    "host": "mgmt_host",
    "ip": "mgmt_host",
    "mgmt_ip": "mgmt_host",
    "model": "model",
    "device_role": "device_role",
    "role": "device_role",
    "firmware_version": "firmware_version",
    "firmware": "firmware_version",
    "access_mode": "access_mode",
    "access": "access_mode",
    "api_username": "api_username",
    "username": "api_username",
    "user": "api_username",
    "api_password": "api_password",
    "password": "api_password",
    "snmp_community": "snmp_community",
    "community": "snmp_community",
    "city": "city",
    "site": "site",
    "poll_enabled": "poll_enabled",
    "poll": "poll_enabled",
}
_VENDOR_ALIASES = {
    "appear": "appear",
    "haivision": "haivision",
    "hai": "haivision",
    "makito": "haivision",
    "net_insight": "net_insight",
    "net insight": "net_insight",
    "netinsight": "net_insight",
    "nimbra": "net_insight",
    "generic_snmp": "generic_snmp",
    "generic": "generic_snmp",
    "snmp": "generic_snmp",
}
_BLANK_UPDATE_KEYS = (
    "mgmt_host", "model", "device_role", "firmware_version", "access_mode",
    "api_username", "api_password", "snmp_community",
)


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def normalize_vendor(value: str) -> str:
    key = re.sub(r"[\s-]+", " ", (value or "").strip().lower())
    if key in _VENDOR_ALIASES:
        return _VENDOR_ALIASES[key]
    underscored = key.replace(" ", "_")
    if underscored in _VENDOR_ALIASES:
        return _VENDOR_ALIASES[underscored]
    if underscored in VALID_VENDORS:
        return underscored
    raise ValueError(f"unknown vendor {value!r}, must be one of {sorted(VALID_VENDORS)}")


def parse_device_csv(text: str) -> list[dict]:
    raw = (text or "").lstrip("\ufeff")
    if not raw.strip():
        raise ValueError("CSV is empty")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    mapping = {}
    for header in reader.fieldnames:
        key = _CSV_ALIASES.get(_norm_header(header))
        if key:
            mapping[header] = key
    if "name" not in mapping.values() or "vendor" not in mapping.values():
        raise ValueError("CSV needs name and vendor columns")
    rows = []
    for index, src in enumerate(reader, start=2):
        if index - 1 > CSV_MAX_ROWS:
            raise ValueError(f"CSV has more than {CSV_MAX_ROWS} data rows")
        row = {"_line": index}
        empty = True
        for header, key in mapping.items():
            value = (src.get(header) or "").strip()
            if value:
                empty = False
            row[key] = value
        if empty:
            continue
        rows.append(row)
    if not rows:
        raise ValueError("CSV has no device rows")
    return rows


def _resolve_import_site(db: Database, row: dict, holding_id: int) -> int:
    site_name = (row.get("site") or "").strip()
    city_name = (row.get("city") or "").strip()
    if site_name:
        site = db.find_site_by_name(site_name)
        if site is not None:
            if city_name:
                city = db.find_city_by_name(city_name)
                site_city = ""
                if "city_name" in site.keys():
                    site_city = site["city_name"] or ""
                if not site_city:
                    site_city = site["city"] or ""
                if city is not None and site["city_id"] not in (None, city["id"]) and site_city.lower() != city_name.lower():
                    return holding_id
            return site["id"]
    return holding_id


def _blank_updates(device, row: dict) -> dict:
    fields = {}
    for key in _BLANK_UPDATE_KEYS:
        incoming = (row.get(key) or "").strip()
        if not incoming:
            continue
        current = getattr(device, key, None)
        if current is None or str(current).strip() == "":
            if key == "access_mode" and incoming not in VALID_ACCESS_MODES:
                continue
            fields[key] = incoming
    if "mgmt_host" in fields and device.poll_enabled is False and fields["mgmt_host"]:
        fields["poll_enabled"] = True
    return fields


def import_devices(db: Database, env_path: Path, text: str) -> dict:
    rows = parse_device_csv(text)
    holding_id = db.ensure_unassigned_site()
    created, updated, skipped, errors = [], [], [], []
    for row in rows:
        name = (row.get("name") or "").strip()
        line = row.get("_line")
        try:
            if not name:
                raise ValueError("name is required")
            vendor = normalize_vendor(row.get("vendor") or "")
            existing = db.get_device_by_name(name)
            if existing is not None:
                fields = _blank_updates(existing, row)
                if fields:
                    db.update_device(existing.id, **fields)
                    updated.append({"id": existing.id, "name": existing.name, "line": line})
                else:
                    skipped.append({
                        "id": existing.id,
                        "name": existing.name,
                        "line": line,
                        "reason": "already exists",
                    })
                continue
            host = (row.get("mgmt_host") or "").strip()
            if host and db.find_device_by_mgmt_host(host) is not None:
                raise ValueError(f"management host {host} is already in use")
            site_id = _resolve_import_site(db, row, holding_id)
            access = (row.get("access_mode") or "").strip() or "direct_api"
            if access not in VALID_ACCESS_MODES:
                raise ValueError(f"invalid access_mode {access!r}")
            poll = row.get("poll_enabled")
            body = {
                "name": name,
                "vendor": vendor,
                "site_id": site_id,
                "mgmt_host": host,
                "model": row.get("model") or "",
                "device_role": row.get("device_role") or "",
                "firmware_version": row.get("firmware_version") or "",
                "access_mode": access,
                "api_username": row.get("api_username") or "",
                "api_password": row.get("api_password") or "",
                "snmp_community": row.get("snmp_community") or "",
            }
            if poll != "":
                body["poll_enabled"] = _as_bool(poll) if poll else bool(host)
            status, payload = _handle_devices(db, env_path, "POST", None, body)
            if status != 201:
                raise ValueError(payload.get("error") if isinstance(payload, dict) else "create failed")
            created.append({
                "id": payload["device"]["id"],
                "name": name,
                "line": line,
                "site_id": site_id,
            })
        except (ValueError, LookupError, sqlite3.IntegrityError) as exc:
            errors.append({"line": line, "name": name, "error": str(exc)})
    return {
        "ok": True,
        "holding_site_id": holding_id,
        "holding_city": UNASSIGNED_CITY_NAME,
        "holding_site": UNASSIGNED_SITE_NAME,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }


def _handle_bulk(db: Database, env_path: Path, collection: str, body: dict) -> tuple[int, dict]:
    ids = _bulk_ids(body)
    if body.get("merge_into") is not None:
        if collection != "devices":
            raise ValueError("merge is only for devices")
        keep = int(body["merge_into"])
        sources = [item_id for item_id in ids if item_id != keep]
        if not sources:
            raise ValueError("select at least one other device to merge into the kept device")
        if db.get_device(keep) is None:
            raise LookupError("keep device not found")
        merged, errors = [], []
        for item_id in sources:
            try:
                db.merge_devices(item_id, keep)
                merged.append(item_id)
            except (LookupError, ValueError, sqlite3.IntegrityError) as exc:
                errors.append({"id": item_id, "error": str(exc)})
        return 200, {"kept": keep, "merged": merged, "errors": errors}
    delete = _as_bool(body.get("delete"))
    patch = body.get("patch") if isinstance(body.get("patch"), dict) else {}
    if not delete and not patch:
        raise ValueError("bulk request needs patch fields, delete=true, or merge_into")
    updated, deleted, errors = [], [], []
    for item_id in ids:
        try:
            if delete:
                if collection == "devices":
                    _handle_devices(db, env_path, "DELETE", item_id, {})
                elif collection == "flows":
                    _handle_flows(db, "DELETE", item_id, {})
                else:
                    raise ValueError(f"bulk delete not supported for {collection}")
                deleted.append(item_id)
            else:
                if collection == "devices":
                    _handle_devices(db, env_path, "PATCH", item_id, patch)
                elif collection == "flows":
                    _handle_flows(db, "PATCH", item_id, patch)
                else:
                    raise ValueError(f"bulk edit not supported for {collection}")
                updated.append(item_id)
        except (LookupError, ValueError, sqlite3.IntegrityError) as exc:
            errors.append({"id": item_id, "error": str(exc)})
    return 200, {"updated": updated, "deleted": deleted, "errors": errors}


def handle(db: Database, env_path: Path, method: str, path: str, body: dict,
           pin_dir: Optional[Path] = None) -> tuple[int, dict]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "api":
        raise LookupError("not found")
    collection = parts[1]
    if collection == "devices" and len(parts) == 3 and parts[2] == "import":
        if method != "POST":
            raise LookupError("not found")
        text = body.get("csv")
        if not isinstance(text, str):
            raise ValueError("csv text is required")
        return 200, import_devices(db, env_path, text)
    if collection == "geocode" and method == "POST":
        query = (body.get("query") or body.get("q") or "").strip()
        kind = body.get("kind") or "search"
        try:
            hit = geocode(query, kind=kind)
        except (GeocodeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        if hit is None:
            return 200, {"hit": None}
        return 200, {"hit": hit}
    if len(parts) == 4 and parts[1] == "sites" and parts[3] == "pin":
        if method != "POST":
            raise LookupError("not found")
        site_id = int(parts[2])
        return 200, save_site_pin(db, site_id, body, pin_dir)
    if len(parts) > 2 and parts[2] == "bulk":
        if method != "POST":
            raise LookupError("not found")
        if collection not in {"devices", "flows"}:
            raise LookupError("not found")
        try:
            return _handle_bulk(db, env_path, collection, body)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"cannot apply change: {exc}") from exc
    item_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    if len(parts) > 2 and not parts[2].isdigit():
        raise LookupError("not found")
    try:
        if collection == "cities":
            return _handle_cities(db, method, item_id, body)
        if collection == "sites":
            return _handle_sites(db, method, item_id, body)
        if collection == "devices":
            return _handle_devices(db, env_path, method, item_id, body)
        if collection == "ports":
            return _handle_ports(db, method, item_id, body)
        if collection == "flows":
            return _handle_flows(db, method, item_id, body)
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"cannot apply change: {exc}") from exc
    raise LookupError("not found")
