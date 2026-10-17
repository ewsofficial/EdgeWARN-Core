"""NEXRAD-owned GUI orchestration settings."""

from __future__ import annotations

from common.config.loader import load_config


def _section(name: str):
    """One top-level section. ``load_config`` is memoized, so this is cheap."""
    return load_config("nexrad")["gui"]


def nexrad_source_max_age_minutes() -> int:
    """How stale a NEXRAD artifact may be and still be worth rendering."""
    return _section("nexrad_gui")["retention_minutes"]


def nexrad_poll_interval_seconds() -> float:
    """Sleep between NEXRAD render poll cycles."""
    return _section("nexrad_gui")["poll_interval_seconds"]


def nexrad_poll_interval_min_seconds() -> float:
    """Floor applied to a caller-supplied poll interval, so no caller can spin."""
    return _section("nexrad_gui")["poll_interval_min_seconds"]


def nexrad_render_max_workers() -> int:
    """Ceiling on the NEXRAD render thread pool; the effective size is
    ``min(this, pending artifact count)``."""
    return _section("nexrad_gui")["max_workers"]


def gui_cleanup_max_age_minutes() -> int:
    """Age above which rendered NEXRAD GUI outputs are removed."""
    return _section("gui")["cleanup_max_age_minutes"]


def nexrad_variable_colormaps() -> dict:
    """Map radar moments to client display keys."""
    return dict(load_config("nexrad")["variable_colormaps"])
