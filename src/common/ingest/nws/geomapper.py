
import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence, cast
from shapely.geometry import Polygon, MultiPolygon, shape
from shapely.ops import unary_union

from common.config.loader import load_config, repo_root
from .config import geometry_precision, junk_keys, simplify_tolerance


def _assets_dir() -> Path:
    """Where zone polygons are read from: ``nws.yaml zone_sync.assets_dir``.

    The same key, resolved the same way, that zone_sync.py writes them to. This
    used to walk up from ``__file__`` for any existing ``assets/nws_zones`` and
    ignore the catalog, which agreed with the writer only for an unrelocated
    tree -- under ``--config-dir``, ``EDGEWARN_CONFIG_DIR``, ``--assets-dir``, or
    an edited catalog value, the sync populated one directory and lookups read
    another, and a miss here is silent: alerts lose their polygons.

    Resolved per call, not at import, because the config root is exported into
    the environment after this module is imported.
    """
    zone_sync_cfg = load_config("nws")["zone_sync"]
    configured = repo_root() / zone_sync_cfg["assets_dir"]

    # A mounted/operator-managed tree wins whenever it contains assets. Docker
    # images built with EDGEWARN_SYNC_NWS_ZONES=true carry a fallback snapshot
    # outside that mount so an empty bind mount cannot hide the build output.
    if configured.is_dir() and any(configured.rglob("zones.json")):
        return configured

    bundled_value = os.environ.get("EDGEWARN_BUNDLED_NWS_ZONES_DIR")
    if bundled_value:
        bundled = Path(bundled_value)
        if bundled.is_dir() and any(bundled.rglob("zones.json")):
            return bundled

    return configured


_BOOTSTRAPPED = False


def _reset_bootstrap() -> None:
    """Clear the per-process bootstrap flag. Test-only."""
    global _BOOTSTRAPPED
    _BOOTSTRAPPED = False


def ensure_zone_assets() -> None:
    """Require pre-synchronized NWS zone assets before serving lookups.

    Zone assets are an explicit operational prerequisite.  Fetching thousands
    of zones from an alert-processing worker delays startup unpredictably and
    can leave the registry only partially mapped after a failed bootstrap.
    Use ``edgewarn sync-nws-zones --apply`` to create or refresh the assets
    before starting a pipeline.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    assets = _assets_dir()
    has_data = assets.is_dir() and any(assets.rglob("zones.json"))
    if not has_data:
        raise RuntimeError(
            f"NWS zone assets are missing from {assets}. "
            "Run `edgewarn sync-nws-zones --apply` before starting the pipeline."
        )

    _BOOTSTRAPPED = True


def _ensure_zone_assets() -> None:
    """Backward-compatible private alias for :func:`ensure_zone_assets`."""
    ensure_zone_assets()


class ZoneLookup:
    """Lazy-loading lookup for NWS zone polygons."""
    
    _cache: Dict[str, Dict[str, List]] = {}  # {state_code: {zone_code: polygon_coords}}
    
    @classmethod
    def get_polygon(cls, zone_code: str) -> Optional[List]:
        """Get polygon coordinates for a zone code."""
        if len(zone_code) < 2:
            return None
            
        state_code = zone_code[:2]
        
        if state_code not in cls._cache:
            cls._load_state(state_code)
        
        return cls._cache.get(state_code, {}).get(zone_code)
    
    @classmethod
    def _load_state(cls, state_code: str) -> None:
        """Load zone data for a state into cache."""
        ensure_zone_assets()
        state_file = _assets_dir() / state_code / "zones.json"
        
        if not state_file.exists():
            cls._cache[state_code] = {}
            return
        
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                zones = json.load(f)
            
            cls._cache[state_code] = {
                zone["code"]: zone["Polygon"]
                for zone in zones
                if "code" in zone and "Polygon" in zone
            }
        except Exception:
            cls._cache[state_code] = {}

def round_coords(coords: Sequence[Any], precision: Optional[int] = None) -> List[List[float]]:
    """Round coordinates to a specified precision.

    ``precision`` defaults to ``nws.yaml geomapper.geometry_precision``. It is a
    parameter rather than a plain read so a caller already holding the value can
    pass it down instead of re-resolving it per ring; ``None`` must not become a
    literal here, because this was one of four signatures that each restated the
    same default and had to be edited together.
    """
    if precision is None:
        precision = geometry_precision()
    return [[round(float(c[0]), precision), round(float(c[1]), precision)] for c in coords]


def _normalize_ring(coords: Sequence[Any], precision: Optional[int] = None) -> List[List[float]]:
    """Normalize one linear ring: round, filter bad points, and close ring."""
    if precision is None:
        precision = geometry_precision()
    normalized: List[List[float]] = []
    for point in coords:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            lon = round(float(point[0]), precision)
            lat = round(float(point[1]), precision)
        except Exception:
            continue
        normalized.append([lon, lat])

    if len(normalized) < 3:
        return []

    if normalized[0] != normalized[-1]:
        normalized.append(normalized[0])

    if len(normalized) < 4:
        return []

    return normalized


def _geometry_to_polygon_rings(geometry: Dict[str, Any], precision: Optional[int] = None) -> List[List[List[float]]]:
    """Extract exterior polygon rings from Polygon/MultiPolygon GeoJSON geometry."""
    if not geometry or not isinstance(geometry, dict):
        return []

    if precision is None:
        precision = geometry_precision()

    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return []

    rings: List[List[List[float]]] = []

    if gtype == "Polygon":
        if not coords:
            return []
        ring = _normalize_ring(coords[0], precision=precision)
        if ring:
            rings.append(ring)
        return rings

    if gtype == "MultiPolygon":
        for polygon_coords in coords:
            if not isinstance(polygon_coords, list) or not polygon_coords:
                continue
            ring = _normalize_ring(polygon_coords[0], precision=precision)
            if ring:
                rings.append(ring)
        return rings

    return []

def extract_exterior_polygon(
    polygons: List[List],
    tolerance: Optional[float] = None,
    precision: Optional[int] = None,
) -> List:
    """
    Compute the union of multiple polygons, simplify geometry,
    and return only exterior coordinates with rounded precision.

    ``tolerance`` defaults to ``nws.yaml geomapper.simplify_tolerance`` and
    ``precision`` to ``geomapper.geometry_precision``. Both are resolved once
    here and the precision is passed down, so a MultiPolygon does not re-read
    the catalog per part.
    """
    if not polygons:
        return []

    if tolerance is None:
        tolerance = simplify_tolerance()
    if precision is None:
        precision = geometry_precision()

    shapely_polys = []
    
    for poly_coords in polygons:
        if not poly_coords:
            continue
        try:
            if len(poly_coords) >= 3:
                if isinstance(poly_coords[0], (list, tuple)) and len(poly_coords[0]) == 2:
                    shapely_polys.append(Polygon(poly_coords))
        except Exception:
            continue
    
    if not shapely_polys:
        return []
    
    try:
        unified = unary_union(shapely_polys)
        unified = unified.buffer(0)
        
        # Simplify geometry to reduce point count (especially for coastlines/rivers)
        if tolerance > 0:
            unified = unified.simplify(tolerance=tolerance, preserve_topology=True)
        
        if unified.geom_type == 'Polygon':
            polygon = cast(Polygon, unified)
            return [round_coords(list(polygon.exterior.coords), precision)]
        elif unified.geom_type == 'MultiPolygon':
            multipolygon = cast(MultiPolygon, unified)
            return [round_coords(list(p.exterior.coords), precision) for p in multipolygon.geoms]
        else:
            return []
    except Exception:
        return []

def round_geojson_coords(geometry: Dict[str, Any], precision: Optional[int] = None) -> Dict[str, Any]:
    """Round coordinates in a GeoJSON geometry object."""
    if not geometry or 'coordinates' not in geometry:
        return geometry

    if precision is None:
        precision = geometry_precision()

    def _round_recursive(coords):
        if isinstance(coords, (int, float)):
            return round(float(coords), precision)
        elif isinstance(coords, (list, tuple)):
            return [_round_recursive(c) for c in coords]
        return coords
    
    geometry['coordinates'] = _round_recursive(geometry['coordinates'])
    return geometry


def polygon_to_geojson(polygon_coords: List[List]) -> Dict[str, Any]:
    """Convert GeoMapper polygon rings into Polygon/MultiPolygon GeoJSON geometry."""
    rings: List[List[List[float]]] = []
    for ring in polygon_coords:
        normalized = _normalize_ring(ring)
        if normalized:
            rings.append(normalized)

    if not rings:
        return {}

    if len(rings) == 1:
        return {
            "type": "Polygon",
            "coordinates": [rings[0]],
        }

    return {
        "type": "MultiPolygon",
        "coordinates": [[ring] for ring in rings],
    }


def _repair_generated_geometry(geometry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate/repair generated geometry and return normalized GeoJSON output."""
    if not geometry or "coordinates" not in geometry:
        return None

    try:
        geom_obj = shape(geometry)
    except Exception:
        return None

    if geom_obj.is_empty:
        return None

    if not geom_obj.is_valid:
        try:
            geom_obj = geom_obj.buffer(0)
        except Exception:
            return None
        if geom_obj.is_empty or not geom_obj.is_valid:
            return None

    if geom_obj.geom_type == "Polygon":
        polygon = cast(Polygon, geom_obj)
        ring = _normalize_ring(list(polygon.exterior.coords))
        if not ring:
            return None
        return {
            "type": "Polygon",
            "coordinates": [ring],
        }

    if geom_obj.geom_type == "MultiPolygon":
        multipolygon = cast(MultiPolygon, geom_obj)
        rings: List[List[List[float]]] = []
        for polygon in multipolygon.geoms:
            ring = _normalize_ring(list(polygon.exterior.coords))
            if ring:
                rings.append(ring)

        if not rings:
            return None

        if len(rings) == 1:
            return {
                "type": "Polygon",
                "coordinates": [rings[0]],
            }

        return {
            "type": "MultiPolygon",
            "coordinates": [[ring] for ring in rings],
        }

    return None

def process_warning(feature: Dict[str, Any]) -> Dict[str, Any]:
    """Process a single NWS warning feature (Map Geocodes + Clean Props)."""
    props = feature.get("properties", {})
    
    # Check for original geometry (e.g. storm-based warnings)
    has_geometry_to_skip = False
    if feature.get("geometry") and feature.get("geometry", {}).get("coordinates"):
        # Round existing geometry coordinates but DO NOT simplify
        feature["geometry"] = round_geojson_coords(feature["geometry"])
        has_geometry_to_skip = True
    
    # Check if we already have a zone-mapped Polygon (prevents double simplification/mapping)
    if feature.get("Polygon"):
        normalized_rings = []
        for ring in feature["Polygon"]:
            normalized = _normalize_ring(ring)
            if normalized:
                normalized_rings.append(normalized)

        if normalized_rings:
            feature["Polygon"] = normalized_rings
            if not feature.get("geometry"):
                geometry = polygon_to_geojson(normalized_rings)
                repaired_geometry = _repair_generated_geometry(geometry)
                if repaired_geometry:
                    feature["geometry"] = repaired_geometry
                    feature["Polygon"] = _geometry_to_polygon_rings(repaired_geometry)
                    has_geometry_to_skip = True
                else:
                    feature.pop("geometry", None)
                    feature.pop("Polygon", None)
            else:
                has_geometry_to_skip = True
        else:
            feature.pop("Polygon", None)

    # If geometry exists, skip the zone-to-polygon mapping 
    # (prevents simplification of precise polygons into zone boundaries)
    if has_geometry_to_skip:
        props.pop("geocode", None)
        props.pop("affectedZones", None)
        for key in junk_keys():
            props.pop(key, None)
        return feature

    # Extract geocodes for zone mapping
    geocodes = []
    geocode_data = props.get("geocode", {})
    
    if isinstance(geocode_data, dict):
        ugc_codes = geocode_data.get("UGC", [])
        if ugc_codes:
            geocodes = ugc_codes
    elif isinstance(geocode_data, list):
        geocodes = geocode_data
    
    # Collect all polygon coordinates from matching zones
    all_polygon_coords = []
    
    for code in geocodes:
        poly = ZoneLookup.get_polygon(code)
        if poly:
            all_polygon_coords.extend(poly)
    
    # Compute union and extract exterior
    if all_polygon_coords:
        # Optimization: NWS alerts often cover the same sets of zones.
        # Cache the result of the union operation based on the sorted tuple of zone codes.
        # We need to extract just the codes to form a cache key.
        zone_codes_tuple = tuple(sorted(geocodes))
        
        exterior = _get_cached_union_exterior(zone_codes_tuple)
        if exterior:
            geometry = polygon_to_geojson(exterior)
            repaired_geometry = _repair_generated_geometry(geometry)
            if repaired_geometry:
                feature["geometry"] = repaired_geometry
                feature["Polygon"] = _geometry_to_polygon_rings(repaired_geometry)
             
    # Remove "geocode" if valid geometry exists
    has_geometry = False
    if feature.get("geometry") and feature.get("geometry", {}).get("coordinates"):
        has_geometry = True
    if feature.get("Polygon"):
        has_geometry = True
        
    if has_geometry:
        props.pop("geocode", None)
        props.pop("affectedZones", None)
    
    # Remove junk keys from properties
    for key in junk_keys():
        props.pop(key, None)
    
    return feature

# Helper for caching union operations
from functools import lru_cache

@lru_cache(maxsize=1024)
def _get_cached_union_exterior(zone_codes_tuple):
    """
    Cached helper to compute union of zones.
    Args:
        zone_codes_tuple: Sorted tuple of zone codes
    Returns:
        List of exterior coordinates
    """
    if not zone_codes_tuple:
        return []

    all_poly_coords = []
    for code in zone_codes_tuple:
        poly = ZoneLookup.get_polygon(code)
        if poly:
            all_poly_coords.extend(poly)
    
    if not all_poly_coords:
        return []
        
    return extract_exterior_polygon(all_poly_coords)
