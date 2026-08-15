"""Builtin map pin catalog (broadcast / news / sports / generic).

SVG artwork lives in web/pins.js so the browser can tint with pin_color.
This module is the server-side id/label list for /api/state and validation.
"""
from __future__ import annotations

BUILTIN_PINS = (
    {"id": "tower", "label": "Tower", "group": "broadcast"},
    {"id": "dish", "label": "Satellite dish", "group": "broadcast"},
    {"id": "camera", "label": "Camera", "group": "broadcast"},
    {"id": "rack", "label": "Rack", "group": "broadcast"},
    {"id": "bnc", "label": "BNC", "group": "broadcast"},
    {"id": "microwave", "label": "Microwave", "group": "broadcast"},
    {"id": "capitol", "label": "Capitol", "group": "news"},
    {"id": "mic", "label": "Microphone", "group": "news"},
    {"id": "studio", "label": "Studio", "group": "news"},
    {"id": "newsroom", "label": "Newsroom", "group": "news"},
    {"id": "stadium", "label": "Stadium", "group": "sports"},
    {"id": "field", "label": "Field", "group": "sports"},
    {"id": "arena", "label": "Arena", "group": "sports"},
    {"id": "helmet", "label": "Helmet", "group": "sports"},
    {"id": "building", "label": "Building", "group": "generic"},
    {"id": "office", "label": "Office", "group": "generic"},
    {"id": "home", "label": "Home", "group": "generic"},
    {"id": "warehouse", "label": "Warehouse", "group": "generic"},
    {"id": "star", "label": "Star", "group": "generic"},
    {"id": "pin", "label": "Pin", "group": "generic"},
)

BUILTIN_PIN_IDS = {p["id"] for p in BUILTIN_PINS}
DEFAULT_PIN_ICON = "building"
DEFAULT_PIN_COLOR = "#6aa4ff"


def valid_pin_icon(value: str) -> bool:
    return value in BUILTIN_PIN_IDS or value == "upload"


def valid_pin_color(value: str) -> bool:
    if not value or not value.startswith("#"):
        return False
    hexpart = value[1:]
    if len(hexpart) not in (3, 6):
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in hexpart)
