"""Accessors for ``nws.yaml``.

Every function reads the catalog per call. None of them memoize and none of them
are evaluated at import: the entrypoints import this package well before
the config root is published, so a value bound at module scope would
freeze the repo default and put ``--config-dir`` out of reach.
"""

from common.config.loader import load_config

_CONFIG_NAME = "nws"


def _section(name: str):
    return load_config(_CONFIG_NAME)[name]


def registry_ttl_hours() -> float:
    """How long an alert survives after it stops appearing upstream."""
    return _section("nws")["registry_ttl_hours"]


def edgewarn_alert_retention_minutes() -> int:
    """Age after which EdgeWARN alert files and timestamp snapshots are deleted."""
    return _section("nws")["edgewarn_alert_retention_minutes"]


def dropped_events() -> frozenset[str]:
    """Event names discarded on ingest.

    One-way: a dropped alert is never written, so nothing downstream can recover
    it. Returned as a frozenset because the only use is a membership test.
    """
    return frozenset(_section("nws")["dropped_events"])


def active_alerts_url() -> str:
    """The NWS active-alerts feed, shared by the sync and async downloads."""
    return _section("nws")["active_alerts_url"]


def download_chunk_size_bytes() -> int:
    """Read size for the async alert download's stream to a temp file.

    The sync download has no counterpart: it reads the whole body at once.
    """
    return _section("nws")["download_chunk_size_bytes"]


def tornado_upgrade_event() -> str:
    """The one event whose name is re-derived from the alert description."""
    return _section("nws")["tornado_upgrade"]["event"]


def tornado_upgrade_rules() -> tuple[tuple[str, str], ...]:
    """``(phrase, name)`` pairs in priority order -- the first match wins."""
    return tuple(
        (rule["description_contains"], rule["name"])
        for rule in _section("nws")["tornado_upgrade"]["rules"]
    )


def geometry_precision() -> int:
    """Decimal places kept when an alert polygon is rounded.

    Distinct from ``zone_sync.geometry_precision``, which is a floor that
    escalates rather than a fixed precision -- see the catalog comments.
    """
    return _section("geomapper")["geometry_precision"]


def junk_keys() -> tuple[str, ...]:
    """Alert properties stripped before the alert reaches disk."""
    return _section("geomapper")["junk_keys"]


def simplify_tolerance() -> float:
    """Douglas-Peucker tolerance applied to a zone union, in degrees."""
    return _section("geomapper")["simplify_tolerance"]


def zone_sync_settings():
    """The whole ``zone_sync`` block, for a caller reading several keys at once."""
    return _section("zone_sync")


def zone_geometry_precision() -> tuple[int, int]:
    """``(floor, ceiling)`` for the ring-rounding escalation in zone assets.

    A floor rather than a fixed precision: a ring that degenerates at the floor
    is retried at each higher precision below the ceiling, which is exclusive.
    Returned as a pair because wiring one without the other would leave half the
    escalation window owned by a source literal.
    """
    zone_sync = _section("zone_sync")
    return zone_sync["geometry_precision"], zone_sync["geometry_precision_max"]
