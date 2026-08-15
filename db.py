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

import json
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
VALID_PORT_CAPABILITIES = {"", "input", "output", "assignable"}
VALID_PORT_DIRECTIONS = {"", "input", "output", "unused"}
VALID_GEO_SOURCES = {"", "geocode", "manual"}
VALID_FLOW_STATUSES = VALID_SIGNAL_STATUSES
VALID_SNMP_VERSIONS = {"1", "2c", "3"}


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
    control_driver: Optional[str]
    resolved_driver: Optional[str]
    api_port: int
    api_scheme: str
    api_verify_tls: bool
    api_username_env: Optional[str]
    api_password_env: Optional[str]
    snmp_host: Optional[str]
    snmp_port: int
    snmp_community_env: Optional[str]
    snmp_version: str
    snmp_enabled: bool
    snmp_trap_enabled: bool
    snmp_v3_user_env: Optional[str]
    snmp_v3_sec_level: Optional[str]
    snmp_v3_auth_proto: Optional[str]
    snmp_v3_auth_pass_env: Optional[str]
    snmp_v3_priv_proto: Optional[str]
    snmp_v3_priv_pass_env: Optional[str]
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
        keys = set(row.keys())

        def col(name, default=None):
            return row[name] if name in keys else default

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
            control_driver=col("control_driver"),
            resolved_driver=row["resolved_driver"],
            api_port=row["api_port"],
            api_scheme=row["api_scheme"],
            api_verify_tls=bool(row["api_verify_tls"]),
            api_username_env=row["api_username_env"],
            api_password_env=row["api_password_env"],
            snmp_host=row["snmp_host"],
            snmp_port=row["snmp_port"],
            snmp_community_env=row["snmp_community_env"],
            snmp_version=col("snmp_version") or "2c",
            snmp_enabled=bool(col("snmp_enabled", 0)),
            snmp_trap_enabled=bool(col("snmp_trap_enabled", 1)),
            snmp_v3_user_env=col("snmp_v3_user_env"),
            snmp_v3_sec_level=col("snmp_v3_sec_level") or "authPriv",
            snmp_v3_auth_proto=col("snmp_v3_auth_proto") or "SHA",
            snmp_v3_auth_pass_env=col("snmp_v3_auth_pass_env"),
            snmp_v3_priv_proto=col("snmp_v3_priv_proto") or "AES",
            snmp_v3_priv_pass_env=col("snmp_v3_priv_pass_env"),
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
        from auth import ensure_seeded
        ensure_seeded(self)
        logger.info("Database initialized at %s", self.db_path)

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """ADD COLUMN for tables that already existed before cities / signal fan-out."""
        extras = {
            "cities": [("geo_source", "TEXT NOT NULL DEFAULT ''")],
            "sites": [
                ("city_id", "INTEGER REFERENCES cities(id)"),
                ("address", "TEXT"),
                ("geo_source", "TEXT NOT NULL DEFAULT ''"),
                ("pin_icon", "TEXT NOT NULL DEFAULT 'building'"),
                ("pin_color", "TEXT NOT NULL DEFAULT '#6aa4ff'"),
                ("pin_upload", "TEXT"),
            ],
            "flows": [
                ("signal_label", "TEXT"),
                ("dest_city_id", "INTEGER REFERENCES cities(id)"),
                ("origin_device_id", "INTEGER REFERENCES devices(id)"),
                ("origin_port_id", "INTEGER REFERENCES ports(id)"),
            ],
            "devices": [
                ("snmp_version", "TEXT DEFAULT '2c'"),
                ("snmp_enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("snmp_trap_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("snmp_v3_user_env", "TEXT"),
                ("snmp_v3_sec_level", "TEXT DEFAULT 'authPriv'"),
                ("snmp_v3_auth_proto", "TEXT DEFAULT 'SHA'"),
                ("snmp_v3_auth_pass_env", "TEXT"),
                ("snmp_v3_priv_proto", "TEXT DEFAULT 'AES'"),
                ("snmp_v3_priv_pass_env", "TEXT"),
                ("control_driver", "TEXT"),
            ],
            "ports": [
                ("capability", "TEXT NOT NULL DEFAULT ''"),
                ("direction", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table, columns in extras.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_mgmt_host "
                "ON devices(mgmt_host) WHERE mgmt_host != ''"
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "duplicate management IPs exist; unique index not applied"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
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
                 lng: Optional[float] = None, notes: str = "",
                 geo_source: str = "") -> int:
        if geo_source not in VALID_GEO_SOURCES:
            raise ValueError(f"invalid geo_source {geo_source!r}")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO cities (name, lat, lng, notes, geo_source) VALUES (?, ?, ?, ?, ?)",
                (name, lat, lng, notes, geo_source),
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
                    notes: Optional[str] = None, geo_source: Optional[str] = None) -> None:
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
        if geo_source is not None:
            if geo_source not in VALID_GEO_SOURCES:
                raise ValueError(f"invalid geo_source {geo_source!r}")
            fields.append("geo_source = ?")
            params.append(geo_source)
        if not fields:
            return
        params.append(city_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE cities SET {', '.join(fields)} WHERE id = ?", params)

    def count_sites_for_city(self, city_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sites WHERE city_id = ?", (city_id,)
            ).fetchone()
            return int(row["n"])

    def delete_city(self, city_id: int) -> bool:
        n_sites = self.count_sites_for_city(city_id)
        if n_sites:
            raise ValueError(
                f"Move or delete this city's {n_sites} site"
                f"{'' if n_sites == 1 else 's'} first."
            )
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM cities WHERE id = ?", (city_id,))
            return cur.rowcount > 0

    def add_site(self, name: str, city: str = "", lat: Optional[float] = None,
                 lng: Optional[float] = None, notes: str = "",
                 city_id: Optional[int] = None, address: str = "",
                 geo_source: str = "", pin_icon: str = "building",
                 pin_color: str = "#6aa4ff", pin_upload: Optional[str] = None) -> int:
        if geo_source not in VALID_GEO_SOURCES:
            raise ValueError(f"invalid geo_source {geo_source!r}")
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sites (
                    name, city, city_id, address, lat, lng, notes,
                    geo_source, pin_icon, pin_color, pin_upload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, city, city_id, address, lat, lng, notes,
                 geo_source, pin_icon, pin_color, pin_upload),
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
        allowed = {
            "name", "city", "city_id", "address", "lat", "lng", "notes",
            "geo_source", "pin_icon", "pin_color", "pin_upload",
        }
        if "geo_source" in fields and fields["geo_source"] not in VALID_GEO_SOURCES:
            raise ValueError(f"invalid geo_source {fields['geo_source']!r}")
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

    def merge_sites(self, source_id: int, target_id: int) -> None:
        """Move devices/flows/trunks from source onto target, then delete source."""
        if source_id == target_id:
            return
        if self.get_site(source_id) is None or self.get_site(target_id) is None:
            raise ValueError("both sites must exist to merge")
        with self.connect() as conn:
            conn.execute(
                "UPDATE devices SET site_id = ?, updated_at = ? WHERE site_id = ?",
                (target_id, utcnow_iso(), source_id),
            )
            conn.execute(
                "UPDATE flows SET dest_site_id = ? WHERE dest_site_id = ?",
                (target_id, source_id),
            )
            trunks = conn.execute(
                "SELECT id, site_a_id, site_b_id FROM trunks "
                "WHERE site_a_id = ? OR site_b_id = ?",
                (source_id, source_id),
            ).fetchall()
            for trunk in trunks:
                site_a = target_id if trunk["site_a_id"] == source_id else trunk["site_a_id"]
                site_b = target_id if trunk["site_b_id"] == source_id else trunk["site_b_id"]
                if site_a == site_b:
                    conn.execute("DELETE FROM trunks WHERE id = ?", (trunk["id"],))
                    continue
                conn.execute(
                    "UPDATE trunks SET site_a_id = ?, site_b_id = ? WHERE id = ?",
                    (site_a, site_b, trunk["id"]),
                )
            conn.execute("DELETE FROM sites WHERE id = ?", (source_id,))

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
        control_driver: Optional[str] = None,
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
        snmp_version: str = "2c",
        snmp_enabled: Optional[bool] = None,
        snmp_trap_enabled: bool = True,
        snmp_v3_user_env: Optional[str] = None,
        snmp_v3_sec_level: str = "authPriv",
        snmp_v3_auth_proto: str = "SHA",
        snmp_v3_auth_pass_env: Optional[str] = None,
        snmp_v3_priv_proto: str = "AES",
        snmp_v3_priv_pass_env: Optional[str] = None,
    ) -> int:
        if vendor not in VALID_VENDORS:
            raise ValueError(f"unknown vendor {vendor!r}, must be one of {sorted(VALID_VENDORS)}")
        if access_mode not in VALID_ACCESS_MODES:
            raise ValueError(f"invalid access_mode {access_mode!r}, must be one of {sorted(VALID_ACCESS_MODES)}")
        if snmp_version not in VALID_SNMP_VERSIONS:
            raise ValueError(f"invalid snmp_version {snmp_version!r}")
        host = (mgmt_host or "").strip()
        if snmp_enabled is None:
            # All three channels (API / SNMP GET / traps) are capable by
            # default. GET still no-ops until community or v3 creds exist.
            snmp_enabled = True
        self._require_unique_mgmt_host(host)
        snmp = (snmp_host if snmp_host is not None else host) or ""
        if snmp.strip() and snmp.strip() != host:
            self._require_unique_mgmt_host(snmp.strip())
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO devices (
                        site_id, name, vendor, device_role, model, firmware_version, mgmt_host,
                        access_mode, driver_override, control_driver,
                        api_port, api_scheme, api_verify_tls, api_username_env, api_password_env,
                        snmp_host, snmp_port, snmp_community_env, snmp_version,
                        snmp_enabled, snmp_trap_enabled,
                        snmp_v3_user_env, snmp_v3_sec_level, snmp_v3_auth_proto, snmp_v3_auth_pass_env,
                        snmp_v3_priv_proto, snmp_v3_priv_pass_env,
                        nms_host, nms_port, nms_api_key_env, nms_device_ref,
                        poll_enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        site_id, name, vendor, device_role, model, firmware_version, host,
                        access_mode, driver_override, control_driver,
                        api_port, api_scheme, int(api_verify_tls), api_username_env, api_password_env,
                        snmp_host if snmp_host is not None else host, snmp_port, snmp_community_env,
                        snmp_version, int(snmp_enabled), int(snmp_trap_enabled),
                        snmp_v3_user_env, snmp_v3_sec_level, snmp_v3_auth_proto, snmp_v3_auth_pass_env,
                        snmp_v3_priv_proto, snmp_v3_priv_pass_env,
                        nms_host, nms_port, nms_api_key_env, nms_device_ref,
                        int(poll_enabled),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"cannot add device: {exc}") from exc
            return cur.lastrowid

    def remove_device(self, device_id: int) -> None:
        """Hard delete. Cascades to modules/signals/config_snapshots/poll_log per schema FKs.
        For devices that have ever been polled, consider set_device_status(..., 'decommissioned')
        instead so history in poll_log / config_snapshots survives."""
        with self.connect() as conn:
            conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def merge_devices(self, source_id: int, target_id: int) -> None:
        """Move ports, flows, and history from source onto target, then delete source.

        Same-named ports stay on the target; flows that pointed at the source
        port are remapped. Blank target fields (mgmt_host, model, credential
        env names) are filled from the source.
        """
        if source_id == target_id:
            raise ValueError("cannot merge a device into itself")
        source = self.get_device(source_id)
        target = self.get_device(target_id)
        if source is None or target is None:
            raise ValueError("both devices must exist to merge")
        now = utcnow_iso()
        with self.connect() as conn:
            src = conn.execute("SELECT * FROM devices WHERE id = ?", (source_id,)).fetchone()
            tgt = conn.execute("SELECT * FROM devices WHERE id = ?", (target_id,)).fetchone()
            fills = []
            params = []
            if not (tgt["mgmt_host"] or "").strip() and (src["mgmt_host"] or "").strip():
                conn.execute("UPDATE devices SET mgmt_host = '' WHERE id = ?", (source_id,))
                fills.append("mgmt_host = ?")
                params.append(src["mgmt_host"])
            for col in (
                "model", "firmware_version", "snmp_host", "driver_override",
                "api_username_env", "api_password_env", "snmp_community_env",
                "snmp_v3_user_env", "snmp_v3_auth_pass_env", "snmp_v3_priv_pass_env",
                "nms_host", "nms_api_key_env", "nms_device_ref",
            ):
                if not (tgt[col] or "").strip() and (src[col] or "").strip():
                    fills.append(f"{col} = ?")
                    params.append(src[col])
            if fills:
                params.extend([now, target_id])
                conn.execute(
                    f"UPDATE devices SET {', '.join(fills)}, updated_at = ? WHERE id = ?",
                    params,
                )

            src_ports = conn.execute(
                "SELECT * FROM ports WHERE device_id = ?", (source_id,),
            ).fetchall()
            for port in src_ports:
                existing = conn.execute(
                    "SELECT id FROM ports WHERE device_id = ? AND name = ?",
                    (target_id, port["name"]),
                ).fetchone()
                if existing:
                    for col in ("source_port_id", "dest_port_id", "origin_port_id"):
                        conn.execute(
                            f"UPDATE flows SET {col} = ? WHERE {col} = ?",
                            (existing["id"], port["id"]),
                        )
                    conn.execute("DELETE FROM ports WHERE id = ?", (port["id"],))
                else:
                    conn.execute(
                        "UPDATE ports SET device_id = ? WHERE id = ?",
                        (target_id, port["id"]),
                    )

            for table, key in (
                ("modules", "slot"),
                ("licenses", "feature_name"),
            ):
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE device_id = ?", (source_id,),
                ).fetchall()
                for row in rows:
                    clash = conn.execute(
                        f"SELECT id FROM {table} WHERE device_id = ? AND {key} = ?",
                        (target_id, row[key]),
                    ).fetchone()
                    if clash:
                        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
                    else:
                        conn.execute(
                            f"UPDATE {table} SET device_id = ? WHERE id = ?",
                            (target_id, row["id"]),
                        )

            for table in ("signals", "config_snapshots", "poll_log"):
                conn.execute(
                    f"UPDATE {table} SET device_id = ? WHERE device_id = ?",
                    (target_id, source_id),
                )
            conn.execute(
                "UPDATE trap_log SET device_id = ? WHERE device_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "UPDATE flows SET source_device_id = ? WHERE source_device_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "UPDATE flows SET dest_device_id = ? WHERE dest_device_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "UPDATE flows SET origin_device_id = ? WHERE origin_device_id = ?",
                (target_id, source_id),
            )
            conn.execute("DELETE FROM devices WHERE id = ?", (source_id,))

    def update_device(self, device_id: int, **fields) -> None:
        allowed = {
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
        }
        bools = {"api_verify_tls", "poll_enabled", "snmp_enabled", "snmp_trap_enabled"}
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
        if "snmp_version" in fields and fields["snmp_version"] not in VALID_SNMP_VERSIONS:
            raise ValueError(f"invalid snmp_version {fields['snmp_version']!r}")
        if "mgmt_host" in fields:
            self._require_unique_mgmt_host(
                (fields["mgmt_host"] or "").strip(), exclude_id=device_id,
            )
            fields["mgmt_host"] = (fields["mgmt_host"] or "").strip()
        if "snmp_host" in fields and (fields["snmp_host"] or "").strip():
            self._require_unique_mgmt_host(
                (fields["snmp_host"] or "").strip(), exclude_id=device_id,
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

    def find_device_by_mgmt_host(self, host: str, exclude_id: Optional[int] = None) -> Optional[Device]:
        host = (host or "").strip()
        if not host:
            return None
        with self.connect() as conn:
            if exclude_id is None:
                row = conn.execute(
                    "SELECT * FROM devices WHERE mgmt_host = ? OR snmp_host = ?",
                    (host, host),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM devices WHERE (mgmt_host = ? OR snmp_host = ?) AND id != ?",
                    (host, host, exclude_id),
                ).fetchone()
            return Device.from_row(row) if row else None

    def _require_unique_mgmt_host(self, host: str, exclude_id: Optional[int] = None) -> None:
        host = (host or "").strip()
        if not host:
            return
        other = self.find_device_by_mgmt_host(host, exclude_id=exclude_id)
        if other is not None:
            raise ValueError(
                f"management IP {host} is already used by device {other.name!r}"
            )

    def add_trap(self, source_ip: str, version: Optional[str] = None,
                 trap_oid: Optional[str] = None, generic_trap: Optional[int] = None,
                 varbinds_json: str = "[]", device_id: Optional[int] = None,
                 matched: bool = False) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trap_log (
                    device_id, source_ip, version, trap_oid, generic_trap, varbinds_json, matched
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, source_ip, version, trap_oid, generic_trap, varbinds_json, int(matched)),
            )
            return cur.lastrowid

    def list_traps(self, device_id: Optional[int] = None, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if device_id is None:
                return conn.execute(
                    "SELECT * FROM trap_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return conn.execute(
                "SELECT * FROM trap_log WHERE device_id = ? ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()

    def last_matched_trap(self, device_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM trap_log
                WHERE device_id = ? AND matched = 1
                ORDER BY id DESC LIMIT 1
                """,
                (device_id,),
            ).fetchone()

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
    # Config snapshots (data layer only; capture/diff is a backlog idea)
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
                 slot: str = "", status: str = "unknown",
                 capability: str = "", direction: str = "") -> int:
        if kind not in VALID_PORT_KINDS:
            raise ValueError(f"invalid port kind {kind!r}, must be one of {sorted(VALID_PORT_KINDS)}")
        if capability not in VALID_PORT_CAPABILITIES:
            raise ValueError(f"invalid port capability {capability!r}")
        if direction not in VALID_PORT_DIRECTIONS:
            raise ValueError(f"invalid port direction {direction!r}")
        if status not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid port status {status!r}")
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ports (device_id, name, kind, slot, status, capability, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, name, kind, slot, status, capability, direction),
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
        allowed = {"device_id", "name", "kind", "slot", "status", "capability", "direction"}
        if "kind" in fields and fields["kind"] not in VALID_PORT_KINDS:
            raise ValueError(f"invalid port kind {fields['kind']!r}")
        if "status" in fields and fields["status"] not in VALID_FLOW_STATUSES:
            raise ValueError(f"invalid port status {fields['status']!r}")
        if "capability" in fields and fields["capability"] not in VALID_PORT_CAPABILITIES:
            raise ValueError(f"invalid port capability {fields['capability']!r}")
        if "direction" in fields and fields["direction"] not in VALID_PORT_DIRECTIONS:
            raise ValueError(f"invalid port direction {fields['direction']!r}")
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

    def count_flows_for_port(self, port_id: int,
                             except_flow_id: Optional[int] = None) -> int:
        sql = """
            SELECT COUNT(*) AS n FROM flows
            WHERE (source_port_id = ? OR dest_port_id = ?)
        """
        params: list = [port_id, port_id]
        if except_flow_id is not None:
            sql += " AND id != ?"
            params.append(except_flow_id)
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row["n"])

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

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def ensure_auth_settings(self) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM auth_settings WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO auth_settings (id, session_idle_minutes, ldap_json) "
                    "VALUES (1, 120, '{}')"
                )

    def get_auth_settings(self) -> dict:
        self.ensure_auth_settings()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM auth_settings WHERE id = 1").fetchone()
        ldap = {}
        try:
            ldap = json.loads(row["ldap_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            ldap = {}
        from auth import DEFAULT_LDAP
        merged = dict(DEFAULT_LDAP)
        if isinstance(ldap, dict):
            merged.update(ldap)
        return {
            "session_idle_minutes": int(row["session_idle_minutes"] or 120),
            "ldap": merged,
        }

    def update_auth_settings(self, session_idle_minutes: Optional[int] = None,
                             ldap: Optional[dict] = None) -> None:
        self.ensure_auth_settings()
        fields, params = [], []
        if session_idle_minutes is not None:
            fields.append("session_idle_minutes = ?")
            params.append(max(5, min(1440, int(session_idle_minutes))))
        if ldap is not None:
            fields.append("ldap_json = ?")
            params.append(json.dumps(ldap))
        if not fields:
            return
        with self.connect() as conn:
            conn.execute(
                f"UPDATE auth_settings SET {', '.join(fields)} WHERE id = 1",
                params,
            )

    def list_users(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()

    def get_user(self, user_id) -> Optional[sqlite3.Row]:
        if user_id is None:
            return None
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def get_user_by_username(self, username: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                ((username or "").strip(),),
            ).fetchone()

    def add_user(self, username: str, user_type: str = "local",
                 password_hash: Optional[str] = None, roles: Optional[list] = None,
                 permission_overrides: Optional[dict] = None, enabled: bool = True,
                 must_change_password: bool = False) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO users (
                    username, type, password_hash, roles, permission_overrides,
                    enabled, must_change_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username.strip(),
                    user_type,
                    password_hash,
                    json.dumps(roles or ["viewer"]),
                    json.dumps(permission_overrides or {}),
                    int(enabled),
                    int(must_change_password),
                ),
            )
            return cur.lastrowid

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {
            "username", "type", "password_hash", "roles", "permission_overrides",
            "enabled", "must_change_password",
        }
        sets, params = ["updated_at = ?"], [utcnow_iso()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown user field {key!r}")
            if key in ("roles", "permission_overrides") and not isinstance(value, str):
                value = json.dumps(value)
            if key in ("enabled", "must_change_password"):
                value = int(bool(value))
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(user_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)

    def delete_user(self, user_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cur.rowcount > 0

    def count_users(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"])

    def add_session(self, token: str, user_id=None, username: str = "",
                    ldap_ephemeral: bool = False, ldap_roles=None) -> None:
        roles = ldap_roles
        if roles is not None and not isinstance(roles, str):
            roles = json.dumps(roles)
        now = utcnow_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, user_id, username, ldap_ephemeral, ldap_roles,
                    created_at, last_activity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token, user_id if isinstance(user_id, int) else None,
                 username, int(ldap_ephemeral), roles, now, now),
            )

    def get_session(self, token: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (token,)
            ).fetchone()

    def touch_session(self, token: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_activity = ? WHERE id = ?",
                (utcnow_iso(), token),
            )

    def delete_session(self, token: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (token,))
