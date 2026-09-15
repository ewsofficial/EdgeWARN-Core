"""Versioned StormProb feature extractor (Phase 1).

Single versioned implementation of the training input contract from
StormProb's ``stormprob/model/residual/features.py`` (``flatten_properties``,
property order, ``predict_motion_vector`` mean 0-6 km wind, trajectory-history
derivation) and ``stormprob/model/initial_pred.py``:

- ``FEATURE_SCHEMA_VERSION`` + ``feature_order_checksum()`` identify the exact
  named order; ``verify_against_manifest()`` cross-checks the frozen
  ``models/stormprob/manifest.json`` order.
- Missing values use the checkpoint's documented ``-999`` sentinel with
  ``missing_threshold = -900``; zero winds are never fabricated.
- Clipping + normalization replicate the checkpoint ``normalization-stats.json``
  vectors (median-impute, clip, ``(x-mean)/scale``, binary missing-mask
  append for environment/current streams; clip + normalize for the
  trajectory and radial/stats streams).
- Source timestamps can be audited against the cycle manifest so no future or
  stale environment is mixed into a historical forecast.

Raw deployment values are finite float32 or ``-999``; JSON ``NaN`` is never
emitted.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

FEATURE_SCHEMA_VERSION = "stormprob-input/v1"
HISTORY_STEPS = 30
MISSING_SENTINEL = -999.0
MISSING_THRESHOLD = -900.0
MAX_BASELINE_WIND_HEIGHT_KM = 6.0

IMPORTANT_SCALAR_PROPERTY_FEATURES = (
    "MUCAPE", "MLCAPE", "MLCIN", "CAPE_M10M30", "DCAPE", "PWAT",
    "temp_2m", "dewpoint_2m", "dewpoint_depression",
    "freezing_level_height", "freezing_level_m", "Wetbulb_0C_Hgt",
    "EBShear", "MeanWind_1-3kmAGL", "SRH01km", "SRH02km", "SRW46km",
    "u10m", "v10m",
    "Ref0", "Ref5", "Ref10", "Ref15", "Ref20",
    "maxRALA", "maxPrecipRate", "MESH", "VIL",
    "maxVIL", "p50VIL", "p90VIL", "p95VIL",
    "maxVILDensity", "p50VILDensity", "p90VILDensity", "p95VILDensity",
    "maxVII", "EchoTop50",
    "maxEchoTop18", "p90EchoTop18", "p95EchoTop18",
    "maxEchoTop30", "p90EchoTop30", "p90EchoTop50",
    "MaxLLAz", "maxAzShearLow", "p95AzShearLow", "p98LLAz",
    "maxAzShearMid", "p95AzShearMid", "p98MLAz",
)

MORPHOLOGY_PROPERTY_FEATURES = (
    "morphology.aspect_ratio",
    "morphology.branching_factor",
    "morphology.defect_bearing",
    "morphology.defect_max_depth",
    "morphology.linearity",
    "morphology.solidity",
)


def _levels_from_feature_order(order: tuple[str, ...]) -> tuple[int, ...]:
    """Pressure levels parsed from frozen ``wind_field.u<level>`` names."""
    levels = {int(name[len("wind_field.u"):]) for name in order
              if name.startswith("wind_field.u")}
    return tuple(sorted(levels))


# Uniform 25 hPa spacing from 100 to 1000 hPa (37 levels). The runtime wind
# authority stays ``config/integration.yaml`` ``rap_products.isobaric_levels_mb``
# (exact match per the Phase 0 availability matrix); the frozen feature order
# below and ``verify_against_manifest`` pin the model contract. Arithmetic —
# not a second hand-maintained catalog — so the levels cannot drift from the
# stated spacing without touching this line.
PRESSURE_LEVELS_HPA: tuple[int, ...] = tuple(range(100, 1001, 25))

WIND_FIELD_PROPERTY_FEATURES = tuple(
    f"wind_field.{component}{level}"
    for component in ("u", "v")
    for level in PRESSURE_LEVELS_HPA
)

UNIVERSAL_PROPERTY_FEATURES = (
    IMPORTANT_SCALAR_PROPERTY_FEATURES
    + MORPHOLOGY_PROPERTY_FEATURES
    + WIND_FIELD_PROPERTY_FEATURES
)

DERIVED_FEATURES = (
    "initial_u",
    "initial_v",
    "storm_age_seconds",
    "valid_history_length",
)

CURRENT_FEATURE_ORDER = UNIVERSAL_PROPERTY_FEATURES + DERIVED_FEATURES

TRAJECTORY_FEATURE_ORDER = (
    "dx_km", "dy_km", "dt_minutes",
    "velocity_u", "velocity_v",
    "velocity_5min_u", "velocity_5min_v",
    "velocity_10min_u", "velocity_10min_v",
    "acceleration_u_per_minute", "acceleration_v_per_minute",
    "turn_rate_degrees_per_minute",
    "path_efficiency_10min", "maximum_speed_10min",
    "track_age_hours", "valid_history_fraction",
)

N_CURRENT = len(CURRENT_FEATURE_ORDER)
N_PROPERTY = len(UNIVERSAL_PROPERTY_FEATURES)
N_TRAJECTORY = len(TRAJECTORY_FEATURE_ORDER)

assert N_CURRENT == 135, f"feature order must hold 135 entries, has {N_CURRENT}"
assert N_PROPERTY == 131, f"property order must hold 131 entries, has {N_PROPERTY}"
assert N_TRAJECTORY == 16

# Values written by the current EdgeWARN integration pipeline that are error
# markers rather than measurements.
_ERROR_MARKERS = frozenset({"MATCH_ERROR", "PROCESSING_ERROR", "ERROR"})


class NoUsableWindPair(ValueError):
    """No paired u/v wind level is usable for the initial-motion mean."""


def feature_order_checksum() -> str:
    """SHA-256 over the exact current-feature name order."""
    return hashlib.sha256(",".join(CURRENT_FEATURE_ORDER).encode("utf-8")).hexdigest()


def _repo_manifest_path() -> Path | None:
    from .assets import manifest_path

    try:
        path = manifest_path()
    except FileNotFoundError:
        return None
    return path if path.is_file() else None


def verify_against_manifest() -> dict:
    """Cross-check the frozen extractor order against manifest.json.

    Returns ``{"ok": True, ...}`` or ``{"ok": False, ...}`` with details;
    never raises so pipeline wiring stays failure-isolated.
    """
    try:
        path = _repo_manifest_path()
        if path is None:
            return {"ok": False, "reason": "manifest-not-found"}
        manifest = json.loads(path.read_text())
        expected = manifest["features"]["current_feature_order"]
        actual = list(CURRENT_FEATURE_ORDER)
        if expected != actual:
            return {"ok": False, "reason": "order-mismatch", "manifest": str(path),
                    "manifest_len": len(expected), "extractor_len": len(actual),
                    "first_diff": next(
                        (i for i, (a, b) in enumerate(zip(expected, actual)) if a != b),
                        min(len(expected), len(actual)))}
        manifest_levels = list(manifest["features"]["pressure_levels_hpa"])
        if manifest_levels != list(PRESSURE_LEVELS_HPA):
            return {"ok": False, "reason": "levels-mismatch", "manifest": str(path)}
        if _levels_from_feature_order(tuple(actual)) != PRESSURE_LEVELS_HPA:
            return {"ok": False, "reason": "levels-order-inconsistent"}
        return {"ok": True, "manifest": str(path),
                "checksum": feature_order_checksum()}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "reason": f"manifest-error: {exc}"}


def flatten_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Recursively flatten nested dicts with dot-joined keys (training port)."""
    if not isinstance(properties, dict):
        raise TypeError("properties must be a dict.")
    flattened: dict[str, Any] = {}
    for key, value in properties.items():
        if isinstance(value, dict):
            for nested_key, nested_value in flatten_properties(value).items():
                flattened[f"{key}.{nested_key}"] = nested_value
        else:
            flattened[key] = value
    return flattened


def coerce_finite_or_sentinel(value: Any) -> tuple[float, str]:
    """Coerce one raw value to finite float32 or the ``-999`` sentinel.

    Returns ``(value, flag)`` where flag is ``ok`` or ``missing-field``.
    ``None``, missing keys, error-marker strings, bools, NaN/inf, and any
    value at or below the ``-900`` missing threshold all map to the sentinel.
    """
    if value is None:
        return MISSING_SENTINEL, "missing-field"
    if isinstance(value, bool):
        return MISSING_SENTINEL, "missing-field"
    if isinstance(value, str):
        if value in _ERROR_MARKERS:
            return MISSING_SENTINEL, "missing-field"
        try:
            value = float(value)
        except ValueError:
            return MISSING_SENTINEL, "missing-field"
    if not isinstance(value, (int, float)):
        return MISSING_SENTINEL, "missing-field"
    number = float(value)
    if not math.isfinite(number) or number <= MISSING_THRESHOLD:
        if math.isfinite(number) and number <= MISSING_THRESHOLD:
            return MISSING_SENTINEL, "missing-field"
        return MISSING_SENTINEL, "missing-field"
    return float(np.float32(number)), "ok"


def build_property_feature_vector(properties: dict[str, Any]) -> tuple[list[float], list[str]]:
    """Build the 131 named property features with sentinel + per-field flags."""
    if not isinstance(properties, dict):
        raise TypeError("properties must be a dict.")
    flattened = flatten_properties(properties)
    vector: list[float] = []
    flags: list[str] = []
    for name in UNIVERSAL_PROPERTY_FEATURES:
        value, flag = coerce_finite_or_sentinel(flattened.get(name))
        vector.append(value)
        flags.append(flag)
    return vector, flags


def pressure_to_height_km(pressure_hpa: int) -> float:
    """Standard-atmosphere height (exact ``initial_pred.py`` port)."""
    if pressure_hpa <= 0:
        raise ValueError("Pressure level must be positive.")
    return 44330.0 * (1.0 - (pressure_hpa / 1013.25) ** 0.190284) / 1000.0


def _usable_wind_pairs(wind_field: dict[str, Any]) -> list[tuple[int, float, float]]:
    """Paired u/v levels where both components are finite and not sentinel."""
    components: dict[int, dict[str, float]] = {}
    for key, value in wind_field.items():
        if not key or key[0] not in {"u", "v"} or not key[1:].isdigit():
            continue
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number) or number <= MISSING_THRESHOLD:
            continue
        components.setdefault(int(key[1:]), {})[key[0]] = number
    pairs = [(level, parts["u"], parts["v"]) for level, parts in components.items()
             if "u" in parts and "v" in parts]
    pairs.sort(key=lambda item: item[0], reverse=True)
    return pairs


def predict_initial_wind(properties: dict[str, Any]) -> dict[str, float]:
    """Mean 0-6 km environmental wind in m/s (``initial_pred.py`` port).

    Uses only paired levels at or below 6 km standard-atmosphere height.
    Raises :class:`NoUsableWindPair` instead of fabricating zero winds.
    """
    if not isinstance(properties, dict):
        raise TypeError("properties must be a dict.")
    wind_field = properties.get("wind_field")
    if not isinstance(wind_field, dict):
        raise NoUsableWindPair("properties.wind_field is absent or not a mapping")
    pairs = _usable_wind_pairs(wind_field)
    included = [(u, v) for level, u, v in pairs
                if pressure_to_height_km(level) <= MAX_BASELINE_WIND_HEIGHT_KM]
    if not included:
        raise NoUsableWindPair("wind_field has no usable paired level at or below 6 km")
    u_mean = sum(u for u, _ in included) / len(included)
    v_mean = sum(v for _, v in included) / len(included)
    return {"u": float(u_mean), "v": float(v_mean)}


def parse_analysis_timestamp(value: Any) -> datetime:
    """Parse a naive-ISO analysis timestamp (TZ-naive convention is pinned)."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp value: {value!r}") from exc


def _to_epoch_seconds(moment: datetime) -> float:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc).timestamp()
    return moment.astimezone(timezone.utc).timestamp()


def storm_age_seconds(entries: list[dict], index: int) -> float:
    """Current minus first-valid-track timestamp (training port)."""
    current = parse_analysis_timestamp(entries[index].get("timestamp"))
    for entry in entries[: index + 1]:
        try:
            first = parse_analysis_timestamp(entry.get("timestamp"))
        except ValueError:
            continue
        age = (current - first).total_seconds()
        if age < 0:
            raise ValueError("Storm entry timestamps must not precede the first timestamp.")
        return float(age)
    raise ValueError("No valid storm entry timestamp is available.")


def build_current_feature_vector(
    entries: list[dict],
    index: int,
    history_steps: int = HISTORY_STEPS,
) -> tuple[list[float], dict]:
    """Build the 135 current-feature row for ``entries[index]`` (training port).

    Property features use the ``-999`` sentinel instead of raising; the four
    derived features follow training exactly. Returns ``(vector, meta)`` where
    meta carries per-field flags and the ``initial_wind`` status.
    """
    if not entries:
        raise ValueError("entries must not be empty.")
    if index < 0 or index >= len(entries):
        raise IndexError("index is out of range for entries.")
    if history_steps <= 0:
        raise ValueError("history_steps must be positive.")
    entry = entries[index]
    properties = entry.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("entry['properties'] must be a dict.")
    property_values, property_flags = build_property_feature_vector(properties)
    try:
        initial = predict_initial_wind(properties)
        initial_status = "ok"
    except NoUsableWindPair as exc:
        initial = {"u": MISSING_SENTINEL, "v": MISSING_SENTINEL}
        initial_status = f"skipped:no-usable-wind-pair ({exc})"
    age = storm_age_seconds(entries, index)
    valid_length = min(index + 1, history_steps)
    vector = property_values + [initial["u"], initial["v"], age, float(valid_length)]
    meta = {
        "property_flags": property_flags,
        "initial_u": initial["u"],
        "initial_v": initial["v"],
        "initial_status": initial_status,
        "storm_age_seconds": age,
        "valid_history_length": valid_length,
    }
    return vector, meta


# ---------------------------------------------------------------------------
# Normalization (checkpoint vectors from normalization-stats.json)
# ---------------------------------------------------------------------------

# NOTE: intentionally uncached so a miss never poisons a long-lived worker;
# load_normalization below caches only successful loads (lru_cache does not
# cache raised exceptions), giving self-healing once assets appear.
def _normalization_path() -> Path | None:
    from .assets import asset_dir

    try:
        candidate = asset_dir() / "normalization-stats.json"
    except FileNotFoundError:
        return None
    return candidate if candidate.is_file() else None


@lru_cache(maxsize=1)
def load_normalization() -> dict:
    """Load checkpoint normalization vectors (cached; raises if absent)."""
    path = _normalization_path()
    if path is None:
        raise FileNotFoundError("models/stormprob/normalization-stats.json not found")
    payload = json.loads(path.read_text())
    radial = payload["radial_model_config"]
    motion = payload["motion_model_config"]
    return {
        "radial_env": radial["env_norm"],
        "radial_profile": radial["radial_norm"],
        "radial_stats": radial["stats_norm"],
        "motion_current": {
            "mean": motion["current_feature_normalization_mean"],
            "scale": motion["current_feature_normalization_scale"],
            "median": motion["current_feature_normalization_median"],
            "clip_min": motion["current_feature_normalization_clip_min"],
            "clip_max": motion["current_feature_normalization_clip_max"],
            "missing_threshold": motion["current_feature_missing_threshold"],
        },
        "motion_trajectory": {
            "mean": motion["trajectory_normalization_mean"],
            "scale": motion["trajectory_normalization_scale"],
            "clip_min": motion["trajectory_normalization_clip_min"],
            "clip_max": motion["trajectory_normalization_clip_max"],
        },
    }


def _as_f32(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


def normalize_with_impute(raw: np.ndarray, norm: dict) -> tuple[np.ndarray, np.ndarray]:
    """Median-impute (<= threshold), clip, ``(x-mean)/scale``; append miss mask."""
    raw = _as_f32(raw)
    threshold = float(norm.get("missing_threshold", MISSING_THRESHOLD))
    missing = raw <= threshold
    imputed = np.where(missing, _as_f32(norm["median"]), raw)
    clipped = np.clip(imputed, _as_f32(norm["clip_min"]), _as_f32(norm["clip_max"]))
    normalized = (clipped - _as_f32(norm["mean"])) / _as_f32(norm["scale"])
    return normalized.astype(np.float32), missing


def normalize_radial_env(raw135: list | np.ndarray, norm: dict | None = None) -> np.ndarray:
    """Radial environment stream: 135 raw channels -> 270 normalized+mask."""
    norm = norm or load_normalization()["radial_env"]
    normalized, missing = normalize_with_impute(np.asarray(raw135), norm)
    return np.concatenate([normalized, missing.astype(np.float32)]).astype(np.float32)


def normalize_motion_current(raw135: list | np.ndarray, norm: dict | None = None) -> np.ndarray:
    """Motion current/history stream: same median-impute scheme + mask."""
    norm = norm or load_normalization()["motion_current"]
    normalized, missing = normalize_with_impute(np.asarray(raw135), norm)
    return np.concatenate([normalized, missing.astype(np.float32)]).astype(np.float32)


def normalize_trajectory(raw16: list | np.ndarray, norm: dict | None = None) -> np.ndarray:
    """Trajectory stream: clip + normalize, no imputation (training port)."""
    norm = norm or load_normalization()["motion_trajectory"]
    raw = _as_f32(raw16)
    clipped = np.clip(raw, _as_f32(norm["clip_min"]), _as_f32(norm["clip_max"]))
    return ((clipped - _as_f32(norm["mean"])) / _as_f32(norm["scale"])).astype(np.float32)


def normalize_radial_profile(radii64: list | np.ndarray, norm: dict | None = None) -> np.ndarray:
    """Radial stream: clip + normalize, no imputation (training port)."""
    norm = norm or load_normalization()["radial_profile"]
    raw = _as_f32(radii64)
    clipped = np.clip(raw, _as_f32(norm["clip_min"]), _as_f32(norm["clip_max"]))
    return ((clipped - _as_f32(norm["mean"])) / _as_f32(norm["scale"])).astype(np.float32)


def normalize_radial_stats(stats: list | np.ndarray, norm: dict | None = None) -> np.ndarray:
    """Statistics stream (log-area first column): clip + normalize, sliced."""
    norm = norm or load_normalization()["radial_stats"]
    raw = _as_f32(stats)
    width = raw.shape[-1]
    mean = _as_f32(norm["mean"])[:width]
    scale = _as_f32(norm["scale"])[:width]
    clip_min = _as_f32(norm["clip_min"])[:width]
    clip_max = _as_f32(norm["clip_max"])[:width]
    clipped = np.clip(raw, clip_min, clip_max)
    return ((clipped - mean) / scale).astype(np.float32)


def apply_history_mask(sequence: np.ndarray, mask: list | np.ndarray) -> np.ndarray:
    """Zero rows where the history mask is false (training port)."""
    sequence = np.asarray(sequence, dtype=np.float32)
    valid = np.asarray(mask, dtype=bool)
    return (sequence * valid.reshape(-1, *([1] * (sequence.ndim - 1)))).astype(np.float32)


# ---------------------------------------------------------------------------
# Source-timestamp audit against the cycle manifest
# ---------------------------------------------------------------------------

def audit_source_times(
    cycle_time: datetime,
    source_times: dict[str, datetime | str | None],
    *,
    future_tolerance_seconds: float = 120.0,
    rap_max_age_seconds: float | None = None,
) -> list[str]:
    """Flag future or stale environment sources mixed into one forecast.

    ``source_times`` maps a source family (``rap``, ``probsevere``, ``mrms``,
    ...) to its analysis time (or ``None`` when the source was absent).
    Returns machine-readable issue strings; empty means aligned. Absent
    sources are reported as ``absent-source:<family>``, never silently
    treated as current.
    """
    issues: list[str] = []
    if cycle_time.tzinfo is None:
        cycle_time = cycle_time.replace(tzinfo=timezone.utc)
    for family, value in source_times.items():
        if value is None:
            issues.append(f"absent-source:{family}")
            continue
        try:
            moment = value if isinstance(value, datetime) else parse_analysis_timestamp(value)
        except ValueError:
            issues.append(f"corrupt-source-time:{family}")
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        delta = (moment - cycle_time).total_seconds()
        if delta > future_tolerance_seconds:
            issues.append(f"future-source:{family}:+{delta:.0f}s")
        if rap_max_age_seconds is not None and family == "rap" and -delta > rap_max_age_seconds:
            issues.append(f"stale-source:{family}:age{-delta:.0f}s")
    return issues


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "HISTORY_STEPS",
    "MISSING_SENTINEL",
    "MISSING_THRESHOLD",
    "IMPORTANT_SCALAR_PROPERTY_FEATURES",
    "MORPHOLOGY_PROPERTY_FEATURES",
    "PRESSURE_LEVELS_HPA",
    "WIND_FIELD_PROPERTY_FEATURES",
    "UNIVERSAL_PROPERTY_FEATURES",
    "DERIVED_FEATURES",
    "CURRENT_FEATURE_ORDER",
    "TRAJECTORY_FEATURE_ORDER",
    "N_CURRENT",
    "N_PROPERTY",
    "N_TRAJECTORY",
    "NoUsableWindPair",
    "feature_order_checksum",
    "verify_against_manifest",
    "flatten_properties",
    "coerce_finite_or_sentinel",
    "build_property_feature_vector",
    "pressure_to_height_km",
    "predict_initial_wind",
    "parse_analysis_timestamp",
    "storm_age_seconds",
    "build_current_feature_vector",
    "load_normalization",
    "normalize_with_impute",
    "normalize_radial_env",
    "normalize_motion_current",
    "normalize_trajectory",
    "normalize_radial_profile",
    "normalize_radial_stats",
    "apply_history_mask",
    "audit_source_times",
]
