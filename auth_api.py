"""HTTP handlers for /api/auth/* and /api/admin/*."""
from __future__ import annotations

from typing import Optional

from audit import audit_log, audit_read
from svc_util import (
    CONTROL_ACTIONS,
    SvcError,
    control_service,
    list_services,
    restart_service,
    service_logs,
)
from auth import (
    DEFAULT_LDAP,
    DEFAULT_ROLES,
    MIN_PASSWORD_LEN,
    catalog,
    cookie_header,
    destroy_session,
    hash_password,
    parse_overrides,
    parse_roles,
    public_user_row,
    user_payload,
    valid_username,
    verify_password,
)
from db import Database


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _require(user: Optional[dict], perm: str) -> dict:
    if not user:
        raise AuthError("login required", 401)
    if user.get("must_change_password") and perm not in ("dashboard",):
        raise AuthError("password change required", 403)
    if not (user.get("permissions") or {}).get(perm):
        raise AuthError("access denied", 403)
    return user


def handle_auth(db: Database, method: str, path: str, body: dict,
                user: Optional[dict], token: Optional[str],
                secure: bool, client_ip: str) -> tuple[int, dict, Optional[str]]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "api":
        raise LookupError("not found")

    if parts[1] == "auth":
        return _handle_auth_routes(db, method, parts, body, user, token, secure, client_ip)
    if parts[1] == "admin":
        return _handle_admin(db, method, parts, body, user, client_ip)
    raise LookupError("not found")


def _handle_auth_routes(db, method, parts, body, user, token, secure, client_ip):
    action = parts[2] if len(parts) > 2 else ""
    if action == "login" and method == "POST":
        from auth import authenticate
        result = authenticate(db, body.get("username") or "", body.get("password") or "")
        if not result.get("ok"):
            audit_log("login_failed", {"username": body.get("username") or "", "id": None},
                      client_ip, ok=False)
            raise AuthError(result.get("error") or "Invalid credentials", 401)
        idle = result["user"].get("session_idle_minutes") or 120
        cookie = cookie_header(result["token"], idle, secure=secure)
        audit_log("login", result["user"], client_ip, ok=True)
        return 200, {"ok": True, "user": result["user"]}, cookie
    if action == "logout" and method == "POST":
        destroy_session(db, token)
        if user:
            audit_log("logout", user, client_ip, ok=True)
        return 200, {"ok": True}, cookie_header("", clear=True)
    if action == "me" and method == "GET":
        if not user:
            raise AuthError("login required", 401)
        return 200, {"ok": True, "user": user}, None
    if action == "password" and method == "POST":
        if not user:
            raise AuthError("login required", 401)
        if user.get("type") != "local" or user.get("ldap_ephemeral"):
            raise AuthError("LDAP users cannot change password here", 400)
        record = db.get_user(user["id"])
        if record is None:
            raise AuthError("user not found", 404)
        current = body.get("current_password") or ""
        new_pass = body.get("new_password") or ""
        if not verify_password(current, record["password_hash"] or ""):
            raise AuthError("Current password incorrect", 400)
        if len(new_pass) < MIN_PASSWORD_LEN:
            raise AuthError(f"New password must be at least {MIN_PASSWORD_LEN} characters", 400)
        db.update_user(
            record["id"],
            password_hash=hash_password(new_pass),
            must_change_password=False,
        )
        updated = user_payload(db.get_user(record["id"]), db.get_auth_settings())
        audit_log("password_change", updated, client_ip, ok=True)
        return 200, {"ok": True, "user": updated}, None
    raise LookupError("not found")


def _handle_admin(db, method, parts, body, user, client_ip):
    _require(user, "manage_users")
    extra = parts[2] if len(parts) > 2 else ""
    if extra == "audit" and method == "GET":
        _require(user, "view_audit")
        return 200, {"ok": True, **audit_read(
            limit=int(body.get("limit") or 200),
            offset=int(body.get("offset") or 0),
        )}, None
    if extra == "services":
        return _handle_services(method, parts, body, user, client_ip)
    if method == "GET":
        settings = db.get_auth_settings()
        users = [public_user_row(dict(u)) for u in db.list_users()]
        return 200, {
            "ok": True,
            **catalog(),
            "ldap": settings["ldap"],
            "session_idle_minutes": settings["session_idle_minutes"],
            "users": users,
        }, None
    if method != "POST":
        raise AuthError("method not allowed", 405)
    action = body.get("action") or extra
    if action == "save_ldap":
        current = db.get_auth_settings()["ldap"]
        ldap = dict(DEFAULT_LDAP)
        ldap.update(current)
        ldap["enabled"] = bool(body.get("enabled"))
        ldap["host"] = (body.get("host") if "host" in body else ldap["host"]) or ""
        ldap["port"] = int(body.get("port") if "port" in body else ldap["port"] or 636)
        ldap["bind_template"] = (
            body.get("bind_template") if "bind_template" in body else ldap["bind_template"]
        ) or ""
        ldap["base_dn"] = (body.get("base_dn") if "base_dn" in body else ldap.get("base_dn")) or ""
        ldap["ignore_cert"] = bool(body.get("ignore_cert")) if "ignore_cert" in body else bool(ldap.get("ignore_cert", True))
        if "allowed_groups" in body and isinstance(body["allowed_groups"], list):
            ldap["allowed_groups"] = _clean_groups(body["allowed_groups"])
        db.update_auth_settings(ldap=ldap)
        audit_log("save_ldap", user, client_ip, ok=True)
        return 200, {"ok": True, "ldap": db.get_auth_settings()["ldap"]}, None
    if action == "save_session":
        minutes = int(body.get("session_idle_minutes") or 120)
        db.update_auth_settings(session_idle_minutes=minutes)
        audit_log("save_session", user, client_ip, {"session_idle_minutes": minutes}, ok=True)
        return 200, {"ok": True, "session_idle_minutes": db.get_auth_settings()["session_idle_minutes"]}, None
    if action == "save_user":
        return _save_user(db, body, user, client_ip)
    if action == "delete_user":
        return _delete_user(db, body, user, client_ip)
    raise AuthError("unknown action", 400)


def _handle_services(method, parts, body, user, client_ip):
    unit = parts[3] if len(parts) > 3 else ""
    action = parts[4] if len(parts) > 4 else ""
    if method == "GET" and not unit:
        return 200, {"ok": True, **list_services()}, None
    if method == "GET" and action == "logs":
        try:
            lines = int(body.get("lines") or 200)
        except (TypeError, ValueError):
            lines = 200
        try:
            text = service_logs(unit, lines)
        except SvcError as exc:
            raise AuthError(str(exc), 400) from exc
        return 200, {"ok": True, "unit": unit, "log": text}, None
    if method == "POST" and (action == "restart" or action in CONTROL_ACTIONS):
        try:
            result = (
                restart_service(unit) if action == "restart"
                else control_service(unit, action)
            )
        except SvcError as exc:
            audit_log(f"{action}_service", user, client_ip, {"target": unit}, ok=False)
            raise AuthError(str(exc), 400) from exc
        audit_log(f"{action}_service", user, client_ip, {"target": unit}, ok=True)
        status = 202 if result.get("restarting") else 200
        return status, {"ok": True, "unit": unit, **result}, None
    raise LookupError("not found")


def _clean_groups(raw: list) -> list:
    out = []
    for entry in raw:
        if isinstance(entry, str):
            name, roles = entry, ["viewer"]
        else:
            name = (entry.get("name") or "").strip()
            roles = parse_roles(entry.get("roles") or ["viewer"])
        if not name:
            continue
        if not roles:
            roles = ["viewer"]
        out.append({"name": name, "roles": roles})
    return out


def _save_user(db: Database, body: dict, actor: dict, client_ip: str):
    user_id = body.get("id")
    username = (body.get("username") or "").strip()
    user_type = "ldap" if body.get("type") == "ldap" else "local"
    roles = parse_roles(body.get("roles") or [])
    if not username or not valid_username(username):
        raise AuthError("username must be 1-64 letters, digits, . _ -")
    if not roles:
        roles = ["viewer"]
    overrides = parse_overrides(body.get("permission_overrides") or {})
    enabled = bool(body.get("enabled", True))
    password = body.get("password") or ""

    if user_id:
        existing = db.get_user(int(user_id))
        if existing is None:
            raise AuthError("user not found", 404)
        other = db.get_user_by_username(username)
        if other and other["id"] != existing["id"]:
            raise AuthError("username already taken", 400)
        fields = {
            "username": username,
            "type": user_type,
            "roles": roles,
            "enabled": enabled,
        }
        if "permission_overrides" in body:
            fields["permission_overrides"] = overrides
        if user_type == "local" and password:
            if len(password) < MIN_PASSWORD_LEN:
                raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters", 400)
            fields["password_hash"] = hash_password(password)
        if user_type == "ldap":
            fields["password_hash"] = None
        db.update_user(existing["id"], **fields)
        audit_log("save_user", actor, client_ip, {"target": username}, ok=True)
        return 200, {"ok": True, "user": public_user_row(dict(db.get_user(existing["id"])))}, None

    if db.get_user_by_username(username):
        raise AuthError("username already taken", 400)
    password_hash = None
    if user_type == "local":
        if len(password) < MIN_PASSWORD_LEN:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters", 400)
        password_hash = hash_password(password)
    new_id = db.add_user(
        username=username,
        user_type=user_type,
        password_hash=password_hash,
        roles=roles,
        permission_overrides=overrides,
        enabled=enabled,
    )
    audit_log("save_user", actor, client_ip, {"target": username}, ok=True)
    return 201, {"ok": True, "user": public_user_row(dict(db.get_user(new_id)))}, None


def _delete_user(db: Database, body: dict, actor: dict, client_ip: str):
    user_id = int(body.get("id") or 0)
    if user_id == actor.get("id"):
        raise AuthError("cannot delete your own account", 400)
    if db.count_users() <= 1:
        raise AuthError("cannot delete last user", 400)
    target = db.get_user(user_id)
    if target is None:
        raise AuthError("user not found", 404)
    db.delete_user(user_id)
    audit_log("delete_user", actor, client_ip, {"target": target["username"]}, ok=True)
    return 200, {"ok": True}, None
