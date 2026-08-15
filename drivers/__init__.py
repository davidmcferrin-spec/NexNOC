"""NexNOC hardware drivers.

A "driver" is what actually knows how to talk to a device - matched on
vendor + (optionally) model + firmware version range, not just vendor
alone. See base.py for the Driver contract and resolve_driver() matching
logic, and registry.py for the list of every driver NexNOC knows about.

Current drivers (each vendor's DEFAULT - no model/firmware constraints yet):
  - appear.py       - AppearXPlatformDriver (direct_api, JSON/HTTP; SNMP also available)
  - haivision.py     - HaivisionMakitoXDriver (direct_api, JSON/HTTP - self-documenting
                        via on-device /apidoc, see module docstring)
  - net_insight.py   - NetInsightNimbraDriver (direct_snmp per-node; via_nms through
                        Nimbra Vision NOT implemented yet - see module docstring)

poller.py resolves which driver to use per device via
drivers.base.resolve_driver(), using Device.vendor/model/firmware_version
and an optional explicit Device.driver_override.

Adding a driver for a new model or firmware range: write the class in the
relevant vendor module (or a new file), set supported_models/firmware_min/
firmware_max narrower than the vendor's default, set notes, add it to
DRIVER_REGISTRY in registry.py ahead of the default. No changes needed to
poller.py, db.py, or any other driver. Full how-to: docs/DRIVERS.md.
"""
