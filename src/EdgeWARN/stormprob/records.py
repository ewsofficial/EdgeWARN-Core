"""StormProb observation records (Phase 1).

An observation record is the committed per-cycle input for one cell: raw
source values with units, source analysis times, quality/missing flags,
detection geometry, and derived model features. Raw values are retained so
feature versions can be recomputed without rereading legacy JSON.

Readiness policy (plan Phase 1, bullet 3):

- ``inference_ready`` requires valid geometry (full-precision centroid +
  non-degenerate polygon) and valid identity (cell id + timestamp) plus a
  usable initial wind pair. New single-row cells are eligible: the parity
  fixtures confirm the one-valid-row path.
- Legitimately missing weather fields (source present, value absent) keep a
  cell eligible via the ``-999`` sentinel and are flagged
  ``missing-fields:<n>``.
- An absent source family (e.g. no RAP file this cycle) is flagged
  ``missing-source:<family>``; when it removes every usable wind pair the
  cell is not ready (``no-usable-wind-pair``), never zero-filled.
- A corrupt sample (non-finite centroid, unparseable timestamp, non-dict
  properties) is flagged ``corrupt-sample:<reason>`` and is not ready.

Per-feature units pin the assumed EdgeWARN conventions. StormProb records no
per-feature units; temperature units are confirmed Celsius (training sample
values ~20-26 after the RAP kelvin_to_celsius transform). All other units
are marked ``assumed`` and must be re-validated against live feeds in Phase 5.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from . import features as _features
from . import geometry as _geometry

SCHEMA_VERSION = _features.FEATURE_SCHEMA_VERSION

# Quality flags per feature channel.
FLAG_OK = "ok"
FLAG_MISSING_FIELD = "missing-field"
FLAG_MISSING_SOURCE = "missing-source"
FLAG_CORRUPT = "corrupt-value"

# Source families that feed named feature groups. A family listed as absent
# in ``source_times`` upgrades that group's missing flags to missing-source.
_SCALAR_SOURCE_FAMILIES = ("probsevere", "mrms", "rap")
_WIND_SOURCE_FAMILY = "rap"


def _family_for_feature(name: str) -> str:
    if name.startswith("wind_field.") or name in ("u10m", "v10m", "temp_2m",
                                                  "dewpoint_2m", "freezing_level_m"):
        return "rap"
    if name in ("dewpoint_depression", "freezing_level_height"):
        return "rap-derived"
    if name.startswith("morphology."):
        return "detection"
    if name in ("Ref0", "Ref5", "Ref15", "maxRALA", "maxPrecipRate", "maxVIL",
                "p50VIL", "p90VIL", "p95VIL", "maxVILDensity", "p50VILDensity",
                "p90VILDensity", "p95VILDensity", "maxVII", "maxEchoTop18",
                "p90EchoTop18", "p95EchoTop18", "maxEchoTop30", "p90EchoTop30",
                "p90EchoTop50", "maxAzShearLow", "p95AzShearLow",
                "maxAzShearMid", "p95AzShearMid"):
        return "mrms"
    return "probsevere"


def feature_units() -> dict[str, str]:
    """Pinned per-feature units (assumed EdgeWARN conventions unless noted)."""
    units: dict[str, str] = {
        "MUCAPE": "J/kg (assumed)", "MLCAPE": "J/kg (assumed)",
        "MLCIN": "J/kg (assumed)", "CAPE_M10M30": "J/kg (assumed)",
        "DCAPE": "J/kg (assumed)", "PWAT": "mm (assumed)",
        "temp_2m": "degC (confirmed via training values)",
        "dewpoint_2m": "degC (confirmed via training values)",
        "dewpoint_depression": "degC (confirmed via transform)",
        "freezing_level_height": "km (confirmed via transform)",
        "freezing_level_m": "m (assumed)",
        "Wetbulb_0C_Hgt": "m (assumed)", "EBShear": "m/s (assumed)",
        "MeanWind_1-3kmAGL": "m/s (assumed)",
        "SRH01km": "m2/s2 (assumed)", "SRH02km": "m2/s2 (assumed)",
        "SRW46km": "m/s (assumed)",
        "u10m": "m/s (assumed)", "v10m": "m/s (assumed)",
        "Ref0": "dBZ (assumed)", "Ref5": "dBZ (assumed)",
        "Ref10": "dBZ (assumed)", "Ref15": "dBZ (assumed)",
        "Ref20": "dBZ (assumed)",
        "maxRALA": "dBZ (assumed)", "maxPrecipRate": "mm/hr (assumed)",
        "MESH": "mm (assumed)", "VIL": "kg/m2 (assumed)",
        "maxVIL": "kg/m2 (assumed)", "p50VIL": "kg/m2 (assumed)",
        "p90VIL": "kg/m2 (assumed)", "p95VIL": "kg/m2 (assumed)",
        "maxVILDensity": "g/m3 (assumed)", "p50VILDensity": "g/m3 (assumed)",
        "p90VILDensity": "g/m3 (assumed)", "p95VILDensity": "g/m3 (assumed)",
        "maxVII": "kg/m2 (assumed)", "EchoTop50": "kft (assumed)",
        "maxEchoTop18": "kft (assumed)", "p90EchoTop18": "kft (assumed)",
        "p95EchoTop18": "kft (assumed)", "maxEchoTop30": "kft (assumed)",
        "p90EchoTop30": "kft (assumed)", "p90EchoTop50": "kft (assumed)",
        "MaxLLAz": "1/s scaled (assumed)",
        "maxAzShearLow": "1/s scaled (assumed)",
        "p95AzShearLow": "1/s scaled (assumed)",
        "p98LLAz": "1/s scaled (assumed)",
        "maxAzShearMid": "1/s scaled (assumed)",
        "p95AzShearMid": "1/s scaled (assumed)",
        "p98MLAz": "1/s scaled (assumed)",
        "morphology.aspect_ratio": "ratio (assumed)",
        "morphology.branching_factor": "count (assumed)",
        "morphology.defect_bearing": "degrees (assumed)",
        "morphology.defect_max_depth": "pixels (assumed)",
        "morphology.linearity": "ratio (assumed)",
        "morphology.solidity": "ratio (assumed)",
        "initial_u": "m/s (assumed)", "initial_v": "m/s (assumed)",
        "storm_age_seconds": "seconds (exact)",
        "valid_history_length": "count (exact)",
    }
    for level in _features.PRESSURE_LEVELS_HPA:
        units[f"wind_field.u{level}"] = "m/s (assumed)"
        units[f"wind_field.v{level}"] = "m/s (assumed)"
    return units


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return str(value)
    except Exception:
        return None


def build_observation_record(
    cell: dict,
    analysis_time: str | None = None,
    source_times: dict[str, Any] | None = None,
    cycle_manifest=None,
) -> dict:
    """Build the committed Phase 1 observation record for one integrated cell.

    ``cell`` is the post-enrichment detection entry (rounded ``centroid`` /
    ``bbox`` plus integrated ``properties``). Geometry attached at detection
    time under ``cell["stormprob"]["geometry"]`` is reused; otherwise the
    radial profile is derived from the available (rounded) values and flagged
    ``rounded-fallback``. Never raises: failures become ``not-ready`` reasons.
    """
    source_times = dict(source_times or {})
    reasons: list[str] = []
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "feature_order_checksum": _features.feature_order_checksum(),
    }

    cell_id = cell.get("id")
    record["cell_id"] = cell_id
    timestamp = cell.get("timestamp") or cell.get("properties", {}).get("timestamp")
    record["analysis_time"] = analysis_time or timestamp
    if cell_id is None:
        reasons.append("corrupt-sample:missing-cell-id")
    try:
        parsed_time = _features.parse_analysis_timestamp(record["analysis_time"])
        record["analysis_time_parsed"] = True
    except Exception:
        parsed_time = None
        record["analysis_time_parsed"] = False
        reasons.append("corrupt-sample:bad-timestamp")

    properties = cell.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        reasons.append("corrupt-sample:properties-not-mapping")

    # --- geometry: prefer detection-time full precision -------------------
    stored_geometry = (cell.get("stormprob") or {}).get("geometry")
    if isinstance(stored_geometry, dict) and stored_geometry.get("radial"):
        geometry = stored_geometry
        geometry_source = "detection-full-precision"
    else:
        centroid = cell.get("centroid")
        polygon = cell.get("bbox")
        profile = _geometry.radial_profile_for_cell(centroid, polygon)
        geometry = {
            "centroid_full": None,
            "polygon_full": None,
            "radial": profile,
            "fallback": "rounded-fallback",
        }
        geometry_source = "rounded-fallback"
        reasons.append("geometry:rounded-fallback")
    record["geometry_source"] = geometry_source
    record["centroid"] = (geometry.get("centroid_full")
                          or list(cell.get("centroid") or []))
    record["polygon"] = geometry.get("polygon_full") or cell.get("bbox")
    radial = geometry.get("radial") or {}
    record["radial_profile"] = list(radial.get("radii_km") or [0.0] * _geometry.N_RAYS)
    record["radial_log_area"] = radial.get("log_area")
    record["radial_area_km2"] = radial.get("area_km2", 0.0)
    record["geometry_status"] = radial.get("status", _geometry.STATUS_SKIPPED)
    if radial.get("reason"):
        reasons.append(f"geometry:{radial['reason']}")
    if radial.get("longitude_unwrapped"):
        reasons.append("geometry:longitude-unwrapped")

    # --- raw source values -------------------------------------------------
    try:
        flattened = _features.flatten_properties(properties)
    except Exception:
        flattened = {}
        reasons.append("corrupt-sample:properties-not-flattenable")
    raw_values: dict[str, Any] = {}
    for name in _features.UNIVERSAL_PROPERTY_FEATURES:
        value = flattened.get(name)
        if isinstance(value, bool):
            raw_values[name] = "corrupt:bool"
        elif value is None:
            raw_values[name] = None
        elif isinstance(value, (int, float)):
            number = float(value)
            raw_values[name] = number if math.isfinite(number) else "corrupt:non-finite"
        elif isinstance(value, str):
            raw_values[name] = value
        else:
            raw_values[name] = "corrupt:non-scalar"
    record["raw_values"] = raw_values
    record["units"] = feature_units()

    # --- source analysis times --------------------------------------------
    record["source_times"] = {family: _iso_or_none(value)
                              for family, value in source_times.items()}
    absent_families = {family for family, value in source_times.items() if value is None}
    for family in sorted(absent_families):
        reasons.append(f"missing-source:{family}")

    # --- derived model features -------------------------------------------
    try:
        property_vector, property_flags = _features.build_property_feature_vector(properties)
    except Exception:
        property_vector = [_features.MISSING_SENTINEL] * _features.N_PROPERTY
        property_flags = [FLAG_CORRUPT] * _features.N_PROPERTY
        reasons.append("corrupt-sample:feature-extraction-failed")
    try:
        initial = _features.predict_initial_wind(properties)
        initial_status = "ok"
    except _features.NoUsableWindPair as exc:
        initial = {"u": _features.MISSING_SENTINEL, "v": _features.MISSING_SENTINEL}
        initial_status = "no-usable-wind-pair"
        reasons.append(f"no-usable-wind-pair:{exc}")
    except Exception as exc:
        initial = {"u": _features.MISSING_SENTINEL, "v": _features.MISSING_SENTINEL}
        initial_status = "corrupt-wind-field"
        reasons.append(f"corrupt-sample:wind-field ({exc})")

    quality: dict[str, str] = {}
    missing_fields = 0
    for name, flag in zip(_features.UNIVERSAL_PROPERTY_FEATURES, property_flags):
        if flag == FLAG_OK:
            quality[name] = FLAG_OK
        else:
            family = _family_for_feature(name)
            if family in absent_families or (
                    name.startswith("wind_field.") and _WIND_SOURCE_FAMILY in absent_families):
                quality[name] = FLAG_MISSING_SOURCE
            else:
                quality[name] = FLAG_MISSING_FIELD
            missing_fields += 1
    quality["initial_u"] = FLAG_OK if initial_status == "ok" else FLAG_MISSING_FIELD
    quality["initial_v"] = FLAG_OK if initial_status == "ok" else FLAG_MISSING_FIELD
    record["quality"] = quality
    if missing_fields:
        reasons.append(f"missing-fields:{missing_fields}")

    record["current_features_raw"] = property_vector + [
        float(initial["u"]), float(initial["v"]), 0.0, 0.0,
    ]
    record["initial_wind_mps"] = {"u": float(initial["u"]), "v": float(initial["v"])}
    record["initial_wind_status"] = initial_status

    # --- cycle-manifest audit ----------------------------------------------
    if cycle_manifest is not None and parsed_time is not None:
        try:
            manifest_times: dict[str, Any] = {}
            for record_input in getattr(cycle_manifest, "inputs", ()):
                family = getattr(record_input, "family", "")
                moment = getattr(record_input, "analysis_time", None)
                if family and moment is not None:
                    manifest_times.setdefault(str(family), moment)
            rap_max_age = getattr(cycle_manifest, "rap_max_age_seconds", None)
            for issue in _features.audit_source_times(
                    parsed_time, manifest_times, rap_max_age_seconds=rap_max_age):
                reasons.append(f"manifest:{issue}")
        except Exception as exc:
            reasons.append(f"manifest:audit-failed ({exc})")

    # --- readiness ----------------------------------------------------------
    geometry_ok = record["geometry_status"] == _geometry.STATUS_OK
    identity_ok = cell_id is not None and parsed_time is not None and not any(
        r.startswith("corrupt-sample") for r in reasons)
    wind_ok = initial_status == "ok"
    record["inference_ready"] = bool(geometry_ok and identity_ok and wind_ok)
    if not record["inference_ready"]:
        if not geometry_ok:
            reasons.append("not-ready:geometry")
        if not identity_ok:
            reasons.append("not-ready:identity")
        if not wind_ok:
            reasons.append("not-ready:initial-wind")
    # ``storm_age_seconds`` / ``valid_history_length`` are track-relative and
    # are filled by the track builder (tracks.py); placeholders stay 0 here.
    record["reasons"] = sorted(set(reasons))
    record["lineage"] = {
        "parent_ids": list(cell.get("parent_ids") or []),
        "split_from": cell.get("split_from"),
        "event_type": cell.get("event_type"),
        "tracking_mode": cell.get("tracking_mode"),
    }
    return record


__all__ = [
    "SCHEMA_VERSION",
    "FLAG_OK",
    "FLAG_MISSING_FIELD",
    "FLAG_MISSING_SOURCE",
    "FLAG_CORRUPT",
    "feature_units",
    "build_observation_record",
]
