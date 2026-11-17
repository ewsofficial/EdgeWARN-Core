"""GLM L2 merge keeps NaNs and packing safe on write (test-run-1001, issue 4b).

GLM packs most float fields into integers, and several (``event_lat``,
``event_lon``, the time offsets) declare no ``_FillValue``. The merge used to
write NaNs back through that packing, which xarray reports as a
``SerializationWarning`` and stores as arbitrary integers.
"""

import warnings

import netCDF4
import numpy as np
import pytest
import xarray as xr

from common.ingest.mrms.downloader import _write_netcdf_atomically
from common.ingest.mrms.utils import merge_glm_files

EVENTS = ("event_id", "event_time_offset", "event_lat", "event_lon", "event_energy",
          "event_parent_group_id")
GROUPS = ("group_id", "group_time_offset", "group_lat", "group_lon", "group_energy",
          "group_area", "group_quality_flag", "group_parent_flash_id")
FLASHES = ("flash_id", "flash_time_offset_of_first_event", "flash_time_offset_of_last_event",
           "flash_lat", "flash_lon", "flash_energy", "flash_area", "flash_quality_flag")
# (scale_factor, add_offset, _FillValue) for packed float fields, as in GLM L2.
PACKED = {
    "lat": (0.00203128, -66.56, None),
    "lon": (0.00203128, -141.56, None),
    "time_offset": (0.0003814756, -5.0, None),
    "energy": (1.52597e-15, 2.8515e-16, None),
    "area": (152601.86, 0.0, -1),
}


class _Log:
    def write_info(self, _message):
        pass

    write_warning = write_error = write_debug = write_info


def _packing(name):
    for suffix, packing in PACKED.items():
        if suffix in name:
            return packing
    return None


def _write_glm(path, offset, *, fill_first_flash_area=False):
    with netCDF4.Dataset(path, "w") as ds:
        sizes = {"number_of_events": 3, "number_of_groups": 2, "number_of_flashes": 2,
                 "number_of_time_bounds": 2}
        for dim, size in sizes.items():
            ds.createDimension(dim, size)
        for names, dim in ((EVENTS, "number_of_events"), (GROUPS, "number_of_groups"),
                           (FLASHES, "number_of_flashes")):
            for name in names:
                packing = _packing(name)
                if packing is None:
                    var = ds.createVariable(name, "i4", (dim,))
                    var[:] = np.arange(sizes[dim]) + offset
                    continue
                scale, add, fill = packing
                var = ds.createVariable(name, "i2", (dim,), fill_value=fill)
                var.scale_factor = scale
                var.add_offset = add
                var.set_auto_maskandscale(False)
                raw = np.arange(sizes[dim], dtype="i2") * 10 + 100 + offset
                if fill is not None and fill_first_flash_area and name == "flash_area":
                    raw[0] = fill
                var[:] = raw
        for name in ("event_count", "group_count", "flash_count"):
            ds.createVariable(name, "i4", ())[...] = 3
        bounds = ds.createVariable("product_time_bounds", "f8", ("number_of_time_bounds",))
        bounds[:] = [offset * 20.0, offset * 20.0 + 20.0]
        ds.createVariable("product_time", "f8", ())[...] = offset * 20.0


@pytest.fixture
def glm_files(tmp_path):
    first, second = tmp_path / "glm_a.nc", tmp_path / "glm_b.nc"
    _write_glm(first, 0, fill_first_flash_area=True)
    _write_glm(second, 1)
    return [first, second]


def test_merged_glm_round_trips_nans_without_serialization_warnings(tmp_path, glm_files):
    merged = merge_glm_files(glm_files, _Log())
    assert merged is not None
    assert merged.sizes["number_of_events"] == 6
    expected_lat = merged["event_lat"].values.copy()
    # A NaN in a field GLM packs without a fill value (e.g. from a concat gap).
    merged["event_lat"].values[1] = np.nan
    expected_lat[1] = np.nan

    destination = tmp_path / "OR_GLM-L2-LCFA_merged_20261001-231200.nc"
    with warnings.catch_warnings():
        warnings.simplefilter("error", xr.SerializationWarning)
        _write_netcdf_atomically(merged, destination)
    merged.close()

    with xr.open_dataset(destination) as result:
        lat = result["event_lat"].values
        assert np.isnan(lat[1])
        np.testing.assert_allclose(lat[~np.isnan(lat)], expected_lat[~np.isnan(expected_lat)],
                                   atol=0.003)
        # A declared fill value keeps its packing, and its NaN survives too.
        area = result["flash_area"]
        assert np.isnan(area.values[0]) and np.isfinite(area.values[1:]).all()
        assert area.encoding["dtype"] == np.dtype("int16")
        assert int(result["event_count"]) == 6
