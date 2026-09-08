import numpy as np
import xarray as xr

from util.grib_loader import load_grib_fast
from ..config import output_decimals, probsevere_field_map, section
from ..integrate_azshear import integrate_azshear_features as _integrate_azshear_features
from ..utils import StormIntegrationUtils
from .polygon import polygon_for_dataset, polygon_for_longitude_mode, uses_360_longitude
from .stats import prepare_stats_specs, reduce_stats, sanitize_masked_values
from .subset import axis_slice_indices, build_spatial_lookup, extract_spatial_subset

# Suppress cfgrib/xarray compatibility warnings
xr.set_options(use_new_combine_kwarg_defaults=True)


class StormCellIntegrator:
    def __init__(self, io_manager):
        self.io_manager = io_manager

    @staticmethod
    def _axis_slice_indices(coord_vals, min_val, max_val):
        return axis_slice_indices(coord_vals, min_val, max_val)

    @classmethod
    def _polygon_for_dataset(cls, poly, lon_vals):
        return polygon_for_dataset(poly, lon_vals)

    @staticmethod
    def _is_axis_aligned_rectangle(cell):
        bbox = cell.get("bbox")
        if not bbox or len(bbox) < 4:
            return False

        lats = {round(float(point[0]), 8) for point in bbox}
        lons = {round(StormIntegrationUtils.normalize_longitude(point[1]), 8) for point in bbox}
        return len(lats) == 2 and len(lons) == 2

    @classmethod
    def build_cell_contexts(cls, storm_cells):
        contexts = []
        for cell in storm_cells:
            polygon = StormIntegrationUtils.create_cell_polygon(cell)
            contexts.append({
                "polygon_180": polygon,
                "polygon_360": None,
                "axis_aligned_rectangle": cls._is_axis_aligned_rectangle(cell),
                "spatial_lookup_by_grid": {},
            })
        return contexts

    @staticmethod
    def _grid_signature(lat_vals, lon_vals):
        lat_arr = np.asarray(lat_vals)
        lon_arr = np.asarray(lon_vals)

        if lat_arr.ndim == 1 and lon_arr.ndim == 1:
            return (
                "1d",
                lat_arr.shape,
                lon_arr.shape,
                float(lat_arr[0]),
                float(lat_arr[-1]),
                float(lon_arr[0]),
                float(lon_arr[-1]),
            )

        return (
            "2d",
            lat_arr.shape,
            lon_arr.shape,
            float(np.nanmin(lat_arr)),
            float(np.nanmax(lat_arr)),
            float(np.nanmin(lon_arr)),
            float(np.nanmax(lon_arr)),
        )

    @staticmethod
    def _context_polygon_for_longitudes(context, use_360_longitude):
        polygon = context["polygon_180"]
        if polygon is None or not use_360_longitude:
            return polygon

        if context["polygon_360"] is None:
            context["polygon_360"] = polygon_for_longitude_mode(polygon, True)

        return context["polygon_360"]

    def _spatial_lookup_for_cell(self, context, ds, lat_name, lon_name, lat_vals, lon_vals, grid_signature, use_360_longitude):
        cache_key = (grid_signature, use_360_longitude)
        spatial_lookup = context["spatial_lookup_by_grid"].get(cache_key)
        if spatial_lookup is not None:
            return spatial_lookup

        polygon = self._context_polygon_for_longitudes(context, use_360_longitude)
        if polygon is None:
            spatial_lookup = {"empty": True}
        else:
            spatial_lookup = build_spatial_lookup(
                ds,
                lat_name,
                lon_name,
                lat_vals,
                lon_vals,
                polygon,
                axis_aligned_rectangle=context["axis_aligned_rectangle"],
            )

        context["spatial_lookup_by_grid"][cache_key] = spatial_lookup
        return spatial_lookup

    def integrate_azshear_features(self, low_dataset_path, mid_dataset_path, storm_cells):
        return _integrate_azshear_features(self, low_dataset_path, mid_dataset_path, storm_cells)

    def _extract_spatial_subset(self, ds, var, is_grib, var_values, *args):
        if len(args) == 1 and isinstance(args[0], dict):
            return extract_spatial_subset(ds, var, is_grib, var_values, args[0])

        if len(args) != 5:
            raise TypeError("_extract_spatial_subset expected a spatial lookup or lat/lon metadata")

        lat_name, lon_name, lat_vals, lon_vals, poly = args
        spatial_lookup = build_spatial_lookup(
            ds,
            lat_name,
            lon_name,
            lat_vals,
            lon_vals,
            poly,
            axis_aligned_rectangle=False,
        )
        sub_var, _inside = extract_spatial_subset(ds, var, is_grib, var_values, spatial_lookup)
        if sub_var is None:
            return None, None, None

        if spatial_lookup["layout"] == "1d":
            lat_subset = lat_vals[spatial_lookup["lat_slice"]]
            lon_subset = lon_vals[spatial_lookup["lon_slice"]]
            sub_lon, sub_lat = np.meshgrid(lon_subset, lat_subset)
        else:
            row_slice = spatial_lookup["row_slice"]
            col_slice = spatial_lookup["col_slice"]
            sub_lat = lat_vals[row_slice, col_slice]
            sub_lon = lon_vals[row_slice, col_slice]

        return sub_var, sub_lat, sub_lon

    def integrate_ds_via_max(self, dataset_path, storm_cells, output_key, cell_contexts=None):
        if not storm_cells:
            return storm_cells

        is_grib = dataset_path.endswith(".grib2")
        try:
            if is_grib:
                try:
                    ds = load_grib_fast(dataset_path)
                except Exception:
                    ds = xr.open_dataset(dataset_path, engine="cfgrib", decode_timedelta=True)
                    ds.load()
            else:
                ds = xr.open_dataset(dataset_path, decode_timedelta=True)
        except Exception as e:
            self.io_manager.write_error(f"Load error: {e}")
            return storm_cells

        lat_name = "latitude" if "latitude" in ds.coords else "lat"
        lon_name = "longitude" if "longitude" in ds.coords else "lon"
        lat_vals = ds[lat_name].values
        lon_vals = ds[lon_name].values
        grid_signature = self._grid_signature(lat_vals, lon_vals)
        use_360_longitude = uses_360_longitude(lon_vals)

        var = ds.get("unknown")
        if var is None:
            self.io_manager.write_error("Variable 'unknown' not found")
            return storm_cells

        active_cells = storm_cells
        self.io_manager.write_info(f"Integrating {output_key} data for {len(active_cells)} cells")

        var_values = None
        if is_grib:
            var_values = var.values

        if cell_contexts is None:
            cell_contexts = self.build_cell_contexts(storm_cells)

        decimals = output_decimals()

        for cell, context in zip(active_cells, cell_contexts):
            if "properties" not in cell:
                cell["properties"] = {}

            target = cell["properties"]

            if context["polygon_180"] is None:
                target[output_key] = 0
                continue

            try:
                spatial_lookup = self._spatial_lookup_for_cell(
                    context,
                    ds,
                    lat_name,
                    lon_name,
                    lat_vals,
                    lon_vals,
                    grid_signature,
                    use_360_longitude,
                )
                sub_var, inside = self._extract_spatial_subset(ds, var, is_grib, var_values, spatial_lookup)

                if sub_var is None or sub_var.size == 0:
                    target[output_key] = 0
                    continue

                masked_vals = sub_var if inside is None else sub_var[inside]
                masked_vals = masked_vals[masked_vals >= 0]

                if masked_vals.size == 0:
                    target[output_key] = 0
                else:
                    target[output_key] = round(float(np.nanmax(masked_vals)), decimals)

            except Exception as e:
                self.io_manager.write_error(f"Process cell {cell.get('id')}: {e}")
                target[output_key] = "PROCESSING_ERROR"

        ds.close()
        del ds
        return storm_cells

    def integrate_multi_stats(self, dataset_path, storm_cells, stats_config_list, cell_contexts=None):
        if not storm_cells:
            return storm_cells

        stats_specs, zero_results, unique_percentiles, needs_max, needs_mean = prepare_stats_specs(stats_config_list)

        is_grib = dataset_path.endswith(".grib2")
        try:
            if is_grib:
                try:
                    ds = load_grib_fast(dataset_path)
                except Exception:
                    ds = xr.open_dataset(dataset_path, engine="cfgrib", decode_timedelta=True)
                    ds.load()
            else:
                ds = xr.open_dataset(dataset_path, decode_timedelta=True)

        except Exception as e:
            keys = [c["key"] for c in stats_config_list]
            self.io_manager.write_error(f"Load error for {keys}: {e}")
            return storm_cells

        lat_name = "latitude" if "latitude" in ds.coords else "lat"
        lon_name = "longitude" if "longitude" in ds.coords else "lon"
        lat_vals = ds[lat_name].values
        lon_vals = ds[lon_name].values
        grid_signature = self._grid_signature(lat_vals, lon_vals)
        use_360_longitude = uses_360_longitude(lon_vals)

        var_name = list(ds.data_vars)[0]
        if "unknown" in ds.data_vars:
            var_name = "unknown"

        var = ds.get(var_name)
        if var is None:
            self.io_manager.write_error(f"Variable not found in {dataset_path}")
            return storm_cells

        var_values = None
        if is_grib:
            var_values = var.values

        if cell_contexts is None:
            cell_contexts = self.build_cell_contexts(storm_cells)

        keys_str = ", ".join([key for key, _, _ in stats_specs])
        self.io_manager.write_info(f"Integrating [{keys_str}] for {len(storm_cells)} cells")

        for cell, context in zip(storm_cells, cell_contexts):
            if "properties" not in cell:
                cell["properties"] = {}

            target = cell["properties"]

            if context["polygon_180"] is None:
                target.update(zero_results)
                continue

            try:
                spatial_lookup = self._spatial_lookup_for_cell(
                    context,
                    ds,
                    lat_name,
                    lon_name,
                    lat_vals,
                    lon_vals,
                    grid_signature,
                    use_360_longitude,
                )
                sub_var, inside = self._extract_spatial_subset(ds, var, is_grib, var_values, spatial_lookup)

                if sub_var is None or sub_var.size == 0:
                    target.update(zero_results)
                    continue

                masked_vals = sub_var if inside is None else sub_var[inside]
                masked_vals = sanitize_masked_values(masked_vals)

                if masked_vals.size == 0:
                    target.update(zero_results)
                else:
                    target.update(
                        reduce_stats(
                            masked_vals,
                            stats_specs,
                            unique_percentiles,
                            needs_max,
                            needs_mean,
                        )
                    )

            except Exception as e:
                self.io_manager.write_error(f"Process cell {cell.get('id')}: {e}")
                target.update(zero_results)

        ds.close()
        del ds
        return storm_cells

    def integrate_probsevere(self, probsevere_data, storm_cells):
        if not storm_cells:
            return storm_cells

        if not isinstance(probsevere_data, dict) or "features" not in probsevere_data:
            self.io_manager.write_error("Failed to integrate ProbSevere data - Invalid Data Format")
            return storm_cells

        features = probsevere_data["features"]

        feature_lookup = {}
        for feature in features:
            properties = feature.get("properties", {})
            identifier = feature.get("id")
            if identifier is None:
                identifier = properties.get("ID")
            if identifier is not None:
                feature_lookup[str(identifier)] = properties

        field_map = probsevere_field_map()
        for cell in storm_cells:
            cell_id = cell.get("id")

            if "properties" not in cell:
                cell["properties"] = {}

            match = None if cell_id is None else feature_lookup.get(str(cell_id))
            if not match:
                continue

            for target_key, source_key in field_map.items():
                try:
                    raw_value = match[source_key]
                    cell["properties"][target_key] = float(raw_value)
                except (TypeError, ValueError):
                    cell["properties"][target_key] = "MATCH_ERROR"
                except KeyError:
                    cell["properties"][target_key] = "MATCH_ERROR"

        return storm_cells
