"""
drivers/haivision.py - Haivision Makito X series (Makito X, Makito X4,
Makito MX1/FX) encoders and decoders.

Confirmed against the on-device explorer saved from a live Makito X4
Encoder 1.8.0 at https://<device>/apidoc/ (Huntsville unit 10.207.9.245):

  Auth:     POST /apis/authentication  {"username","password"} over HTTPS
            (session cookie; GET /apis/authentication returns the session)
  Status:   GET  /apis/status          cardStatus, firmwareVersion, serial…
  Inventory GET  /apis/videnc  /apis/audenc  /apis/streams  /apis/vidin
  Also:     /apis/system_info  /apis/services  /apis/datetime  /apidoc

There is no /apis/license in 1.8.0. Do not send start/stop/edit from the
poller — those exist on the device but belong to Phase 4 routing control.
"""

from __future__ import annotations

from typing import Any, Optional

from drivers.base import CollectResult, DiscoveryResult, Driver, InventoryItem, sdi_layout
from drivers.http_util import JsonHttpClient

# Confirmed GET paths from Makito X4 Encoder 1.8.0 /apidoc.
DISCOVERY_CANDIDATES = [
    "/apidoc",
    "/apis/status",
    "/apis/system_info",
    "/apis/authentication",
    "/apis/videnc",
    "/apis/audenc",
    "/apis/streams",
    "/apis/vidin",
    "/apis/services",
    "/apis/datetime",
]


def _as_list(payload: Any) -> list:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if all(isinstance(v, dict) for v in payload.values()):
            return list(payload.values())
    return []


def _item_id(row: dict, fallback: str) -> str:
    for key in ("id", "uid", "name", "label"):
        if row.get(key) not in (None, ""):
            return str(row[key])
    return fallback


def _item_status(row: dict) -> str:
    raw = str(row.get("status") or row.get("state") or "").lower()
    if raw in ("error", "failed", "down", "stopped"):
        return "down"
    if raw in ("warning", "degraded"):
        return "degraded"
    if raw in ("ok", "running", "started", "up", "active"):
        return "healthy"
    return "unknown"


class HaivisionMakitoXDriver(Driver):
    driver_id = "haivision.makito_x.default"
    vendor = "haivision"
    notes = (
        "Default Makito X series driver. Confirmed against Makito X4 Encoder "
        "1.8.0 /apidoc: POST /apis/authentication, GET /apis/status + "
        "videnc/audenc/streams/vidin. No /apis/license in 1.8.0. "
        "Poller must not start/stop/edit."
    )
    # Makito X4 typical SDI count; assignable because a box can encode and/or decode.
    connectors = sdi_layout(4, "assignable")

    def __init__(self, host: str, port: int = 443, scheme: str = "https",
                 username: Optional[str] = None, password: Optional[str] = None,
                 verify_tls: bool = False, timeout: float = 5.0):
        self._username = username
        self._password = password
        self._logged_in = False
        self._client = JsonHttpClient(
            host=host, port=port, scheme=scheme, verify_tls=verify_tls,
            timeout=timeout, use_cookies=True,
        )

    def login(self) -> None:
        """POST /apis/authentication — confirmed login body on 1.8.0."""
        if not self._username or self._password is None:
            return
        self._client.post_json("/apis/authentication", {
            "username": self._username,
            "password": self._password,
        })
        self._logged_in = True

    def _ensure_session(self) -> None:
        if self._logged_in or not self._username:
            return
        self.login()

    def ping(self) -> bool:
        try:
            if self._username and self._password is not None:
                self._ensure_session()
                self._client.get_json("/apis/status")
                return True
            return self._client.ping("/apidoc")
        except Exception:  # noqa: BLE001 - ping must not raise
            return False

    def discover(self, candidates: Optional[list[str]] = None) -> list[DiscoveryResult]:
        try:
            self._ensure_session()
        except Exception:  # noqa: BLE001 - still probe what we can
            pass
        return self._client.discover(candidates or DISCOVERY_CANDIDATES)

    def get_status(self) -> dict:
        self._ensure_session()
        payload = self._client.get_json("/apis/status")
        return payload if isinstance(payload, dict) else {}

    def collect(self) -> Optional[CollectResult]:
        try:
            self._ensure_session()
            status = self.get_status()
        except Exception as exc:  # noqa: BLE001
            return CollectResult(device_status="degraded", error=str(exc))

        card = str(status.get("cardStatus") or "")
        device_status = "healthy" if card.upper() == "OK" or card == "" else "degraded"
        firmware = status.get("firmwareVersion")
        serial = str(status.get("serialNumber") or "")
        modules = [
            InventoryItem(
                slot="system",
                module_type=str(status.get("cardType") or "Makito"),
                firmware_version=str(firmware or ""),
                serial=serial,
                status=device_status,
            )
        ]
        for path, kind in (
            ("/apis/videnc", "videnc"),
            ("/apis/audenc", "audenc"),
            ("/apis/streams", "stream"),
            ("/apis/vidin", "vidin"),
        ):
            try:
                rows = _as_list(self._client.get_json(path))
            except Exception:  # noqa: BLE001 - one resource failing shouldn't abort
                continue
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                modules.append(InventoryItem(
                    slot=f"{kind}:{_item_id(row, str(i))}",
                    module_type=kind,
                    firmware_version=str(firmware or ""),
                    serial=serial,
                    status=_item_status(row),
                ))
        return CollectResult(
            device_status=device_status,
            firmware_version=str(firmware) if firmware else None,
            modules=modules,
        )

    def get_json(self, path: str):
        self._ensure_session()
        return self._client.get_json(path)

    def post_json(self, path: str, body: dict):
        self._ensure_session()
        return self._client.post_json(path, body)
