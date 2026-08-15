"""One-shot: build config.json from Newsnation Global Path Naming.xlsx.

Qualified Name / Global Path ID are paths (flows), not devices.
Haivision Source/Dest IP columns are stream endpoints (often ip:udp) —
never management. Appear Control IP is management; Source IP and Dest2
Public IP are media/stream.

Haivision boxes are keyed by serial (siblings inherit). Appear frames are
keyed by Control IP (one X20 per site in this sheet). Devices without a
management IP get empty mgmt_host and poll_enabled=false.

Username/password values from the sheet are written onto each device in
config.json (api_username / api_password) and also into config/nexnoc.env.
config.json is gitignored.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from envfile import default_env_path, upsert_values  # noqa: E402

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
    "DC": "400 N. Capital St",
    "NN DC": "400 N. Capital St",
    "INDY": "Indianapolis",
    "ATL CW": "Atlanta - CW",
    "CW ATL": "Atlanta - CW",
    "NY HMPTNS": "New York - Hamptons",
    "CONN": "Connecticut",
    "WDCW": "WDCW TV Station",
    "CW BURBANK": "Burbank - CW",
    "PAC12": "San Ramon - PAC12",
    "AM": "AM",
    "UNK": "Unknown",
}
SITE_CODE: dict[str, str] = {
    "New York": "NY",
    "Chicago": "CHI",
    "400 N. Capital St": "DC",
    "WDCW TV Station": "WDCW",
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
    "400 N. Capital St": (38.8969, -77.0091),
    "WDCW TV Station": (38.9178, -77.0692),
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
    ip, _port = parse_endpoint(s)
    return ip


def parse_endpoint(s: str) -> tuple[str | None, str | None]:
    """Stream endpoint: IP and optional UDP/SRT port. Not a management address."""
    s = (s or "").strip()
    if not s or s in ("N/A", "?", "??"):
        return None, None
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)(?::(\d+))?", s)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def endpoint_label(ip: str | None, port: str | None) -> str:
    if not ip:
        return ""
    return f"{ip}:{port}" if port else ip


def cred_of(s: str) -> str:
    s = (s or "").strip()
    if not s or s in ("N/A", "?", "??", "n/a"):
        return ""
    return s


def octets(ip: str) -> tuple[str, str]:
    parts = ip.split(".")
    return parts[2], parts[3]


def hai_device_name(site: str, serial: str = "", stream_ip: str | None = None,
                    hint: str = "") -> str:
    """Physical box name. Serial wins (last 7 — last 5 collides in this fleet).
    Stream last-octet is a path/link id, not the box."""
    code = site_code(site)
    if serial:
        return f"{code}-HAI-{serial[-7:]}"
    if stream_ip:
        _third, last = octets(stream_ip)
        return f"{code}-HAI-{last}"
    return f"{code}-HAI-{slug(hint)}"


def appear_device_name(site: str) -> str:
    return f"{site_code(site)}-X20"


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


def write_sheet_secrets(secrets: dict[str, str], env_path: Path | None = None) -> int:
    """Write username/password values to nexnoc.env. Never logs values."""
    clean = {k: v for k, v in secrets.items() if k and v}
    if not clean:
        return 0
    upsert_values(env_path or default_env_path(), clean)
    return len(clean)


def open_xlsx(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except PermissionError:
        tmp = Path(tempfile.gettempdir()) / "path_naming_copy.xlsx"
        shutil.copy2(path, tmp)
        return zipfile.ZipFile(tmp)


def build_inventory(xlsx: Path | None = None, collect_secrets: dict | None = None) -> dict:
    path = xlsx or XLSX
    zf = open_xlsx(path)
    ss = load_shared_strings(zf)
    hai = load_sheet(zf, ss, "xl/worksheets/sheet2.xml")
    appear = load_sheet(zf, ss, "xl/worksheets/sheet3.xml")

    devices: OrderedDict[str, dict] = OrderedDict()
    ports: list[dict] = []
    port_keys: set[tuple[str, str]] = set()
    flows: list[dict] = []
    stream_index: dict[str, str] = {}
    names_taken: set[str] = set()

    def add_dev(place: str, vendor: str, *, serial: str = "",
                control_ip: str | None = None, stream_ip: str | None = None,
                hint: str = "", role: str = "") -> str | None:
        """One physical box. Haivision stream IPs are never management.
        Appear management is Control IP only."""
        pl = norm_place(place) or "UNK"
        site = SITE.get(pl, title_place(pl))
        serial = cred_of(serial)
        role = role or ("frame" if vendor == "appear" else "encoder")

        key = None
        if serial:
            key = f"serial:{serial}"
        elif stream_ip and stream_ip in stream_index:
            key = stream_index[stream_ip]
        elif vendor == "appear" and control_ip:
            key = f"ip:{control_ip}"
        elif vendor == "haivision" and stream_ip:
            key = f"stream:{stream_ip}"
        else:
            key = f"pending:{vendor}:{site}:{slug(hint)}"

        if key not in devices:
            if vendor == "appear":
                name = appear_device_name(site)
                host = control_ip or ""
            else:
                name = hai_device_name(site, serial=serial, stream_ip=stream_ip, hint=hint)
                host = ""
            if name in names_taken:
                extra = slug(serial or stream_ip or hint or "X", 8)
                name = f"{name}-{extra}"
            names_taken.add(name)
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
                "poll_enabled": bool(host),
            }
            if serial:
                devices[key]["_serial"] = serial
        else:
            if serial:
                devices[key]["_serial"] = serial
            if vendor == "appear" and control_ip and not devices[key]["mgmt_host"]:
                devices[key]["mgmt_host"] = control_ip
                devices[key]["poll_enabled"] = True
        if stream_ip:
            stream_index[stream_ip] = key
        return devices[key]["name"]

    def apply_creds(dev_name: str | None, user: str, password: str) -> None:
        if not dev_name:
            return
        user, password = cred_of(user), cred_of(password)
        if not user and not password:
            return
        for d in devices.values():
            if d["name"] != dev_name:
                continue
            if user and not d.get("api_username"):
                d["api_username"] = user
            if password and not d.get("api_password"):
                d["api_password"] = password
            return

    def add_port(dev: str | None, name: str, kind: str) -> None:
        if not dev or not name:
            return
        key = (dev, name)
        if key in port_keys:
            return
        port_keys.add(key)
        ports.append({"device": dev, "name": name, "kind": kind})

    # Sibling channels on one Global Path ID share the encoder/decoder.
    # HAI 1012 has no Source IP because it is In 2 on the same box as HAI 1011.
    # Q/S/W are stream endpoints (often ip:port), not management addresses.
    hai_paths = []
    path_last: dict[str, dict] = {}
    for r in sorted(hai):
        if r == 1:
            continue
        row = hai[r]
        qname = cell(row, "A")
        if not qname.upper().startswith("HAI"):
            continue
        path_id = cell(row, "B") or qname
        src_place = cell(row, "C") or "UNK"
        prev = path_last.get(path_id) or {}
        src_ip, src_udp = parse_endpoint(cell(row, "Q"))
        if not src_ip and prev.get("src_place") == src_place:
            src_ip = prev.get("src_ip")
        dst_place = cell(row, "J") or prev.get("dst_place") or ""
        dst2_place = cell(row, "M") or prev.get("dst2_place") or ""
        dst_ip, dst_udp = parse_endpoint(cell(row, "S"))
        if not dst_ip and dst_place == (prev.get("dst_place") or ""):
            dst_ip = prev.get("dst_ip")
            dst_udp = dst_udp or prev.get("dst_udp")
        dst2_ip, dst2_udp = parse_endpoint(cell(row, "W"))
        if not dst2_ip and dst2_place == (prev.get("dst2_place") or ""):
            dst2_ip = prev.get("dst2_ip")
            dst2_udp = dst2_udp or prev.get("dst2_udp")
        src_serial = cred_of(cell(row, "E")) or (
            prev.get("src_serial") if prev.get("src_place") == src_place else ""
        )
        dst_serial = cred_of(cell(row, "P"))
        if not dst_serial:
            same_box = (
                (dst_ip and dst_ip == prev.get("dst_ip"))
                or (not dst_ip and dst_place == (prev.get("dst_place") or ""))
            )
            if same_box:
                dst_serial = prev.get("dst_serial") or ""
        src_user = cred_of(cell(row, "H")) or (
            prev.get("src_user") if prev.get("src_place") == src_place else ""
        )
        src_pass = cred_of(cell(row, "I")) or (
            prev.get("src_pass") if prev.get("src_place") == src_place else ""
        )
        dst_user = cred_of(cell(row, "U")) or (
            prev.get("dst_user") if dst_place == (prev.get("dst_place") or "") else ""
        )
        dst_pass = cred_of(cell(row, "V")) or (
            prev.get("dst_pass") if dst_place == (prev.get("dst_place") or "") else ""
        )
        dst2_user = cred_of(cell(row, "Y")) or (
            prev.get("dst2_user") if dst2_place == (prev.get("dst2_place") or "") else ""
        )
        dst2_pass = cred_of(cell(row, "Z")) or (
            prev.get("dst2_pass") if dst2_place == (prev.get("dst2_place") or "") else ""
        )
        path_last[path_id] = {
            "src_place": src_place, "src_ip": src_ip, "src_serial": src_serial,
            "src_user": src_user, "src_pass": src_pass,
            "dst_place": dst_place, "dst_ip": dst_ip, "dst_udp": dst_udp,
            "dst_serial": dst_serial, "dst_user": dst_user, "dst_pass": dst_pass,
            "dst2_place": dst2_place, "dst2_ip": dst2_ip, "dst2_udp": dst2_udp,
            "dst2_user": dst2_user, "dst2_pass": dst2_pass,
        }
        dests = []
        if dst_place:
            dests.append({
                "place": dst_place, "sdi": cell(row, "K"),
                "ip": dst_ip, "udp": dst_udp, "serial": dst_serial,
                "user": dst_user, "password": dst_pass,
            })
        if dst2_place:
            dests.append({
                "place": dst2_place, "sdi": cell(row, "N"),
                "ip": dst2_ip, "udp": dst2_udp, "serial": "",
                "user": dst2_user, "password": dst2_pass,
            })
        hai_paths.append({
            "qname": qname,
            "path_id": path_id,
            "content": cell(row, "F"),
            "src_place": src_place,
            "src_ip": src_ip,
            "src_udp": src_udp,
            "src_user": src_user,
            "src_pass": src_pass,
            "src_port": cell(row, "D") or "In 1",
            "serial": src_serial,
            "dests": dests,
        })

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

    for rec in hai_paths:
        src_name = add_dev(
            rec["src_place"], "haivision",
            serial=rec["serial"], stream_ip=rec["src_ip"],
            hint=rec["path_id"] or rec["qname"], role="encoder",
        )
        apply_creds(src_name, rec["src_user"], rec["src_pass"])
        add_port(src_name, rec["src_port"], "sdi_in")
        if rec["src_ip"]:
            add_port(src_name, rec["src_ip"], "net")
        signal = rec["content"] or rec["qname"]
        for dest in rec["dests"]:
            dpl = norm_place(dest["place"])
            if not dpl or not src_name:
                continue
            dst_name = add_dev(
                dest["place"], "haivision",
                serial=dest["serial"], stream_ip=dest["ip"],
                hint=f"{rec['path_id'] or rec['qname']}-DST", role="decoder",
            )
            apply_creds(dst_name, dest["user"], dest["password"])
            if dst_name and dest["sdi"]:
                add_port(dst_name, dest["sdi"], "sdi_out")
            if dest["ip"]:
                add_port(dst_name, dest["ip"], "net")
            stream = endpoint_label(dest["ip"], dest["udp"])
            flow = {
                "signal": signal,
                "label": rec["qname"],
                "source_device": src_name,
                "source_port": rec["src_port"],
                "dest_city": CITY[dpl],
                "dest_site": SITE[dpl],
                "direction": "contribution",
            }
            if dst_name:
                flow["dest_device"] = dst_name
                if dest["sdi"]:
                    flow["dest_port"] = dest["sdi"]
            if stream:
                flow["dest_label"] = stream
            elif dest["sdi"] and not dst_name:
                flow["dest_label"] = dest["sdi"]
            flows.append(flow)

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
        ctrl, media = appear_ctrl.get(
            src_pl, (host_of(cell(row, "I")), host_of(cell(row, "H"))),
        )
        src_name = add_dev(
            src_place, "appear", control_ip=ctrl, stream_ip=media,
            hint=src_pl, role="frame",
        )
        apply_creds(src_name, cell(row, "J"), cell(row, "K"))
        src_port = cell(row, "D")
        add_port(src_name, src_port, "sdi_in")
        if media:
            add_port(src_name, media, "net")
        dests: list[tuple[str, str, str | None]] = []
        if cell(row, "F"):
            dests.append((cell(row, "F"), cell(row, "G"), None))
        dest2_place = cell(row, "L")
        dest2_stream = host_of(cell(row, "M"))
        if dest2_place:
            dests.append((dest2_place, "", dest2_stream))
        elif dest2_stream:
            dests.append(("CHI", "", dest2_stream))
        signal = cell(row, "E") or qname
        for dplace, dport, dstream in dests:
            dpl = norm_place(dplace)
            if not dpl or not src_name:
                continue
            dest_ctrl, dest_media = appear_ctrl.get(dpl, (None, None))
            stream_ip = dstream or dest_media
            dst_name = add_dev(
                dplace, "appear", control_ip=dest_ctrl, stream_ip=stream_ip,
                hint=dpl, role="frame",
            )
            if dpl == "CHI":
                apply_creds(dst_name, cell(row, "N"), cell(row, "O"))
            if dst_name and dport:
                add_port(dst_name, dport, "sdi_out")
            if stream_ip:
                add_port(dst_name, stream_ip, "net")
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
            if stream_ip:
                flow["dest_label"] = stream_ip
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
        if collect_secrets is not None:
            if d.get("api_username"):
                collect_secrets[d["api_username_env"]] = d["api_username"]
            if d.get("api_password"):
                collect_secrets[d["api_password_env"]] = d["api_password"]
        row = {k: v for k, v in d.items() if not k.startswith("_")}
        device_list.append(row)

    return {
        "poll_interval_seconds": 30,
        "map": {
            "tile_url": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
            "tile_subdomains": "",
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
            "Qualified Name is the path (flows[].label), not a device. "
            "Haivision Source/Dest IPs are stream endpoints — not management. "
            "Appear Control IP is management; Source IP / Dest2 Public IP are media. "
            "Haivision boxes are named from serial (last 7). "
            "Devices without a management IP have empty mgmt_host and poll_enabled=false. "
            "api_username / api_password on each device are the sheet values. "
        ),
    }


def main() -> None:
    secrets: dict[str, str] = {}
    config = build_inventory(collect_secrets=secrets)
    out = ROOT / "config.json"
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    ensure_env_example(config["devices"])
    secret_count = write_sheet_secrets(secrets)
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
    print(f"devices with credentials in config.json: {sum(1 for d in config['devices'] if d.get('api_username') or d.get('api_password'))}")
    print(f"credential values also written to env file: {secret_count}")


if __name__ == "__main__":
    main()
