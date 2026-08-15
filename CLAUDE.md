# CLAUDE.md — context for future work on this project

## What this is
NexNOC: multi-site, multi-vendor NOC/management dashboard for broadcast
video/signal-path infrastructure, for David McFerrin (NewsNation/Nexstar).
Started Appear-only (5 frames / 4 sites), generalized to multi-vendor
(Appear, Haivision, Net Insight), then restructured around a **driver**
abstraction so devices are matched to handling code by vendor+model+
firmware, not just vendor. Full scope: inventory, health, geo+topology map,
license mgmt, config backup, routing control, kiosk board.

## Decisions made so far
- **Project renamed Appear-NOC → NexNOC**, then generalized to multi-vendor.
- **Driver architecture (latest evolution)**: `vendors/` package renamed to
  `drivers/`. `drivers/base.py` defines `Driver` (ping/discover) with
  class-level matching metadata (`vendor`, `supported_models`,
  `firmware_min`/`firmware_max`) and `resolve_driver()` which picks the most
  specific matching driver, falling back to each vendor's default (a driver
  with no model/firmware constraints). `devices` table has `firmware_version`
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
- **Routing control is explicitly NOT built on the poller's code path.**
  Production risk feature — needs its own auth gate, confirm/diff step,
  and audit trail before it touches a live device.

## Known unknowns per vendor — READ BEFORE BUILDING PHASE 2/3
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
  or response schemas for Phase 2/3 parsing logic. Confirm first,
  implement second.

## Phase status
- ✅ Phase 1: DB schema (all phases, vendor/driver-agnostic `devices`
  table), sites/devices bootstrap from `config.json`, async health poller
  with automatic driver resolution + dispatch (reachability-only),
  `discover()` CLI tool for HTTP-based drivers, unit tests including a
  dedicated driver-resolution test suite with fixture drivers.
- ✅ Phase 2: Frontend — Leaflet geo map (CDN tiles now; local `/tiles`
  pack for production) + trunk/city drill-down + filterable table +
  inventory + `/kiosk`. Stdlib `server.py`, client polls `/api/state`
  every 5s (no WebSocket). Signal health derives from host device when
  the signal row is still `unknown`. Bootstrap now also loads
  trunks/signals from `config.json`.
- ⬜ Phase 3: License tracking, config backup/diff/restore — per driver,
  blocked on confirmed API/SNMP/OID details for each.
- ⬜ Phase 4: Routing control workflow (propose/diff/confirm/execute/audit).
  Cross-vendor routing (e.g. Appear frame → Haivision encoder) needs its
  own design pass, not yet scoped.
- ✅ Kiosk board (`/kiosk`; same polling layer as Phase 2).

## Conventions used in this codebase
- No ORM — explicit SQL in `db.py`, deliberately (see docstring).
- Blocking I/O (`urllib`, `subprocess`) runs via `loop.run_in_executor`
  inside the asyncio poll loop — never call driver methods directly from
  async code without offloading.
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
  drivers earlier). See README.md "Adding a new driver" for the full
  checklist.
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
