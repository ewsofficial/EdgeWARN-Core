from datetime import datetime, timezone

import numpy as np
import rasterio.features
from skimage import measure
from shapely.geometry import MultiPolygon, Polygon, mapping, shape

from EdgeWARN.process.detect.config import section
from util.release import get_release_version

class CellDataSaver:
    def __init__(self, bboxes, radar_ds, mapped_ds, expanded_ds, ps_ds, preciptype_ds, use_probsevere_geometry=False):
        self.bboxes = bboxes
        self.radar_ds = radar_ds
        self.mapped_ds = mapped_ds
        self.expanded_ds = expanded_ds
        self.ps_ds = ps_ds
        self.preciptype_ds = preciptype_ds
        self.use_probsevere_geometry = use_probsevere_geometry
        self._hail_present = None
    
    def create_json_structure(self, latest_timestamp, features):
        """
        Creates the main structure for the output data
        
        Args:
            latest_timestamp (str): Latest timestamp of the data
            features (list of dict): List of features to be saved
        """
        return {
            "source": "Edgemont Weather Service",
            "product": "EdgeWARN Storm Cells",
            "version": get_release_version(),
            "modified": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "latest_timestamp": latest_timestamp,
            "features": features
        }

    @staticmethod
    def __round_polygon_points(points, decimals=None):
        if decimals is None:
            decimals = section("save")["polygon_decimals"]
        return [
            [round(float(lat), decimals), round(float(lon) % 360, decimals)]
            for lat, lon in points
        ]

    @staticmethod
    def __normalize_geometry(feature_geometry):
        geom = shape(feature_geometry)
        if geom.geom_type == 'Polygon':
            shell = [(lon % 360, lat) for lon, lat in geom.exterior.coords]
            holes = [[(lon % 360, lat) for lon, lat in ring.coords] for ring in geom.interiors]
            return Polygon(shell, holes)
        if geom.geom_type == 'MultiPolygon':
            normalized = []
            for poly in geom.geoms:
                shell = [(lon % 360, lat) for lon, lat in poly.exterior.coords]
                holes = [[(lon % 360, lat) for lon, lat in ring.coords] for ring in poly.interiors]
                normalized.append(Polygon(shell, holes))
            return MultiPolygon(normalized)
        return geom

    @staticmethod
    def __axis_slice_indices(coord_vals, min_val, max_val):
        coord_asc = coord_vals[0] <= coord_vals[-1]
        lo = min(min_val, max_val)
        hi = max(min_val, max_val)

        if coord_asc:
            start = int(np.searchsorted(coord_vals, lo, side='left'))
            stop = int(np.searchsorted(coord_vals, hi, side='right'))
        else:
            reversed_vals = coord_vals[::-1]
            rev_start = int(np.searchsorted(reversed_vals, lo, side='left'))
            rev_stop = int(np.searchsorted(reversed_vals, hi, side='right'))
            start = len(coord_vals) - rev_stop
            stop = len(coord_vals) - rev_start

        start = max(0, min(start, len(coord_vals)))
        stop = max(start, min(stop, len(coord_vals)))
        return slice(start, stop)

    def __geometry_to_mask_and_slice(self, geometry):
        lats = self.radar_ds['latitude'].values
        lons = self.radar_ds['longitude'].values

        min_lon, min_lat, max_lon, max_lat = geometry.bounds
        lat_slice = self.__axis_slice_indices(lats, min_lat, max_lat)
        lon_slice = self.__axis_slice_indices(lons, min_lon, max_lon)

        if lat_slice.start == lat_slice.stop or lon_slice.start == lon_slice.stop:
            return None, None

        local_lats = lats[lat_slice]
        local_lons = lons[lon_slice]
        lat_res = local_lats[1] - local_lats[0] if len(local_lats) > 1 else (lats[1] - lats[0])
        lon_res = local_lons[1] - local_lons[0] if len(local_lons) > 1 else (lons[1] - lons[0])

        from affine import Affine

        transform = (
            Affine.translation(local_lons[0] - lon_res / 2, local_lats[0] - lat_res / 2)
            * Affine.scale(lon_res, lat_res)
        )
        mask = rasterio.features.rasterize(
            [(mapping(geometry), 1)],
            out_shape=(len(local_lats), len(local_lons)),
            transform=transform,
            fill=0,
            all_touched=True,
            dtype=np.uint8,
        )
        return mask.astype(bool), (lat_slice, lon_slice)

    @staticmethod
    def __geometry_to_bbox_points(geometry):
        if geometry.is_empty:
            return []

        if geometry.geom_type == 'Polygon':
            coords = list(geometry.exterior.coords)
        elif geometry.geom_type == 'MultiPolygon':
            largest = max(geometry.geoms, key=lambda poly: poly.area, default=None)
            coords = [] if largest is None else list(largest.exterior.coords)
        else:
            coords = []

        return [(lat, lon % 360) for lon, lat in coords]

    def __hail_mask_to_polygon(self, hail_mask, row_offset, col_offset):
        """Trace a hail mask and convert its local contour to grid coordinates."""
        if not np.any(hail_mask):
            return []

        # skimage.measure.find_contours requires a two-dimensional input with
        # both dimensions at least two elements long.
        if hail_mask.ndim != 2 or any(size < 2 for size in hail_mask.shape):
            return []

        hail_cfg = section("hail")
        contours = measure.find_contours(
            hail_mask.astype(float),
            hail_cfg["contour_level"],
        )
        if not contours:
            return []

        contour = max(contours, key=lambda candidate: candidate.shape[0])
        sampled = contour[::hail_cfg["contour_sampling_step"]]

        lats = self.radar_ds['latitude'].values
        lons = self.radar_ds['longitude'].values
        r_global = (sampled[:, 0] + row_offset).astype(int)
        c_global = (sampled[:, 1] + col_offset).astype(int)

        np.clip(r_global, 0, lats.shape[0] - 1, out=r_global)
        if lats.ndim == 1:
            np.clip(c_global, 0, lons.shape[0] - 1, out=c_global)
            lat_vals = lats[r_global]
            lon_vals = lons[c_global] % 360
        else:
            np.clip(c_global, 0, lats.shape[1] - 1, out=c_global)
            lat_vals = lats[r_global, c_global]
            lon_vals = lons[r_global, c_global] % 360

        return np.column_stack((lat_vals, lon_vals)).tolist()

    def __create_hailcore_polygon(self, poly_id, slice_obj):
        """
        Creates a hail core polygon by tracing the exterior of hail-classified
        cells within a ProbSevere polygon, using a slice to avoid full-grid scans.
        """
        if self.preciptype_ds is None:
            return []

        if self._hail_present is False:
            return []

        hail_cfg = section("hail")

        # Slices are passed from create_entry
        poly_subgrid = self.expanded_ds['PolygonID'].values[slice_obj]
        precip_subgrid = self.preciptype_ds['unknown'].values[slice_obj]

        # Create mask on subgrid
        poly_mask = poly_subgrid == poly_id
        if not np.any(poly_mask):
            return []

        hail_mask = (precip_subgrid == hail_cfg["preciptype_class"]) & poly_mask
        return self.__hail_mask_to_polygon(
            hail_mask,
            slice_obj[0].start,
            slice_obj[1].start,
        )

    def __create_direct_hailcore_polygon(self, mask, slice_offset=None):
        if self.preciptype_ds is None:
            return []

        if self._hail_present is False:
            return []

        hail_cfg = section("hail")

        rows, cols = np.nonzero(mask)
        if rows.size == 0:
            return []

        rmin, rmax = rows.min(), rows.max() + 1
        cmin, cmax = cols.min(), cols.max() + 1
        row_offset = 0 if slice_offset is None else slice_offset[0].start
        col_offset = 0 if slice_offset is None else slice_offset[1].start
        sl = (
            slice(rmin + row_offset, rmax + row_offset),
            slice(cmin + col_offset, cmax + col_offset),
        )

        local_mask_slice = mask[rmin:rmax, cmin:cmax]
        precip_slice = self.preciptype_ds['unknown'].values[sl]
        hail_mask = (precip_slice == hail_cfg["preciptype_class"]) & local_mask_slice
        return self.__hail_mask_to_polygon(
            hail_mask,
            rmin + row_offset,
            cmin + col_offset,
        )

    def __weighted_centroid(self, mask, grid_slice):
        """Reflectivity-weighted centroid of gates selected by one polygon."""
        rows, cols = np.nonzero(mask)
        refl_vals = self.radar_ds['unknown'].values[grid_slice][mask]
        valid = ~np.isnan(refl_vals)
        refl_vals = refl_vals[valid]
        if not refl_vals.size:
            return None, float('nan')
        rows = rows[valid] + grid_slice[0].start
        cols = cols[valid] + grid_slice[1].start
        lats = self.radar_ds['latitude'].values
        lons = self.radar_ds['longitude'].values
        if lats.ndim == 1:
            lat_vals, lon_vals = lats[rows], lons[cols]
        else:
            lat_vals, lon_vals = lats[rows, cols], lons[rows, cols]
        maximum = float(np.nanmax(refl_vals))
        weights = np.exp(refl_vals - maximum)
        total = np.sum(weights)
        if total <= 0:
            return None, maximum
        return (float(np.sum(lat_vals * weights) / total),
                float(np.sum(lon_vals * weights) / total) % 360), maximum

    def __create_entry_from_mask(self, poly_id, bbox, mask, grid_slice, morphology_engine,
                                 stormprob_polygon=None, stormprob_mask_slice=None):
        count = np.count_nonzero(mask)
        if count == 0:
            return None

        _centroid_decimals = section("save")["centroid_decimals"]
        refl_slice = self.radar_ds['unknown'].values[grid_slice]
        morph_stats = morphology_engine.process_cell(mask, refl_slice)

        # Keep the public detection centroid tied to its detection mask.
        centroid_full, max_refl_val = self.__weighted_centroid(mask, grid_slice)
        centroid = (tuple(round(value, _centroid_decimals) for value in centroid_full)
                    if centroid_full is not None else (np.nan, np.nan))

        if self.use_probsevere_geometry:
            hail_core = self.__create_direct_hailcore_polygon(
                mask,
                slice_offset=grid_slice,
            )
        else:
            hail_core = self.__create_hailcore_polygon(poly_id, grid_slice)

        entry = {
            "id": int(poly_id),
            "num_gates": int(count),
            "centroid": centroid,
            "bbox": self.__round_polygon_points(bbox),
            "hail_core": self.__round_polygon_points(hail_core),
            "max_refl": max_refl_val,
            "event_type": "ACTIVE",
            "parent_ids": [],
            "split_from": None,
            "properties": {
                "morphology": morph_stats
            }
        }

        # StormProb was trained on the original ProbSevere polygon. Its radial
        # profile and centroid must both come from that polygon, even when the
        # public detection footprint has been expanded by the watershed path.
        try:
            from EdgeWARN.stormprob.geometry import attach_stormprob_geometry
            ps_centroid = None
            if stormprob_mask_slice is not None:
                ps_centroid, _ = self.__weighted_centroid(*stormprob_mask_slice)
            attach_stormprob_geometry(entry, ps_centroid, stormprob_polygon)
        except Exception:
            pass

        return entry

    def __create_entries_from_probsevere_geometry(self, morphology_engine):
        results = []

        for feature in (self.ps_ds or {}).get('features', []):
            properties = feature.get('properties') or {}
            poly_id = int(properties.get('ID', 0) or 0)
            if poly_id <= 0:
                continue

            geometry = feature.get('geometry')
            if not geometry:
                continue

            normalized_geometry = self.__normalize_geometry(geometry)
            mask, mask_slice = self.__geometry_to_mask_and_slice(normalized_geometry)
            if mask is None or mask_slice is None:
                continue

            bbox = self.__geometry_to_bbox_points(normalized_geometry)
            entry = self.__create_entry_from_mask(
                poly_id,
                bbox,
                mask,
                mask_slice,
                morphology_engine,
                stormprob_polygon=bbox,
                stormprob_mask_slice=(mask, mask_slice),
            )
            if entry is not None:
                results.append(entry)

        return results

    def create_entry(self, vil_ds=None, et_ds=None):
        """
        Appends maximum reflectivity, num_gates, and reflectivity-weighted centroid
        to each ProbSevere cell entry using exponential weighting.
        Optimized with slice-based processing and Watershed-expanded masks.
        
        Includes detection-stage morphology metrics for downstream analysis.
        """
        from EdgeWARN.process.detect.tools.morphology import MorphologyEngine

        if self.radar_ds is None:
            return []

        if self.preciptype_ds is not None and self._hail_present is None:
            self._hail_present = bool(np.any(self.preciptype_ds['unknown'].values == section('hail')['preciptype_class']))

        if self.use_probsevere_geometry:
            return self.__create_entries_from_probsevere_geometry(MorphologyEngine)
        
        # CRITICAL: Use expanded_ds (the watershed result) for all attribute calculations
        polygon_grid = self.expanded_ds['PolygonID'].values
        results = []
        
        # Get bounding boxes slices for all polygons
        import scipy.ndimage
        max_id = np.max(polygon_grid)
        if max_id == 0:
            return []
            
        slices = scipy.ndimage.find_objects(polygon_grid, max_label=max_id)
        ps_geometries = {}
        for feature in (self.ps_ds or {}).get('features', []):
            try:
                ps_id = int((feature.get('properties') or {}).get('ID', 0) or 0)
                if ps_id > 0 and feature.get('geometry'):
                    ps_geometries[ps_id] = self.__normalize_geometry(feature['geometry'])
            except (TypeError, ValueError):
                continue

        for poly_id, bbox in self.bboxes.items():
            if poly_id == 0:
                continue
                
            # slice index is poly_id - 1
            if poly_id > len(slices):
                continue
                
            sl = slices[poly_id - 1]
            if sl is None:
                continue

            # Extract sub-grids
            mask_slice = polygon_grid[sl] == poly_id
            ps_geometry = ps_geometries.get(poly_id)
            ps_polygon = (self.__geometry_to_bbox_points(ps_geometry)
                          if ps_geometry is not None else None)
            ps_mask_slice = (self.__geometry_to_mask_and_slice(ps_geometry)
                             if ps_geometry is not None else (None, None))
            if ps_mask_slice[0] is None:
                ps_mask_slice = None
            
            # Pre-filter: if mask is empty (shouldn't happen if slice is valid)
            entry = self.__create_entry_from_mask(
                poly_id,
                bbox,
                mask_slice,
                sl,
                MorphologyEngine,
                stormprob_polygon=ps_polygon,
                stormprob_mask_slice=ps_mask_slice,
            )
            if entry is not None:
                results.append(entry)

        return results



        
