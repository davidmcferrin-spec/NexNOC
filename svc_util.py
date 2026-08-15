"""Admin → Services: status / logs / restart via scripts/nexnoc-svc.

nexnoc-web runs as the nexnoc user and cannot talk to systemd directly.
setup.sh installs a sudoers drop-in that allows only this helper.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

SERVICE_UNITS = (
    ("nexnoc-web", "Dashboard (loopback HTTP)"),
    ("nexnoc-poller", "Device health poller"),
    ("nexnoc-trapd", "SNMP trap listener"),
    ("apache2", "Apache (site + API proxy)"),
)
ALLOWED_UNITS = {unit for unit, _label in SERVICE_UNITS}
# Restarting these kills the request path (origin or the reverse proxy),
# so the helper is fired and forgotten and the API returns 202 immediately.
DETACHED_RESTART_UNITS = frozenset({"nexnoc-web", "apache2"})
DEFAULT_HELPER = "/opt/nexnoc/scripts/nexnoc-svc"


class SvcError(RuntimeError):
    pass


def helper_path() -> str:
    override = os.environ.get("NEXNOC_SVC_HELPER", "").strip()
    if override:
        return override
    local = Path(__file__).resolve().parent / "scripts" / "nexnoc-svc"
    if local.is_file():
        return str(local)
    return DEFAULT_HELPER


def check_unit(unit: str) -> str:
    name = (unit or "").strip()
    if name not in ALLOWED_UNITS:
        raise SvcError(f"unknown unit {unit!r}")
    return name


def parse_show(text: str) -> dict:
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        raw[key.strip()] = value.strip()
    pid_raw = raw.get("MainPID") or "0"
    restarts_raw = raw.get("NRestarts") or "0"
    return {
        "active": raw.get("ActiveState") or "unknown",
        "sub": raw.get("SubState") or "",
        "description": raw.get("Description") or "",
        "pid": int(pid_raw) if pid_raw.isdigit() else 0,
        "restarts": int(restarts_raw) if restarts_raw.isdigit() else 0,
        "since": raw.get("ActiveEnterTimestamp") or "",
    }


def _run(args: list[str], timeout: float = 15) -> str:
    cmd = ["sudo", "-n", helper_path(), *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SvcError("sudo or nexnoc-svc is not installed — run setup.sh") from exc
    except subprocess.TimeoutExpired as exc:
        raise SvcError("service helper timed out") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "service helper failed").strip()
        raise SvcError(err)
    return result.stdout


def service_status(unit: str) -> dict:
    return parse_show(_run(["status", check_unit(unit)]))


def service_logs(unit: str, lines: int = 200) -> str:
    unit = check_unit(unit)
    count = max(1, min(int(lines), 1000))
    return _run(["logs", unit, str(count)], timeout=20)


def restart_service(unit: str) -> dict:
    unit = check_unit(unit)
    if unit in DETACHED_RESTART_UNITS:
        try:
            subprocess.Popen(
                ["sudo", "-n", helper_path(), "restart", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SvcError("sudo or nexnoc-svc is not installed — run setup.sh") from exc
        return {"ok": True, "restarting": True}
    _run(["restart", unit], timeout=30)
    return {"ok": True, "restarting": False}


def list_services() -> dict:
    services = []
    available = True
    error: Optional[str] = None
    for unit, label in SERVICE_UNITS:
        row = {
            "id": unit,
            "label": label,
            "active": "unknown",
            "sub": "",
            "description": "",
            "pid": 0,
            "restarts": 0,
            "since": "",
        }
        try:
            row.update(service_status(unit))
        except SvcError as exc:
            available = False
            error = str(exc)
            row["error"] = str(exc)
        services.append(row)
    return {"available": available, "error": error, "services": services}
