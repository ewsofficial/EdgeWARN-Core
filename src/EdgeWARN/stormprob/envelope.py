"""0-30 minute swept envelope derived from instantaneous StormProb footprints.

This is a separate alert product. Instantaneous 15/30-minute probability
contours are not the intervening-path coverage. Construction matches the
StormProb evaluation script: linearly interpolate centroids every 5 minutes
from analysis time through 30 minutes, translate the neighboring instantaneous
footprints, and union the results with the current detection polygon.
"""
from __future__ import annotations

import math
from typing import Any

from shapely.affinity import translate
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

STEP_MINUTES = 5
SWEEP_MINUTES = 30
GEOMETRY_KIND = "swept-envelope-0-30min"
CONSTRUCTION = (
    "union of current detection polygon, 15/30-minute instantaneous 0.25 "
    "contours, and 5-minute centroid-path translations of those footprints"
)


def _closed_lonlat_ring(coords: list[list[float]]) -> list[list[float]]:
    ring = [[float(lon), float(lat)] for lon, lat in coords]
    if not ring:
        return ring
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def polygon_from_latlon_ring(points: Any):
    """Build a shapely polygon from EdgeWARN `[lat, lon]` rings."""
    if not points:
        return None
    ring = _closed_lonlat_ring([[float(pt[1]), float(pt[0])] for pt in points])
    if len(ring) < 4:
        return None
    geometry = shape({"type": "Polygon", "coordinates": [ring]})
    if geometry.is_empty or not geometry.is_valid:
        geometry = geometry.buffer(0)
    return None if geometry.is_empty else geometry


def polygon_from_geojson(geometry: dict | None):
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    item = shape(geometry)
    if item.is_empty:
        return None
    if not item.is_valid:
        item = item.buffer(0)
    return None if item.is_empty else item


def _centroid_xy(polygon) -> tuple[float, float]:
    point = polygon.centroid
    return float(point.x), float(point.y)


def _translate_to(polygon, origin: tuple[float, float], target: tuple[float, float]):
    return translate(polygon, xoff=target[0] - origin[0], yoff=target[1] - origin[1])


def _lerp(start: tuple[float, float], end: tuple[float, float], fraction: float):
    return (start[0] + (end[0] - start[0]) * fraction,
            start[1] + (end[1] - start[1]) * fraction)


def swept_envelope_0_30(*, current_polygon, lead_15, lead_30) -> dict[str, Any]:
    """Union a 0-30 minute path envelope. Empty contours are not fabricated."""
    current = polygon_from_latlon_ring(current_polygon) or polygon_from_geojson(current_polygon)
    fifteen = polygon_from_geojson(lead_15)
    thirty = polygon_from_geojson(lead_30)
    if current is None or fifteen is None or thirty is None:
        missing = []
        if current is None:
            missing.append("current")
        if fifteen is None:
            missing.append("lead-15")
        if thirty is None:
            missing.append("lead-30")
        return {"status": "skipped:incomplete-sweep-inputs",
                "reason": ",".join(missing), "geometry": None,
                "geometry_kind": GEOMETRY_KIND, "construction": CONSTRUCTION}

    origin = _centroid_xy(current)
    mid = _centroid_xy(fifteen)
    end = _centroid_xy(thirty)
    pieces = [current, fifteen, thirty]
    for minute in range(STEP_MINUTES, SWEEP_MINUTES, STEP_MINUTES):
        if minute < 15:
            target = _lerp(origin, mid, minute / 15.0)
            pieces.append(_translate_to(current, origin, target))
            pieces.append(_translate_to(fifteen, mid, target))
        elif minute != 15:
            target = _lerp(mid, end, (minute - 15) / 15.0)
            pieces.append(_translate_to(fifteen, mid, target))
            pieces.append(_translate_to(thirty, end, target))
    union = unary_union(pieces)
    if union.is_empty:
        return {"status": "no-polygon:empty-swept-envelope", "reason": "empty-union",
                "geometry": None, "geometry_kind": GEOMETRY_KIND,
                "construction": CONSTRUCTION}
    if not union.is_valid:
        union = union.buffer(0)
    geometry = mapping(union)
    return {"status": "ok", "reason": None, "geometry": geometry,
            "geometry_kind": GEOMETRY_KIND, "construction": CONSTRUCTION}


def geojson_to_alert_ring(geometry: dict | None) -> list[tuple[float, float]]:
    """AlertPayload geometry is a closed `[lat, lon]` ring."""
    polygon = polygon_from_geojson(geometry)
    if polygon is None:
        return []
    if polygon.geom_type == "MultiPolygon":
        polygon = max(polygon.geoms, key=lambda item: item.area)
    ring = _closed_lonlat_ring(list(polygon.exterior.coords))
    return [(lat, lon) for lon, lat in ring]


def normalize_geojson(geometry: dict | None, *, wrap_lon: bool = True) -> dict | None:
    """Force closed rings and the public 0..360 longitude domain."""
    polygon = polygon_from_geojson(geometry)
    if polygon is None:
        return None
    payload = mapping(polygon)

    def _normalize_ring(ring):
        points = []
        for lon, lat in ring:
            if wrap_lon:
                lon = lon % 360.0
            if not math.isfinite(lon) or not math.isfinite(lat):
                continue
            points.append([float(lon), float(lat)])
        return _closed_lonlat_ring(points)

    if payload["type"] == "Polygon":
        payload["coordinates"] = [_normalize_ring(ring) for ring in payload["coordinates"]]
        if len(payload["coordinates"][0]) < 4:
            return None
    elif payload["type"] == "MultiPolygon":
        payload["coordinates"] = [
            [_normalize_ring(ring) for ring in polygon_coords]
            for polygon_coords in payload["coordinates"]
        ]
        payload["coordinates"] = [coords for coords in payload["coordinates"] if len(coords[0]) >= 4]
        if not payload["coordinates"]:
            return None
    return payload
