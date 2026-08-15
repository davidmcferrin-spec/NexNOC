"""
db.py - SQLite data access layer for NexNOC.

Stdlib only (sqlite3). Provides:
  - Database: connection + schema bootstrap
  - Typed CRUD helpers for sites, devices, modules, trunks, signals,
    poll_log, config_snapshots

Design notes:
  - Row factory is sqlite3.Row so callers can access columns by name.
  - Foreign keys are enforced (PRAGMA foreign_keys = ON) per connection, since
    SQLite does not enable this by default.
  - Timestamps are stored as ISO-8601 UTC strings (matches schema.sql defaults)
    so they sort correctly as text and are human-readable in the DB file.
  - No ORM. At this table count/complexity, an ORM adds indirection without
    reducing risk. Every query here is intentionally explicit.
  - "Device" is vendor-agnostic on purpose - an Appear frame, a Haivision
    Makito X4, and a Net Insight Nimbra node are all rows in the same table,
    distinguished by `vendor` and `access_mode`. See schema.sql for the field
    reference and drivers/base.py for how vendor+model+firmware resolve to a driver.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger("nexnoc.db")

VALID_VENDORS = {"appear", "haivision", "net_insight", "generic_snmp"}
VALID_ACCESS_MODES = {"direct_api", "direct_snmp", "via_nms"}
VALID_DEVICE_STATUSES = {"unknown", "healthy", "degraded", "unreachable", "decommissioned"}
VALID_SIGNAL_STATUSES = {"unknown", "up", "degraded", "down"}
VALID_PORT_KINDS = {"sdi_in", "sdi_out", "net", "mgmt", "other"}
VALID_FLOW_STATUSES = VALID_SIGNAL_STATUSES


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string matching schema.sql's format."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class DatabaseError(RuntimeError):
    """Raised for any DB-layer failure that callers should treat as fatal to the operation."""


@dataclass
class Device:
    id: int
    site_id: int
    name: str
    vendor: str
    device_role: Optional[str]
    model: Optional[str]
    firmware_version: Optional[str]
    mgmt_host: str
    access_mode: str
    driver_override: Optional[str]
    resolved_driver: Optional[str]
    api_port: int
    api_scheme: str
    api_verify_tls: bool
    api_username_env: Optional[str]
    api_password_env: Optional[str]
    snmp_host: Optional[str]
    snmp_port: int
    snmp_community_env: Optional[str]
    nms_host: Optional[str]
    nms_port: Optional[int]
    nms_api_key_env: Optional[str]
    nms_device_ref: Optional[str]
    poll_enabled: bool
    status: str
    last_seen_at: Optional[str]
    last_error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Device":
        return cls(
            id=row["id"],
            site_id=row["site_id"],
            name=row["name"],
            vendor=row["vendor"],
            device_role=row["device_role"],
            model=row["model"],
            firmware_version=row["firmware_version"],
            mgmt_host=row["mgmt_host"],
            access_mode=row["access_mode"],
            driver_override=row["driver_override"],
            resolved_driver=row["resolved_driver"],
            api_port=row["api_port"],
            api_scheme=row["api_scheme"],
            api_verify_tls=bool(row["api_verify_tls"]),
            api_username_env=row["api_username_env"],
            api_password_env=row["api_password_env"],
            snmp_host=row["snmp_host"],
            snmp_port=row["snmp_port"],
            snmp_community_env=row["snmp_community_env"],
            nms_host=row["nms_host"],
            nms_port=row["nms_port"],
            nms_api_key_env=row["nms_api_key_env"],
            nms_device_ref=row["nms_device_ref"],
            poll_enabled=bool(row["poll_enabled"]),
            status=row["status"],
            last_seen_at=row["last_seen_at"],
            last_error=row["last_error"],
        )


class Database:
    """Owns the SQLite connection and exposes CRUD operations.

    Usage:
        db = Database("/var/lib/nexnoc/noc.db")
        db.initialize()
        with db.connect() as conn:
            ...
    """

    def __init__(self, db_path: str, schema_path: Optional[str] = None):
        self.db_path = db_path
        self.schema_path = schema_path or str(Path(__file__).parent / "schema.sql")

    def initialize(self) -> None:
        """Create the DB file and apply schema.sql. Idempotent (schema uses IF NOT EXISTS)."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        schema_sql = Path(self.schema_path).read_text(encoding="utf-8")
        with self.connect() as conn:
            try:
                conn.executescript(schema_sql)
                self._migrate_schema(conn)
            except sqlite3.Error as exc:
                raise DatabaseError(f"Failed to apply schema from {self.schema_path}: {exc}") from exc
        logger.info("Database initialized at %s", self.db_path)

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """ADD COLUMN for tables that already existed before cities / signal fan-out."""
        extras = {
            "sites": [("city_id", "INTEGER REFERENCES cities(id)")],
            "flows": [
                ("signal_label", "TEXT"),
                ("dest_city_id", "INTEGER REFERENCES cities(id)"),
                ("origin_device_id", "INTEGER REFERENCES devices(id)"),
                ("origin_port_id", "INTEGER REFERENCES ports(id)"),
            ],
        }
        for table, columns in extras.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------
    def add_city(self, name: str, lat: Optional[float] = None,
                 lng: Optional[float] = None, notes: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO cities (name, lat, lng, notes) VALUES (?, ?, ?, ?)",
                (name, lat, lng, notes),
            )
            return cur.lastrowid

    def list_cities(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cities ORDER BY name").fetchall()

    def get_city(self, city_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cities WHERE id = ?", (city_id,)).fetchone()

    def get_city_by_name(self, name: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cities WHERE name = ?", (name,)).fetchone()

    def update_city(self, city_id: int, name: Optional[str] = None,
                    lat: Optional[float] = None, lng: Optional[float] = None,
                    notes: Optional[str] = None) -> None:
        fields: list[str] = []
        params: list = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if lat is not None:
            fields.append("lat = ?")
            params.append(lat)
        if lng is not None:
            fields.append("lng = ?")
            params.append(lng)
        if notes is not None:
            fields.append("notes = ?")
            params.append(notes)
        if not fields:
            return
        params.append(city_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE cities SET {', '.join(fields)} WHERE id = ?", params)

    def delete_city(self, city_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM cities WHERE id = ?", (city_id,))
            return cur.rowcount > 0

    def add_site(self, name: str, city: str = "", lat: Optional[float] = None,
                 lng: Optional[float] = None, notes: str = "",
                 city_id: Optional[int] = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sites (name, city, city_id, lat, lng, notes) VALUES (?, ?, ?, ?, ?, ?)",
                (name, city, city_id, lat, lng, notes),
            )
            return cur.lastrowid

    def set_site_city(self, site_id: int, city_id: Optional[int], city: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sites SET city_id = ?, city = CASE WHEN ? != '' THEN ? ELSE city END WHERE id = ?",
                (city_id, city, city, site_id),
            )

    def list_sites(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT s.*, COALESCE(c.name, s.city, '') AS city_name,
                       c.lat AS city_lat, c.lng AS city_lng
                FROM sites s
                LEFT JOIN cities c ON c.id = s.city_id
                ORDER BY city_name, s.name
                """
            ).fetchall()

    def get_site(self, site_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()

    def get_site_by_name(self, name: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sites WHERE name = ?", (name,)).fetchone()

    def update_site(self, site_id: int, **fields) -> None:
        allowed = {"name", "city", "city_id", "lat", "lng", "notes"}
        sets, params = ["updated_at = ?"], [utcnow_iso()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update site field {key!r}")
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(site_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", params)

    def delete_site(self, site_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------
    def add_device(
        self,
        site_id: int,
        name: str,
        vendor: str,
        mgmt_host: str,
        device_role: str = "",
        model: str = "",
        firmware_version: str = "",
        access_mode: str = "direct_api",
        driver_override: Optional[str] = None,
        api_port: int = 443,
        api_scheme: str = "https",
        api_verify_tls: bool = False,
        api_username_env: Optional[str] = None,
        api_password_env: Optional[str] = None,
        snmp_host: Optional[str] = None,
        snmp_port: int = 161,
        snmp_community_env: Optional[str] = None,
        nms_host: Optional[str] = None,
        nms_port: Optional[int] = None,
        nms_api_key_env: Optional[str] = None,
        nms_device_ref: Optional[str] = None,
        poll_enabled: bool = True,
    ) -> int:
        if vendor not in VALID_VENDORS:
            raise ValueError(f"unknown vendor {vendor!r}, must be one of {sorted(VALID_VENDORS)}")
        if access_mode not in VALID_ACCESS_MODES:
            raise ValueError(f"invalid access_mode {access_mode!r}, must be one of {sorted(VALID_ACCESS_MODES)}")
        host = mgmt_host if mgmt_host is not None else ""
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO devices (
                    site_id, name, vendor, device_role, model, firmware_version, mgmt_host,
                    access_mode, driver_override,
                    api_port, api_scheme, api_verify_tls, api_username_env, api_password_env,
                    snmp_host, snmp_port, snmp_community_env,
                    nms_host, nms_port, nms_api_key_env, nms_device_ref,
                    poll_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    site_id, name, vendor, device_role, model, firmware_version, host,
                    access_mode, driver_override,
                    api_port, api_scheme, int(api_verify_tls), api_username_env, api_password_env,
                    snmp_host if snmp_host is not None else host, snmp_port, snmp_community_env,
                    nms_host, nms_port, nms_api_key_env, nms_device_ref,
                    int(poll_enabled),
                ),
            )
            return cur.lastrowid

    def remove_device(self, device_id: int) -> None:
        """Hard delete. Cascades to modules/signals/config_snapshots/poll_log per schema FKs.
        For devices that have ever been polled, consider set_device_status(..., 'decommissioned')
        instead so history in poll_log / config_snapshots survives."""
        with self.connect() as conn:
            conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def update_device(self, device_id: int, **fields) -> None:
        allowed = {
            "site_id", "name", "vendor", "device_role", "model", "firmware_version",
            "mgmt_host", "access_mode", "driver_override",
            "api_port", "api_scheme", "api_verify_tls",
            "api_username_env", "api_password_env",
            "snmp_host", "snmp_port", "snmp_community_env",
            "nms_host", "nms_port", "nms_api_key_env", "nms_device_ref",
            "poll_enabled",
        }
        bools = {"api_verify_tls", "poll_enabled"}
        sets, params = ["updated_at = ?"], [utcnow_iso()]
        if "vendor" in fields and fields["vendor"] not in VALID_VENDORS:
            raise ValueError(
                f"unknown vendor {fields['vendor']!r}, must be one of {sorted(VALID_VENDORS)}"
            )
        if "access_mode" in fields and fields["access_mode"] not in VALID_ACCESS_MODES:
            raise ValueError(
                f"invalid access_mode {fields['access_mode']!r}, "
                f"must be one of {sorted(VALID_ACCESS_MODES)}"
            )
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update device field {key!r}")
            if key in bools and value is not None:
                value = int(bool(value))
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(device_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", params)

    def list_devices(self, include_decommissioned: bool = False, vendor: Optional[str] = None) -> list[Device]:
        with self.connect() as conn:
            query = "SELECT * FROM devices"
            clauses, params = [], []
            if not include_decommissioned:
                clauses.append("status != 'decommissioned'")
            if vendor:
                clauses.append("vendor = ?")
                params.append(vendor)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY name"
            rows = conn.execute(query, params).fetchall()
            return [Device.from_row(r) for r in rows]

    def get_device(self, device_id: int) -> Optional[Device]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            return Device.from_row(row) if row else None

    def set_device_status(self, device_id: int, status: str, error: Optional[str] = None) -> None:
        if status not in VALID_DEVICE_STATUSES:
            raise ValueError(f"invalid device status {status!r}, must be one of {sorted(VALID_DEVICE_STATUSES)}")
        now = utcnow_iso()
        with self.connect() as conn:
            if status in ("healthy", "degraded"):
                conn.execute(
                    "UPDATE devices SET status = ?, last_error = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
                    (status, error, now, now, device_id),
                )
            else:
                conn.execute(
                    "UPDATE devices SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (status, error, now, device_id),
                )

    def set_device_firmware(self, device_id: int, firmware_version: str) -> None:
        """Write back firmware discovered during collect() so later
        resolve_driver() calls can match a firmware-ranged driver."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE devices SET firmware_version = ?, updated_at = ? WHERE id = ?",
                (firmware_version, utcnow_iso(), device_id),
            )

    def set_resolved_driver(self, device_id: int, driver_id: str) -> None:
        """Record which driver_id was actually used for a device's most
        recent poll - informational only (doesn't affect future resolution;
        that's always recomputed from vendor/model/firmware_version/
        driver_override). Lets the interface show "this device is using
        driver X" without recomputing resolve_driver() itself."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE devices SET resolved_driver = ?, updated_at = ? WHERE id = ?",
                (driver_id, utcnow_iso(), device_id),
            )

    # ------------------------------------------------------------------
    # Modules
    # ------------------------------------------------------------------
    def upsert_module(self, device_id: int, slot: str, module_type: str = "",
                       firmware_version: str = "", serial: str = "", status: str = "unknown") -> None:
        now = utcnow_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO modules (device_id, slot, module_type, firmware_version, serial, status, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, slot) DO UPDATE SET
                    module_type = excluded.module_type,
                    firmware_version = excluded.firmware_version,
                    serial = excluded.serial,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at
                """,
                (device_id, slot, module_type, firmware_version, serial, status, now),
            )

    def list_modules(self, device_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM modules WHERE device_id = ? ORDER BY slot", (device_id,)
            ).fetchall()

    # ------------------------------------------------------------------
    # Poll log
    # ------------------------------------------------------------------
    def record_poll(self, device_id: int, method: str, success: bool,
                     latency_ms: Optional[int] = None, error_message: Optional[str] = None) -> None:
        if method not in ("api", "snmp", "nms"):
            raise ValueError("method must be 'api', 'snmp', or 'nms'")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO poll_log (device_id, method, success, latency_ms, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, method, int(success), latency_ms, error_message),
            )

    def recent_poll_history(self, device_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM poll_log WHERE device_id = ? ORDER BY polled_at DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()

    # ------------------------------------------------------------------
    # Config snapshots (data layer only in Phase 1; capture logic lands Phase 3)
    # ------------------------------------------------------------------
    def add_config_snapshot(self, device_id: int, config_hash: str, config_json: str,
                             note: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO config_snapshots (device_id, config_hash, config_json, note) VALUES (?, ?, ?, ?)",
                (device_id, config_hash, config_json, note),
            )
            return cur.lastrowid

    def latest_config_snapshot(self, device_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM config_snapshots WHERE device_id = ? ORDER BY taken_at DESC LIMIT 1",
                (device_id,),
            ).fetchone()

    def latest_poll_at(self) -> Optional[str]:
        """Most recent poll_log timestamp across all devices, or None if never polled."""
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(polled_at) AS ts FROM poll_log").fetchone()
            return row["ts"] if row else None

    # ------------------------------------------------------------------
    # Trunks (Phase 2 geo view)
    # ------------------------------------------------------------------
    def add_trunk(self, site_a_id: int, site_b_id: int, label: str) -> int:
        if site_a_id == site_b_id:
            raise ValueError("trunk endpoints must be different sites")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO trunks (site_a_id, site_b_id, label) VALUES (?, ?, ?)",
                (site_a_id, site_b_id, label),
            )
            return cur.lastrowid

    def list_trunks(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    t.id, t.site_a_id, t.site_b_id, t.label, t.created_at,
                    a.name AS site_a_name, a.lat AS site_a_lat, a.lng AS site_a_lng,
                    b.name AS site_b_name, b.lat AS site_b_lat, b.lng AS site_b_lng
                FROM trunks t
                JOIN sites a ON a.id = t.site_a_id
                JOIN sites b ON b.id = t.site_b_id
                ORDER BY t.label
                """
            ).fetchall()

    def get_trunk(self, trunk_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM trunks WHERE id = ?", (trunk_id,)).fetchone()

    def get_trunk_by_label(self, label: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM trunks WHERE label = ?", (label,)).fetchone()

    # ------------------------------------------------------------------
    # Signals (Phase 2 table + trunk drill-down)
    # ------------------------------------------------------------------
    def add_signal(
        self,
        device_id: int,
        source_label: str,
        destination_label: str,
        trunk_id: Optional[int] = None,
        direction: str = "",
        status: str = "unknown",
    ) -> int:
        if status not in VALID_SIGNAL_STATUSES:
            raise ValueError(
                f"invalid signal status {status!r}, must be one of {sorted(VALID_SIGNAL_STATUSES)}"
            )
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    device_id, trunk_id, source_label, destination_label, direction, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (device_id, trunk_id, source_label, destination_label, direction, status),
            )
            return cur.lastrowid

    def list_signals(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    s.id, s.device_id, s.trunk_id, s.source_label, s.destination_label,
                    s.direction, s.status, s.last_status_change, s.last_polled_at,
                    d.name AS device_name, d.status AS device_status, d.vendor AS device_vendor,
                    d.site_id AS site_id,
                    site.name AS site_name,
                    t.label AS trunk_label
                FROM signals s
                JOIN devices d ON d.id = s.device_id
                JOIN sites site ON site.id = d.site_id
                LEFT JOIN trunks t ON t.id = s.trunk_id
                ORDER BY s.source_label, s.destination_label
                """
            ).fetchall()

    def get_signal(self, signal_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()

    def find_signal(
        self, device_id: int, source_label: str, destination_label: str
    ) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM signals
                WHERE device_id = ? AND source_label = ? AND destination_label = ?
                """,
                (device_id, source_label, destination_label),
            ).fetchone()

    def add_port(self, device_id: int, name: str, kind: str = "other",
                 slot: str = "", status: str = "unknown") -> int:
        if kind not in VALID_PORT_KINDS:
            raise ValueError(f"invalid port kind {kind!r}, must be one of {sorted(VALID_PORT_KINDS)}")
        if status not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid port status {status!r}")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO ports (device_id, name, kind, slot, status) VALUES (?, ?, ?, ?, ?)",
                (device_id, name, kind, slot, status),
            )
            return cur.lastrowid

    def list_ports(self, device_id: Optional[int] = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if device_id is None:
                return conn.execute(
                    """
                    SELECT p.*, d.name AS device_name, d.site_id
                    FROM ports p JOIN devices d ON d.id = p.device_id
                    ORDER BY d.name, p.kind, p.name
                    """
                ).fetchall()
            return conn.execute(
                "SELECT * FROM ports WHERE device_id = ? ORDER BY kind, name",
                (device_id,),
            ).fetchall()

    def find_port(self, device_id: int, name: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM ports WHERE device_id = ? AND name = ?",
                (device_id, name),
            ).fetchone()

    def get_port(self, port_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM ports WHERE id = ?", (port_id,)).fetchone()

    def update_port(self, port_id: int, **fields) -> None:
        allowed = {"device_id", "name", "kind", "slot", "status"}
        if "kind" in fields and fields["kind"] not in VALID_PORT_KINDS:
            raise ValueError(f"invalid port kind {fields['kind']!r}")
        if "status" in fields and fields["status"] not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid port status {fields['status']!r}")
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update port field {key!r}")
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(port_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE ports SET {', '.join(sets)} WHERE id = ?", params)

    def delete_port(self, port_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM ports WHERE id = ?", (port_id,))
            return cur.rowcount > 0

    def add_flow(
        self,
        label: str,
        source_device_id: int,
        source_port_id: Optional[int] = None,
        dest_site_id: Optional[int] = None,
        dest_device_id: Optional[int] = None,
        dest_port_id: Optional[int] = None,
        dest_label: str = "",
        direction: str = "",
        status: str = "unknown",
        signal_label: str = "",
        dest_city_id: Optional[int] = None,
        origin_device_id: Optional[int] = None,
        origin_port_id: Optional[int] = None,
    ) -> int:
        if status not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid flow status {status!r}")
        if (
            dest_site_id is None and dest_device_id is None
            and dest_city_id is None and not dest_label
        ):
            raise ValueError("flow needs dest_city_id, dest_site_id, dest_device_id, or dest_label")
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO flows (
                    label, signal_label, source_device_id, source_port_id,
                    origin_device_id, origin_port_id,
                    dest_city_id, dest_site_id, dest_device_id, dest_port_id, dest_label,
                    direction, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    label, signal_label or None, source_device_id, source_port_id,
                    origin_device_id, origin_port_id,
                    dest_city_id, dest_site_id, dest_device_id, dest_port_id, dest_label,
                    direction, status,
                ),
            )
            return cur.lastrowid

    def list_flows(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    f.id, f.label, f.signal_label, f.direction, f.status,
                    f.last_status_change, f.last_polled_at, f.dest_label,
                    f.source_device_id, f.source_port_id,
                    f.origin_device_id, f.origin_port_id,
                    f.dest_city_id, f.dest_site_id, f.dest_device_id, f.dest_port_id,
                    src.name AS source_device_name,
                    src.status AS source_device_status,
                    src.vendor AS source_device_vendor,
                    src.site_id AS source_site_id,
                    src_site.name AS source_site_name,
                    src_site.lat AS source_site_lat,
                    src_site.lng AS source_site_lng,
                    src_site.city_id AS source_city_id,
                    COALESCE(src_city.name, src_site.city, '') AS source_city_name,
                    sp.name AS source_port_name,
                    sp.kind AS source_port_kind,
                    orig.name AS origin_device_name,
                    orig_site.name AS origin_site_name,
                    COALESCE(orig_city.name, orig_site.city, '') AS origin_city_name,
                    op.name AS origin_port_name,
                    dest_city.name AS dest_city_name,
                    dest_city.lat AS dest_city_lat,
                    dest_city.lng AS dest_city_lng,
                    dst_site.name AS dest_site_name,
                    dst_site.lat AS dest_site_lat,
                    dst_site.lng AS dest_site_lng,
                    dst_site.city_id AS dest_site_city_id,
                    COALESCE(dest_city.name, dst_city.name, dst_site.city, '') AS dest_city_resolved,
                    dst.name AS dest_device_name,
                    dp.name AS dest_port_name
                FROM flows f
                JOIN devices src ON src.id = f.source_device_id
                JOIN sites src_site ON src_site.id = src.site_id
                LEFT JOIN cities src_city ON src_city.id = src_site.city_id
                LEFT JOIN ports sp ON sp.id = f.source_port_id
                LEFT JOIN devices orig ON orig.id = f.origin_device_id
                LEFT JOIN sites orig_site ON orig_site.id = orig.site_id
                LEFT JOIN cities orig_city ON orig_city.id = orig_site.city_id
                LEFT JOIN ports op ON op.id = f.origin_port_id
                LEFT JOIN cities dest_city ON dest_city.id = f.dest_city_id
                LEFT JOIN sites dst_site ON dst_site.id = f.dest_site_id
                LEFT JOIN cities dst_city ON dst_city.id = dst_site.city_id
                LEFT JOIN devices dst ON dst.id = f.dest_device_id
                LEFT JOIN ports dp ON dp.id = f.dest_port_id
                ORDER BY COALESCE(f.signal_label, f.label), src.name, f.id
                """
            ).fetchall()

    def find_flow(
        self,
        source_device_id: int,
        label: str,
        dest_site_id: Optional[int],
        dest_device_id: Optional[int],
        dest_label: str,
        dest_city_id: Optional[int] = None,
    ) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM flows
                WHERE source_device_id = ? AND label = ?
                  AND IFNULL(dest_site_id, -1) = IFNULL(?, -1)
                  AND IFNULL(dest_device_id, -1) = IFNULL(?, -1)
                  AND IFNULL(dest_city_id, -1) = IFNULL(?, -1)
                  AND IFNULL(dest_label, '') = IFNULL(?, '')
                """,
                (
                    source_device_id, label, dest_site_id, dest_device_id,
                    dest_city_id, dest_label or "",
                ),
            ).fetchone()

    def get_flow(self, flow_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()

    def update_flow(self, flow_id: int, **fields) -> None:
        allowed = {
            "label", "signal_label", "source_device_id", "source_port_id",
            "origin_device_id", "origin_port_id",
            "dest_city_id", "dest_site_id", "dest_device_id", "dest_port_id",
            "dest_label", "direction", "status",
        }
        if "status" in fields and fields["status"] not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid flow status {fields['status']!r}")
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update flow field {key!r}")
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(flow_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE flows SET {', '.join(sets)} WHERE id = ?", params)

    def delete_flow(self, flow_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
            return cur.rowcount > 0

    def set_flow_status(self, flow_id: int, status: str) -> None:
        if status not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid flow status {status!r}")
        now = utcnow_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE flows
                SET status = ?, last_status_change = ?, last_polled_at = ?
                WHERE id = ?
                """,
                (status, now, now, flow_id),
            )

    def set_signal_status(self, signal_id: int, status: str) -> None:
        if status not in VALID_SIGNAL_STATUSES:
            raise ValueError(
                f"invalid signal status {status!r}, must be one of {sorted(VALID_SIGNAL_STATUSES)}"
            )
        now = utcnow_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE signals
                SET status = ?, last_status_change = ?, last_polled_at = ?
                WHERE id = ?
                """,
                (status, now, now, signal_id),
            )
