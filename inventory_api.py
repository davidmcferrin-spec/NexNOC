"""Write API for cities / sites / devices / ports / flows.

Credential *values* are written only to the env file (never the DB or
config.json). Env *names* stay on the device row. Values are never
returned in responses.
"""
from __future__ import annotations

import base64
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from db import Database
from envfile import is_env_key, is_set, upsert_values
from geocode import GeocodeError, geocode, geocode_or_none
from pins import DEFAULT_PIN_COLOR, DEFAULT_PIN_ICON, valid_pin_color, valid_pin_icon

SECRET_KEYS = {
    "api_username", "api_password", "snmp_community",
    "snmp_v3_user", "snmp_v3_auth_pass", "snmp_v3_priv_pass",
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


def device_secret_flags(device, env_path: Path) -> dict:
    return {
        "api_username_env": device.api_username_env,
        "api_password_env": device.api_password_env,
        "snmp_community_env": device.snmp_community_env,
        "snmp_v3_user_env": device.snmp_v3_user_env,
        "snmp_v3_auth_pass_env": device.snmp_v3_auth_pass_env,
        "snmp_v3_priv_pass_env": device.snmp_v3_priv_pass_env,
        "api_username_set": is_set(env_path, device.api_username_env),
        "api_password_set": is_set(env_path, device.api_password_env),
        "snmp_community_set": is_set(env_path, device.snmp_community_env),
        "snmp_v3_user_set": is_set(env_path, device.snmp_v3_user_env),
        "snmp_v3_auth_set": is_set(env_path, device.snmp_v3_auth_pass_env),
        "snmp_v3_priv_set": is_set(env_path, device.snmp_v3_priv_pass_env),
        "credentials_ready": (
            is_set(env_path, device.api_username_env)
            and is_set(env_path, device.api_password_env)
        ),
        "snmp_ready": (
            is_set(env_path, device.snmp_community_env)
            or is_set(env_path, device.snmp_v3_user_env)
        ),
    }


def apply_secrets(env_path: Path, names: dict[str, Optional[str]], body: dict) -> None:
    updates = {}
    mapping = (
        ("api_username", names.get("api_username_env")),
        ("api_password", names.get("api_password_env")),
        ("snmp_community", names.get("snmp_community_env")),
        ("snmp_v3_user", names.get("snmp_v3_user_env")),
        ("snmp_v3_auth_pass", names.get("snmp_v3_auth_pass_env")),
        ("snmp_v3_priv_pass", names.get("snmp_v3_priv_pass_env")),
    )
    for body_key, env_name in mapping:
        if body_key not in body:
            continue
        if not env_name:
            raise ValueError(f"{body_key} needs an env var name first")
        if not is_env_key(env_name):
            raise ValueError(f"invalid env var name {env_name!r}")
        raw = body[body_key]
        if raw is None or raw == "":
            continue
        updates[env_name] = str(raw)
    if updates:
        upsert_values(env_path, updates)


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


def _city_payload(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {
        "id": row["id"],
        "name": row["name"],
        "lat": row["lat"],
        "lng": row["lng"],
        "geo_source": row["geo_source"] if "geo_source" in keys else "",
        "notes": row["notes"] or "",
    }


def _site_payload(row) -> dict:
    data = dict(row)
    data.setdefault("address", "")
    data.setdefault("geo_source", "")
    data.setdefault("pin_icon", DEFAULT_PIN_ICON)
    data.setdefault("pin_color", DEFAULT_PIN_COLOR)
    data.setdefault("pin_upload", None)
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
        if "address" in body and "lat" not in body and "lng" not in body:
            # Manual pins stay put; typo fixes must not replace operator coords.
            if (existing["geo_source"] or "") != "manual":
                hit = geocode_or_none(body["address"], kind="address")
                if hit:
                    fields["lat"] = hit["lat"]
                    fields["lng"] = hit["lng"]
                    fields["geo_source"] = "geocode"
        fields.update(_pin_fields(body))
        db.update_site(item_id, **fields)
        return 200, {"site": _site_payload(db.get_site(item_id))}
    if method == "DELETE":
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
        "api_username_env", "api_password_env",
        "snmp_host", "snmp_port", "snmp_community_env", "snmp_version",
        "snmp_enabled", "snmp_trap_enabled",
        "snmp_v3_user_env", "snmp_v3_sec_level", "snmp_v3_auth_proto",
        "snmp_v3_auth_pass_env", "snmp_v3_priv_proto", "snmp_v3_priv_pass_env",
        "nms_host", "nms_port", "nms_api_key_env", "nms_device_ref",
        "poll_enabled",
    ):
        if key not in body:
            continue
        value = body[key]
        if key in {"site_id", "api_port", "snmp_port", "nms_port"}:
            value = _opt_int(value) if key != "site_id" else int(value)
        if key in {"api_verify_tls", "poll_enabled", "snmp_enabled", "snmp_trap_enabled"}:
            value = _as_bool(value)
        if key in {
            "api_username_env", "api_password_env", "snmp_community_env",
            "nms_api_key_env", "snmp_v3_user_env", "snmp_v3_auth_pass_env",
            "snmp_v3_priv_pass_env",
        } and value:
            if not is_env_key(str(value)):
                raise ValueError(f"invalid env var name {value!r}")
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
            api_username_env=body.get("api_username_env"),
            api_password_env=body.get("api_password_env"),
            snmp_community_env=body.get("snmp_community_env"),
            snmp_host=body.get("snmp_host"),
            snmp_port=int(body.get("snmp_port") or 161),
            snmp_version=body.get("snmp_version") or "2c",
            snmp_enabled=body.get("snmp_enabled"),
            snmp_trap_enabled=body.get("snmp_trap_enabled", True),
            snmp_v3_user_env=body.get("snmp_v3_user_env"),
            snmp_v3_sec_level=body.get("snmp_v3_sec_level") or "authPriv",
            snmp_v3_auth_proto=body.get("snmp_v3_auth_proto") or "SHA",
            snmp_v3_auth_pass_env=body.get("snmp_v3_auth_pass_env"),
            snmp_v3_priv_proto=body.get("snmp_v3_priv_proto") or "AES",
            snmp_v3_priv_pass_env=body.get("snmp_v3_priv_pass_env"),
            nms_host=body.get("nms_host"),
            nms_port=_opt_int(body.get("nms_port")),
            nms_api_key_env=body.get("nms_api_key_env"),
            nms_device_ref=body.get("nms_device_ref"),
            poll_enabled=bool(poll),
        )
        device = db.get_device(device_id)
        apply_secrets(env_path, {
            "api_username_env": device.api_username_env,
            "api_password_env": device.api_password_env,
            "snmp_community_env": device.snmp_community_env,
            "snmp_v3_user_env": device.snmp_v3_user_env,
            "snmp_v3_auth_pass_env": device.snmp_v3_auth_pass_env,
            "snmp_v3_priv_pass_env": device.snmp_v3_priv_pass_env,
        }, body)
        if body.get("snmp_enabled") is not False and any(
            body.get(key) for key in ("snmp_community", "snmp_v3_user")
        ):
            db.update_device(device_id, snmp_enabled=True)
        stamp_device_connectors(db, device)
        device = db.get_device(device_id)
        return 201, {"device": {"id": device.id, "name": device.name, **device_secret_flags(device, env_path)}}
    if item_id is None:
        raise LookupError("device id required")
    device = db.get_device(item_id)
    if device is None:
        raise LookupError("device not found")
    if method == "PATCH":
        fields = _device_fields(body)
        if fields:
            db.update_device(item_id, **fields)
        device = db.get_device(item_id)
        apply_secrets(env_path, {
            "api_username_env": device.api_username_env,
            "api_password_env": device.api_password_env,
            "snmp_community_env": device.snmp_community_env,
            "snmp_v3_user_env": device.snmp_v3_user_env,
            "snmp_v3_auth_pass_env": device.snmp_v3_auth_pass_env,
            "snmp_v3_priv_pass_env": device.snmp_v3_priv_pass_env,
        }, body)
        if "snmp_enabled" not in fields and body.get("snmp_enabled") is not False and any(
            body.get(key) for key in ("snmp_community", "snmp_v3_user")
        ):
            db.update_device(item_id, snmp_enabled=True)
        device = db.get_device(item_id)
        return 200, {"device": {"id": device.id, "name": device.name, **device_secret_flags(device, env_path)}}
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
