"""One-shot: build config.json from Newsnation Global Path Naming.xlsx.

Includes every HAI and Appear path, even when IPs or credentials are missing.
Devices without a management IP get an empty mgmt_host and poll_enabled=false
so they show up in the portal for later editing.

Never writes credential *values* — only env var names.
"""
from __future__ import annotations

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "Example Docs" / "Newsnation Global Path Naming.xlsx"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

CITY: dict[str, str] = {
    "NY": "New York",
    "CHI": "Chicago",
    "DC": "Washington DC",
    "NN DC": "Washington DC",
    "INDY": "Indianapolis",
    "ATL CW": "Atlanta",
    "CW ATL": "Atlanta",
    "NY HMPTNS": "New York",
    "CONN": "Connecticut",
    "WDCW": "Washington DC",
    "CW BURBANK": "Burbank",
    "PAC12": "San Ramon",
    "AM": "AM",
    "UNK": "Unknown",
}
SITE: dict[str, str] = {
    "NY": "New York",
    "CHI": "Chicago",
    "DC": "Washington DC",
    "NN DC": "Washington DC - NN",
    "INDY": "Indianapolis",
    "ATL CW": "Atlanta - CW",
    "CW ATL": "Atlanta - CW",
    "NY HMPTNS": "New York - Hamptons",
    "CONN": "Connecticut",
    "WDCW": "Washington DC - WDCW",
    "CW BURBANK": "Burbank - CW",
    "PAC12": "San Ramon - PAC12",
    "AM": "AM",
    "UNK": "Unknown",
}
SITE_CODE: dict[str, str] = {
    "New York": "NY",
    "Chicago": "CHI",
    "Washington DC": "DC",
    "Washington DC - NN": "NNDC",
    "Washington DC - WDCW": "WDCW",
    "Indianapolis": "INDY",
    "Atlanta - CW": "ATL",
    "New York - Hamptons": "HMP",
    "Connecticut": "CONN",
    "Burbank - CW": "BUR",
    "San Ramon - PAC12": "PAC",
    "AM": "AM",
    "Unknown": "UNK",
}
CITY_COORDS: dict[str, tuple[float, float]] = {
    "New York": (40.7128, -74.0060),
    "Chicago": (41.8781, -87.6298),
    "Washington DC": (38.9072, -77.0369),
    "Indianapolis": (39.7684, -86.1581),
    "Atlanta": (33.7490, -84.3880),
    "Connecticut": (41.3083, -72.9279),
    "Burbank": (34.1808, -118.3090),
    "San Ramon": (37.7799, -121.9780),
}
SITE_COORDS: dict[str, tuple[float, float]] = {
    "New York": (40.7128, -74.0060),
    "Chicago": (41.8781, -87.6298),
    "Washington DC": (38.9072, -77.0369),
    "Washington DC - NN": (38.9072, -77.0369),
    "Washington DC - WDCW": (38.9200, -77.0100),
    "Indianapolis": (39.7684, -86.1581),
    "Atlanta - CW": (33.7490, -84.3880),
    "New York - Hamptons": (40.9634, -72.1848),
    "Connecticut": (41.3083, -72.9279),
    "Burbank - CW": (34.1808, -118.3090),
    "San Ramon - PAC12": (37.7799, -121.9780),
}
PLACE_ALIASES = {
    "NY": "NY",
    "CHI": "CHI",
    "CHICAGO": "CHI",
    "DC": "DC",
    "NN DC": "NN DC",
    "INDY": "INDY",
    "ATL CW": "ATL CW",
    "ALT CW": "ATL CW",
    "CW ATL": "CW ATL",
    "NY HMPTNS": "NY HMPTNS",
    "CONN": "CONN",
    "WDCW": "WDCW",
    "CW BURBANK": "CW BURBANK",
    "PAC12": "PAC12",
    "PAC 12": "PAC12",
    "PAC 12 SAN RAMON": "PAC12",
    "AM": "AM",
}


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall("m:si", NS):
        out.append("".join(t.text or "" for t in si.findall(".//m:t", NS)))
    return out


def col_row(ref: str) -> tuple[str, int]:
    m = re.match(r"([A-Z]+)(\d+)", ref)
    return m.group(1), int(m.group(2))


def load_sheet(zf: zipfile.ZipFile, ss: list[str], path: str) -> dict[int, dict[str, str]]:
    root = ET.fromstring(zf.read(path))
    rows: dict[int, dict[str, str]] = {}
    for c in root.findall(".//m:c", NS):
        ref = c.get("r")
        if not ref:
            continue
        col, row = col_row(ref)
        t = c.get("t")
        v = c.find("m:v", NS)
        is_el = c.find("m:is", NS)
        if t == "s" and v is not None and v.text:
            val = ss[int(v.text)]
        elif t == "inlineStr" and is_el is not None:
            val = "".join(x.text or "" for x in is_el.findall(".//m:t", NS))
        elif v is not None and v.text is not None:
            val = v.text
        else:
            val = ""
        rows.setdefault(row, {})[col] = val
    return rows


def cell(row: dict, col: str) -> str:
    return (row.get(col) or "").strip()


def host_of(s: str) -> str | None:
    s = (s or "").strip()
    if not s or s in ("N/A", "?", "??"):
        return None
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", s)
    return m.group(1) if m else None


def short(ip: str) -> str:
    _a, _b, c, d = ip.split(".")
    return f"{c}.{d}"


def title_place(s: str) -> str:
    return " ".join(part.capitalize() for part in s.split())


def site_code(site: str) -> str:
    if site in SITE_CODE:
        return SITE_CODE[site]
    parts = re.findall(r"[A-Z0-9]+", site.upper())
    return (parts[0][:6] if parts else "UNK")


def slug(s: str, limit: int = 16) -> str:
    cleaned = re.sub(r"[^A-Z0-9]+", "", (s or "").upper())
    return (cleaned or "PENDING")[:limit]


def norm_place(s: str) -> str | None:
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"\?+$", "", s).strip()
    if not s:
        return None
    key = s.upper()
    if key in PLACE_ALIASES:
        return PLACE_ALIASES[key]
    if key not in CITY:
        CITY[key] = title_place(s)
        SITE[key] = title_place(s)
        SITE_CODE.setdefault(SITE[key], slug(key, 6))
    return key


def env_base(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


def ensure_env_example(device_list: list[dict]) -> None:
    """Append missing *_USER / *_PASS placeholders. Never overwrite values."""
    path = ROOT / "config" / "nexnoc.env.example"
    existing = path.read_text(encoding="utf-8") if path.is_file() else (
        "# NexNOC credentials — copy to /etc/nexnoc/nexnoc.env (mode 0640).\n"
        "# Names must match the *_env fields in config.json.\n"
        "# Never put values in config.json. Placeholders only — not production secrets.\n\n"
    )
    present = set()
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            present.add(stripped.split("=", 1)[0].strip())
    additions = []
    for d in device_list:
        for key in (d.get("api_username_env"), d.get("api_password_env")):
            if key and key not in present:
                additions.append(f"{key}=change_me")
                present.add(key)
    if not additions:
        return
    if not existing.endswith("\n"):
        existing += "\n"
    if not existing.endswith("\n\n"):
        existing += "\n"
    existing += "# --- additional names from path sheet (fill in the portal or here) ---\n"
    existing += "\n".join(additions) + "\n"
    path.write_text(existing, encoding="utf-8")


def build_inventory(xlsx: Path | None = None) -> dict:
    path = xlsx or XLSX
    zf = zipfile.ZipFile(path)
    ss = load_shared_strings(zf)
    hai = load_sheet(zf, ss, "xl/worksheets/sheet2.xml")
    appear = load_sheet(zf, ss, "xl/worksheets/sheet3.xml")

    devices: OrderedDict[str, dict] = OrderedDict()
    ports: list[dict] = []
    port_keys: set[tuple[str, str]] = set()
    flows: list[dict] = []

    def add_dev(ip: str | None, place: str, vendor: str, role: str,
                serial: str = "", hint: str = "") -> str | None:
        pl = norm_place(place) or "UNK"
        site = SITE.get(pl, title_place(pl))
        prefix = "HAI" if vendor == "haivision" else "X20"
        code = site_code(site)
        if ip:
            key = f"ip:{ip}"
            name = f"{code}-{prefix}-{short(ip)}"
            host = ip
            poll = True
        else:
            tag = slug(hint or serial or f"{pl}-{role}")
            key = f"pending:{vendor}:{site}:{role}:{tag}"
            name = f"{code}-{prefix}-{tag}"
            host = ""
            poll = False
        if key not in devices:
            env = env_base(name)
            devices[key] = {
                "site": site,
                "name": name,
                "vendor": vendor,
                "device_role": role,
                "model": "Makito X4" if vendor == "haivision" else "X20",
                "firmware_version": "",
                "mgmt_host": host,
                "access_mode": "direct_api",
                "api_port": 443,
                "api_scheme": "https",
                "api_verify_tls": False,
                "api_username_env": f"{env}_USER",
                "api_password_env": f"{env}_PASS",
                "poll_enabled": poll,
            }
            if serial:
                devices[key]["_serial"] = serial
        else:
            if serial:
                devices[key]["_serial"] = serial
            if ip and not devices[key]["mgmt_host"]:
                devices[key]["mgmt_host"] = ip
                devices[key]["poll_enabled"] = True
        return devices[key]["name"]

    def add_port(dev: str | None, name: str, kind: str) -> None:
        if not dev or not name:
            return
        key = (dev, name)
        if key in port_keys:
            return
        port_keys.add(key)
        ports.append({"device": dev, "name": name, "kind": kind})

    for r in sorted(hai):
        if r == 1:
            continue
        row = hai[r]
        qname = cell(row, "A")
        if not qname.upper().startswith("HAI"):
            continue
        content = cell(row, "F")
        src_place = cell(row, "C") or "UNK"
        src_ip = host_of(cell(row, "Q"))
        dst_ip = host_of(cell(row, "S"))
        dst2_ip = host_of(cell(row, "W"))
        src_port = cell(row, "D") or "In 1"
        src_name = add_dev(src_ip, src_place, "haivision", "encoder",
                           cell(row, "E"), hint=qname)
        add_port(src_name, src_port, "sdi_in")
        dests = []
        if cell(row, "J"):
            dests.append((cell(row, "J"), cell(row, "K"), dst_ip))
        if cell(row, "M"):
            dests.append((cell(row, "M"), cell(row, "N"), dst2_ip))
        signal = content or qname
        for dplace, dport, dip in dests:
            dpl = norm_place(dplace)
            if not dpl or not src_name:
                continue
            dst_name = add_dev(dip, dplace, "haivision", "decoder", hint=f"{qname}-DST")
            if dst_name and dport:
                add_port(dst_name, dport, "sdi_out")
            flow = {
                "signal": signal,
                "label": qname,
                "source_device": src_name,
                "source_port": src_port,
                "dest_city": CITY[dpl],
                "dest_site": SITE[dpl],
                "direction": "contribution",
            }
            if dst_name:
                flow["dest_device"] = dst_name
                if dport:
                    flow["dest_port"] = dport
            elif dport:
                flow["dest_label"] = dport
            flows.append(flow)

    appear_ctrl: dict[str, tuple[str | None, str | None]] = {}
    for r in sorted(appear):
        if r == 1:
            continue
        row = appear[r]
        pl = norm_place(cell(row, "C"))
        ctrl = host_of(cell(row, "I"))
        media = host_of(cell(row, "H"))
        if pl and (ctrl or media):
            appear_ctrl[pl] = (ctrl, media)

    for r in sorted(appear):
        if r == 1:
            continue
        row = appear[r]
        qname = cell(row, "A")
        if not qname:
            continue
        src_place = cell(row, "C") or "UNK"
        src_pl = norm_place(src_place)
        if not src_pl:
            continue
        ctrl, media = appear_ctrl.get(src_pl, (host_of(cell(row, "I")), host_of(cell(row, "H"))))
        ip = ctrl or media
        src_name = add_dev(ip, src_place, "appear", "frame", hint=src_pl)
        if src_name:
            # Re-find by walking devices — name is unique
            for d in devices.values():
                if d["name"] == src_name:
                    d["vendor"] = "appear"
                    d["model"] = "X20"
                    d["device_role"] = "frame"
                    break
        src_port = cell(row, "D")
        add_port(src_name, src_port, "sdi_in")
        dests: list[tuple[str, str]] = []
        if cell(row, "F"):
            dests.append((cell(row, "F"), cell(row, "G")))
        dest2 = cell(row, "L")
        if dest2:
            dests.append((dest2, ""))
        if host_of(cell(row, "M")) and not any(norm_place(a) == "CHI" for a, _ in dests):
            dests.append(("CHI", ""))
        signal = cell(row, "E") or qname
        for dplace, dport in dests:
            dpl = norm_place(dplace)
            if not dpl or not src_name:
                continue
            dest_ctrl, dest_media = appear_ctrl.get(dpl, (None, None))
            dip = dest_ctrl or dest_media
            if not dip and dpl == "CHI":
                dip = host_of(cell(row, "M"))
            dst_name = add_dev(dip, dplace, "appear", "frame", hint=dpl)
            if dst_name:
                for d in devices.values():
                    if d["name"] == dst_name:
                        d["vendor"] = "appear"
                        d["model"] = "X20"
                        d["device_role"] = "frame"
                        break
            if dst_name and dport:
                add_port(dst_name, dport, "sdi_out")
            flow = {
                "signal": signal,
                "label": qname,
                "source_device": src_name,
                "source_port": src_port,
                "dest_city": CITY[dpl],
                "dest_site": SITE[dpl],
                "direction": "contribution",
            }
            if dst_name:
                flow["dest_device"] = dst_name
                if dport:
                    flow["dest_port"] = dport
            flows.append(flow)

    site_to_city = {SITE[k]: CITY[k] for k in SITE}
    used_sites = {d["site"] for d in devices.values()}
    used_cities = {site_to_city[s] for s in used_sites if s in site_to_city}
    for f in flows:
        used_sites.add(f["dest_site"])
        used_cities.add(f["dest_city"])
        src = next((d for d in devices.values() if d["name"] == f["source_device"]), None)
        if src:
            used_cities.add(site_to_city.get(src["site"], src["site"]))

    cities = []
    for name in sorted(used_cities):
        coords = CITY_COORDS.get(name)
        row: dict = {"name": name}
        if coords:
            row["lat"], row["lng"] = coords
        cities.append(row)
    sites = []
    for name in sorted(used_sites):
        coords = SITE_COORDS.get(name)
        row = {"name": name, "city": site_to_city.get(name, name)}
        if coords:
            row["lat"], row["lng"] = coords
        sites.append(row)

    device_list = []
    for d in devices.values():
        row = {k: v for k, v in d.items() if not k.startswith("_")}
        device_list.append(row)

    return {
        "poll_interval_seconds": 30,
        "map": {
            "tile_url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            "tile_subdomains": "abcd",
            "min_zoom": 3,
            "max_zoom": 18,
            "local_tile_dir": "",
            "_comment": "Leave local_tile_dir empty for CDN tiles (dev). For production: python3 scripts/fetch_tiles.py --out /var/lib/nexnoc/tiles then set local_tile_dir to that path.",
        },
        "cities": cities,
        "sites": sites,
        "devices": device_list,
        "ports": ports,
        "flows": flows,
        "_comment": (
            "Built from Example Docs/Newsnation Global Path Naming.xlsx (HAI + Appear tabs). "
            "Every path is included, including unused / PAC12 / missing-IP rows. "
            "Devices without a management IP have empty mgmt_host and poll_enabled=false — "
            "edit host and credentials in the portal. "
            "Credential *values* are NOT in this file — set them in the Edit tab "
            "or /etc/nexnoc/nexnoc.env. Do not copy passwords out of the spreadsheet."
        ),
    }


def main() -> None:
    config = build_inventory()
    out = ROOT / "config.json"
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    ensure_env_example(config["devices"])
    print(f"wrote {out}")
    print(
        f"cities={len(config['cities'])} sites={len(config['sites'])} "
        f"devices={len(config['devices'])} ports={len(config['ports'])} "
        f"flows={len(config['flows'])}"
    )
    pending = 0
    for d in config["devices"]:
        host = d["mgmt_host"] or "(no IP - edit in portal)"
        if not d["mgmt_host"]:
            pending += 1
        print(f"  {d['name']:28} {d['vendor']:10} {d['site']:24} {host}")
    print(f"pending (no management IP): {pending}")


if __name__ == "__main__":
    main()
