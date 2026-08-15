"""Local + LDAP authentication, roles, and server-side sessions.

Stdlib only. Passwords use hashlib.scrypt. LDAP bind shells out to
ldapsearch (same pattern as snmpget). Roles are defined in code;
assignments and overrides live in SQLite.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs

from db import Database, utcnow_iso

logger = logging.getLogger("nexnoc.auth")

COOKIE_NAME = "nexnoc_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._\\-]{1,64}$")
MIN_PASSWORD_LEN = 6

PERMISSIONS = [
    "dashboard",
    "manage_inventory",
    "manage_credentials",
    "manage_users",
    "view_audit",
    "manage_licenses",
    "manage_backups",
    "view_routing",
    "propose_routing",
    "execute_routing",
]

PERMISSION_META = {
    "dashboard": {
        "label": "Dashboard",
        "description": "Sign in and view the map, links table, and read-only inventory.",
    },
    "manage_inventory": {
        "label": "Manage inventory",
        "description": "Create, edit, and delete cities, sites, devices, ports, and flows.",
    },
    "manage_credentials": {
        "label": "Manage credentials",
        "description": "Write device secret values into nexnoc.env.",
    },
    "manage_users": {
        "label": "Manage users",
        "description": "Admin: users, LDAP, session timeout, and audit log.",
    },
    "view_audit": {
        "label": "View audit log",
        "description": "Read the append-only audit log.",
    },
    "manage_licenses": {
        "label": "Manage licenses",
        "description": "Reserved for a possible future license-tracking feature — not on the roadmap, unused.",
    },
    "manage_backups": {
        "label": "Manage backups",
        "description": "Reserved for a possible future config-backup feature — not on the roadmap, unused.",
    },
    "view_routing": {
        "label": "View routing",
        "description": "Reserved for a possible future routing control feature — not on the roadmap, unused.",
    },
    "propose_routing": {
        "label": "Propose routing",
        "description": "Reserved for a possible future routing control feature — not on the roadmap, unused.",
    },
    "execute_routing": {
        "label": "Execute routing",
        "description": "Reserved for a possible future routing control feature — not on the roadmap, unused.",
    },
}

DEFAULT_ROLES = {
    "viewer": {
        "label": "Viewer",
        "description": "Signed-in board: map, links, read-only inventory. No edits or admin.",
        "permissions": {
            "dashboard": True,
        },
    },
    "operator": {
        "label": "Operator",
        "description": "Viewer plus inventory and credential writes. No user or LDAP admin.",
        "permissions": {
            "dashboard": True,
            "manage_inventory": True,
            "manage_credentials": True,
        },
    },
    "admin": {
        "label": "Administrator",
        "description": "Full access, including users, LDAP, and audit.",
        "permissions": {perm: True for perm in PERMISSIONS},
    },
}

DEFAULT_LDAP = {
    "enabled": False,
    "host": "",
    "port": 636,
    "bind_template": "{username}@example.com",
    "base_dn": "",
    "ignore_cert": True,
    "allowed_groups": [],
}

SEED_USERS = (
    ("admin", "password", ["admin"]),
    ("user", "password", ["viewer"]),
)


def _scrypt_n() -> int:
    raw = (os.environ.get("NEXNOC_SCRYPT_N") or "").strip()
    if raw.isdigit():
        n = int(raw)
        if n >= 2 and (n & (n - 1)) == 0:
            return n
    if "unittest" in sys.modules:
        return 16
    return 16384


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n = _scrypt_n()
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=8, p=1, dklen=32,
    )
    return f"scrypt${n}$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
    )
    return secrets.compare_digest(digest, expected)


def normalize_username(username: str) -> str:
    return (username or "").strip()


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username))


def parse_roles(raw) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
    return [r for r in values if r in DEFAULT_ROLES]


def parse_overrides(raw) -> dict[str, bool]:
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            data = {}
    out = {}
    for key, val in data.items():
        if key in PERMISSIONS:
            out[key] = bool(val)
    return out


def effective_permissions(roles: list[str], overrides: Optional[dict] = None) -> dict[str, bool]:
    merged = {perm: False for perm in PERMISSIONS}
    for role_id in roles:
        role = DEFAULT_ROLES.get(role_id)
        if not role:
            continue
        for perm, val in role["permissions"].items():
            if val:
                merged[perm] = True
    for perm, val in (overrides or {}).items():
        if perm in PERMISSIONS:
            merged[perm] = bool(val)
    return merged


def _row(user) -> dict:
    if user is None:
        return {}
    if isinstance(user, dict):
        return user
    return {key: user[key] for key in user.keys()}


def user_payload(user: dict, settings: Optional[dict] = None) -> dict:
    user = _row(user)
    roles = parse_roles(user.get("roles"))
    overrides = parse_overrides(user.get("permission_overrides"))
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "type": user.get("type") or "local",
        "roles": roles,
        "permissions": effective_permissions(roles, overrides),
        "must_change_password": bool(user.get("must_change_password")),
        "enabled": bool(user.get("enabled", True)),
        "session_idle_minutes": int(
            (settings or {}).get("session_idle_minutes") or 120
        ),
    }


def public_user_row(user: dict) -> dict:
    data = dict(user)
    data.pop("password_hash", None)
    data["roles"] = parse_roles(data.get("roles"))
    data["permission_overrides"] = parse_overrides(data.get("permission_overrides"))
    data["enabled"] = bool(data.get("enabled", True))
    data["must_change_password"] = bool(data.get("must_change_password"))
    return data


def ensure_seeded(db: Database) -> None:
    db.ensure_auth_settings()
    if db.list_users():
        return
    for username, password, roles in SEED_USERS:
        db.add_user(
            username=username,
            user_type="local",
            password_hash=hash_password(password),
            roles=roles,
            must_change_password=True,
        )
    logger.info("Seeded local users admin and user (change passwords on first login)")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def session_expired(session: dict, idle_minutes: int) -> bool:
    last = _parse_iso(session.get("last_activity") or session.get("created_at"))
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age > max(5, min(1440, idle_minutes)) * 60


def create_session(db: Database, user: dict) -> str:
    token = secrets.token_hex(32)
    db.add_session(
        token,
        user_id=user.get("id"),
        username=user["username"],
        ldap_ephemeral=bool(user.get("ldap_ephemeral")),
        ldap_roles=user.get("roles") if user.get("ldap_ephemeral") else None,
    )
    return token


def destroy_session(db: Database, token: Optional[str]) -> None:
    if token:
        db.delete_session(token)


def load_session_user(db: Database, token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    session = _row(db.get_session(token))
    if not session:
        return None
    settings = db.get_auth_settings()
    idle = int(settings.get("session_idle_minutes") or 120)
    if session_expired(session, idle):
        db.delete_session(token)
        return None
    if session["ldap_ephemeral"]:
        stored = db.get_user_by_username(session["username"])
        if stored and stored["type"] == "ldap":
            if not stored["enabled"]:
                db.delete_session(token)
                return None
            db.touch_session(token)
            payload = user_payload(stored, settings)
            payload["session_id"] = token
            return payload
        ephemeral = {
            "id": session["user_id"] or f"ldap:{session['username']}",
            "username": session["username"],
            "type": "ldap",
            "roles": parse_roles(session["ldap_roles"]),
            "permission_overrides": {},
            "must_change_password": False,
            "enabled": True,
            "ldap_ephemeral": True,
        }
        db.touch_session(token)
        payload = user_payload(ephemeral, settings)
        payload["session_id"] = token
        payload["ldap_ephemeral"] = True
        return payload
    record = db.get_user(session["user_id"]) if session["user_id"] else None
    user = _row(record) if record else None
    if user is None or not user["enabled"]:
        db.delete_session(token)
        return None
    db.touch_session(token)
    payload = user_payload(user, settings)
    payload["session_id"] = token
    return payload


def authenticate(db: Database, username: str, password: str) -> dict:
    username = normalize_username(username)
    if not username or password == "":
        return {"ok": False, "error": "Invalid credentials"}
    if not valid_username(username):
        return {"ok": False, "error": "Invalid credentials"}

    record = db.get_user_by_username(username)
    user = _row(record) if record else None
    if user and not user["enabled"]:
        return {"ok": False, "error": "Account disabled"}

    if user and user["type"] == "local":
        if not verify_password(password, user.get("password_hash") or ""):
            return {"ok": False, "error": "Invalid credentials"}
        token = create_session(db, user)
        return {"ok": True, "user": user_payload(user, db.get_auth_settings()), "token": token}

    settings = db.get_auth_settings()
    ldap = settings.get("ldap") or DEFAULT_LDAP
    if not ldap.get("enabled"):
        return {"ok": False, "error": "Invalid credentials"}

    from ldap_util import ldap_bind_user

    result = ldap_bind_user(ldap, username, password)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "Invalid credentials"}

    if user:
        if user["type"] != "ldap":
            return {"ok": False, "error": "Invalid credentials"}
        token = create_session(db, user)
        return {"ok": True, "user": user_payload(user, settings), "token": token}

    from ldap_util import roles_from_ldap_groups

    group_roles = roles_from_ldap_groups(
        result.get("member_of") or [], ldap.get("allowed_groups") or [],
    )
    if not group_roles:
        return {"ok": False, "error": "Not authorized — no matching LDAP group"}
    ephemeral = {
        "id": f"ldap:{username.lower()}",
        "username": username,
        "type": "ldap",
        "roles": group_roles,
        "permission_overrides": {},
        "must_change_password": False,
        "enabled": True,
        "ldap_ephemeral": True,
    }
    token = create_session(db, ephemeral)
    return {
        "ok": True,
        "user": user_payload(ephemeral, settings),
        "token": token,
        "ephemeral": True,
    }


def cookie_header(token: str, idle_minutes: int = 120, secure: bool = False,
                  clear: bool = False) -> str:
    if clear:
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    max_age = max(5, min(1440, idle_minutes)) * 60
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def token_from_cookie(header: str) -> Optional[str]:
    if not header:
        return None
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == COOKIE_NAME:
            return value.strip() or None
    return None


def request_is_secure(headers) -> bool:
    proto = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https"


_LOGIN_PATHS = {"", "/", "/index.html", "/login", "/login.html"}


def next_url(raw: str) -> str:
    value = (raw or "/dashboard").strip() or "/dashboard"
    if not value.startswith("/") or value.startswith("//"):
        return "/dashboard"
    if any(ch in value for ch in ("\r", "\n", "\\")):
        return "/dashboard"
    path, qsep, query = value.partition("?")
    path_only, hsep, fragment = path.partition("#")
    if path_only in _LOGIN_PATHS:
        path_only = "/dashboard"
    return path_only + (hsep + fragment) + (qsep + query)


def parse_query(path: str) -> dict[str, str]:
    if "?" not in path:
        return {}
    qs = parse_qs(path.split("?", 1)[1], keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in qs.items()}


def catalog() -> dict:
    return {
        "roles": DEFAULT_ROLES,
        "permissions": PERMISSIONS,
        "permission_meta": PERMISSION_META,
    }
