import xarray as xr
import numpy as np
import shapely.vectorized as sv
from .config import section
from .utils import StormIntegrationUtils, io_manager


def _build_flash_spatial_index(flash_lats, flash_lons, bin_size):
    """Build a coarse spatial bin index for flash candidate lookup."""
    spatial_index = {}
    lat_bins = np.floor(flash_lats / bin_size).astype(np.int64)
    lon_bins = np.floor(flash_lons / bin_size).astype(np.int64)

    for idx, (lat_bin, lon_bin) in enumerate(zip(lat_bins, lon_bins)):
        key = (int(lat_bin), int(lon_bin))
        if key not in spatial_index:
            spatial_index[key] = []
        spatial_index[key].append(idx)

    return spatial_index


def _candidate_indices_from_bounds(bounds, spatial_index, bin_size):
    """Collect candidate flash indices for polygon bounds from spatial bins."""
    minx, miny, maxx, maxy = bounds

    min_lat_bin = int(np.floor(miny / bin_size))
    max_lat_bin = int(np.floor(maxy / bin_size))
    min_lon_bin = int(np.floor(minx / bin_size))
    max_lon_bin = int(np.floor(maxx / bin_size))

    candidates = []
    for lat_bin in range(min_lat_bin, max_lat_bin + 1):
        for lon_bin in range(min_lon_bin, max_lon_bin + 1):
            indices = spatial_index.get((lat_bin, lon_bin))
            if indices:
                candidates.extend(indices)

    if not candidates:
        return None

    return np.array(candidates, dtype=np.int64)

def integrate_glm(storm_cells, glm_file_path=None):
    """
    Integrate GOES GLM flash count and total flash energy into storm cells.
    
    Args:
        storm_cells (list): List of storm cell dictionaries.
        glm_file_path (str, optional): Path to the GLM L2 LCFA NetCDF file. 
                                       REQUIRED. If None, logs error and returns.
                                       
    Returns:
        list: Updated storm cells with GLM_FLASH_COUNT and GLM_TOTAL_ENERGY.
    """
    if glm_file_path is None:
        io_manager.write_error("GLM file path not provided to integrate_glm")
        return storm_cells

    bin_size = section("glm")["bin_size_degrees"]

    # Load dataset
    ds = None
    try:
        ds = xr.open_dataset(glm_file_path, engine="netcdf4")
    except Exception as e:
        io_manager.write_error(f"Failed to load GLM file {glm_file_path}: {e}")
        return storm_cells

    try:
        # Check variables
        required_vars = ["flash_lat", "flash_lon", "flash_energy"]
        for v in required_vars:
            if v not in ds:
                io_manager.write_error(f"Variable '{v}' not found in GLM file")
                ds.close()
                return storm_cells

        flash_lats = ds["flash_lat"].values
        flash_lons = ds["flash_lon"].values
        # Cell polygons use the conventional [-180, 180] range. Keeping flashes
        # in 0..360 made every western-hemisphere candidate miss its polygon.
        flash_lons = (flash_lons + 180) % 360 - 180
        # flash_energy in Joules (J) - sometimes it's femtojoules (fJ) depending on product, 
        # but usually post-processed to J or similar unit. Taking raw values as requested.
        flash_energies = ds["flash_energy"].values

        finite_mask = np.isfinite(flash_lats) & np.isfinite(flash_lons)
        flash_lats = flash_lats[finite_mask]
        flash_lons = flash_lons[finite_mask]

        active_cells = storm_cells
        flash_energies = flash_energies[finite_mask]

        flash_spatial_index = _build_flash_spatial_index(
            flash_lats,
            flash_lons,
            bin_size,
        )

        io_manager.write_info(f"Integrating GLM data for {len(active_cells)} cells")

        for cell in active_cells:
            # Ensure properties exist
            if "properties" not in cell:
                cell["properties"] = {}
            target = cell["properties"]
            
            poly = StormIntegrationUtils.create_cell_polygon(cell)
            
            if poly is None:
                target["GLM_FLASH_COUNT"] = 0
                target["GLM_TOTAL_ENERGY"] = 0.0
                continue
                
            try:
                candidate_idx = _candidate_indices_from_bounds(
                    poly.bounds,
                    flash_spatial_index,
                    bin_size,
                )

                if candidate_idx is None or candidate_idx.size == 0:
                    target["GLM_FLASH_COUNT"] = 0
                    target["GLM_TOTAL_ENERGY"] = 0.0
                    continue

                subset_lats = flash_lats[candidate_idx]
                subset_lons = flash_lons[candidate_idx]
                subset_energies = flash_energies[candidate_idx]

                minx, miny, maxx, maxy = poly.bounds
                bbox_mask = (
                    (subset_lats >= miny) & (subset_lats <= maxy) &
                    (subset_lons >= minx) & (subset_lons <= maxx)
                )

                if not np.any(bbox_mask):
                    target["GLM_FLASH_COUNT"] = 0
                    target["GLM_TOTAL_ENERGY"] = 0.0
                    continue

                subset_lats = subset_lats[bbox_mask]
                subset_lons = subset_lons[bbox_mask]
                subset_energies = subset_energies[bbox_mask]

                # Precise check
                inside = sv.contains(poly, subset_lons, subset_lats)
                
                if not np.any(inside):
                     target["GLM_FLASH_COUNT"] = 0
                     target["GLM_TOTAL_ENERGY"] = 0.0
                else:
                    # Filter energies
                    final_energies = subset_energies[inside]
                    
                    target["GLM_FLASH_COUNT"] = int(len(final_energies))
                    target["GLM_TOTAL_ENERGY"] = float(np.nansum(final_energies))
                    
            except Exception as e:
                io_manager.write_error(f"Process cell {cell.get('id')} for GLM: {e}")
                target["GLM_FLASH_COUNT"] = "PROCESSING_ERROR"
                target["GLM_TOTAL_ENERGY"] = "PROCESSING_ERROR"

    except Exception as e:
        io_manager.write_error(f"Error during GLM integration: {e}")
    finally:
        if ds:
            ds.close()

    return storm_cells
