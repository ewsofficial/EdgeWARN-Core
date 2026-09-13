import gc
import json

import numpy as np
import shapely.vectorized as sv
import util.file as fs
import xarray as xr
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from util.grib_loader import load_grib_fast

from ..geometry.cell_polygon import StormIntegrationUtils
from .constants import (
    azshear_buffer_km,
    azshear_history_window,
    azshear_low_threshold,
    azshear_mid_threshold,
)
from .geometry import (
    buffer_polygon_km,
    distance_km,
    grid_spacing_km,
    polygon_area_km2,
    polygon_major_axis_orientation_deg,
)
from .metrics import (
    extract_azshear_candidates,
    find_best_cross_layer_pair,
    summarize_cross_layer_metrics,
    summarize_level_metrics,
)


def _is_grib_path(dataset_path):
    lower_path = str(dataset_path).lower()
    return lower_path.endswith((".grib2", ".grib", ".grb2", ".grb"))


def _open_azshear_dataset(integrator, dataset_path):
    is_grib = _is_grib_path(dataset_path)
    if is_grib:
        try:
            return load_grib_fast(dataset_path), True
        except Exception as exc:
            integrator.io_manager.write_warning(
                f"Fast GRIB loader failed for AzShear file {dataset_path}; falling back to xarray: {exc}"
            )

    return xr.open_dataset(dataset_path, decode_timedelta=True), is_grib


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_recent_history(cell_id, history_cache):
    if cell_id in history_cache:
        return history_cache[cell_id]

    try:
        from EdgeWARN.stormprob.database import StormProbRepository
        history = StormProbRepository().legacy_history(cell_id, limit=azshear_history_window())
        if history:
            history_cache[cell_id] = history
            return history
    except FileNotFoundError:
        pass

    history_file = fs.CELL_DIR / f"{cell_id}.json"
    if not history_file.exists():
        history_cache[cell_id] = []
        return history_cache[cell_id]

    try:
        with open(history_file, "r") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            history_cache[cell_id] = payload[-azshear_history_window():]
        else:
            history_cache[cell_id] = []
    except Exception:
        history_cache[cell_id] = []

    return history_cache[cell_id]


def _history_level_presence_and_peak(entry, level_key):
    props = entry.get("properties", {}) if isinstance(entry, dict) else {}
    azshear = props.get("azshear", {}) if isinstance(props, dict) else {}
    if not isinstance(azshear, dict):
        return False, 0.0
    level = azshear.get(level_key)
    if not isinstance(level, dict):
        return False, 0.0

    # New schema
    core = level.get("core_structure")
    if isinstance(core, dict):
        component_count = int(_safe_float(core.get("component_count", 0), 0.0))
        peak = _safe_float(core.get("largest_component_peak_azshear", 0.0), 0.0)
        return component_count > 0, peak

    # Legacy schema fallback
    peak = _safe_float(level.get("peak_value", 0.0), 0.0)
    return bool(level), peak


def _compute_level_persistence(history_entries, level_key, threshold):
    if not history_entries:
        return {
            "dominant_component_persistence": 0.0,
            "peak_persistence": 0.0,
        }

    present_count = 0
    peak_count = 0
    for entry in history_entries:
        present, peak = _history_level_presence_and_peak(entry, level_key)
        if present:
            present_count += 1
        if peak >= threshold:
            peak_count += 1

    denom = max(len(history_entries), 1)
    return {
        "dominant_component_persistence": round(present_count / denom, 3),
        "peak_persistence": round(peak_count / denom, 3),
    }


def _compute_simultaneous_persistence(history_entries):
    if not history_entries:
        return 0.0

    both_count = 0
    for entry in history_entries:
        low_present, _ = _history_level_presence_and_peak(entry, "low")
        mid_present, _ = _history_level_presence_and_peak(entry, "mid")
        if low_present and mid_present:
            both_count += 1

    return round(both_count / max(len(history_entries), 1), 3)


def _normalize_search_polygon(raw_poly, geom):
    if geom is None or geom.is_empty:
        return raw_poly

    normalized = geom.buffer(0)
    if normalized.is_empty:
        return raw_poly
    if isinstance(normalized, Polygon):
        return normalized

    if hasattr(normalized, "geoms"):
        intersecting = [part for part in normalized.geoms if part.intersects(raw_poly) or part.covers(raw_poly.centroid)]
        if intersecting:
            merged = unary_union(intersecting).buffer(0)
            if isinstance(merged, Polygon):
                return merged
        largest = max(normalized.geoms, key=lambda part: part.area, default=None)
        if isinstance(largest, Polygon):
            return largest

    return raw_poly


def _build_search_polygons(raw_polys):
    buffer_km = azshear_buffer_km()
    buffered_polys = [buffer_polygon_km(raw_poly, buffer_km) for raw_poly in raw_polys]
    search_polys = []

    for index, raw_poly in enumerate(raw_polys):
        buffered_poly = buffered_polys[index]
        other_buffered = [
            other_poly
            for other_index, other_poly in enumerate(buffered_polys)
            if other_index != index and other_poly is not None and not other_poly.is_empty
        ]
        if not other_buffered:
            search_polys.append(buffered_poly)
            continue

        exclusive_ring = buffered_poly.difference(raw_poly).difference(unary_union(other_buffered))
        search_poly = raw_poly.union(exclusive_ring)
        search_polys.append(_normalize_search_polygon(raw_poly, search_poly))

    return search_polys


def _candidate_pixel_arrays(candidate):
    pixel_lats = candidate.get("_pixel_lats")
    pixel_lons = candidate.get("_pixel_lons")
    if pixel_lats is not None and pixel_lons is not None:
        comp_lats = np.asarray(pixel_lats, dtype=float)
        comp_lons = np.asarray(pixel_lons, dtype=float)
        finite = np.isfinite(comp_lats) & np.isfinite(comp_lons)
        return comp_lats[finite], comp_lons[finite]

    component_mask = candidate.get("_component_mask")
    lat_grid = candidate.get("_lat_grid")
    lon_grid = candidate.get("_lon_grid")
    if component_mask is None or lat_grid is None or lon_grid is None:
        return np.array([], dtype=float), np.array([], dtype=float)

    comp_lats = np.asarray(lat_grid[component_mask], dtype=float)
    comp_lons = np.asarray(lon_grid[component_mask], dtype=float)
    finite = np.isfinite(comp_lats) & np.isfinite(comp_lons)
    return comp_lats[finite], comp_lons[finite]


def _candidate_pixel_signature(candidate):
    cached_signature = candidate.get("_pixel_signature")
    if cached_signature is not None:
        return cached_signature

    comp_lats, comp_lons = _candidate_pixel_arrays(candidate)
    if comp_lats.size == 0 or comp_lons.size == 0:
        signature = frozenset()
        candidate["_pixel_signature"] = signature
        return signature

    lat_vals = np.round(comp_lats, 4)
    lon_vals = np.round(comp_lons, 4)
    signature = frozenset(zip(lat_vals.tolist(), lon_vals.tolist()))
    candidate["_pixel_signature"] = signature
    return signature


def _candidate_pixel_bounds(candidate):
    bounds = candidate.get("_pixel_bbox")
    if bounds is not None:
        return bounds

    comp_lats, comp_lons = _candidate_pixel_arrays(candidate)
    if comp_lats.size == 0 or comp_lons.size == 0:
        return None

    bounds = (
        float(np.nanmin(comp_lons)),
        float(np.nanmin(comp_lats)),
        float(np.nanmax(comp_lons)),
        float(np.nanmax(comp_lats)),
    )
    candidate["_pixel_bbox"] = bounds
    return bounds


def _bounds_overlap(bounds_a, bounds_b):
    if bounds_a is None or bounds_b is None:
        return True

    return not (
        bounds_a[2] < bounds_b[0]
        or bounds_b[2] < bounds_a[0]
        or bounds_a[3] < bounds_b[1]
        or bounds_b[3] < bounds_a[1]
    )


def _record_signature(record):
    signature = record.get("signature")
    if signature is None:
        signature = _candidate_pixel_signature(record["candidate"])
        record["signature"] = signature
    return signature


def _candidate_overlap_fraction(signature_a, signature_b):
    if not signature_a or not signature_b:
        return 0.0

    overlap = len(signature_a & signature_b)
    if overlap <= 0:
        return 0.0

    return overlap / max(min(len(signature_a), len(signature_b)), 1)


def _candidate_duplicate_tolerance_km(candidate_a, candidate_b):
    spacings = [
        float(candidate_a.get("_lat_spacing_km", 0.0)),
        float(candidate_a.get("_lon_spacing_km", 0.0)),
        float(candidate_b.get("_lat_spacing_km", 0.0)),
        float(candidate_b.get("_lon_spacing_km", 0.0)),
    ]
    spacing = max([value for value in spacings if value > 0.0], default=1.0)
    return max(spacing * 1.5, 1.0)


def _candidate_records_match(record_a, record_b):
    if record_a["cell_index"] == record_b["cell_index"]:
        return False

    if not _bounds_overlap(record_a.get("bounds"), record_b.get("bounds")):
        return False

    overlap_fraction = _candidate_overlap_fraction(_record_signature(record_a), _record_signature(record_b))
    if overlap_fraction >= 0.6:
        return True

    peak_distance = distance_km(
        float(record_a["candidate"].get("peak_lat", 0.0)),
        float(record_a["candidate"].get("peak_lon", 0.0)),
        float(record_b["candidate"].get("peak_lat", 0.0)),
        float(record_b["candidate"].get("peak_lon", 0.0)),
    )
    centroid_distance = distance_km(
        float(record_a["candidate"].get("centroid_lat", 0.0)),
        float(record_a["candidate"].get("centroid_lon", 0.0)),
        float(record_b["candidate"].get("centroid_lat", 0.0)),
        float(record_b["candidate"].get("centroid_lon", 0.0)),
    )
    tolerance_km = _candidate_duplicate_tolerance_km(record_a["candidate"], record_b["candidate"])
    return overlap_fraction > 0.0 and peak_distance <= tolerance_km and centroid_distance <= (tolerance_km * 2.0)


def _candidate_record_rank(record):
    candidate = record["candidate"]
    return (
        float(candidate.get("area_km2", 0.0)),
        int(candidate.get("pixel_count", 0)),
        float(candidate.get("peak_value", 0.0)),
    )


def _group_candidate_records(candidate_records):
    groups = []
    ordered_records = sorted(candidate_records, key=_candidate_record_rank, reverse=True)
    for record in ordered_records:
        matched_group = None
        for group in groups:
            if _candidate_records_match(record, group["representative"]):
                matched_group = group
                break

        if matched_group is None:
            groups.append({
                "representative": record,
                "records": [record],
            })
            continue

        matched_group["records"].append(record)
        if _candidate_record_rank(record) > _candidate_record_rank(matched_group["representative"]):
            matched_group["representative"] = record

    return groups


def _candidate_overlap_area_km2(candidate, poly):
    if poly is None:
        return 0.0

    if not _bounds_overlap(_candidate_pixel_bounds(candidate), poly.bounds):
        return 0.0

    comp_lats, comp_lons = _candidate_pixel_arrays(candidate)
    if comp_lats.size == 0 or comp_lons.size == 0:
        return 0.0

    inside = sv.contains(poly.buffer(1e-9), comp_lons, comp_lats)
    count_inside = int(np.count_nonzero(inside))
    if count_inside <= 0:
        return 0.0

    pixel_count = max(int(candidate.get("pixel_count", 0)), 1)
    pixel_area_km2 = float(candidate.get("area_km2", 0.0)) / pixel_count
    return count_inside * pixel_area_km2


def _nearest_owner_index(candidate, owner_indices, owner_polygons):
    if not owner_indices:
        return None

    centroid_lat = float(candidate.get("centroid_lat", 0.0))
    centroid_lon = float(candidate.get("centroid_lon", 0.0))
    return min(
        owner_indices,
        key=lambda cell_index: distance_km(
            centroid_lat,
            centroid_lon,
            float(owner_polygons[cell_index].centroid.y),
            float(owner_polygons[cell_index].centroid.x),
        ),
    )


def _resolve_owner_tie(candidate, owner_indices, owner_polygons):
    if not owner_indices:
        return None

    overlap_scores = []
    for cell_index in owner_indices:
        poly = owner_polygons.get(cell_index)
        if poly is None:
            continue
        overlap_scores.append((cell_index, _candidate_overlap_area_km2(candidate, poly)))

    if overlap_scores:
        best_overlap = max(score for _, score in overlap_scores)
        if best_overlap > 0.0:
            best_indices = [
                cell_index
                for cell_index, score in overlap_scores
                if abs(score - best_overlap) <= 1e-6
            ]
            if len(best_indices) == 1:
                return best_indices[0]
            owner_indices = best_indices

    return _nearest_owner_index(candidate, owner_indices, owner_polygons)


def _owner_index_for_candidate(candidate, source_indices, owner_polygons):
    candidate_sources = [
        cell_index
        for cell_index in dict.fromkeys(source_indices)
        if owner_polygons.get(cell_index) is not None
    ]
    if not candidate_sources:
        return None

    peak_point = Point(float(candidate.get("peak_lon", 0.0)), float(candidate.get("peak_lat", 0.0)))
    centroid_point = Point(float(candidate.get("centroid_lon", 0.0)), float(candidate.get("centroid_lat", 0.0)))

    peak_hits = [
        cell_index
        for cell_index in candidate_sources
        if owner_polygons[cell_index].covers(peak_point)
    ]
    if len(peak_hits) == 1:
        return peak_hits[0]
    if peak_hits:
        return _resolve_owner_tie(candidate, peak_hits, owner_polygons)

    centroid_hits = [
        cell_index
        for cell_index in candidate_sources
        if owner_polygons[cell_index].covers(centroid_point)
    ]
    if len(centroid_hits) == 1:
        return centroid_hits[0]
    if centroid_hits:
        return _resolve_owner_tie(candidate, centroid_hits, owner_polygons)

    return _resolve_owner_tie(candidate, candidate_sources, owner_polygons)


def _assign_owned_candidates(candidate_map, owner_polygons):
    owned_candidates = {cell_index: [] for cell_index in candidate_map}
    candidate_records = []
    for cell_index, candidates in candidate_map.items():
        for candidate in candidates:
            candidate_records.append(
                {
                    "cell_index": cell_index,
                    "candidate": candidate,
                    "bounds": _candidate_pixel_bounds(candidate),
                }
            )

    for group in _group_candidate_records(candidate_records):
        canonical_record = max(group["records"], key=_candidate_record_rank)
        owner_index = _owner_index_for_candidate(
            canonical_record["candidate"],
            [record["cell_index"] for record in group["records"]],
            owner_polygons,
        )
        if owner_index is None:
            owner_index = canonical_record["cell_index"]
        owned_candidates.setdefault(owner_index, []).append(canonical_record["candidate"])

    return owned_candidates


def _extract_level_candidates(
    integrator,
    dataset,
    var,
    is_grib,
    var_values,
    lat_name,
    lon_name,
    lat_vals,
    lon_vals,
    search_poly,
    threshold,
    pixel_area_km2,
    lat_spacing_km,
    lon_spacing_km,
):
    dataset_poly = integrator._polygon_for_dataset(search_poly, lon_vals)
    subset, subset_lat, subset_lon = integrator._extract_spatial_subset(
        dataset,
        var,
        is_grib,
        var_values,
        lat_name,
        lon_name,
        lat_vals,
        lon_vals,
        dataset_poly,
    )
    if subset is None:
        return []

    inside = sv.contains(dataset_poly.buffer(1e-9), subset_lon, subset_lat)
    masked = np.where(inside, np.asarray(subset), np.nan)
    masked[masked < 0] = np.nan
    return extract_azshear_candidates(
        masked,
        subset_lat,
        subset_lon,
        threshold,
        pixel_area_km2,
        lat_spacing_km,
        lon_spacing_km,
    )


def integrate_azshear_features(integrator, low_dataset_path, mid_dataset_path, storm_cells):
    if not storm_cells or not low_dataset_path or not mid_dataset_path:
        return storm_cells
    history_cache = {}
    buffer_km = azshear_buffer_km()
    low_threshold = azshear_low_threshold()
    mid_threshold = azshear_mid_threshold()

    try:
        low_ds, low_is_grib = _open_azshear_dataset(integrator, low_dataset_path)
        mid_ds, mid_is_grib = _open_azshear_dataset(integrator, mid_dataset_path)
    except Exception as exc:
        integrator.io_manager.write_error(f"Load error for AzShear features: {exc}")
        return storm_cells

    try:
        low_lat_name = "latitude" if "latitude" in low_ds.coords else "lat"
        low_lon_name = "longitude" if "longitude" in low_ds.coords else "lon"
        mid_lat_name = "latitude" if "latitude" in mid_ds.coords else "lat"
        mid_lon_name = "longitude" if "longitude" in mid_ds.coords else "lon"

        low_lat_vals = low_ds[low_lat_name].values
        low_lon_vals = low_ds[low_lon_name].values
        mid_lat_vals = mid_ds[mid_lat_name].values
        mid_lon_vals = mid_ds[mid_lon_name].values

        low_var_name = "unknown" if "unknown" in low_ds.data_vars else list(low_ds.data_vars)[0]
        mid_var_name = "unknown" if "unknown" in mid_ds.data_vars else list(mid_ds.data_vars)[0]
        low_var = low_ds[low_var_name]
        mid_var = mid_ds[mid_var_name]

        low_var_values = low_var.values if low_is_grib else None
        mid_var_values = mid_var.values if mid_is_grib else None

        low_lat_spacing_km, low_lon_spacing_km = grid_spacing_km(low_lat_vals, low_lon_vals)
        mid_lat_spacing_km, mid_lon_spacing_km = grid_spacing_km(mid_lat_vals, mid_lon_vals)
        low_pixel_area_km2 = max(low_lat_spacing_km * low_lon_spacing_km, 0.01)
        mid_pixel_area_km2 = max(mid_lat_spacing_km * mid_lon_spacing_km, 0.01)

        cell_contexts = []
        low_candidate_map = {}
        mid_candidate_map = {}
        low_owner_polygons = {}
        mid_owner_polygons = {}

        raw_polys = []
        raw_poly_indices = []

        for cell_index, cell in enumerate(storm_cells):
            cell.setdefault("properties", {})
            cell["properties"]["azshear"] = None

            raw_poly = StormIntegrationUtils.create_cell_polygon(cell)
            if raw_poly is None:
                cell_contexts.append(None)
                continue

            cell_contexts.append({
                "raw_poly": raw_poly,
                "reflectivity_axis_deg": polygon_major_axis_orientation_deg(raw_poly),
            })
            raw_polys.append(raw_poly)
            raw_poly_indices.append(cell_index)

        search_polys = _build_search_polygons(raw_polys)

        for raw_poly_index, cell_index in enumerate(raw_poly_indices):
            cell = storm_cells[cell_index]
            raw_poly = raw_polys[raw_poly_index]
            search_poly = search_polys[raw_poly_index]
            cell_contexts[cell_index].update(
                {
                    "search_poly": search_poly,
                    "buffer_km": buffer_km,
                    "buffered_area_km2": max(polygon_area_km2(search_poly), 1e-6),
                }
            )
            low_owner_polygons[cell_index] = integrator._polygon_for_dataset(raw_poly, low_lon_vals)
            mid_owner_polygons[cell_index] = integrator._polygon_for_dataset(raw_poly, mid_lon_vals)

            try:
                low_candidate_map[cell_index] = _extract_level_candidates(
                    integrator,
                    low_ds,
                    low_var,
                    low_is_grib,
                    low_var_values,
                    low_lat_name,
                    low_lon_name,
                    low_lat_vals,
                    low_lon_vals,
                    search_poly,
                    low_threshold,
                    low_pixel_area_km2,
                    low_lat_spacing_km,
                    low_lon_spacing_km,
                )
                mid_candidate_map[cell_index] = _extract_level_candidates(
                    integrator,
                    mid_ds,
                    mid_var,
                    mid_is_grib,
                    mid_var_values,
                    mid_lat_name,
                    mid_lon_name,
                    mid_lat_vals,
                    mid_lon_vals,
                    search_poly,
                    mid_threshold,
                    mid_pixel_area_km2,
                    mid_lat_spacing_km,
                    mid_lon_spacing_km,
                )
            except Exception as exc:
                integrator.io_manager.write_error(f"Process AzShear cell {cell.get('id')}: {exc}")
                low_candidate_map[cell_index] = []
                mid_candidate_map[cell_index] = []

        low_owned_candidates = _assign_owned_candidates(low_candidate_map, low_owner_polygons)
        mid_owned_candidates = _assign_owned_candidates(mid_candidate_map, mid_owner_polygons)

        for cell_index, cell in enumerate(storm_cells):
            context = cell_contexts[cell_index] if cell_index < len(cell_contexts) else None
            if context is None:
                continue

            try:
                low_candidates = low_owned_candidates.get(cell_index, [])
                mid_candidates = mid_owned_candidates.get(cell_index, [])
                buffered_area_km2 = context["buffered_area_km2"]
                reflectivity_axis_deg = context["reflectivity_axis_deg"]

                low_summary, low_dominant = summarize_level_metrics(
                    low_candidates,
                    buffered_area_km2,
                    reflectivity_axis_deg,
                )
                mid_summary, mid_dominant = summarize_level_metrics(
                    mid_candidates,
                    buffered_area_km2,
                    reflectivity_axis_deg,
                )

                cell_id = cell.get("id")
                history_entries = _read_recent_history(cell_id, history_cache) if cell_id is not None else []
                low_summary["persistence"] = _compute_level_persistence(
                    history_entries,
                    "low",
                    low_threshold,
                )
                mid_summary["persistence"] = _compute_level_persistence(
                    history_entries,
                    "mid",
                    mid_threshold,
                )
                simultaneous_persistence = _compute_simultaneous_persistence(history_entries)

                paired_low, paired_mid, pair_count = find_best_cross_layer_pair(low_candidates, mid_candidates)
                cross_layer = summarize_cross_layer_metrics(
                    paired_low,
                    paired_mid,
                    low_summary,
                    mid_summary,
                    simultaneous_persistence,
                    pair_count,
                )

                has_low = low_dominant is not None
                has_mid = mid_dominant is not None
                if not has_low and not has_mid:
                    cell["properties"]["azshear"] = None
                    continue

                cell["properties"]["azshear"] = {
                    "buffer_km": context["buffer_km"],
                    "low": low_summary if has_low else None,
                    "mid": mid_summary if has_mid else None,
                    "cross_layer": cross_layer,
                }
            except Exception as exc:
                integrator.io_manager.write_error(f"Summarize AzShear cell {cell.get('id')}: {exc}")
                cell["properties"]["azshear"] = None
    finally:
        low_ds.close()
        mid_ds.close()
        gc.collect()

    return storm_cells
