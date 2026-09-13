"""Versioned float32 StormProb sampling and instantaneous contour construction.

Input arrays are the paired ONNX outputs and committed observation geometry.
The 20-member ensemble uses NumPy PCG64 seed 42 by default. An explicit noise
array permits exact replay of the PyTorch evaluation ensemble during parity
validation. This module never interprets a lead contour as a swept envelope.
"""
from __future__ import annotations

import math

import numpy as np


VERSION = "stormprob-postprocess/v1"
LEADS_MINUTES = (15, 30, 45, 60)
ENSEMBLE_SIZE = 20
GRID_HALF_WIDTH_KM = 100
GRID_RESOLUTION_KM = 1
THRESHOLD = 0.25


def sample_radii(mean, log_std, current_radii, *, noise=None):
    """Return `[1,20,4,64]` radii from the low-mode Fourier distribution."""
    mean = np.asarray(mean, dtype=np.float32)
    log_std = np.asarray(log_std, dtype=np.float32)
    current = np.asarray(current_radii, dtype=np.float32)
    if mean.shape != (1, 4, 33) or log_std.shape != mean.shape or current.shape != (1, 64):
        raise ValueError("Invalid StormProb Fourier or current-radii shape")
    if noise is None:
        noise = np.random.default_rng(42).standard_normal((1, ENSEMBLE_SIZE, 4, 33),
                                                          dtype=np.float32)
    noise = np.asarray(noise, dtype=np.float32)
    if noise.shape != (1, ENSEMBLE_SIZE, 4, 33):
        raise ValueError("Invalid StormProb ensemble noise shape")
    stochastic = np.zeros(33, dtype=np.float32)
    stochastic[[0, 1, 2, 3, 17, 18, 19]] = 1
    coefficients = mean[:, None] + np.exp(log_std)[:, None] * stochastic * noise
    angle = np.arange(64, dtype=np.float32) * np.float32(2 * math.pi / 64)
    mode = np.arange(1, 17, dtype=np.float32)
    phase = mode[:, None] * angle[None]
    decoded = coefficients[..., :1].copy()
    decoded = decoded + (coefficients[..., 1:17, None] * np.cos(phase)).sum(axis=-2)
    decoded = decoded + (coefficients[..., 17:, None] * np.sin(phase)).sum(axis=-2)
    return current[:, None, None, :] + np.float32(30) * np.tanh(decoded / np.float32(30))


def occupancy_probability(radii, displacement_km):
    """Return four uncalibrated 201x201 occupancy probabilities."""
    radii = np.asarray(radii, dtype=np.float32)
    centres = np.asarray(displacement_km, dtype=np.float32)
    if radii.shape != (1, ENSEMBLE_SIZE, 4, 64) or centres.shape != (1, 4, 2):
        raise ValueError("Invalid StormProb radii or displacement shape")
    axis = np.arange(-GRID_HALF_WIDTH_KM, GRID_HALF_WIDTH_KM + 1, dtype=np.float32)
    east, north = np.meshgrid(axis, axis, indexing="xy")
    counts = np.zeros((4, 201, 201), dtype=np.uint8)
    for lead in range(4):
        dx = east - centres[0, lead, 0]
        dy = north - centres[0, lead, 1]
        angle = np.mod(np.arctan2(dy, dx), np.float32(2 * math.pi)) * np.float32(64 / (2 * math.pi))
        floor = np.floor(angle)
        lo = floor.astype(np.int64) % 64
        hi = (lo + 1) % 64
        frac = angle - floor
        distance = np.sqrt(dx * dx + dy * dy)
        for member in range(ENSEMBLE_SIZE):
            profile = radii[0, member, lead]
            interpolated = profile[lo] + (profile[hi] - profile[lo]) * frac
            counts[lead] += distance <= interpolated
    return counts.astype(np.float32) / np.float32(ENSEMBLE_SIZE)


def calibrated_masks(probability, calibrator):
    """Apply the four lead-specific isotonic tables at the 0.25 threshold."""
    probability = np.asarray(probability, dtype=np.float32)
    if probability.shape != (4, 201, 201):
        raise ValueError("Invalid StormProb occupancy grid shape")
    masks = np.zeros_like(probability, dtype=np.bool_)
    for lead in range(4):
        levels = np.asarray(calibrator["parameters"][lead]["calibrated_probabilities"],
                            dtype=np.float32)
        if levels.shape != (21,):
            raise ValueError(f"Invalid StormProb calibrator lead {lead}")
        index = np.clip(np.rint(probability[lead] * ENSEMBLE_SIZE).astype(np.int64), 0, 20)
        masks[lead] = levels[index] >= THRESHOLD
    return masks


def displacement(initial_wind_mps, residual_motion_mps):
    """Return east/north displacement in km for the four lead times."""
    initial = np.asarray(initial_wind_mps, dtype=np.float32)
    residual = np.asarray(residual_motion_mps, dtype=np.float32)
    if initial.shape != (2,) or residual.shape != (1, 4, 2):
        raise ValueError("Invalid StormProb wind or residual shape")
    seconds = np.asarray(LEADS_MINUTES, dtype=np.float32) * np.float32(60)
    return ((initial[None] + residual[0]) * seconds[:, None] / np.float32(1000))[None]


def polygons_from_masks(masks, centroid_latlon):
    """Polygonize each instantaneous mask into valid GeoJSON lon/lat rings."""
    from rasterio.features import shapes
    from affine import Affine
    from shapely.affinity import translate
    from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape

    masks = np.asarray(masks, dtype=np.bool_)
    lat, lon = map(float, centroid_latlon)
    if masks.shape != (4, 201, 201) or not (-90 < lat < 90) or not math.isfinite(lon):
        raise ValueError("Invalid StormProb mask or centroid")
    transform = Affine.translation(-100.5, 100.5) * Affine.scale(1, -1)
    results = []
    for lead in range(4):
        if not masks[lead].any():
            results.append({"status": "no-polygon:empty-0.25-contour", "geometry": None})
            continue
        polygons = []
        # Rasterio expects north-to-south rows; occupancy rows are ascending north.
        image = np.ascontiguousarray(masks[lead, ::-1].astype(np.uint8))
        for geometry, value in shapes(image, mask=image.astype(np.bool_), transform=transform):
            if value != 1:
                continue
            rings = []
            for ring in geometry["coordinates"]:
                geographic = []
                for east_km, north_km in ring:
                    point_lon = lon + east_km / (111 * math.cos(math.radians(lat)))
                    point_lat = lat + north_km / 111
                    geographic.append([float(point_lon), float(point_lat)])
                rings.append(geographic)
            polygons.append(shape({"type": "Polygon", "coordinates": rings}))
        # Keep the public 0..360 domain without a ring jumping across the
        # longitude seam. Separate any piece west of zero or east of 360.
        pieces = []
        for polygon in polygons:
            for slab, shift in ((box(-360, -90, 0, 90), 360),
                                (box(0, -90, 360, 90), 0),
                                (box(360, -90, 720, 90), -360)):
                part = polygon.intersection(slab)
                if part.is_empty:
                    continue
                part = translate(part, xoff=shift)
                if isinstance(part, Polygon):
                    pieces.append(part)
                elif isinstance(part, MultiPolygon):
                    pieces.extend(part.geoms)
        geometry = (mapping(MultiPolygon(pieces)) if pieces else None)
        status = "ok" if geometry is not None else "no-polygon:polygonization-empty"
        results.append({"status": status, "geometry": geometry})
    return results
