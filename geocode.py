"""Nominatim geocoding for city centers and site street addresses.

Stdlib only. Results are stored on the city/site row so air-gapped
production does not need Nominatim at runtime — only when an operator
creates or edits a location. Manual lat/lng always wins once set.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger("nexnoc.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "NexNOC/0.2 (broadcast NOC inventory geocoding)"
TIMEOUT = 8.0

FetchFn = Callable[[str], bytes]


class GeocodeError(RuntimeError):
    """Nominatim failed or returned nothing usable."""


def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _geocode_enabled() -> bool:
    return os.environ.get("NEXNOC_GEOCODE", "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


def geocode(query: str, *, kind: str = "search",
            fetch: Optional[FetchFn] = None) -> Optional[dict]:
    """Return {lat, lng, display_name} or None if nothing matched.

    kind=city prefers a place/city hit; kind=address is a street search.
    Set NEXNOC_GEOCODE=0 to skip outbound lookups (tests / air-gap edits).
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("geocode query is required")
    if fetch is None and not _geocode_enabled():
        return None
    params = {
        "q": query,
        "format": "json",
        "limit": "1",
        "addressdetails": "0",
    }
    if kind == "city":
        params["class"] = "place"
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)
    opener = fetch or _default_fetch
    try:
        raw = opener(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeocodeError(f"geocode request failed: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeocodeError(f"geocode response was not JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        return None
    hit = data[0]
    try:
        lat = float(hit["lat"])
        lng = float(hit["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodeError(f"geocode hit missing lat/lon: {exc}") from exc
    return {
        "lat": lat,
        "lng": lng,
        "display_name": hit.get("display_name") or query,
        "source": "geocode",
    }


def geocode_or_none(query: str, *, kind: str = "search",
                    fetch: Optional[FetchFn] = None) -> Optional[dict]:
    """Best-effort wrapper: log and return None instead of raising."""
    try:
        return geocode(query, kind=kind, fetch=fetch)
    except (GeocodeError, ValueError) as exc:
        logger.warning("geocode %r failed: %s", query, exc)
        return None
