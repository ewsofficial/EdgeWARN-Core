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
OPERATIONAL_GEOMETRY_VERSION = "stormprob-operational-envelope/v1"
LEADS_MINUTES = (15, 30, 45, 60)
ENSEMBLE_SIZE = 20
GRID_HALF_WIDTH_KM = 100
GRID_RESOLUTION_KM = 1
THRESHOLD = 0.25
OPERATIONAL_BUFFER_KM = 1.0
OPERATIONAL_MIN_POINTS = 4
OPERATIONAL_MAX_POINTS = 12
OPERATIONAL_MAX_AREA_INFLATION = 1.25


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


def _vertex_count(polygon) -> int:
    return max(0, len(polygon.exterior.coords) - 1)


def _add_vertex_if_needed(polygon):
    """Turn a triangular polygon into a four-vertex polygon."""
    if _vertex_count(polygon) >= OPERATIONAL_MIN_POINTS:
        return polygon
    coords = list(polygon.exterior.coords)[:-1]
    longest = max(range(len(coords)), key=lambda i: (
        (coords[(i + 1) % len(coords)][0] - coords[i][0]) ** 2
        + (coords[(i + 1) % len(coords)][1] - coords[i][1]) ** 2))
    a = coords[longest]
    b = coords[(longest + 1) % len(coords)]
    midpoint = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    coords.insert(longest + 1, midpoint)
    from shapely.geometry import Polygon
    return Polygon(coords)


def _circumscribed_polygon(points, vertex_limit):
    """Build an outer polygon with evenly distributed support-line normals."""
    from shapely.geometry import Polygon

    points = np.asarray(points, dtype=np.float64)
    center = points.mean(axis=0)
    centered = points - center
    covariance = centered.T @ centered
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axis = eigenvectors[:, int(np.argmax(eigenvalues))]
    rotation = math.atan2(float(axis[1]), float(axis[0]))
    angles = rotation + np.arange(vertex_limit, dtype=np.float64) * (
        2.0 * math.pi / vertex_limit)
    normals = np.column_stack((np.cos(angles), np.sin(angles)))
    offsets = np.max(centered @ normals.T, axis=0)
    extent = max(float(np.max(np.abs(centered))) * 4.0, 100.0)
    polygon = [np.array([-extent, -extent]), np.array([extent, -extent]),
               np.array([extent, extent]), np.array([-extent, extent])]
    for normal, offset in zip(normals, offsets):
        clipped = []
        for start, end in zip(polygon, polygon[1:] + polygon[:1]):
            start_inside = float(start @ normal) <= offset + 1e-9
            end_inside = float(end @ normal) <= offset + 1e-9
            if start_inside:
                clipped.append(start)
            if start_inside != end_inside:
                fraction = (offset - float(start @ normal)) / float((end - start) @ normal)
                clipped.append(start + fraction * (end - start))
        polygon = clipped
        if len(polygon) < 3:
            raise ValueError("support-line envelope is degenerate")
    result = Polygon(np.asarray(polygon) + center)
    if not result.is_valid:
        result = result.buffer(0)
    return result


def _operational_envelope_local(original, predicted, *, buffer_km=OPERATIONAL_BUFFER_KM):
    """Build a compact buffered envelope in local east/north kilometres."""
    from shapely.geometry import MultiPoint, Polygon

    original = np.asarray(original, dtype=np.float64)
    predicted = np.asarray(predicted, dtype=np.float64)
    points = np.vstack((original, predicted))
    envelope = MultiPoint([tuple(point) for point in points]).convex_hull
    if envelope.geom_type != "Polygon":
        raise ValueError("forecast envelope is not a polygon")
    envelope = envelope.buffer(0).convex_hull

    reference = envelope.buffer(float(buffer_km), join_style=2)
    for vertex_limit in range(OPERATIONAL_MIN_POINTS, OPERATIONAL_MAX_POINTS + 1):
        # Preserve a naturally compact hull. Otherwise use an outer
        # support-line approximation and add vertices until its fit is good.
        candidate = (envelope if _vertex_count(envelope) <= vertex_limit
                     else _circumscribed_polygon(points, vertex_limit))
        candidate = _add_vertex_if_needed(candidate)
        buffered = candidate.buffer(float(buffer_km), join_style=2)
        if buffered.geom_type != "Polygon" or not (
                OPERATIONAL_MIN_POINTS <= _vertex_count(buffered) <= OPERATIONAL_MAX_POINTS):
            continue
        if not buffered.covers(envelope):
            continue
        inflation = buffered.area / max(reference.area, 1e-9)
        if inflation <= OPERATIONAL_MAX_AREA_INFLATION:
            return buffered
    raise ValueError("cannot fit forecast envelope within operational point and area limits")


def calibrated_radial_boundary(radii_km, calibrator, lead_index, *,
                               threshold=THRESHOLD):
    """Convert calibrated occupancy probability into a radial boundary.

    Along a ray, occupancy at a radius is the number of ensemble members whose
    radial extent reaches that radius. The first calibrated member count at or
    above the threshold therefore selects the corresponding order statistic.
    """
    radii = np.asarray(radii_km, dtype=np.float64)
    if radii.shape != (ENSEMBLE_SIZE, 64):
        raise ValueError("invalid ensemble radial shape")
    levels = np.asarray(calibrator["parameters"][lead_index][
        "calibrated_probabilities"], dtype=np.float64)
    if levels.shape != (ENSEMBLE_SIZE + 1,) or not np.all(np.isfinite(levels)):
        raise ValueError("invalid calibrated probability levels")
    qualifying = np.flatnonzero(levels >= float(threshold))
    if qualifying.size == 0:
        raise ValueError("calibrator has no qualifying probability level")
    member_count = int(qualifying[0])
    # Count zero means the outermost ensemble boundary is the conservative
    # finite representation of an everywhere-qualified contour.
    if member_count == 0:
        return np.max(radii, axis=0).astype(np.float32)
    return np.sort(radii, axis=0)[::-1][member_count - 1].astype(np.float32)


def operational_envelope(original_polygon_latlon, centroid_latlon, displacement_km,
                         radii_km, calibrator, lead_index, *,
                         buffer_km=OPERATIONAL_BUFFER_KM):
    """Return an adaptive 4-12 point, 1 km buffered forecast envelope.

    The envelope spans the original detection polygon and the calibrated
    predicted radial shape for one lead. Four points are used when the shape
    fits; additional support points are added up to twelve when needed to keep
    buffered area inflation within the operational tolerance. All geometry
    operations are performed in local east/north kilometres; the result is
    GeoJSON in [longitude, latitude].
    """
    original_local, _ = _to_local(original_polygon_latlon, centroid_latlon)
    centroid = np.asarray(centroid_latlon, dtype=np.float64)
    displacement = np.asarray(displacement_km, dtype=np.float64)
    radii = calibrated_radial_boundary(radii_km, calibrator, lead_index)
    if radii.shape != (64,) or displacement.shape != (2,):
        raise ValueError("invalid operational forecast shape")
    angle = np.arange(64, dtype=np.float64) * (2.0 * math.pi / 64.0)
    predicted_local = np.column_stack((
        displacement[0] + radii * np.cos(angle),
        displacement[1] + radii * np.sin(angle)))
    buffered = _operational_envelope_local(original_local, predicted_local,
                                            buffer_km=buffer_km)
    lat, lon = map(float, centroid)
    scale = 111.0 * math.cos(math.radians(lat))
    coords = [[float(lon + east / scale) % 360.0,
               float(lat + north / 111.0)]
              for east, north in list(buffered.exterior.coords)]
    return {"type": "Polygon", "coordinates": [coords]}


def _to_local(polygon_latlon, centroid_latlon):
    """Local conversion kept private to avoid coupling to detection geometry."""
    points = np.asarray(polygon_latlon, dtype=np.float64)
    centroid = np.asarray(centroid_latlon, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 3:
        raise ValueError("invalid original polygon")
    if centroid.shape != (2,) or not np.all(np.isfinite(points)):
        raise ValueError("invalid operational geometry coordinates")
    scale = 111.0 * math.cos(math.radians(float(centroid[0])))
    if not math.isfinite(scale) or abs(scale) < 1e-6:
        raise ValueError("invalid longitude scale")
    east = ((points[:, 1] - centroid[1] + 180.0) % 360.0 - 180.0) * scale
    north = (points[:, 0] - centroid[0]) * 111.0
    return np.column_stack((east, north)), False


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
