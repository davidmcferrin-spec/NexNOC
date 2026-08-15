# CLAUDE.md — context for future work on this project

## What this is
NexNOC: multi-site, multi-vendor NOC/management dashboard for broadcast
video/signal-path infrastructure, for David McFerrin (NewsNation/Nexstar).
Started Appear-only (5 frames / 4 sites), generalized to multi-vendor
(Appear, Haivision, Net Insight), then restructured around a **driver**
abstraction so devices are matched to handling code by vendor+model+
firmware, not just vendor. Planned scope: inventory, health, geo+topology
map, kiosk board. License tracking, config backup, and routing control
were pulled off the active roadmap — see "Future ideas" below.

## Decisions made so far
- **Project renamed Appear-NOC → NexNOC**, then generalized to multi-vendor.
- **Driver architecture (latest evolution)**: `vendors/` package renamed to
  `drivers/`. `drivers/base.py` defines `Driver` (ping/discover) with
  class-level matching metadata (`vendor`, `supported_models`,
  `firmware_min`/`firmware_max`, `notes`) and `resolve_driver()` which picks the most
  specific matching driver, falling back to each vendor's default (a driver
  with no model/firmware constraints). Inclusive firmware bounds: `firmware_min`
  is `>=`, `firmware_max` is `<=`; either side may be omitted. `notes` is
  operator-facing and exported by `driver_catalog()`. How-to: docs/DRIVERS.md. `devices` table has `firmware_version`
  (for matching), `driver_override` (explicit pin, bypasses resolution), and
  `resolved_driver` (informational, written back after each poll). Adding a
  driver for a new model/firmware range is a 2-line change (write the class,
  add it to `drivers/registry.py` ahead of the default) — no changes to
  `poller.py`, `db.py`, or other drivers.
- **Vendor abstraction predates this**: `db.py`'s `devices` table is
  vendor-agnostic; `access_mode` column (`direct_api` / `direct_snmp` /
  `via_nms`) exists because vendors differ *structurally*, not just in API
  shape — this is orthogonal to driver selection (access_mode says how to
  reach the device network-wise; driver says how to interpret what it says).
- **Routing changes** = signal-path routing between devices/sites (not
  internal module I/O routing).
- **Map** needs two views: (1) geo map of sites with trunk-level (bundled
  signal group) status, drillable to individual signals within a trunk;
  (2) a filterable flat table of all source→destination links + status.
  The geo view is Leaflet (vendored, no npm) with real tiles — pan/zoom.
  One undirected trunk per city pair; click the trunk to list paths.
  Dev uses Carto Dark Matter CDN; production sets `map.local_tile_dir` and
  the stdlib server serves `/tiles/{z}/{x}/{y}.png`. Do not go back to a
  hand-drawn CONUS SVG silhouette. Sample inventory is CHI / NYC / DC /
  ATL / Indy — not Huntsville or LA.
- **Connectivity**: central management host reaches devices directly — no
  per-site relay/agent needed.
- **DB**: SQLite. Single-digit device count doesn't justify a server RDBMS.
  Production layout (Debian/Ubuntu): `sudo ./setup.sh` → `/opt/nexnoc`,
  `/etc/nexnoc/{config.json,nexnoc.env}`, `/var/lib/nexnoc/noc.db`,
  systemd `nexnoc-poller` + `nexnoc-web` (127.0.0.1:8080), Apache reverse
  proxy. MySQL is not implemented.
- **Stack constraint**: stdlib-only Python (no pip beyond stdlib), no
  Docker, no Node.
- **SNMP is a real, implemented parallel channel, not just an
  `access_mode` category.** Per-device `snmp_enabled` (GET alongside the
  vendor API, v1/v2c/v3, `snmp_version` CHECK-constrained) and
  `snmp_trap_enabled` (accept traps) are independent flags — a device can
  be `access_mode=direct_api` and still have SNMP GET/traps on for
  cross-checking. `trapd.py` is a stdlib-only SNMPv1/v2c BER decoder
  (no pysnmp) listening on UDP 162; v3 traps come in via `snmptrapd`'s
  `traphandle` piping a line-based payload to
  `trapd.py --from-snmptrapd`. Traps are matched to a device by
  `mgmt_host`/`agent_addr` + community (v3 trusts the transport instead of
  checking a community), logged to `trap_log` regardless of match, and —
  only if matched — run through the resolved driver's `interpret_trap()`
  to flip device status (`degraded`/`healthy`) without waiting for the
  next poll. Community strings and v3 secrets are never stored or logged,
  only used in-memory to match.
- **Device merge exists for inventory cleanup** (e.g. a device got added
  twice, or discovered under two hostnames): `db.merge_devices(source,
  target)` moves ports/flows/poll history onto the target (same-named
  ports collapse, flows get remapped to the surviving port id), backfills
  blank target fields (mgmt_host, credentials, model, firmware) from the
  source, then deletes the source. Exposed via bulk inventory API
  (`merge_into` in a bulk device-collection body), alongside bulk
  patch/delete for other collections.
- **Audit logging is fail-closed.** `audit.audit_writable()` is checked
  *before* an inventory write is allowed to proceed — if the audit line
  can't be appended (disk full, permissions), the write itself is
  refused, not just unlogged. Log is append-only JSONL, rotates at 10MB.

## Known unknowns per vendor — READ BEFORE ADDING VENDOR-SPECIFIC PARSING
- **Appear**: No public REST manual. Live DC X20 Prometheus is confirmed
  at `/prometheus/system/metrics`, `/prometheus/product/metrics`,
  `/prometheus/ipgateway/metrics`, `/prometheus/alarms/metrics`. Metric
  names (`total_alarms`, `apr_x_sdi_lock_status`, slot gauges, port
  rates) come from those scrapes. Do not invent JSON sub-API paths
  (MMI/IpGateway REST).
- **Haivision**: Confirmed from Makito X4 Encoder 1.8.0 `/apidoc` (saved
  from 10.207.9.245). Session login `POST /apis/authentication`
  `{username,password}` over HTTPS; inventory via `GET /apis/status`,
  `/apis/videnc`, `/apis/audenc`, `/apis/streams`, `/apis/vidin`. No
  `/apis/license` in 1.8.0. Poller must not call start/stop/edit.
- **Net Insight**: Per-node access is SNMP/CLI/web-GUI; no confirmed
  per-node REST API. The REST API that exists belongs to Nimbra Vision (a
  separate central NMS product) — `via_nms` access mode has NO driver
  implemented yet (`drivers/net_insight.py` raises `NotImplementedError`
  deliberately). Nimbra devices serve their own downloadable enterprise MIB
  from the web GUI — pull that for real OIDs before building beyond basic
  MIB-2 reachability.
- **General rule**: do not invent or assume specific endpoint paths, OIDs,
  or response schemas. Confirm first, implement second.

## Phase status
- ✅ Phase 1: DB schema (vendor/driver-agnostic `devices`
  table), sites/devices bootstrap from `config.json`, async health poller
  with automatic driver resolution + dispatch (reachability-only),
  `discover()` CLI tool for HTTP-based drivers, unit tests including a
  dedicated driver-resolution test suite with fixture drivers.
- ✅ Phase 2: Frontend — Leaflet geo map (CDN tiles now; local `/tiles`
  pack for production) + trunk/city drill-down + filterable table +
  inventory + `/kiosk`. Stdlib `server.py`, client polls `/api/state`
  every 5s (no WebSocket). Signal health derives from host device when
  the signal row is still `unknown`. Bootstrap now also loads
  trunks/signals from `config.json`. Display name is **NexNOC** (not
  NEXNOC). Header clock is jammed to server time (`GET /api/time` plus
  `server_time_ms` on `/api/state`); hover shows PC↔server offset;
  **Alt+J** / Jam re-syncs. **Zones** opens a draggable ET/CT/MT/PT
  overlay (position persisted in localStorage). Three consecutive
  `/api/state` failures turn the board red (`body.backend-lost`) —
  including status pills — because stale health is untrusted.
  Local + LDAPS auth (`auth.py`): roles viewer / operator / admin, seeded
  `admin`/`password` and `user`/`password`. `/kiosk` and `/api/state` stay
  anonymous; writes and `GET /dashboard` require a session. `GET /` is
  the login page (`index.html`); the board is `dashboard.html`. LDAP via `ldapsearch`.
- ✅ Kiosk board (`/kiosk`; same polling layer as Phase 2).
- ✅ Out-of-phase-order infrastructure (not on the Phase 1–2 roadmap, but
  live): local+LDAP auth/RBAC (see Phase 2 entry above), SNMP GET +
  trap ingestion (v1/v2c/v3) as a channel parallel to driver polling,
  device merge + bulk inventory patch/delete, fail-closed audit logging,
  overlapping poller cadence (~80-device freshness).

The planned phases are done. Do not start a new product phase unless
David asks for one.

## Production hardening (docs/DEPLOY.md)
`setup.sh` produces a working install, not a hardened one. Go-live gaps
tracked in **docs/DEPLOY.md** and surfaced by `setup.sh --check`: TLS is
off by default (vhost ships commented out; `auth.request_is_secure()`
already does the right thing off `X-Forwarded-Proto`, so enabling TLS is
purely an Apache/certbot step, no code change); no firewall rules are
applied (`nexnoc-trapd` binds UDP 162 on all interfaces — scope it to the
device management subnet); no DB backup is scheduled
(`scripts/nexnoc-backup-db` exists, needs a cron entry); `nexnoc.env`
ships full of `change_me` placeholders that need real per-device values
before the poller can reach anything. Log rotation, systemd hardening
(`NoNewPrivileges`/`ProtectSystem=strict`/capability-scoped trapd), and
credential-never-in-DB were already handled before this pass — don't
re-flag them.

## Future ideas — not scheduled
Not on the active roadmap. No driver hooks or UI for these exist beyond
empty schema tables and reserved permission names; don't build toward
them opportunistically as a side effect of other work.
- **License tracking** (was "Phase 3"): per-device feature/expiry from
  vendor APIs or SNMP. Blocked on confirmed paths/OIDs — Appear
  Prometheus has no license scrape confirmed; Haivision 1.8.0 has no
  `/apis/license`; Net Insight needs the enterprise MIB first.
  `licenses` stays in `schema.sql` unpopulated; `manage_licenses` stays
  reserved in `auth.py`.
- **Config backup/diff/restore** (was also "Phase 3"): scheduled snapshot
  into `config_snapshots` (table already exists), plus a diff view.
  Same confirmation block as licenses. `manage_backups` stays reserved.
- **Routing control** (was "Phase 4"): propose → diff → explicit confirm →
  push → verify → audit workflow to change which source port feeds a
  flow's destination — pulled off the active roadmap. A design pass was
  done and is kept for reference at **docs/ROUTING.md** (route =
  `flows.source_port_id` edit, 3 new optional `Driver` methods, confirm as
  a separate request from propose, no auto-rollback on an ambiguous
  execute/verify failure) in case this gets picked back up, but treat it
  as a backlog idea, not a plan — re-scope before touching it again.
  `routing_audit` stays in `schema.sql` unpopulated (harmless — same
  reasoning as `licenses` / `config_snapshots`); `view_routing` /
  `propose_routing` / `execute_routing` stay reserved permission names in
  `auth.py`, unassigned to any default role.

## Poller freshness (~80 devices)
Do **not** treat flipping `poll_interval_seconds` to 10 as the fix —
leave the default at 30s. Expected fleet is ~80 devices (two national
networks). Target is ~10s **time-to-glass**, not a 10s config knob.

Implemented: overlapping per-device cadence (a hung box does not delay
the others), 32 in-flight cap + dedicated `ThreadPoolExecutor`, Appear
`ping()` only `/prometheus/system/metrics` (`collect()` still all four),
2s HTTP timeout, Haivision driver cache + one 401 re-login, SQLite WAL
+ `busy_timeout=5000`. Keep 3-miss hysteresis (~30s to `unreachable`).
Sub-10s breaks stay on traps (`nexnoc-trapd`). Board `/api/state` every
5s is fine. Do not shard, add MySQL, or add per-site agents at this size.

## Conventions used in this codebase
- No ORM — explicit SQL in `db.py`, deliberately (see docstring).
- Blocking I/O (`urllib`, `subprocess`) runs via `loop.run_in_executor`
  on a dedicated 32-worker pool inside the asyncio poll loop — never
  call driver methods directly from async code without offloading.
- The poller caches one `Driver` instance per device (Haivision session
  cookies). Rebuilds when host/creds/model/firmware/override change.
  `ping()` must stay cheap; HTTP timeout is 2s (`http_util`).
- Credentials: DB/config store only env var *names*; values resolved from
  `os.environ` at poll time (`poller.py:resolve_env`). Never log credential
  values.
- Device status transitions require `CONSECUTIVE_FAILURES_THRESHOLD` (3)
  misses before flipping to `unreachable`, to avoid flapping. In-memory
  failure counter resets on poller restart (acceptable).
- **Adding a driver**: implement `Driver` in `drivers/<vendor>.py` (new
  file for a new vendor; new class in the existing file, or a new file, for
  a new model/firmware range within an existing vendor). Register in
  `drivers/registry.py` — order matters when multiple non-default drivers
  could match the same device (first match wins; put narrower/newer
  drivers earlier). Set `notes` (operator-facing; exported by
  `driver_catalog()`). Full how-to, firmware `>=` / `<=` ranges, and
  checklists: **docs/DRIVERS.md**.
- `DriverError` / `DriverAuthError` / `DriverUnreachableError`
  (`drivers/base.py`) are the shared exception hierarchy across all HTTP
  drivers — driver-specific code should raise/catch these, not raw
  `urllib` exceptions. `DriverResolutionError` is separate — it's about
  *picking* a driver (unknown vendor, bad override, no match), not about
  talking to a device once one's picked.
- `resolve_driver()` (`drivers/base.py`) is pure and registry-agnostic —
  it takes the registry as an argument rather than importing
  `drivers.registry` itself, specifically so `tests/test_driver_resolution.py`
  can test matching logic against fixture drivers without depending on (or
  being broken by future changes to) the real Appear/Haivision/Net Insight
  drivers.
