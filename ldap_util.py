"""LDAPS user-bind via system ldapsearch (stdlib subprocess).

Install: sudo apt install ldap-utils
Never log bind passwords. Username is restricted to sAMAccountName-safe
characters before it is placed in a filter.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger("nexnoc.ldap")

_USERNAME = re.compile(r"^[A-Za-z0-9._\\-]{1,64}$")


class LdapError(RuntimeError):
    pass


def ldap_build_uri(ldap: dict) -> Optional[str]:
    host = (ldap.get("host") or "").strip()
    if not host:
        return None
    port = int(ldap.get("port") or 636)
    if "://" not in host:
        host = "ldaps://" + host.lstrip("/")
    if not re.search(r":\d+$", host):
        host = f"{host}:{port}"
    return host


def ldap_infer_base_dn(ldap: dict) -> str:
    if (ldap.get("base_dn") or "").strip():
        return ldap["base_dn"].strip()
    template = ldap.get("bind_template") or ""
    match = re.search(r"@([^{}\s]+)", template)
    if not match:
        return ""
    parts = [p for p in match.group(1).lower().split(".") if p]
    return ",".join(f"DC={p}" for p in parts)


def ldap_make_bind_dn(username: str, ldap: dict) -> str:
    template = ldap.get("bind_template") or "{username}"
    return template.replace("{username}", username)


def ldap_group_matches(member_of_dn: str, group_name: str) -> bool:
    group_name = group_name.lower().strip()
    if not group_name:
        return False
    dn = member_of_dn.lower()
    if dn == group_name:
        return True
    match = re.match(r"^cn=([^,]+)", dn)
    if match:
        return match.group(1).lower() == group_name
    return group_name in dn


def roles_from_ldap_groups(member_of: list[str], allowed_groups: list) -> list[str]:
    from auth import DEFAULT_ROLES

    roles: dict[str, bool] = {}
    for entry in allowed_groups:
        if isinstance(entry, str):
            entry = {"name": entry, "roles": ["viewer"]}
        name = entry.get("name") or ""
        group_roles = entry.get("roles") or ["viewer"]
        for dn in member_of:
            if ldap_group_matches(dn, name):
                for role in group_roles:
                    if role in DEFAULT_ROLES:
                        roles[role] = True
    return list(roles)


def _parse_member_of(ldif: str) -> list[str]:
    groups = []
    for line in (ldif or "").splitlines():
        if line.lower().startswith("memberof:"):
            value = line.split(":", 1)[1].strip()
            if value:
                groups.append(value)
    return groups


def ldap_bind_user(ldap: dict, username: str, password: str) -> dict:
    if password == "":
        return {"ok": False, "error": "Password required"}
    if not _USERNAME.match(username):
        return {"ok": False, "error": "Invalid credentials"}

    uri = ldap_build_uri(ldap)
    if not uri:
        return {"ok": False, "error": "LDAP host is not configured"}
    base = ldap_infer_base_dn(ldap)
    if not base:
        return {
            "ok": False,
            "error": "LDAP base DN not configured — set base_dn or use a bind_template with @domain",
        }

    bind_dn = ldap_make_bind_dn(username, ldap)
    env = os.environ.copy()
    if ldap.get("ignore_cert", True):
        env["LDAPTLS_REQCERT"] = "never"

    cmd = [
        "ldapsearch", "-LLL", "-o", "ldif-wrap=no",
        "-H", uri, "-x",
        "-D", bind_dn, "-w", password,
        "-b", base, "-s", "sub",
        f"(sAMAccountName={username})",
        "memberOf",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=8, env=env, check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "LDAP unavailable — install ldap-utils (ldapsearch)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "LDAP server unreachable (timeout)"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ldapsearch failed: %s", exc)
        return {"ok": False, "error": "LDAP unavailable"}

    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        low = err.lower()
        if "can't contact" in low or "connect" in low or "timeout" in low:
            return {"ok": False, "error": "LDAP server unreachable"}
        if "invalid credentials" in low or proc.returncode == 49:
            return {"ok": False, "error": "Invalid username or password"}
        return {"ok": False, "error": "Invalid username or password"}

    return {
        "ok": True,
        "bind_dn": bind_dn,
        "member_of": _parse_member_of(proc.stdout or ""),
    }
