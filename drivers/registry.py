"""
registry.py - The list of every driver NexNOC knows about.

Deliberately explicit, not auto-discovered via decorators or plugin
scanning - matches this codebase's "no ORM, no magic" convention (see
db.py's docstring). Adding a driver is a two-line change: import it, add it
to DRIVER_REGISTRY. Order matters only for resolving ties between multiple
non-default drivers that both match a device - see base.py:resolve_driver()
docstring.
"""

from __future__ import annotations

from drivers.appear import AppearXPlatformDriver
from drivers.haivision import HaivisionMakitoXDriver
from drivers.net_insight import NetInsightNimbraDriver

DRIVER_REGISTRY: list[type] = [
    AppearXPlatformDriver,
    HaivisionMakitoXDriver,
    NetInsightNimbraDriver,
]
