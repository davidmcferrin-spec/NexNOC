#!/usr/bin/env python3
"""Build a local XYZ tile pack for the NexNOC map (production / air-gap).

Dev uses Carto Dark Matter from the public CDN. Production should not.
Run this on a machine that can reach the tile URL, then copy the output
directory to the NOC host and set map.local_tile_dir in config.json.

    python3 scripts/fetch_tiles.py --out tiles \\
        --url "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png" \\
        --bbox -125,24,-66,50 --min-zoom 3 --max-zoom 8

Then on the server:

    map.local_tile_dir = /var/lib/nexnoc/tiles

The dashboard serves those files at /tiles/{z}/{x}/{y}.png and stops
calling the CDN.

Respect the tile provider's terms. Public Carto/OSM tiles are meant for
light interactive use, not a public mirror. This script is for a private
NOC pack. Prefer a pack you rendered yourself if policy requires it.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
DEFAULT_BBOX = (-125.0, 24.0, -66.0, 50.0)  # CONUS
SUBDOMAINS = "abcd"


def lng_to_x(lng: float, z: int) -> int:
    n = 1 << z
    x = int((lng + 180.0) / 360.0 * n)
    return max(0, min(n - 1, x))


def lat_to_y(lat: float, z: int) -> int:
    lat = max(-85.05112878, min(85.05112878, lat))
    lat_rad = math.radians(lat)
    n = 1 << z
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, y))


def tile_range(west: float, south: float, east: float, north: float, z: int) -> tuple[int, int, int, int]:
    x0 = lng_to_x(west, z)
    x1 = lng_to_x(east, z)
    y0 = lat_to_y(north, z)
    y1 = lat_to_y(south, z)
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    return x0, x1, y0, y1


def count_tiles(bbox: tuple[float, float, float, float], zmin: int, zmax: int) -> int:
    total = 0
    for z in range(zmin, zmax + 1):
        x0, x1, y0, y1 = tile_range(*bbox, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def fetch_one(url: str, dest: Path, timeout: float) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return False
    req = urllib.request.Request(url, headers={"User-Agent": "NexNOC-fetch-tiles/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Download an XYZ tile pack for NexNOC")
    parser.add_argument("--out", default="tiles", help="Output directory (z/x/y.png)")
    parser.add_argument("--url", default=DEFAULT_URL, help="Tile URL template with {z}{x}{y} and optional {s}")
    parser.add_argument("--bbox", default="-125,24,-66,50",
                        help="west,south,east,north in WGS84 (default CONUS)")
    parser.add_argument("--min-zoom", type=int, default=3)
    parser.add_argument("--max-zoom", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=0.08, help="Seconds between downloads")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    parts = [float(p) for p in args.bbox.split(",")]
    if len(parts) != 4:
        print("bbox must be west,south,east,north", file=sys.stderr)
        return 2
    bbox = (parts[0], parts[1], parts[2], parts[3])
    if args.min_zoom < 0 or args.max_zoom > 18 or args.min_zoom > args.max_zoom:
        print("invalid zoom range", file=sys.stderr)
        return 2

    total = count_tiles(bbox, args.min_zoom, args.max_zoom)
    print(f"Pack: {args.min_zoom}-{args.max_zoom}  bbox={bbox}  tiles~{total}  -> {args.out}")
    if args.dry_run:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fetched = 0
    skipped = 0
    failed = 0
    n = 0
    for z in range(args.min_zoom, args.max_zoom + 1):
        x0, x1, y0, y1 = tile_range(*bbox, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                url = args.url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
                if "{s}" in url:
                    url = url.replace("{s}", SUBDOMAINS[n % len(SUBDOMAINS)])
                dest = out / str(z) / str(x) / f"{y}.png"
                try:
                    if fetch_one(url, dest, args.timeout):
                        fetched += 1
                        time.sleep(args.sleep)
                    else:
                        skipped += 1
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    failed += 1
                    print(f"fail {z}/{x}/{y}: {exc}", file=sys.stderr)
                n += 1
                if n % 100 == 0:
                    print(f"  {n}/{total}  fetched={fetched} skipped={skipped} failed={failed}")
    print(f"Done. fetched={fetched} skipped={skipped} failed={failed}  dir={out.resolve()}")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
