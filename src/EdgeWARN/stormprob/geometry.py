"""Full-precision detection geometry for StormProb inputs (Phase 1).

Ports the training convention from StormProb's
``stormprob/model/radial_morphology/model.py`` (``entry_to_radial_profile`` /
``polygon_to_radial_profile``) into dependency-light NumPy code:

- centroid is ``[lat, lon]``;
- rays start east and rotate counterclockwise;
- east = dlon * 111 * cos(lat), north = dlat * 111;
- log-area is the exact shoelace polygon area, ``log(max(area, 1e-6))``.

Longitude handling: training data uses the 0-360 domain and subtracts
longitudes directly. EdgeWARN detection also normalizes to 0-360
(``save.py``), so direct subtraction is identical for every CONUS cell.
When a polygon straddles the 0/360 meridian (|raw delta| > 180 deg, never
observed in the US domain), deltas are unwrapped to the minimal signed
angle and the result is flagged ``longitude_unwrapped=True`` instead of
producing ~40,000 km east offsets.

Degenerate polygons and NaNs are rejected with a machine-readable reason
code; existing morphology calculation is untouched.
"""

from __future__ import annotations

import math

import numpy as np

N_RAYS = 64
MAX_RADIUS_KM = 100.0
KM_PER_DEG = 111.0
LOG_AREA_FLOOR = 1e-6
ZERO_AREA_KM2 = 1e-9

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"

# Machine-readable geometry reject reasons.
REASON_EMPTY_POLYGON = "empty-polygon"
REASON_TOO_FEW_POINTS = "too-few-points"
REASON_NON_FINITE_CENTROID = "non-finite-centroid"
REASON_NON_FINITE_VERTEX = "non-finite-vertex"
REASON_ZERO_AREA = "degenerate-zero-area"
REASON_INTERNAL_ERROR = "internal-error"


def lon_delta_deg(lon: float, clon: float) -> tuple[float, bool]:
    """Minimal signed longitude delta in degrees, plus an unwrap flag."""
    raw = float(lon) - float(clon)
    wrapped = (raw + 180.0) % 360.0 - 180.0
    # ``%`` can return -180 for raw == +180; keep the training-exact +180.
    if raw == 180.0:
        wrapped = 180.0
    return wrapped, abs(raw) > 180.0


def to_local_en_km(
    polygon_latlon: list | np.ndarray,
    centroid_latlon: list | tuple | np.ndarray,
) -> tuple[np.ndarray, bool]:
    """Convert [lat, lon] vertices to east/north km relative to the centroid."""
    centroid = np.asarray(centroid_latlon, dtype=np.float64)
    points = np.asarray(polygon_latlon, dtype=np.float64)
    clat = float(centroid[0])
    scale = KM_PER_DEG * math.cos(math.radians(clat))
    unwrapped = False
    east = np.empty(points.shape[0], dtype=np.float64)
    for i, lon in enumerate(points[:, 1]):
        delta, was_wrapped = lon_delta_deg(float(lon), float(centroid[1]))
        east[i] = delta * scale
        unwrapped = unwrapped or was_wrapped
    north = (points[:, 0] - clat) * KM_PER_DEG
    return np.column_stack((east, north)), unwrapped


def shoelace_area_km2(local_en: np.ndarray) -> float:
    """Exact polygon area in km^2 via the shoelace formula."""
    east = local_en[:, 0]
    north = local_en[:, 1]
    return float(0.5 * abs(np.sum(east * np.roll(north, -1) - np.roll(east, -1) * north)))


def polygon_to_radial_profile(
    local_en: np.ndarray,
    n_angles: int = N_RAYS,
    max_radius_km: float = MAX_RADIUS_KM,
) -> np.ndarray:
    """Outermost ray/edge intersection per angle (exact training port)."""
    points = np.asarray(local_en, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 3:
        return np.zeros(n_angles, dtype=np.float32)
    angles = 2.0 * math.pi * np.arange(n_angles) / n_angles
    result = np.zeros(n_angles, dtype=np.float32)
    rolled = np.roll(points, -1, axis=0)
    for index, angle in enumerate(angles):
        dx, dy = math.cos(angle), math.sin(angle)
        best = 0.0
        for start, end in zip(points, rolled):
            edge_x = end[0] - start[0]
            edge_y = end[1] - start[1]
            denominator = dx * edge_y - dy * edge_x
            if abs(denominator) < 1e-10:
                continue
            radius = (start[0] * edge_y - start[1] * edge_x) / denominator
            fraction = (start[0] * dy - start[1] * dx) / denominator
            if radius >= 0.0 and 0.0 <= fraction <= 1.0 and radius > best:
                best = float(radius)
        result[index] = min(best, max_radius_km)
    return result


def _is_finite_pair(value) -> bool:
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return False
    return arr.shape == (2,) and bool(np.all(np.isfinite(arr)))


def radial_profile_for_cell(
    centroid_latlon,
    polygon_latlon,
    n_angles: int = N_RAYS,
    max_radius_km: float = MAX_RADIUS_KM,
) -> dict:
    """Derive the 64-ray profile and exact log-area for one detected cell.

    Returns a JSON-serializable dict with ``status`` ``ok``/``skipped``,
    ``radii_km`` (float32 list), ``log_area``, ``area_km2``, and a ``reason``
    code when skipped. Never raises for bad geometry.
    """
    base = {
        "n_rays": int(n_angles),
        "max_radius_km": float(max_radius_km),
        "rays": "start-east-rotate-counterclockwise",
        "centroid_order": "[lat, lon]",
        "longitude_unwrapped": False,
    }
    try:
        if not _is_finite_pair(centroid_latlon):
            return {**base, "status": STATUS_SKIPPED,
                    "reason": REASON_NON_FINITE_CENTROID,
                    "radii_km": [0.0] * n_angles, "log_area": None, "area_km2": 0.0}
        if polygon_latlon is None:
            return {**base, "status": STATUS_SKIPPED,
                    "reason": REASON_EMPTY_POLYGON,
                    "radii_km": [0.0] * n_angles, "log_area": None, "area_km2": 0.0}
        points = np.asarray(polygon_latlon, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] == 0:
            return {**base, "status": STATUS_SKIPPED,
                    "reason": REASON_EMPTY_POLYGON,
                    "radii_km": [0.0] * n_angles, "log_area": None, "area_km2": 0.0}
        if points.shape[0] < 3:
            return {**base, "status": STATUS_SKIPPED,
                    "reason": REASON_TOO_FEW_POINTS,
                    "radii_km": [0.0] * n_angles, "log_area": None, "area_km2": 0.0}
        if not bool(np.all(np.isfinite(points))):
            return {**base, "status": STATUS_SKIPPED,
                    "reason": REASON_NON_FINITE_VERTEX,
                    "radii_km": [0.0] * n_angles, "log_area": None, "area_km2": 0.0}
        local, unwrapped = to_local_en_km(points, centroid_latlon)
        area = shoelace_area_km2(local)
        if area <= ZERO_AREA_KM2:
            return {**base, "status": STATUS_SKIPPED,
                    "reason": REASON_ZERO_AREA, "longitude_unwrapped": unwrapped,
                    "radii_km": [0.0] * n_angles,
                    "log_area": float(math.log(LOG_AREA_FLOOR)), "area_km2": area}
        radii = polygon_to_radial_profile(local, n_angles=n_angles,
                                          max_radius_km=max_radius_km)
        return {**base, "status": STATUS_OK, "reason": None,
                "longitude_unwrapped": unwrapped,
                "radii_km": [float(v) for v in radii],
                "log_area": float(math.log(max(area, LOG_AREA_FLOOR))),
                "area_km2": area}
    except Exception:
        return {**base, "status": STATUS_SKIPPED,
                "reason": REASON_INTERNAL_ERROR,
                "radii_km": [0.0] * n_angles, "log_area": None, "area_km2": 0.0}


def attach_stormprob_geometry(
    entry: dict,
    centroid_full,
    polygon_full,
    n_angles: int = N_RAYS,
) -> dict:
    """Attach full-precision geometry + radial profile to a detection entry.

    Additive only: existing ``centroid``/``bbox``/``properties`` keys are
    untouched. ``centroid_full`` is the unrounded reflectivity-weighted
    centroid; ``polygon_full`` is the pre-JSON-rounding detection polygon as
    ``[[lat, lon], ...]``. Never raises.
    """
    try:
        profile = radial_profile_for_cell(centroid_full, polygon_full,
                                          n_angles=n_angles)
    except Exception:
        profile = radial_profile_for_cell(None, None, n_angles=n_angles)
    try:
        centroid_list = [float(centroid_full[0]), float(centroid_full[1]) % 360.0]
        if not all(math.isfinite(v) for v in centroid_list):
            raise ValueError("non-finite centroid")
    except Exception:
        centroid_list = None
    try:
        polygon_list = [[float(lat), float(lon) % 360.0] for lat, lon in polygon_full]
        if not all(math.isfinite(v) for pt in polygon_list for v in pt):
            raise ValueError("non-finite polygon vertex")
    except Exception:
        polygon_list = None
    stormprob = entry.setdefault("stormprob", {})
    stormprob["geometry"] = {
        "centroid_full": centroid_list,
        "polygon_full": polygon_list,
        "radial": profile,
    }
    return entry


__all__ = [
    "N_RAYS",
    "MAX_RADIUS_KM",
    "KM_PER_DEG",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "REASON_EMPTY_POLYGON",
    "REASON_TOO_FEW_POINTS",
    "REASON_NON_FINITE_CENTROID",
    "REASON_NON_FINITE_VERTEX",
    "REASON_ZERO_AREA",
    "REASON_INTERNAL_ERROR",
    "lon_delta_deg",
    "to_local_en_km",
    "shoelace_area_km2",
    "polygon_to_radial_profile",
    "radial_profile_for_cell",
    "attach_stormprob_geometry",
]
