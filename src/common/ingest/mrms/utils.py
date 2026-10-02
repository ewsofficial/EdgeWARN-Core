import re
from datetime import datetime, timezone, timedelta
import xarray as xr
import netCDF4
import traceback

GOES_PATTERN = re.compile(r"s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d{1})")
MRMS_PATTERN = re.compile(r"(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})")

def extract_timestamp(filepath: str) -> datetime:
    """
    Extract timestamp from filepath and return timezone-aware datetime object.

    Supports multiple formats:
    - MRMS: YYYYMMDD-HHMMSS or YYYYMMDD_HHMMSS
    - GOES: sYYYYDDDHHMMSSS (start time from GOES file naming convention)

    Returns a default timestamp if no pattern is found.

    Args:
        filepath (str): The filename/filepath string to search for timestamp

    Returns:
        datetime: A timezone-aware datetime object (UTC)
    """
    goes_match = GOES_PATTERN.search(filepath)
    if goes_match:
        year, day_of_year, hour, minute, second, _ = map(int, goes_match.groups())
        dt_aware = datetime(year, 1, 1, hour, minute, second, tzinfo=timezone.utc)
        return dt_aware + timedelta(days=day_of_year - 1)

    mrms_match = MRMS_PATTERN.search(filepath)
    if mrms_match:
        year, month, day, hour, minute, second = map(int, mrms_match.groups())
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

    return datetime.now(timezone.utc).replace(second=0, microsecond=0)

def merge_files(file_list, io_manager):
    """
    Merge multiple xarray datasets from a list of file paths.

    Args:
        file_list (list): List of file paths to xarray-compatible
        io_manager: IO manager for logging
    Returns:
        xarray.Dataset: Concatenated dataset
    """
    datasets = [xr.open_dataset(f, engine="netcdf4") for f in file_list]
    if not datasets:
        io_manager.write_error("No datasets to concatenate.")
        return None
    
    merged_dataset = xr.merge(datasets)
    io_manager.write_info(f"Merged {len(datasets)} datasets.")
    for ds in datasets:
        ds.close()
    
    return merged_dataset


# Encoding keys that describe on-disk packing and remain valid after a
# concatenation changes a variable's length. Chunk/storage layout keys do not.
_GLM_PACKING_KEYS = ("dtype", "scale_factor", "add_offset", "_FillValue",
                     "missing_value", "_Unsigned", "zlib", "complevel", "shuffle")


def _glm_packing_encoding(source):
    """Packing encoding to write a merged GLM variable without losing NaNs.

    GLM L2 packs most floating-point fields into (unsigned) integers, and
    several of them (for example ``event_lat``/``event_lon`` and the time
    offsets) carry no ``_FillValue``. Writing float data back through an
    integer encoding without a fill value turns NaNs into integers, so those
    variables are written unpacked instead. Packing that does declare a fill
    value is preserved.
    """
    import numpy as np

    encoding = {key: source.encoding[key] for key in _GLM_PACKING_KEYS if key in source.encoding}
    dtype = encoding.get("dtype")
    if dtype is None:
        return encoding
    packs_float = (np.issubdtype(np.dtype(dtype), np.integer)
                   and np.issubdtype(source.dtype, np.floating))
    has_fill = encoding.get("_FillValue") is not None or encoding.get("missing_value") is not None
    if packs_float and not has_fill:
        for key in ("dtype", "scale_factor", "add_offset", "_Unsigned", "_FillValue", "missing_value"):
            encoding.pop(key, None)
    return encoding


def merge_glm_files(file_list, io_manager):
    """
    Merge multiple GLM L2 NetCDF files into a single dataset.
    
    Concatenates event, group, and flash data along their respective dimensions.
    Updates scalar counts and time bounds.
    
    Args:
        file_list (list): List of file paths to GLM NetCDF files
        io_manager: IO manager for logging
        
    Returns:
        xarray.Dataset: Merged dataset containing all events/groups/flashes
    """
    if not file_list:
        io_manager.write_warning("No GLM files to merge.")
        return None
        
    datasets = []
    try:
        for f in file_list:
            ds = xr.open_dataset(f, engine="netcdf4")
            datasets.append(ds)
    except Exception as e:
        io_manager.write_error(f"Error opening GLM files: {e}")
        return None

    if not datasets:
        return None

    io_manager.write_info(f"Merging {len(datasets)} GLM datasets...")

    try:
        # 1. Concatenate along specific dimensions
        # Define variable groups
        event_vars = [
            "event_id", "event_time_offset", "event_lat", "event_lon", 
            "event_energy", "event_parent_group_id"
        ]
        group_vars = [
            "group_id", "group_time_offset", "group_lat", "group_lon", 
            "group_energy", "group_area", "group_quality_flag", 
            "group_parent_flash_id"
        ]
        flash_vars = [
            "flash_id", "flash_time_offset_of_first_event", 
            "flash_time_offset_of_last_event", "flash_lat", "flash_lon", 
            "flash_energy", "flash_area", "flash_quality_flag"
        ]

        # Subset and concat Events
        # We drop other dimensions to avoid conflicts
        events_ds = xr.concat(
            [ds[event_vars].drop_dims(["number_of_groups", "number_of_flashes"], errors="ignore") for ds in datasets], 
            dim="number_of_events", 
            coords="minimal",
            compat="override"
        )
        
        # Subset and concat Groups
        groups_ds = xr.concat(
            [ds[group_vars].drop_dims(["number_of_events", "number_of_flashes"], errors="ignore") for ds in datasets], 
            dim="number_of_groups", 
            coords="minimal",
            compat="override"
        )
        
        # Subset and concat Flashes
        flashes_ds = xr.concat(
            [ds[flash_vars].drop_dims(["number_of_events", "number_of_groups"], errors="ignore") for ds in datasets], 
            dim="number_of_flashes", 
            coords="minimal",
            compat="override"
        )

        # 2. Merge the concatenated datasets
        merged = xr.merge([events_ds, groups_ds, flashes_ds], compat="override")

        # 3. Update scalar variables and bounds
        total_event_count = sum(ds["event_count"].values for ds in datasets)
        total_group_count = sum(ds["group_count"].values for ds in datasets)
        total_flash_count = sum(ds["flash_count"].values for ds in datasets)
        
        # Time bounds: min of starts, max of ends
        min_start = min(ds["product_time_bounds"].values[0] for ds in datasets)
        max_end = max(ds["product_time_bounds"].values[-1] for ds in datasets)
        
        # Product time: use the earliest one (or min)
        min_product_time = min(ds["product_time"].values for ds in datasets)

        # Assign updated values
        merged["event_count"] = total_event_count
        merged["group_count"] = total_group_count
        merged["flash_count"] = total_flash_count
        
        # Handle time bounds (might need to be careful with shape if it's strictly 1D or 2D)
        # Usually product_time_bounds is (number_of_time_bounds: 2)
        # We'll just create a new array
        merged["product_time_bounds"] = (("number_of_time_bounds",), [min_start, max_end])
        merged["product_time"] = min_product_time

        # 4. Copy other metadata/attributes from the first dataset
        # (xr.merge might have done some of this, but we ensure global attrs are preserved)
        merged.attrs = datasets[0].attrs

        # Concatenation does not reliably carry each variable's packing, and the
        # source packing can write NaNs as integers. Set it explicitly.
        for name in (*event_vars, *group_vars, *flash_vars):
            merged[name].encoding = _glm_packing_encoding(datasets[0][name])
        
        # Update specific attributes if needed (e.g., time coverage)
        merged.attrs["time_coverage_start"] = str(min_start)
        merged.attrs["time_coverage_end"] = str(max_end)

        # Close original datasets
        for ds in datasets:
            ds.close()

        return merged

    except Exception as e:
        io_manager.write_error(f"Error merging GLM datasets: {e}")
        traceback.print_exc()
        for ds in datasets:
            ds.close()
        return None
