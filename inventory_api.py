"""Write API for cities / sites / devices / ports / flows.

Credential *values* are written only to the env file (never the DB or
config.json). Env *names* stay on the device row. Values are never
returned in responses.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from db import Database
from envfile import is_env_key, is_set, upsert_values

SECRET_KEYS = {
    "api_username", "api_password", "snmp_community",
    "snmp_v3_user", "snmp_v3_auth_pass", "snmp_v3_priv_pass",
}


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


def _city_payload(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "lat": row["lat"],
        "lng": row["lng"],
        "notes": row["notes"] or "",
    }


def _handle_cities(db: Database, method: str, item_id: Optional[int], body: dict):
    if method == "POST":
        if not body.get("name"):
            raise ValueError("city name is required")
        city_id = db.add_city(
            name=body["name"],
            lat=_opt_float(body.get("lat")),
            lng=_opt_float(body.get("lng")),
            notes=body.get("notes") or "",
        )
        return 201, {"city": _city_payload(db.get_city(city_id))}
    if item_id is None:
        raise LookupError("city id required")
    if db.get_city(item_id) is None:
        raise LookupError("city not found")
    if method == "PATCH":
        fields = {}
        for key in ("name", "notes"):
            if key in body:
                fields[key] = body[key]
        if "lat" in body:
            fields["lat"] = _opt_float(body["lat"])
        if "lng" in body:
            fields["lng"] = _opt_float(body["lng"])
        db.update_city(item_id, **fields)
        return 200, {"city": _city_payload(db.get_city(item_id))}
    if method == "DELETE":
        db.delete_city(item_id)
        return 200, {"deleted": item_id}
    raise LookupError("not found")


def _handle_sites(db: Database, method: str, item_id: Optional[int], body: dict):
    if method == "POST":
        if not body.get("name"):
            raise ValueError("site name is required")
        site_id = db.add_site(
            name=body["name"],
            city=body.get("city") or "",
            city_id=_opt_int(body.get("city_id")),
            lat=_opt_float(body.get("lat")),
            lng=_opt_float(body.get("lng")),
            notes=body.get("notes") or "",
        )
        return 201, {"site": dict(db.get_site(site_id))}
    if item_id is None:
        raise LookupError("site id required")
    if db.get_site(item_id) is None:
        raise LookupError("site not found")
    if method == "PATCH":
        fields = {}
        for key in ("name", "city", "notes"):
            if key in body:
                fields[key] = body[key]
        if "city_id" in body:
            fields["city_id"] = _opt_int(body["city_id"])
        if "lat" in body:
            fields["lat"] = _opt_float(body["lat"])
        if "lng" in body:
            fields["lng"] = _opt_float(body["lng"])
        db.update_site(item_id, **fields)
        return 200, {"site": dict(db.get_site(item_id))}
    if method == "DELETE":
        try:
            db.delete_site(item_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Move or delete this site's devices (and trunks) first."
            ) from exc
        return 200, {"deleted": item_id}
    raise LookupError("not found")


def _device_fields(body: dict) -> dict:
    fields = {}
    for key in (
        "site_id", "name", "vendor", "device_role", "model", "firmware_version",
        "mgmt_host", "access_mode", "driver_override",
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
            driver_override=body.get("driver_override"),
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
        port_id = db.add_port(
            device_id=int(body["device_id"]),
            name=body["name"],
            kind=body.get("kind") or "other",
            slot=body.get("slot") or "",
        )
        return 201, {"port": dict(db.get_port(port_id))}
    if item_id is None:
        raise LookupError("port id required")
    if db.get_port(item_id) is None:
        raise LookupError("port not found")
    if method == "PATCH":
        fields = {}
        for key in ("name", "kind", "slot", "status"):
            if key in body:
                fields[key] = body[key]
        if "device_id" in body:
            fields["device_id"] = int(body["device_id"])
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
        flow_id = db.add_flow(
            label=body["label"],
            source_device_id=int(body["source_device_id"]),
            source_port_id=_opt_int(body.get("source_port_id")),
            dest_site_id=_opt_int(body.get("dest_site_id")),
            dest_device_id=_opt_int(body.get("dest_device_id")),
            dest_port_id=_opt_int(body.get("dest_port_id")),
            dest_label=body.get("dest_label") or "",
            direction=body.get("direction") or "",
            signal_label=body.get("signal_label") or "",
            dest_city_id=_opt_int(body.get("dest_city_id")),
            origin_device_id=_opt_int(body.get("origin_device_id")),
            origin_port_id=_opt_int(body.get("origin_port_id")),
        )
        return 201, {"flow": dict(db.get_flow(flow_id))}
    if item_id is None:
        raise LookupError("flow id required")
    if db.get_flow(item_id) is None:
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
        db.update_flow(item_id, **fields)
        return 200, {"flow": dict(db.get_flow(item_id))}
    if method == "DELETE":
        db.delete_flow(item_id)
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


def handle(db: Database, env_path: Path, method: str, path: str, body: dict) -> tuple[int, dict]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "api":
        raise LookupError("not found")
    collection = parts[1]
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
