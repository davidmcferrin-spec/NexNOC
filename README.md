# NexNOC

Multi-site, multi-vendor NOC/management tool for broadcast video/signal-path
infrastructure: inventory, health monitoring, geo + topology map, and a
kiosk "big board" display.

**Status: planned phases complete.** Sites/devices inventory across
multiple vendors, a pluggable driver architecture, an async health
poller with automatic driver resolution, API/SNMP discovery tools, and a
stdlib-only dashboard (geo map, trunk drill-down, filterable links
table, inventory, kiosk board), plus local+LDAP auth, SNMP GET/traps,
and overlapping poll cadence. License tracking, config backup, and
routing control are backlog ideas only — see Roadmap.

## Architecture: devices are driver-driven

A **vendor** is a rough family (Appear, Haivision, Net Insight). A
**driver** is what actually knows how to talk to a specific box — matched
on vendor + model + firmware version, not vendor alone. This matters
because a firmware upgrade or a new model in the same product line can
change the API/SNMP surface enough that the old driver stops working
correctly for it, without changing which *vendor* it is.

Each vendor currently has exactly one driver — its **default** (no
model/firmware constraints, matches anything from that vendor). When you
need different handling for a specific model or firmware range, write a
narrower class (`supported_models` and/or inclusive `firmware_min` /
`firmware_max`) and register it *before* the default. Full how-to:
**[docs/DRIVERS.md](docs/DRIVERS.md)**.

A device can also be pinned to an exact driver via `driver_override` in the
DB/config — bypasses auto-resolution entirely, for the case where matching
picks wrong for one specific unit. Whichever driver actually gets used is
recorded back to `devices.resolved_driver` after each poll, so the
interface can show "this device is using driver X" without recomputing it.

```
Device row (vendor, model, firmware_version, driver_override)
        │
        ▼
resolve_driver()  ->  driver class  ->  driver instance (per poll)
        │                                      │
        ▼                                      ▼
devices.resolved_driver (informational)   .ping() / .discover() / ...
```

## Supported vendors (Phase 1)

| Vendor | Default driver | Access mode | Status |
|---|---|---|---|
| **Appear** (X Platform: X10/X20/X5/XM/XC5000/XC5100) | `appear.x_platform.default` | `direct_api` (Prometheus text) | Confirmed scrapes: `/prometheus/{system,product,ipgateway,alarms}/metrics` |
| **Haivision** (Makito X4 / X / MX1 / FX) | `haivision.makito_x.default` | `direct_api` (JSON/HTTP) | Makito X4 1.8.0 `/apidoc` confirmed: session `POST /apis/authentication`, `GET /apis/status` + videnc/audenc/streams/vidin |
| **Net Insight** (Nimbra MSR/Edge/600/1000/400/680) | `net_insight.nimbra.default` | `direct_snmp` (per-node), or `via_nms` (Nimbra Vision) | SNMP path only; Vision REST driver **not implemented** — see `drivers/net_insight.py` |
| Anything else | — | — | Add a `drivers/<name>.py` implementing `Driver` — see [docs/DRIVERS.md](docs/DRIVERS.md) |

Vendors are structurally different, not just different API shapes — see
each `drivers/*.py` module docstring before assuming a pattern from one
applies to another.

## Stack

Python 3 stdlib only — `sqlite3`, `asyncio`, `urllib`, `ssl`, `subprocess`.
No pip packages, no Docker, no Node. SNMP shells out to the system
`snmpget`/`snmpwalk` (`apt install snmp`) rather than a pip SNMP library.

## Known gaps — read before building Phase 2/3

**Appear**: No public REST manual. Live DC X20 Prometheus is at
`/prometheus/system/metrics`, `/prometheus/product/metrics`,
`/prometheus/ipgateway/metrics`, `/prometheus/alarms/metrics`. Do not
invent MMI/IpGateway JSON paths.

**Haivision**: Confirmed from a live Makito X4 Encoder 1.8.0 `/apidoc`.
Login is `POST /apis/authentication` with `{username, password}` (session
cookie, HTTPS). Health/inventory: `GET /apis/status`, `/apis/videnc`,
`/apis/audenc`, `/apis/streams`, `/apis/vidin`. No license endpoint in
1.8.0. The poller must not call start/stop/edit.

**Net Insight**: Structurally SNMP-first. Per-node management has no
confirmed HTTP/REST API — CLI, web GUI, and SNMP v1/v2c/v3 are the
documented paths. A REST API exists, but it belongs to the separate Nimbra
Vision NMS product (northbound API), not to each node — if you don't run
Vision, SNMP is the only path. Pull each Nimbra node's own enterprise MIB
from its web GUI (Control Networks → SNMP → MIB specifications) for real
OIDs rather than guessing; standard MIB-2 OIDs work for basic reachability
in the meantime (`drivers/snmp_util.py:snmp_ping`).

**Do not invent or assume specific endpoint paths, OIDs, or response
schemas when building Phase 2/3 parsing logic.** Confirm first, implement second.

## Setup (Debian / Ubuntu LTS)

Production path: Apache on :80 serves `web/` and proxies `/api/` to
Python on `127.0.0.1:8080`. SQLite at `/var/lib/nexnoc/noc.db`. No pip,
no MySQL.

```bash
sudo ./setup.sh
# edit /etc/nexnoc/config.json  (inventory)
# set device usernames/passwords in Inventory (stored in noc.db)
sudo systemctl restart nexnoc-poller
```

Open `http://<hostname>/` or `http://<hostname>/kiosk`.

```bash
sudo ./setup.sh update     # after git pull: rsync + restart
sudo ./setup.sh --check    # sanity
sudo ./setup.sh status
```

Overrides: `NEXNOC_PREFIX` `NEXNOC_DATA` `NEXNOC_ETC` `NEXNOC_SERVER_NAME`.

`setup.sh` gets it running; it is not yet hardened for production by
itself (plaintext HTTP, no firewall rules, no DB backup). Before treating
an install as production, work through **[docs/DEPLOY.md](docs/DEPLOY.md)**
— TLS, firewall, credential sweep, backups. `sudo ./setup.sh --check`
flags what's still open from that list.

### Local / manual (no Apache)

```bash
cp config.example.json config.json
# edit config.json: your real sites, devices, vendors, models, IPs

# set credentials as environment variables (names referenced in config.json,
# not the values themselves — never commit real credentials)
export CHI_X20_1_USER=admin
export CHI_X20_1_PASS='...'
export CHI_MX4_1_USER=admin
export CHI_MX4_1_PASS='...'
export CHI_NIMBRA_1_SNMP_COMMUNITY='...'

python3 poller.py --config config.json --db /var/lib/nexnoc/noc.db --bootstrap-only
```

Re-run with `--bootstrap-only` any time you edit `config.json` to add a
device, trunk, or signal — it's idempotent, matches on unique `name`
(or trunk label / signal source+destination), and never deletes a row
that's been removed from the file (avoids a typo nuking poll history;
decommission explicitly instead).

### Run the poller

```bash
python3 poller.py --config config.json --db /var/lib/nexnoc/noc.db
python3 poller.py --verbose --log-file poller.log --config config.json --db noc.db
```

Each interval logs a summary of polls that finished since the last
summary (`api X/Y ok, snmp X/Y ok`). `--verbose` (or `NEXNOC_VERBOSE=1`)
adds a DEBUG line per device: `api=ok/fail/skip snmp=… collect=… status=…`.
`--log-file` (or `NEXNOC_LOG_FILE`) writes a rotating file in addition to
stdout. Production writes `/var/log/nexnoc/poller.log` and
`journalctl -u nexnoc-poller -f`.

Each device is scheduled on its own `poll_interval_seconds` cadence
(config.json, default 30s) with up to 32 polls in flight — a hung box
does not delay the others. Appear `ping()` hits only
`/prometheus/system/metrics`; `collect()` still scrapes all four
confirmed paths. HTTP timeout is 2s. Driver instances are cached so
Haivision keeps its session cookie (re-login once on 401). SQLite uses
WAL. A device flips to `unreachable` only after 3 consecutive missed
polls (`CONSECUTIVE_FAILURES_THRESHOLD` in poller.py) — avoids
status-flapping on a single dropped packet.

All three channels are capable per device (operator can opt out):

1. **Vendor API** — `ping()` + `collect()` on the resolved driver.
2. **SNMP GET** — `snmp_ping()` / `snmp_collect()` (default: MIB-2
   `sysDescr`). Runs when `snmp_enabled` is on *and* community or v3
   creds exist. New devices default `snmp_enabled` on.
3. **SNMP traps** — `nexnoc-trapd` on UDP 162 dispatches through
   `Driver.interpret_trap()`. Generic linkDown/authFailure hold
   `degraded` until linkUp/coldStart/warmStart; a healthy poll does not
   clear a held trap. Vendor enterprise OIDs are stored until a driver
   override is confirmed.

Production uses `nexnoc-poller.service` / `nexnoc-web.service` /
`nexnoc-trapd.service` from `setup.sh`. Device credentials live on the
device row in SQLite. SNMPv3
traps can be handed off from `snmptrapd` — see
`config/snmptrapd.nexnoc.conf`. Management IPs must be unique (empty
host is allowed many times for pending boxes).

### Run the dashboard

```bash
python3 server.py --config config.json --db /var/lib/nexnoc/noc.db --port 8080
```

`--config` is optional (bootstraps if given). Open http://127.0.0.1:8080
to sign in (seeded `admin` / `password` or `user` / `password` — change
both on first login). The board is `/dashboard`. `/kiosk` is an anonymous
wall board and stays public. The page polls `/api/state` every 5 seconds —
no WebSocket stack. Bind defaults to localhost.

The map is Leaflet (vendored in `web/vendor/leaflet/`, no npm). Dev uses
Esri Light Gray Canvas tiles from the public CDN (switchable on the board) — pan, zoom, real geography.
Kiosk uses the same map. For production, build a local XYZ pack and point
`map.local_tile_dir` at it so the browser never leaves the LAN:

```bash
python3 scripts/fetch_tiles.py --out /var/lib/nexnoc/tiles \
  --bbox -125,24,-66,50 --min-zoom 3 --max-zoom 8
```

Then in `/etc/nexnoc/config.json`:

```json
"map": { "local_tile_dir": "/var/lib/nexnoc/tiles", "min_zoom": 3, "max_zoom": 8 }
```

`nexnoc-web` already passes `--map-config /etc/nexnoc/config.json`. After
that, tiles are served at `/tiles/{z}/{x}/{y}.png` from disk. Respect the
tile provider's terms; the fetch script is for a private NOC pack.

A signal left at status `unknown` (the default when you omit `status` in
config.json) follows its host device: healthy → up, degraded → degraded,
unreachable → down. An explicit signal status always wins. A trunk's
status is the worst signal in the bundle.

### Discover real API paths (Appear, Haivision)

```bash
python3 poller.py --config config.json --db noc.db --discover CHI-X20-1
```

Net Insight `direct_snmp` devices don't support `--discover` (no HTTP API to
probe) — use `drivers.snmp_util.snmp_walk()` to explore the MIB tree
directly instead, once you have real OIDs from the device's own MIB export.

### Run the tests

```bash
python3 -m unittest discover -s tests -v
```

Unit tests, no external network access required (fake local HTTP server +
mocks). Includes a dedicated suite (`tests/test_driver_resolution.py`) for
the resolution logic itself, using fixture drivers independent of the real
Appear/Haivision/Net Insight ones.

## Authentication and roles

Local + LDAPS login, same model as XPMON-Dashboard. Users and sessions live
in SQLite. Seeded on first start:

| Username | Password | Role |
|---|---|---|
| `admin` | `password` | Administrator |
| `user` | `password` | Viewer |

Change both on first login (`must_change_password`) on the login page
before `/dashboard` opens. Roles OR together;
per-user grant/deny overrides win.

| Role | Map / links / inventory | Inventory writes | Users / LDAP |
|---|---|---|---|
| viewer | read | | |
| operator | read | yes (including device passwords) | |
| admin | read | yes | yes |

`/kiosk` stays anonymous. `GET /api/state` and `GET /api/time` stay public
so the wall board can poll; unauthenticated `/api/state` omits credential
slot names and `*_set` flags. `GET /` is the login page. `GET /dashboard`
and all writes require a session.

LDAP uses LDAPS user bind via system `ldapsearch` (`apt install ldap-utils`).
Type only the sAMAccountName; set bind template to `{username}@nexstar.tv`.
AD groups map to roles under **Admin → LDAP**. Group-only users get an
ephemeral session (no row until an admin adds them).

Idle timeout defaults to 120 minutes (Admin → Session). **Admin →
Services and logs** shows systemd status and journal lines for
`nexnoc-web`, `nexnoc-poller`, `nexnoc-trapd`, and `apache2`, and can
restart them. That uses `scripts/nexnoc-svc` via a sudoers drop-in
installed by `setup.sh` — re-run setup on an existing box or Restart
will fail. Audit log is
`audit.jsonl` next to the DB (or `/var/lib/nexnoc/audit.jsonl`); inventory
writes are refused if the audit line cannot be written — `audit.audit_writable()`
is checked *before* the write proceeds, so a full disk or bad permission on
the audit file blocks the change rather than silently losing the log entry.

## SNMP monitoring and traps

SNMP is a channel parallel to driver polling, not a replacement — a device
polled via `direct_api` can still have SNMP GET and/or traps turned on for
cross-checking. Per-device flags (`snmp_enabled`, `snmp_trap_enabled`,
`snmp_version` v1/v2c/v3) live alongside the existing `access_mode` column.

`trapd.py` is a stdlib-only SNMPv1/v2c BER decoder listening on UDP 162
(`nexnoc-trapd.service`; needs `CAP_NET_BIND_SERVICE` or run on `--port 1162`
in dev). SNMPv3 traps arrive via `snmptrapd`'s `traphandle`, piping a
line-based payload into `trapd.py --from-snmptrapd`. Every trap is logged to
`trap_log` (source IP, OID, varbinds — never the community string or v3
secrets); traps that match a known device by `mgmt_host`/agent address (and
community, for v1/v2c) also run through that device's resolved driver
`interpret_trap()`, which can flip status to `degraded`/`healthy` immediately
instead of waiting for the next poll cycle.

## Inventory admin: merge and bulk operations

`db.merge_devices(source, target)` folds a duplicate device entry into
another: ports and flows move to the target (same-named ports collapse,
flows remap to the surviving port), blank target fields (mgmt_host,
credentials, model/firmware) are backfilled from the source, then
the source row is deleted. Exposed through the bulk inventory API
(`POST .../bulk` with `merge_into`, or `patch`/`delete` across a set of ids)
for admins cleaning up inventory without hand-editing SQLite.

## Security notes

- Device credentials (API username/password, SNMP community/v3, NMS key)
  are stored on the device row. The poller reads those columns. Passwords
  are never returned by the HTTP API or written to the log.
- `api_verify_tls` defaults to `False` per-device because broadcast
  appliances commonly ship self-signed certs — an explicit per-device
  opt-out, not a silent global default.
- Routing control is **not implemented and not on the roadmap** (see
  Roadmap section) — if it's ever built, it must not go on the same code
  path as the read-only poller. Permission names (`view_routing`,
  `propose_routing`, `execute_routing`) are already reserved on the admin
  role, unused, in case it comes back.

## Files

| File | Purpose |
|---|---|
| `schema.sql` | Full DB schema (live tables plus unused backlog tables: licenses, config_snapshots, routing_audit) |
| `db.py` | SQLite data access layer — `Device` is vendor/driver-agnostic; also owns `merge_devices()` |
| `inventory_api.py` | Inventory REST handlers: CRUD, bulk patch/delete/merge, port-direction inference |
| `audit.py` | Append-only JSONL audit log; `audit_writable()` gates writes fail-closed |
| `docs/DRIVERS.md` | How to add a vendor, model, or firmware-ranged driver |
| `docs/ROUTING.md` | Routing control design — backlog idea, not on the roadmap, not implemented |
| `docs/DEPLOY.md` | Go-live checklist: TLS, firewall, credential sweep, DB backups |
| `scripts/nexnoc-backup-db` | Online SQLite backup (cron-friendly, gzip + retention) |
| `scripts/nexnoc-svc` | Allowlisted systemd status/logs/restart for Admin → Services |
| `drivers/base.py` | `Driver` contract + `resolve_driver()` matching logic |
| `drivers/registry.py` | The list of every driver NexNOC knows about |
| `drivers/http_util.py` | Shared HTTP/JSON transport (Appear, Haivision) |
| `drivers/snmp_util.py` | Shared SNMP helpers (Net Insight, and any vendor's SNMP fallback) |
| `drivers/appear.py` | Appear default driver |
| `drivers/haivision.py` | Haivision default driver |
| `drivers/net_insight.py` | Net Insight default driver |
| `poller.py` | Bootstrap (config → DB) + driver resolution + async health poll loop + CLI |
| `server.py` | Stdlib HTTP server: `web/` frontend + `/api/state` + device detail |
| `web/` | `index.html` login, `dashboard.html` board (Leaflet map, links, inventory, kiosk) |
| `web/vendor/leaflet/` | Vendored Leaflet 1.9.4 (no npm) |
| `scripts/fetch_tiles.py` | Build a local CONUS XYZ tile pack for air-gapped / production use |
| `config.example.json` | Template multi-vendor inventory config (includes example trunks/signals) |
| `setup.sh` | Debian/Ubuntu installer: apt, systemd, Apache (site + `/api` proxy), SQLite bootstrap |
| `systemd/` | `nexnoc-poller.service` + `nexnoc-web.service` + `nexnoc-trapd.service` |
| `trapd.py` | SNMPv1/v2c UDP trap listener (v3 via snmptrapd traphandle) |
| `config/apache-nexnoc.conf` | Apache vhost: `web/` + `/tiles` + `/uploads/pins`; `/api/` → loopback |
| `config/nexnoc.env.example` | Optional process env file (copied to `/etc/nexnoc/nexnoc.env`) |
| `tests/` | Unit tests (no network) |

## Adding a new driver

See **[docs/DRIVERS.md](docs/DRIVERS.md)** for the full how-to (matching
rules, inclusive `firmware_min` / `firmware_max`, `notes`, checklists,
and worked examples).

Short version:

- **New vendor:** add the slug to `db.VALID_VENDORS`, write a default
  `Driver` in `drivers/<name>.py` (`ping()` minimum, `notes` set),
  register it in `drivers/registry.py`, add the vendor to the inventory
  form in `web/app.js`. Branch `poller.py:build_driver()` only if the
  constructor args differ from HTTP or SNMP.
- **New model or firmware range:** new class with `supported_models`
  and/or inclusive `firmware_min` / `firmware_max`, listed *before* the
  vendor default in `DRIVER_REGISTRY`. Devices pick it up on the next
  poll from `model` / `firmware_version`.

## Roadmap

- **Phase 2** — Done. Leaflet geo map (CDN tiles in dev, local `/tiles`
  pack in production) + city/hop drill-down + filterable source/destination
  table + inventory + kiosk. Live updates via 5s polling of `/api/state`
  (stdlib HTTP; no WebSocket).
- **Kiosk** — Done as `/kiosk` (same polling refresh as the operator UI).

**Not on the roadmap — backlog ideas only** (nothing implemented, nothing
scheduled; empty tables and reserved permission names stay in place so
a later pickup does not need a schema migration):

- **License tracking** (was "Phase 3") — per-device feature/expiry.
  Needs confirmed API/SNMP/OID details per vendor first.
- **Config backup/diff/restore** (was also "Phase 3") —
  `config_snapshots` table already exists; needs a confirmed export
  surface, a snapshot job, and a diff view.
- **Routing control** (was "Phase 4") — propose → diff → explicit
  confirm → push → verify → `routing_audit` log. A design pass exists
  at **[docs/ROUTING.md](docs/ROUTING.md)** for reference if it's ever
  picked back up.
