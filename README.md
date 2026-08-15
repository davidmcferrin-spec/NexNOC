# NexNOC

Multi-site, multi-vendor NOC/management tool for broadcast video/signal-path
infrastructure: inventory, health monitoring, geo + topology map, license
tracking, config backup, and controlled routing changes, with a kiosk
"big board" display.

**Status: Phase 1–2 complete.** Sites/devices inventory across multiple
vendors, a pluggable driver architecture, SQLite schema for all phases, an
async health poller with automatic driver resolution, API/SNMP discovery
tools, and a stdlib-only dashboard (geo map, trunk drill-down, filterable
links table, inventory, kiosk board). Phase 3+ still blocked on confirmed
per-vendor API/OID details.

## Architecture: devices are driver-driven

A **vendor** is a rough family (Appear, Haivision, Net Insight). A
**driver** is what actually knows how to talk to a specific box — matched
on vendor + model + firmware version, not vendor alone. This matters
because a firmware upgrade or a new model in the same product line can
change the API/SNMP surface enough that the old driver stops working
correctly for it, without changing which *vendor* it is.

Each vendor currently has exactly one driver — its **default** (no
model/firmware constraints, matches anything from that vendor). When you
need different handling for a specific model or firmware range:

1. Write a new driver class narrower than the default (`supported_models`
   and/or `firmware_min`/`firmware_max` set).
2. Register it in `drivers/registry.py`, listed *before* the default (see
   `drivers/base.py:resolve_driver()` for the tie-break rule).
3. Nothing else changes — `poller.py`, `db.py`, and every other driver are
   untouched. Existing devices keep using whatever driver actually matches
   their `model`/`firmware_version`.

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
| Anything else | — | — | Add a `drivers/<name>.py` implementing `Driver` (see `drivers/base.py`) |

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

Production path: Apache on :80, Python dashboard on `127.0.0.1:8080`,
SQLite at `/var/lib/nexnoc/noc.db`. No pip, no MySQL.

```bash
sudo ./setup.sh
# edit /etc/nexnoc/config.json  (inventory)
# edit /etc/nexnoc/nexnoc.env   (credential values; mode 0640)
sudo systemctl restart nexnoc-poller
```

Open `http://<hostname>/` or `http://<hostname>/kiosk`.

```bash
sudo ./setup.sh update     # after git pull: rsync + restart
sudo ./setup.sh --check    # sanity
sudo ./setup.sh status
```

Overrides: `NEXNOC_PREFIX` `NEXNOC_DATA` `NEXNOC_ETC` `NEXNOC_SERVER_NAME`.

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
```

Polls every device concurrently on `poll_interval_seconds` (config.json,
default 30s), resolving and dispatching to the right driver automatically
per device. A device flips to `unreachable` only after 3 consecutive
missed polls (`CONSECUTIVE_FAILURES_THRESHOLD` in poller.py) — avoids
status-flapping on a single dropped packet.

Production uses `nexnoc-poller.service` / `nexnoc-web.service` from
`setup.sh`, with credentials in `/etc/nexnoc/nexnoc.env`
(`EnvironmentFile=`, mode 0640).

### Run the dashboard

```bash
python3 server.py --config config.json --db /var/lib/nexnoc/noc.db --port 8080
```

`--config` is optional (bootstraps if given). Open http://127.0.0.1:8080
for the map / links / inventory views, or http://127.0.0.1:8080/kiosk for
the wall-board layout. The page polls `/api/state` every 5 seconds — no
WebSocket stack. Bind defaults to localhost; this is a read-only ops view
with no auth gate, so do not expose it past the management LAN without a
reverse proxy.

The map is Leaflet (vendored in `web/vendor/leaflet/`, no npm). Dev uses
Carto Dark Matter tiles from the public CDN — pan, zoom, real geography.
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

## Security notes

- Credentials are never stored in the DB or `config.json` — only the *names*
  of environment variables that hold them. Resolved via `os.environ` at
  poll time (`poller.py:resolve_env`).
- `api_verify_tls` defaults to `False` per-device because broadcast
  appliances commonly ship self-signed certs — an explicit per-device
  opt-out, not a silent global default.
- Routing control (Phase 4) is intentionally **not** built on the same code
  path as the read-only poller. It needs its own auth gate, a confirm/diff
  step, and an audit trail (`routing_audit` table already in schema.sql).

## Files

| File | Purpose |
|---|---|
| `schema.sql` | Full DB schema for all phases (Phase 2+ tables created now, unused until built) |
| `db.py` | SQLite data access layer — `Device` is vendor/driver-agnostic |
| `drivers/base.py` | `Driver` contract + `resolve_driver()` matching logic |
| `drivers/registry.py` | The list of every driver NexNOC knows about |
| `drivers/http_util.py` | Shared HTTP/JSON transport (Appear, Haivision) |
| `drivers/snmp_util.py` | Shared SNMP helpers (Net Insight, and any vendor's SNMP fallback) |
| `drivers/appear.py` | Appear default driver |
| `drivers/haivision.py` | Haivision default driver |
| `drivers/net_insight.py` | Net Insight default driver |
| `poller.py` | Bootstrap (config → DB) + driver resolution + async health poll loop + CLI |
| `server.py` | Stdlib HTTP server: `web/` frontend + `/api/state` + device detail |
| `web/` | Dashboard (Leaflet map, links table, inventory, kiosk) — vanilla HTML/CSS/JS |
| `web/vendor/leaflet/` | Vendored Leaflet 1.9.4 (no npm) |
| `scripts/fetch_tiles.py` | Build a local CONUS XYZ tile pack for air-gapped / production use |
| `config.example.json` | Template multi-vendor inventory config (includes example trunks/signals) |
| `setup.sh` | Debian/Ubuntu installer: apt, systemd, Apache reverse-proxy, SQLite bootstrap |
| `systemd/` | `nexnoc-poller.service` + `nexnoc-web.service` |
| `config/apache-nexnoc.conf` | Apache vhost template (proxies to loopback) |
| `config/nexnoc.env.example` | Credential env template (copied to `/etc/nexnoc/nexnoc.env`) |
| `tests/` | Unit tests (no network) |

## Adding a new driver

**For a new vendor entirely:**
1. Add the vendor name to `db.VALID_VENDORS`.
2. Create `drivers/<name>.py` implementing `Driver` (`drivers/base.py`) —
   at minimum `ping()`; `discover()` if a probing approach makes sense.
   Leave `supported_models`/`firmware_min`/`firmware_max` unset so it's
   that vendor's default.
3. Register it in `drivers/registry.py`.
4. If it's HTTP-based, compose a `drivers.http_util.JsonHttpClient`. If
   SNMP-based, use `drivers.snmp_util`. If neither fits (e.g. a vendor whose
   only integration point is a separate NMS), model it after
   `drivers/net_insight.py`'s `via_nms` pattern.
5. Add device config fields to `poller.py:build_driver()` if the vendor
   needs constructor args beyond what `devices` already has columns for.

**For a new model or firmware range within an existing vendor:**
1. Write a new class in that vendor's module (or a new file) with
   `supported_models` and/or `firmware_min`/`firmware_max` set narrower
   than the default.
2. Register it in `drivers/registry.py`, *before* the vendor's default.
3. Devices with a matching `model`/`firmware_version` pick it up
   automatically on the next poll; nothing else changes.

## Roadmap

- **Phase 2** — Done. Leaflet geo map (CDN tiles in dev, local `/tiles`
  pack in production) + city/hop drill-down + filterable source/destination
  table + inventory + kiosk. Live updates via 5s polling of `/api/state`
  (stdlib HTTP; no WebSocket).
- **Phase 3** — License tracking (needs real API/SNMP/OID details per
  vendor — discovery first) + config backup/diff/restore (`config_snapshots`
  table already exists; needs a scheduled snapshot job + diff view).
- **Phase 4** — Routing control: propose → diff → explicit confirm → push →
  verify → `routing_audit` log. Separate auth scope from read-only viewing.
  Cross-vendor routing (e.g. an Appear frame feeding a Haivision encoder)
  needs its own design pass once Phase 1–3 land per vendor.
- **Kiosk** — Done as `/kiosk` (same polling refresh as the operator UI).
