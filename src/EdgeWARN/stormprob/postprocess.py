"""Versioned float32 StormProb joint sampling and instantaneous contour construction.

Input arrays are the paired ONNX outputs and committed observation geometry.
Each of the 20 ensemble members pairs an independent Fourier shape draw with an
independent Gaussian centroid draw. By default both come from one NumPy PCG64(42)
stream: shape noise ``[1,20,4,33]`` first, then centroid noise ``[1,20,4,2]``.
Explicit noise permits exact replay of the PyTorch evaluation ensemble during
parity validation. Probability is the raw member fraction ``k/20``; no
calibration is applied. A lead contour is never interpreted as a swept envelope;
``swept_member_mask`` instead unions each member's footprint across the
T+15..T+60 window on the caller's axes, leaving the lead polygons untouched.
"""
from __future__ import annotations

import math

import numpy as np


VERSION = "stormprob-postprocess/v4"
OPERATIONAL_GEOMETRY_VERSION = "stormprob-operational-envelope/v2"
LEADS_MINUTES = (15, 30, 45, 60)
ENSEMBLE_SIZE = 20
GRID_HALF_WIDTH_KM = 100
GRID_RESOLUTION_KM = 1
THRESHOLD = 0.25
MIN_SAMPLED_RADIUS_KM = 0.1
SWEEP_STEP_MINUTES = 5
SWEEP_START_MINUTES = LEADS_MINUTES[0]
SWEEP_END_MINUTES = LEADS_MINUTES[-1]
_RASTER_BAND_ROWS = 64
_RASTER_MEMBER_BUCKETS = 4
_RASTER_REACH_SPREAD = 0.10
OPERATIONAL_BUFFER_KM = 1.0
OPERATIONAL_MIN_POINTS = 4
OPERATIONAL_MAX_POINTS = 12
OPERATIONAL_MAX_AREA_INFLATION = 1.25


def _sweep_fractions() -> tuple[np.float32, ...]:
    """Fractions of one lead interval at the configured sweep step."""
    gap = LEADS_MINUTES[1] - LEADS_MINUTES[0]
    if SWEEP_STEP_MINUTES <= 0 or gap % SWEEP_STEP_MINUTES:
        raise ValueError("StormProb sweep step must divide the lead spacing")
    steps = gap // SWEEP_STEP_MINUTES
    return tuple(np.float32(index) / np.float32(steps) for index in range(1, steps + 1))


SWEEP_FRACTIONS = _sweep_fractions()


def sample_radii(mean, log_std, current_radii, *, noise=None):
    """Return `[1,20,4,64]` radii with the joint model's 0.1 km floor."""
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
    radii = current[:, None, None, :] + np.float32(30) * np.tanh(decoded / np.float32(30))
    return np.maximum(radii, np.float32(MIN_SAMPLED_RADIUS_KM))


def sample_joint(mean, log_std, current_radii, base_km, offset_km, cholesky, *,
                 noise=None):
    """Return paired member radii `[1,20,4,64]` and centres `[1,20,4,2]` in km.

    Centre = base + offset + L @ z, with L the lower-triangular Cholesky factor.
    ``noise`` is an optional ``(shape_noise, centroid_noise)`` pair.
    """
    base = np.asarray(base_km, dtype=np.float32)
    offset = np.asarray(offset_km, dtype=np.float32)
    chol = np.asarray(cholesky, dtype=np.float32)
    if base.shape != (1, 4, 2) or offset.shape != base.shape or chol.shape != (1, 4, 2, 2):
        raise ValueError("Invalid StormProb centroid shape")
    if noise is None:
        rng = np.random.default_rng(42)
        shape_noise = rng.standard_normal((1, ENSEMBLE_SIZE, 4, 33), dtype=np.float32)
        centroid_noise = rng.standard_normal((1, ENSEMBLE_SIZE, 4, 2), dtype=np.float32)
    else:
        shape_noise, centroid_noise = noise
    centroid_noise = np.asarray(centroid_noise, dtype=np.float32)
    if centroid_noise.shape != (1, ENSEMBLE_SIZE, 4, 2):
        raise ValueError("Invalid StormProb centroid noise shape")
    radii = sample_radii(mean, log_std, current_radii, noise=shape_noise)
    centres = (base + offset)[:, None] + (chol[:, None] @ centroid_noise[..., None])[..., 0]
    return radii, centres


def occupancy_probability(radii, centres_km):
    """Return four raw 201x201 member-fraction probabilities, members about own centres."""
    radii = np.asarray(radii, dtype=np.float32)
    centres = np.asarray(centres_km, dtype=np.float32)
    if radii.shape != (1, ENSEMBLE_SIZE, 4, 64) or centres.shape != (1, ENSEMBLE_SIZE, 4, 2):
        raise ValueError("Invalid StormProb radii or centre shape")
    axis = np.arange(-GRID_HALF_WIDTH_KM, GRID_HALF_WIDTH_KM + 1, dtype=np.float32)
    counts = np.zeros((4, 201, 201), dtype=np.uint8)
    for lead in range(4):
        for member in range(ENSEMBLE_SIZE):
            cx, cy = centres[0, member, lead]
            # Pixels beyond the member's largest ray cannot be occupied, so
            # rasterize only its bounding box (1 km margin); the result is unchanged.
            reach = float(np.max(radii[0, member, lead])) + 1.0
            columns = _grid_window(axis, cx, reach)
            rows = _grid_window(axis, cy, reach)
            if columns.start >= columns.stop or rows.start >= rows.stop:
                continue
            east, north = np.meshgrid(axis[columns], axis[rows], indexing="xy")
            dx = east - cx
            dy = north - cy
            angle = np.mod(np.arctan2(dy, dx), np.float32(2 * math.pi)) * np.float32(64 / (2 * math.pi))
            floor = np.floor(angle)
            lo = floor.astype(np.int64) % 64
            hi = (lo + 1) % 64
            frac = angle - floor
            distance = np.sqrt(dx * dx + dy * dy)
            profile = np.maximum(radii[0, member, lead], 0.0)
            interpolated = profile[lo] + (profile[hi] - profile[lo]) * frac
            counts[lead, rows, columns] += distance <= interpolated
    return counts.astype(np.float32) / np.float32(ENSEMBLE_SIZE)


def _grid_window(axis, centre, reach):
    """Index slice of grid coordinates within [centre - reach, centre + reach]."""
    return _axis_window(axis, centre - reach, centre + reach)


def _axis_window(axis, low, high):
    """Index slice of ascending axis coordinates within [low, high]."""
    start = int(np.searchsorted(axis, np.float32(low), side="left"))
    stop = int(np.searchsorted(axis, np.float32(high), side="right"))
    return slice(start, stop)


def swept_member_mask(radii, centres_km, east_axis_km, north_axis_km):
    """Return per-member swept coverage as a ``uint32`` bitmask patch.

    Bit ``m`` is set where ensemble member ``m`` covers the point at any instant
    in ``[T+15, T+60]``. Between leads each member's radii and centre are
    interpolated by member index in five-minute steps, matching the swept branch
    of the StormProb evaluation scorecard. ``east_axis_km`` and
    ``north_axis_km`` are ascending kilometres about the cell's analysis
    centroid, so longitude scaling is the caller's separable projection. Radii
    are floored at the joint model's 0.1 km minimum and are never negative.
    """
    radii = np.maximum(np.asarray(radii, dtype=np.float32),
                       np.float32(MIN_SAMPLED_RADIUS_KM))
    centres = np.asarray(centres_km, dtype=np.float32)
    if radii.shape != (1, ENSEMBLE_SIZE, 4, 64) or centres.shape != (1, ENSEMBLE_SIZE, 4, 2):
        raise ValueError("Invalid StormProb radii or centre shape")
    east = np.array(east_axis_km, dtype=np.float32, copy=True).reshape(-1)
    north = np.array(north_axis_km, dtype=np.float32, copy=True).reshape(-1)
    for name, axis in (("east", east), ("north", north)):
        if axis.size == 0:
            raise ValueError(f"Empty StormProb {name} axis")
        if not np.all(np.isfinite(axis)) or np.any(np.diff(axis) <= 0):
            raise ValueError(f"StormProb {name} axis must be finite and ascending")
    mask = np.zeros((north.size, east.size), dtype=np.uint32)
    bits = np.arange(ENSEMBLE_SIZE, dtype=np.uint32)[:, None, None]
    for member_radii, member_centres in _sweep_instants(radii, centres):
        _rasterize_instant(mask, member_radii, member_centres, east, north, bits)
    return mask


def _sweep_instants(radii, centres):
    """Yield the ten ``[20,64]`` radii / ``[20,2]`` centre blocks of the window."""
    radii = radii[0]
    centres = centres[0]
    yield radii[:, 0], centres[:, 0]
    for lead in range(len(LEADS_MINUTES) - 1):
        for fraction in SWEEP_FRACTIONS:
            remaining = np.float32(1) - fraction
            yield (radii[:, lead] * remaining + radii[:, lead + 1] * fraction,
                   centres[:, lead] * remaining + centres[:, lead + 1] * fraction)


def _rasterize_instant(mask, radii, centres, east, north, bits):
    """OR one instant's all-member coverage into the swept bitmask patch.

    Members are swept one reach bucket at a time, and each bucket handles all its
    members in one vectorized call per row band. Bucketing keeps a compact
    member from paying for the bounding box of a member that has travelled far,
    while every member keeps the same test, so the grouping cannot change the
    result. Pixels beyond a member's largest ray are rejected before the angular
    interpolation.
    """
    reach = np.max(radii, axis=1)
    cx = centres[:, 0]
    cy = centres[:, 1]
    for members in _reach_buckets(reach):
        _rasterize_members(mask, radii[members], cx[members], cy[members],
                           east, north, bits[members])


def _reach_buckets(reach):
    """Group member indices by similar reach, at most ``_RASTER_MEMBER_BUCKETS``."""
    order = np.argsort(reach, kind="stable")
    top = float(reach[order[-1]])
    if top <= 0.0 or top - float(reach[order[0]]) <= max(1.0, _RASTER_REACH_SPREAD * top):
        return [order]
    limit = max(1.0, _RASTER_REACH_SPREAD * top)
    groups = []
    start = 0
    base = float(reach[order[0]])
    for position in range(1, order.size):
        value = float(reach[order[position]])
        if value - base > limit:
            groups.append(order[start:position])
            start = position
            base = value
    groups.append(order[start:])
    if len(groups) > _RASTER_MEMBER_BUCKETS:
        size = math.ceil(order.size / _RASTER_MEMBER_BUCKETS)
        groups = [order[index:index + size] for index in range(0, order.size, size)]
    return groups


def _rasterize_members(mask, radii, cx, cy, east, north, bits):
    """OR one reach bucket's coverage into the swept bitmask patch."""
    if radii.shape[0] == 0:
        return
    reach = np.max(radii, axis=1)
    columns = _axis_window(east, float(np.min(cx - reach)), float(np.max(cx + reach)))
    rows = _axis_window(north, float(np.min(cy - reach)), float(np.max(cy + reach)))
    if columns.start >= columns.stop or rows.start >= rows.stop:
        return
    dx = east[columns][None, None, :] - cx[:, None, None]
    members = np.arange(radii.shape[0], dtype=np.intp)[:, None, None]
    for start in range(rows.start, rows.stop, _RASTER_BAND_ROWS):
        stop = min(rows.stop, start + _RASTER_BAND_ROWS)
        dy = north[start:stop][None, :, None] - cy[:, None, None]
        # Square-then-sqrt replaces the per-element libm ``hypot``; patch
        # windows are at most a few hundred km, so float32 cannot overflow.
        distance = np.square(dx) + np.square(dy)
        np.sqrt(distance, out=distance)
        inside = distance <= reach[:, None, None]
        if not inside.any():
            continue
        angle = np.zeros(distance.shape, dtype=np.float32)
        np.arctan2(dy, dx, out=angle, where=inside)
        np.multiply(angle, np.float32(64 / (2 * math.pi)), out=angle)
        # arctan2 spans (-pi, pi], i.e. (-32, 32] rays, so one conditional add
        # wraps the negatives as remainder(angle, 64) did, without the full-array
        # float modulo that dominates a wide-spread raster. Rounding can leave a
        # value just below a ray boundary at exactly 64.0; the integer wrap below
        # maps that back to ray 0, as the reference arithmetic did.
        np.add(angle, np.float32(64), out=angle, where=angle < 0)
        floor = np.floor(angle)
        # ``angle`` becomes the fractional position between the two adjacent rays.
        np.subtract(angle, floor, out=angle)
        lo = floor.astype(np.intp)
        lo[lo == 64] = 0
        hi = np.mod(lo + 1, 64)
        low_radius = radii[members, lo]
        high_radius = radii[members, hi]
        interpolated = low_radius + (high_radius - low_radius) * angle
        inside &= distance <= interpolated
        packed = inside.astype(np.uint32)
        np.left_shift(packed, bits, out=packed)
        mask[start:stop, columns] |= np.bitwise_or.reduce(packed, axis=0)


def base_displacement(initial_wind_mps, residual_motion_mps):
    """Return frozen-motion east/north displacement in km for the four lead times."""
    initial = np.asarray(initial_wind_mps, dtype=np.float32)
    residual = np.asarray(residual_motion_mps, dtype=np.float32)
    if initial.shape != (2,) or residual.shape != (1, 4, 2):
        raise ValueError("Invalid StormProb wind or residual shape")
    seconds = np.asarray(LEADS_MINUTES, dtype=np.float32) * np.float32(60)
    return ((initial[None] + residual[0]) * seconds[:, None] / np.float32(1000))[None]


def contour_polygon(probability, threshold=THRESHOLD):
    """Return the largest >=threshold component exterior as local km points, or None."""
    probability = np.asarray(probability, dtype=np.float32)
    if probability.shape != (201, 201):
        raise ValueError("Invalid StormProb occupancy grid shape")
    polygons = _mask_polygons_local(probability >= np.float32(threshold))
    if not polygons:
        return None
    largest = max(polygons, key=lambda polygon: polygon.area)
    return np.asarray(largest.exterior.coords[:-1], dtype=np.float64)


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


def _reduced_outer_hull(envelope, vertex_limit):
    """Remove the least costly convex-hull edges without excluding the hull."""
    from shapely.geometry import Polygon

    coords = np.asarray(envelope.exterior.coords[:-1], dtype=np.float64)
    while len(coords) > vertex_limit:
        best = None
        count = len(coords)
        for index in range(count):
            a, b = coords[(index - 1) % count], coords[index]
            c, d = coords[(index + 1) % count], coords[(index + 2) % count]
            first, second = b - a, d - c
            denominator = float(np.linalg.det(np.stack((first, second))))
            if abs(denominator) < 1e-10:
                continue
            intersection = a + (float(np.linalg.det(np.stack((c - a, second))))
                                / denominator) * first
            if not np.all(np.isfinite(intersection)):
                continue
            candidate_coords = np.asarray([
                intersection if i == index else coords[i]
                for i in range(count) if i != (index + 1) % count
            ])
            candidate = Polygon(candidate_coords)
            if not candidate.is_valid or not candidate.covers(envelope):
                continue
            if best is None or candidate.area < best[0]:
                best = (candidate.area, candidate_coords)
        if best is None:
            return None
        coords = best[1]
    return Polygon(coords)


def _simplified_outer_hull(envelope, vertex_limit, reference, buffer_km):
    """Find a low-vertex simplification whose buffered form still covers hull."""
    extent = max(float(envelope.bounds[2] - envelope.bounds[0]),
                 float(envelope.bounds[3] - envelope.bounds[1]), 1.0)
    # Douglas-Peucker tolerances are deterministic and span sub-grid detail to
    # the full local footprint.  The buffered candidate is what must preserve
    # containment, so simplification may remove vertices inside the 1 km margin.
    tolerances = np.geomspace(1e-3, extent * 2.0, num=64)
    best = None
    for tolerance in tolerances:
        candidate = envelope.simplify(float(tolerance), preserve_topology=True)
        if candidate.geom_type != "Polygon" or not candidate.is_valid:
            continue
        if not OPERATIONAL_MIN_POINTS <= _vertex_count(candidate) <= vertex_limit:
            continue
        buffered = candidate.buffer(float(buffer_km), join_style=2)
        if (buffered.geom_type != "Polygon" or not buffered.is_valid
                or not buffered.covers(envelope)):
            continue
        inflation = buffered.area / max(reference.area, 1e-9)
        if inflation <= OPERATIONAL_MAX_AREA_INFLATION:
            score = (inflation, _vertex_count(buffered))
            if best is None or score < best[0]:
                best = (score, buffered)
    return None if best is None else best[1]


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
    # Equal-angle support lines can miss a tight fit even when the original
    # convex hull has a compliant outer simplification.
    candidate = _reduced_outer_hull(envelope, OPERATIONAL_MAX_POINTS)
    if candidate is not None:
        buffered = candidate.buffer(float(buffer_km), join_style=2)
        if (buffered.geom_type == "Polygon" and buffered.is_valid
                and OPERATIONAL_MIN_POINTS <= _vertex_count(buffered) <= OPERATIONAL_MAX_POINTS
                and buffered.covers(envelope)
                and buffered.area / max(reference.area, 1e-9) <= OPERATIONAL_MAX_AREA_INFLATION):
            return buffered
    simplified = _simplified_outer_hull(
        envelope, OPERATIONAL_MAX_POINTS, reference, float(buffer_km))
    if simplified is not None:
        return simplified
    raise ValueError("cannot fit forecast envelope within operational point and area limits")


def operational_envelope(original_polygon_latlon, centroid_latlon, contour_local_km, *,
                         buffer_km=OPERATIONAL_BUFFER_KM):
    """Return an adaptive 4-12 point, 1 km buffered forecast envelope.

    The envelope spans the original detection polygon and the 0.25 occupancy
    contour for one lead (local east/north km about ``centroid_latlon``). Four
    points are used when the shape fits; additional support points are added up
    to twelve when needed to keep buffered area inflation within the
    operational tolerance. The result is GeoJSON in [longitude, latitude].
    """
    original_local, _ = _to_local(original_polygon_latlon, centroid_latlon)
    centroid = np.asarray(centroid_latlon, dtype=np.float64)
    predicted_local = np.asarray(contour_local_km, dtype=np.float64)
    if (predicted_local.ndim != 2 or predicted_local.shape[1] != 2
            or not np.all(np.isfinite(predicted_local))):
        raise ValueError("invalid operational forecast contour")
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


def _mask_polygons_local(mask):
    """Polygonize one 201x201 grid mask into shapely polygons in local km."""
    from affine import Affine
    from rasterio.features import shapes
    from shapely.geometry import shape

    transform = Affine.translation(-100.5, 100.5) * Affine.scale(1, -1)
    # Rasterio expects north-to-south rows; occupancy rows are ascending north.
    image = np.ascontiguousarray(np.asarray(mask, dtype=np.bool_)[::-1].astype(np.uint8))
    if not image.any():
        return []
    return [shape(geometry) for geometry, value in
            shapes(image, mask=image.astype(np.bool_), transform=transform) if value == 1]


def polygons_from_masks(masks, centroid_latlon):
    """Polygonize each instantaneous mask into valid GeoJSON lon/lat rings."""
    from shapely.affinity import translate
    from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape

    masks = np.asarray(masks, dtype=np.bool_)
    lat, lon = map(float, centroid_latlon)
    if masks.shape != (4, 201, 201) or not (-90 < lat < 90) or not math.isfinite(lon):
        raise ValueError("Invalid StormProb mask or centroid")
    results = []
    for lead in range(4):
        if not masks[lead].any():
            results.append({"status": "no-polygon:empty-0.25-contour", "geometry": None})
            continue
        polygons = []
        for local in _mask_polygons_local(masks[lead]):
            rings = []
            for ring in (local.exterior, *local.interiors):
                geographic = []
                for east_km, north_km in ring.coords:
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
