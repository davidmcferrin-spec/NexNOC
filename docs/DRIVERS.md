# How to write a NexNOC driver

A **vendor** is a family (`appear`, `haivision`, `net_insight`, `generic_snmp`).
A **driver** is the code that talks to a specific box. Matching is
**vendor + optional model + optional firmware range**, not vendor alone.

You do **not** change `poller.py`, `db.py`, or other drivers for a new model
or firmware range. Write a class, register it, done.

**Stack constraint:** stdlib-only Python. No pip packages. HTTP uses
`drivers/http_util.py`. SNMP shells out via `drivers/snmp_util.py`.

**Confirm first, implement second.** Do not invent REST paths, JSON schemas,
or enterprise OIDs. Probe a live box (`discover()`, `/apidoc`, device MIB
download) and only then parse those confirmed surfaces.

This document is the maintained how-to. The short checklist in `README.md`
points here. Matching logic lives in `drivers/base.py`; the list of known
drivers is `drivers/registry.py`.

---

## 1. How a device finds its driver

```
Device row (vendor, model, firmware_version, driver_override)
        │
        ▼
resolve_driver()  →  driver class  →  new instance each poll
        │
        ▼
devices.resolved_driver  (informational, written after each poll)
```

Resolution order in `drivers/base.py`:

1. **`driver_override`** — explicit pin. Always wins. Unknown id →
   `DriverResolutionError`.
2. **Most specific match** — first registered non-default driver whose
   `applies_to(model, firmware)` is true.
3. **Vendor default** — no `supported_models`, no `firmware_min`, no
   `firmware_max`.
4. **Error** — unknown vendor, or no default and nothing matched.

When two non-default drivers both match, **registry order wins**. List
narrower / newer drivers **first**. Keep firmware ranges non-overlapping.

---

## 2. Matching fields (class-level)

| Attribute | Meaning | Unset (`None`) means |
|---|---|---|
| `driver_id` | Unique id, e.g. `appear.x_platform.default` | required |
| `vendor` | Must be in `db.VALID_VENDORS` | required |
| `supported_models` | List of **substrings**, case-insensitive, matched against `Device.model` | any model |
| `firmware_min` | Inclusive lower bound (`>=`) | no lower bound |
| `firmware_max` | Inclusive upper bound (`<=`) | no upper bound |
| `notes` | Operator-facing sentence: what this driver covers, confirmed surface, what it must not do | hidden in catalog/UI |
| `connectors` | Optional BNC/SDI template stamped on create | none |

A driver is the **vendor default** only when all three of `supported_models`,
`firmware_min`, and `firmware_max` are unset. Every vendor must have exactly
one default.

`notes` is documentation, not a matching field. It is exported by
`driver_catalog()` and shown in the inventory UI (driver dropdown title +
notes line under the monitor-driver select).

### Model matching

```python
supported_models = ["X20", "X10"]
```

Matches `"Appear X20"`, `"x20-dc"`, `"X10 frame"`. Does **not** match if
`model` is empty.

Use short, distinctive tokens. `"X"` is too broad.

### Firmware: greater-than, less-than, or a range

Bounds are **inclusive**. Either side can be omitted.

```python
# Firmware 3.0.0 and newer (greater than or equal)
firmware_min = "3.0.0"

# Firmware 2.9.9 and older (less than or equal)
firmware_max = "2.9.9"

# Inclusive window
firmware_min = "2.4.0"
firmware_max = "2.9.99"
```

Parser (`_parse_version`) is best-effort, not strict semver:

- `"2.4.1"` → `(2, 4, 1)`
- `"v2.4.1"` / `"V2.4.1"` → same
- Non-numeric junk is stripped (`"2.4.1-rc3"` still compares)
- Empty / unparseable → `(0,)`

**If either bound is set, the device must have a `firmware_version`.**
Empty firmware → this driver does not match, and the vendor default (or a
model-only driver) is used.

That is intentional. First poll often has no firmware yet. `collect()` can
return `firmware_version`; the poller writes it back via
`db.set_device_firmware()`. The **next** poll can then pick the
firmware-specific driver. Seed `firmware_version` in config (or the
inventory form) if you need the specific driver on the first cycle.

---

## 3. What a driver must implement

Minimum: `ping()`. Everything else is optional.

| Method | Required | Rules |
|---|---|---|
| `ping()` | yes | Cheapest reachability. **Must not raise** — catch transport errors, return `False`. |
| `collect()` | no | Richer health/inventory. Return `CollectResult` or `None`. Not required for ping. |
| `discover()` | no | Probe candidate HTTP paths. Default raises. |
| `snmp_ping()` / `snmp_collect()` | no | Default is MIB-2 `sysDescr`. Override only after confirmed vendor OIDs. |
| `interpret_trap()` | no | Default handles generic MIB-2 traps. Override only for confirmed enterprise OIDs. |

Raise these (not raw `urllib` exceptions) from HTTP helpers:

- `DriverError` — generic talk-to-device failure
- `DriverAuthError` — bad credentials
- `DriverUnreachableError` — timeout / refused / DNS
- `DriverResolutionError` — picking a driver failed (not your problem inside `ping`/`collect`)

Drivers are **stateless**. The poller constructs a fresh instance every
cycle. Do not hold long-lived connections.

**Never** start/stop/edit/route from a poller driver. Write paths belong to
Phase 4 (separate auth, confirm/diff, audit).

---

## 4. Recipe A — new vendor (new make)

1. Add the vendor slug to `VALID_VENDORS` in `db.py` (e.g. `"acme"`).
2. Create `drivers/<vendor>.py` with a **default** class (no model/firmware
   constraints).
3. Import it and append it in `drivers/registry.py`.
4. If the constructor is not “HTTP host/port/user/pass” or “SNMP
   host/community”, add a branch in `poller.py` `build_driver()`.
5. Add tests (resolution fixtures if matching is interesting; HTTP/SNMP
   tests with no live network).
6. Add the vendor to the inventory form’s vendor `<select>` in
   `web/app.js` (`deviceForm` / bulk edit).
7. Add a device in `config.json` or the inventory UI.

HTTP skeleton:

```python
"""drivers/acme.py — Acme Widget 4000.

Confirmed against a live unit at <host> on <date>:
  GET /api/health   → {"status": "ok", "firmware": "1.2.3"}
Do not invent other paths until confirmed.
"""
from typing import Optional
from drivers.base import CollectResult, Driver

class AcmeWidgetDriver(Driver):
    driver_id = "acme.widget.default"
    vendor = "acme"
    notes = "Default Acme Widget driver. Confirmed /api/health on Widget 4000 fw 1.2.3."

    def __init__(self, host: str, port: int = 443, scheme: str = "https",
                 username=None, password=None, verify_tls: bool = False,
                 timeout: float = 5.0):
        from drivers.http_util import JsonHttpClient
        self._client = JsonHttpClient(
            host=host, port=port, scheme=scheme,
            verify_tls=verify_tls, timeout=timeout,
        )

    def ping(self) -> bool:
        try:
            self._client.get_json("/api/health")  # confirmed path only
            return True
        except Exception:
            return False

    def collect(self) -> Optional[CollectResult]:
        body = self._client.get_json("/api/health")
        return CollectResult(
            device_status="healthy" if body.get("status") == "ok" else "degraded",
            firmware_version=body.get("firmware") or None,
        )
```

SNMP-only vendors: copy `drivers/generic_snmp.py` or
`drivers/net_insight.py`. Do not invent enterprise OIDs — pull the device’s
own MIB first. `generic_snmp` already covers “we can ping it, no vendor API
yet.”

`access_mode` (`direct_api` / `direct_snmp` / `via_nms`) is **how we reach
the box**. The driver is **how we interpret what it says**. They are
orthogonal.

---

## 5. Recipe B — new model or firmware-specific driver

Same vendor, different API/SNMP surface.

1. New class in the existing vendor file (or a new file).
2. Set `supported_models` and/or `firmware_min` / `firmware_max`.
3. Set `notes` describing the range and the confirmed surface.
4. Register it in `DRIVER_REGISTRY` **ahead of** the vendor default (and
   ahead of any broader sibling).
5. Existing devices keep the default until their `model` /
   `firmware_version` match.

Example — Haivision Makito X4, firmware 2.0 and newer (API changed):

```python
class HaivisionMakitoX4Fw2Driver(Driver):
    driver_id = "haivision.makito_x4.fw2plus"
    vendor = "haivision"
    supported_models = ["Makito X4", "X4"]
    firmware_min = "2.0.0"          # >= 2.0.0
    notes = (
        "Makito X4 firmware 2.x. Confirmed /apis/... on 2.0.1. "
        "1.8.x stays on haivision.makito_x.default."
    )
    # ... ping/collect against the 2.x surface ...
```

Example — old firmware only (`<=`):

```python
class AppearX20LegacyDriver(Driver):
    driver_id = "appear.x20.fw_lt_4"
    vendor = "appear"
    supported_models = ["X20"]
    firmware_max = "3.99.99"        # <= 3.99.99
    notes = "X20 firmware 3.x and older. Prometheus path set confirmed on 3.2."
```

Example — inclusive window:

```python
firmware_min = "4.0.0"
firmware_max = "4.9.99"
```

Registry order (narrowest first):

```python
DRIVER_REGISTRY = [
    HaivisionMakitoX4Fw2Driver,   # model + firmware_min
    AppearX20LegacyDriver,        # model + firmware_max
    AppearXPlatformDriver,        # appear default
    HaivisionMakitoXDriver,       # haivision default
    NetInsightNimbraDriver,
    GenericSnmpDriver,
]
```

---

## 6. Adding the device (make / model / firmware)

In `config.json` or the inventory UI:

```json
{
  "site": "Chicago - Wacker",
  "name": "CHI-MX4-ENC-2",
  "vendor": "haivision",
  "device_role": "encoder",
  "model": "Makito X4",
  "firmware_version": "2.0.1",
  "mgmt_host": "10.0.1.21",
  "access_mode": "direct_api",
  "api_port": 443,
  "api_scheme": "https",
  "api_verify_tls": false,
  "api_username_env": "CHI_MX4_2_USER",
  "api_password_env": "CHI_MX4_2_PASS"
}
```

- `vendor` must be in `VALID_VENDORS`.
- `model` should contain a substring from `supported_models`.
- `firmware_version` can start empty; `collect()` may fill it. Seed it if
  you need a ranged driver immediately.
- Omit `driver_override` unless auto-resolve is wrong for one unit. Pin
  with the exact `driver_id`.
- Store **env var names** for credentials, never values. Never log
  credential values.

After the next poll, `devices.resolved_driver` shows which class was used.
The inventory form’s monitor-driver dropdown lists each driver’s
`firmware_min` / `firmware_max` and shows `notes` under the select.

---

## 7. Notes (human-readable)

Put a module docstring at the top of every driver file: confirmed
paths/OIDs, date, which live unit, what is **not** implemented.

Set the class `notes` attribute to a short operator-facing summary of the
same facts. `driver_catalog()` exports it; the dashboard shows it when
picking or viewing a driver.

Cities/sites already have a `notes` column — that is inventory commentary,
not driver matching.

---

## 8. Tests

`tests/test_driver_resolution.py` uses **fixture** drivers, not the real
Appear/Haivision classes. Copy that pattern.

Cover at least:

- default matches anything
- model substring, case-insensitive
- `firmware_min` inclusive; below-min rejected
- `firmware_max` inclusive; above-max rejected
- empty firmware + ranged driver → no match → default
- registry order when two specifics overlap
- `driver_override` wins; unknown override raises
- `driver_catalog()` includes `notes`, `firmware_min`, `firmware_max`

HTTP/SNMP tests: no live network. Fake the client / `snmpget`.

---

## 9. Checklist

**New vendor**

- [ ] Slug in `db.VALID_VENDORS`
- [ ] `drivers/<vendor>.py` with a default class (`ping()` at minimum)
- [ ] Confirmed endpoints/OIDs in the module docstring
- [ ] `notes` set on the class
- [ ] Registered in `drivers/registry.py`
- [ ] `build_driver()` branch only if constructor args differ
- [ ] Vendor option added in `web/app.js` device form
- [ ] Tests
- [ ] Device row with `vendor` / `model` / `access_mode`

**New model or firmware range**

- [ ] New class, narrower than the default
- [ ] `firmware_min` and/or `firmware_max` inclusive; ranges do not overlap siblings
- [ ] `notes` describes the range and confirmed surface
- [ ] Listed **before** the default in `DRIVER_REGISTRY`
- [ ] Devices have `model` (and `firmware_version` if ranged)
- [ ] Tests for the new `applies_to` cases

**Do not**

- Invent API paths or enterprise OIDs
- Call start/stop/edit/route from the poller
- Log passwords or community strings
- Call driver I/O directly from async code (poller already offloads via
  `run_in_executor`)
- Skip the vendor default — something must catch unmatched models

---

## 10. Current defaults

| Vendor | Default `driver_id` | Access | Notes |
|---|---|---|---|
| Appear | `appear.x_platform.default` | `direct_api` | Prometheus scrapes only. No invented MMI/IpGateway JSON. |
| Haivision | `haivision.makito_x.default` | `direct_api` | Makito X4 1.8.0 `/apidoc`. Session login. No start/stop/edit. |
| Net Insight | `net_insight.nimbra.default` | `direct_snmp` | Per-node SNMP. `via_nms` (Nimbra Vision) is `NotImplementedError`. |
| Catch-all | `generic.snmp.default` | `direct_snmp` | MIB-2 reachability until a real vendor driver exists. |

When a firmware upgrade or new model changes the API/SNMP surface, add a
narrower class (Recipe B) rather than rewriting the default.
