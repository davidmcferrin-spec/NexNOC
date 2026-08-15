-- NexNOC - SQLite schema
-- Multi-vendor video/signal-path NOC: Appear, Haivision (Makito X4 etc.), Net Insight
-- (Nimbra), and a generic SNMP fallback for anything else.
--
-- Phase 1 tables (sites, devices, modules, poll_log, config_snapshots) are used now.
-- Phase 2+ tables (trunks, signals, licenses, routing_audit) exist now so the
-- schema doesn't need a migration later, but aren't populated until those phases land.
--
-- SQLite chosen deliberately: single-digit sites/devices is a small,
-- single-writer workload. No server process to maintain, single file to
-- back up, zero dependency footprint. Revisit only if concurrent-write
-- contention becomes real (it won't at this scale).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Cities: a metro can have more than one site (Wacker and Midway in Chicago).
-- The map rolls up to cities; sites stay first-class under a city.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    lat         REAL,
    lng         REAL,
    geo_source  TEXT NOT NULL DEFAULT '',   -- 'geocode' | 'manual' | ''
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Sites: the physical locations. Many sites may share one city.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,      -- e.g. "Chicago - Wacker", "Huntsville HQ"
    city        TEXT,                      -- display / bootstrap fallback
    city_id     INTEGER REFERENCES cities(id) ON DELETE SET NULL,
    address     TEXT,                      -- street address; geocoded to lat/lng
    lat         REAL,                      -- for the geo map view (Phase 2)
    lng         REAL,
    geo_source  TEXT NOT NULL DEFAULT '',  -- 'geocode' | 'manual' | ''
    pin_icon    TEXT NOT NULL DEFAULT 'building',  -- builtin id, or 'upload'
    pin_color   TEXT NOT NULL DEFAULT '#6aa4ff',
    pin_upload  TEXT,                      -- filename under the pin upload dir
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Devices: any managed unit - Appear frame, Haivision encoder/decoder,
-- Net Insight Nimbra node, or a generic SNMP-only device. `vendor` selects
-- which adapter in vendors/ handles it (see vendors/base.py ADAPTER_REGISTRY).
--
-- Credentials are stored as references (env var names), never as plaintext
-- values in this DB - see vendors/http_util.py, poller.py resolve_credentials().
--
-- `access_mode` matters because vendors differ structurally, not just by API
-- shape:
--   direct_api  - device exposes its own HTTP/JSON API (Appear, Haivision)
--   direct_snmp - device is managed via SNMP only, no useful device-local
--                 REST API (Net Insight Nimbra nodes without Vision NMS)
--   via_nms     - device is only reachable/queryable through a central NMS's
--                 northbound API (e.g. Net Insight Nimbra Vision aggregating
--                 many nodes) - nms_* columns apply in this mode
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id             INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    name                TEXT NOT NULL UNIQUE,     -- friendly name, e.g. "CHI-X20-1", "HSV-MX4-ENC-2"
    vendor              TEXT NOT NULL,             -- 'appear' | 'haivision' | 'net_insight' | 'generic_snmp'
    device_role         TEXT,                      -- free text: 'frame','encoder','decoder','node', etc.
    model               TEXT,                      -- X10 / X20 / Makito X4 / Nimbra MSR 600 / etc.
    firmware_version    TEXT,                      -- used with `model` for driver selection (see drivers/base.py)
    mgmt_host           TEXT NOT NULL,              -- IP or hostname for the device's own management interface
    access_mode         TEXT NOT NULL DEFAULT 'direct_api'
                            CHECK (access_mode IN ('direct_api','direct_snmp','via_nms')),

    -- Driver selection: normally auto-resolved from vendor+model+firmware_version
    -- (drivers.base.resolve_driver). driver_override pins a specific driver_id,
    -- bypassing auto-resolution entirely - use when the automatic match is wrong
    -- or ambiguous for this specific unit. resolved_driver is written by the
    -- poller after each successful resolution, purely informational (so the
    -- interface can show "this device is using driver X" without recomputing it).
    driver_override     TEXT,
    control_driver      TEXT,               -- Phase 4 pin; monitoring uses driver_override / resolve
    resolved_driver      TEXT,

    -- HTTP API fields (direct_api mode; e.g. Appear, Haivision)
    api_port            INTEGER NOT NULL DEFAULT 443,
    api_scheme          TEXT NOT NULL DEFAULT 'https' CHECK (api_scheme IN ('http','https')),
    api_verify_tls      INTEGER NOT NULL DEFAULT 0,  -- many broadcast appliances run self-signed certs
    api_username_env    TEXT,                       -- name of env var holding the username
    api_password_env    TEXT,                       -- name of env var holding the password

    -- SNMP: parallel monitoring channel (GET v1/v2c/v3) in addition to the
    -- vendor API when snmp_enabled=1. Traps are accepted when snmp_trap_enabled=1.
    -- Values for community / v3 secrets are env var *names*, never plaintext.
    snmp_host           TEXT,                       -- usually same as mgmt_host
    snmp_port           INTEGER NOT NULL DEFAULT 161,
    snmp_community_env  TEXT,
    snmp_version        TEXT NOT NULL DEFAULT '2c'
                            CHECK (snmp_version IN ('1','2c','3')),
    snmp_enabled        INTEGER NOT NULL DEFAULT 0, -- GET alongside API when set
    snmp_trap_enabled   INTEGER NOT NULL DEFAULT 1,
    snmp_v3_user_env    TEXT,
    snmp_v3_sec_level   TEXT DEFAULT 'authPriv',
    snmp_v3_auth_proto  TEXT DEFAULT 'SHA',
    snmp_v3_auth_pass_env TEXT,
    snmp_v3_priv_proto  TEXT DEFAULT 'AES',
    snmp_v3_priv_pass_env TEXT,

    -- Central-NMS fields (via_nms mode; e.g. Net Insight Nimbra Vision)
    nms_host             TEXT,
    nms_port             INTEGER,
    nms_api_key_env      TEXT,
    nms_device_ref       TEXT,                      -- this device's identifier *within* the NMS, if different from `name`

    poll_enabled        INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (status IN ('unknown','healthy','degraded','unreachable','decommissioned')),
    last_seen_at        TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_devices_site ON devices(site_id);
CREATE INDEX IF NOT EXISTS idx_devices_vendor ON devices(vendor);
-- Empty mgmt_host is allowed many times (pending inventory). Non-empty IPs are unique.
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_mgmt_host
    ON devices(mgmt_host) WHERE mgmt_host != '';

-- ---------------------------------------------------------------------------
-- Modules: cards/slots/channels inside a device, discovered during polling
-- (an Appear frame's modules, a Makito X4's encode channels, etc.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS modules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    slot             TEXT NOT NULL,           -- slot/channel identifier as reported by the device
    module_type      TEXT,
    firmware_version TEXT,
    serial           TEXT,
    status           TEXT NOT NULL DEFAULT 'unknown',
    last_seen_at     TEXT,
    UNIQUE(device_id, slot)
);

-- ---------------------------------------------------------------------------
-- Trunks: a labeled bundle of signals between two sites (Phase 2 geo view)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_a_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    site_b_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (site_a_id != site_b_id)
);

-- ---------------------------------------------------------------------------
-- Signals: individual source->destination flows, optionally grouped in a trunk.
-- May originate on any vendor's device - hence device_id, not a vendor-specific FK.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    trunk_id            INTEGER REFERENCES trunks(id) ON DELETE SET NULL,
    source_label        TEXT NOT NULL,
    destination_label   TEXT NOT NULL,
    direction            TEXT,                 -- e.g. 'contribution', 'distribution'
    status               TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (status IN ('unknown','up','degraded','down')),
    last_status_change  TEXT,
    last_polled_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_trunk ON signals(trunk_id);
CREATE INDEX IF NOT EXISTS idx_signals_device ON signals(device_id);

-- ---------------------------------------------------------------------------
-- Ports: SDI I/O, streaming NICs, and the management interface on a device.
-- A frame is not a single path — it has 1..N of each.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,           -- "BNC 1", "SDI-1", "D1 10G"
    kind        TEXT NOT NULL DEFAULT 'other'
                    CHECK (kind IN ('sdi_in','sdi_out','net','mgmt','other')),
    capability  TEXT NOT NULL DEFAULT ''
                    CHECK (capability IN ('','input','output','assignable')),
    direction   TEXT NOT NULL DEFAULT ''
                    CHECK (direction IN ('','input','output','unused')),
    slot        TEXT,
    status      TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('unknown','up','degraded','down')),
    last_seen_at TEXT,
    UNIQUE(device_id, name)
);

CREATE INDEX IF NOT EXISTS idx_ports_device ON ports(device_id);

-- ---------------------------------------------------------------------------
-- Flows: one destination of one encoded signal.
-- A device input (signal_label + source port) can have N destination rows,
-- and those dests do not have to share a city, site, or device.
-- source_* is the port that is sending *this hop* (an input encode, or an
-- output that is forwarding). origin_* is the signal's true source when
-- this row is an output carrying a feed that started elsewhere.
-- dest may be a city and/or site and/or device — none of those are 1:1.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    label               TEXT NOT NULL,
    signal_label        TEXT,
    source_device_id    INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    source_port_id      INTEGER REFERENCES ports(id) ON DELETE SET NULL,
    origin_device_id    INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    origin_port_id      INTEGER REFERENCES ports(id) ON DELETE SET NULL,
    dest_city_id        INTEGER REFERENCES cities(id) ON DELETE SET NULL,
    dest_site_id        INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    dest_device_id      INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    dest_port_id        INTEGER REFERENCES ports(id) ON DELETE SET NULL,
    dest_label          TEXT,
    direction           TEXT,
    status              TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (status IN ('unknown','up','degraded','down')),
    last_status_change  TEXT,
    last_polled_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_flows_source ON flows(source_device_id);
CREATE INDEX IF NOT EXISTS idx_flows_dest_site ON flows(dest_site_id);

-- ---------------------------------------------------------------------------
-- Licenses (Phase 3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    feature_name    TEXT NOT NULL,
    status          TEXT,
    expiry_date     TEXT,
    last_checked_at TEXT,
    UNIQUE(device_id, feature_name)
);

-- ---------------------------------------------------------------------------
-- Config snapshots (Phase 3): periodic pulls of each device's full config for
-- backup / diff / restore. config_json stores the raw pulled config, in
-- whatever shape that vendor's API returns it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    taken_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    config_hash  TEXT NOT NULL,          -- sha256 of config_json, for cheap change detection
    config_json  TEXT NOT NULL,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_config_snapshots_device ON config_snapshots(device_id, taken_at);

-- ---------------------------------------------------------------------------
-- Routing audit (Phase 4): every proposed/executed routing change, who did it
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routing_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    requested_by    TEXT NOT NULL,
    requested_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    old_route_json  TEXT,
    new_route_json  TEXT NOT NULL,
    confirmed       INTEGER NOT NULL DEFAULT 0,
    executed_at     TEXT,
    result          TEXT,               -- 'success' / 'failed' / 'rolled_back'
    error_message   TEXT
);

-- ---------------------------------------------------------------------------
-- Poll log: every poll attempt, success or failure. Foundation for uptime
-- history and for debugging "why does the board say unreachable".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS poll_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    polled_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    method        TEXT NOT NULL CHECK (method IN ('api','snmp','nms')),
    success       INTEGER NOT NULL,
    latency_ms    INTEGER,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_poll_log_device_time ON poll_log(device_id, polled_at);

-- ---------------------------------------------------------------------------
-- SNMP traps (v1/v2c decoded here; v3 via snmptrapd traphandle).
-- Community / v3 secrets are never stored — only source IP, OID, varbinds.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trap_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    source_ip       TEXT NOT NULL,
    version         TEXT,
    trap_oid        TEXT,
    generic_trap    INTEGER,
    varbinds_json   TEXT,
    matched         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trap_log_device_time ON trap_log(device_id, received_at);
CREATE INDEX IF NOT EXISTS idx_trap_log_source ON trap_log(source_ip);

-- ---------------------------------------------------------------------------
-- Auth: local + LDAP users, server-side sessions, LDAP/session settings.
-- Seeded on first initialize (admin/password, user/password).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    username                TEXT NOT NULL UNIQUE,
    type                    TEXT NOT NULL DEFAULT 'local'
                                CHECK (type IN ('local', 'ldap')),
    password_hash           TEXT,
    roles                   TEXT NOT NULL DEFAULT '[]',
    permission_overrides    TEXT NOT NULL DEFAULT '{}',
    enabled                 INTEGER NOT NULL DEFAULT 1,
    must_change_password    INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    username        TEXT NOT NULL,
    ldap_ephemeral  INTEGER NOT NULL DEFAULT 0,
    ldap_roles      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_activity   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS auth_settings (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    session_idle_minutes    INTEGER NOT NULL DEFAULT 120,
    ldap_json               TEXT NOT NULL DEFAULT '{}'
);
