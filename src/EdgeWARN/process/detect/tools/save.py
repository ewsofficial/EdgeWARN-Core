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

    def __create_entry_from_mask(self, poly_id, bbox, mask, grid_slice, morphology_engine):
        count = np.count_nonzero(mask)
        if count == 0:
            return None

        _centroid_decimals = section("save")["centroid_decimals"]
        refl_grid = self.radar_ds['unknown'].values
        lats = self.radar_ds['latitude'].values
        lons = self.radar_ds['longitude'].values

        rows, cols = np.nonzero(mask)
        refl_slice = refl_grid[grid_slice]
        morph_stats = morphology_engine.process_cell(mask, refl_slice)

        refl_vals = refl_slice[mask]
        valid_refl_mask = ~np.isnan(refl_vals)
        refl_vals = refl_vals[valid_refl_mask]

        if refl_vals.size > 0:
            global_rows = rows[valid_refl_mask] + grid_slice[0].start
            global_cols = cols[valid_refl_mask] + grid_slice[1].start
            if lats.ndim == 1:
                lat_vals = lats[global_rows]
                lon_vals = lons[global_cols]
            else:
                lat_vals = lats[global_rows, global_cols]
                lon_vals = lons[global_rows, global_cols]

            max_refl_val = float(np.nanmax(refl_vals))
            weights = np.exp(refl_vals - max_refl_val)
            sum_weights = np.sum(weights)

            if sum_weights > 0:
                lat_centroid = float(np.sum(lat_vals * weights) / sum_weights)
                lon_centroid = float(np.sum(lon_vals * weights) / sum_weights) % 360
                centroid = (
                    round(lat_centroid, _centroid_decimals),
                    round(lon_centroid, _centroid_decimals),
                )
            else:
                centroid = (np.nan, np.nan)
        else:
            max_refl_val = float('nan')
            centroid = (np.nan, np.nan)

        if self.use_probsevere_geometry:
            hail_core = self.__create_direct_hailcore_polygon(
                mask,
                slice_offset=grid_slice,
            )
        else:
            hail_core = self.__create_hailcore_polygon(poly_id, grid_slice)

        return {
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
            
            # Pre-filter: if mask is empty (shouldn't happen if slice is valid)
            entry = self.__create_entry_from_mask(
                poly_id,
                bbox,
                mask_slice,
                sl,
                MorphologyEngine,
            )
            if entry is not None:
                results.append(entry)

        return results



        
