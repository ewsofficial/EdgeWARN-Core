"""Tests for GOES ABI EWMRS transform and reprojection helpers."""

from datetime import datetime, timezone

import numpy as np
import rasterio.transform
import xarray as xr

from EWMRS.render.goes_transform import (
    extract_goes_timestamp_iso,
    load_goes_abi_render_dataset,
    load_reproject_goes_abi_render_array,
    reproject_goes_abi_to_web_mercator,
)


def _make_goes_projection_attrs():
    return {
        "grid_mapping_name": "geostationary",
        "longitude_of_projection_origin": -75.0,
        "latitude_of_projection_origin": 0.0,
        "perspective_point_height": 35786023.0,
        "semi_major_axis": 6378137.0,
        "semi_minor_axis": 6356752.31414,
        "inverse_flattening": 298.2572221,
        "sweep_angle_axis": "x",
    }


def _write_goes_file(path, *, variable_name="Rad", data=None, var_attrs=None):
    if data is None:
        data = np.array([[10.0, 11.0], [12.0, 13.0]], dtype=np.float32)
    if var_attrs is None:
        var_attrs = {}

    data_vars = {
        variable_name: (("y", "x"), data, var_attrs),
        "goes_imager_projection": xr.DataArray(0, attrs=_make_goes_projection_attrs()),
    }

    if variable_name == "Rad":
        data_vars["band_id"] = xr.DataArray(np.array([2], dtype=np.int32), dims=("band",))
        data_vars["kappa0"] = xr.DataArray(np.array([0.02], dtype=np.float32), dims=("band",))
        data_vars["planck_fk1"] = xr.DataArray(np.array([202263.0], dtype=np.float32), dims=("band",))
        data_vars["planck_fk2"] = xr.DataArray(np.array([3698.0], dtype=np.float32), dims=("band",))
        data_vars["planck_bc1"] = xr.DataArray(np.array([0.433], dtype=np.float32), dims=("band",))
        data_vars["planck_bc2"] = xr.DataArray(np.array([0.99939], dtype=np.float32), dims=("band",))

    ds = xr.Dataset(
        data_vars=data_vars,
        coords={
            "x": xr.DataArray(np.array([-0.02, 0.02], dtype=np.float64), attrs={"units": "rad"}),
            "y": xr.DataArray(np.array([0.02, -0.02], dtype=np.float64), attrs={"units": "rad"}),
        },
    )
    ds.to_netcdf(path)
    ds.close()


def test_extract_goes_timestamp_iso_from_filename(tmp_path):
    path = tmp_path / "OR_ABI-L1b-RadC-M6C02_G19_s20261090010000_e20261090010057_c20261090010111.nc"
    path.write_bytes(b"x")

    result = extract_goes_timestamp_iso(path)

    assert result == "2026-04-19T00:10:00"


def test_load_goes_abi_render_dataset_uses_configured_variable(tmp_path):
    goes_file = tmp_path / "goes.nc"
    _write_goes_file(goes_file, variable_name="CMI", data=np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32))

    layer_config = {
        "variable_name": "CMI",
        "fallback_variable_names": ["Rad"],
        "source_type": "goes_abi",
    }
    ds = load_goes_abi_render_dataset(goes_file, layer_config)

    assert ds is not None
    assert "unknown" in ds.data_vars
    assert ds["unknown"].shape == (2, 2)
    np.testing.assert_allclose(ds["unknown"].values, np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32))


def test_load_goes_abi_render_dataset_falls_back_to_rad_and_transforms_reflectance(tmp_path):
    goes_file = tmp_path / "goes.nc"
    _write_goes_file(
        goes_file,
        variable_name="Rad",
        data=np.array([[10.0, 20.0], [30.0, -1.0]], dtype=np.float32),
        var_attrs={"kappa0": 0.02},
    )

    layer_config = {
        "variable_name": "CMI",
        "fallback_variable_names": ["Rad"],
        "source_type": "goes_abi",
        "value_transform": "reflectance_from_rad",
        "mask_min": 0.0,
        "mask_max": 1.2,
    }
    ds = load_goes_abi_render_dataset(goes_file, layer_config)

    assert ds is not None
    expected = np.array([[0.2, 0.4], [0.6, np.nan]], dtype=np.float32)
    np.testing.assert_allclose(ds["unknown"].values, expected, equal_nan=True)


def test_load_goes_abi_render_dataset_applies_brightness_temperature_transform(tmp_path):
    goes_file = tmp_path / "goes.nc"
    _write_goes_file(
        goes_file,
        variable_name="Rad",
        data=np.full((2, 2), 0.2, dtype=np.float32),
        var_attrs={
            "planck_fk1": 202263.0,
            "planck_fk2": 3698.0,
            "planck_bc1": 0.433,
            "planck_bc2": 0.99939,
        },
    )

    layer_config = {
        "variable_name": "CMI",
        "fallback_variable_names": ["Rad"],
        "source_type": "goes_abi",
        "value_transform": "brightness_temp_from_rad",
        "mask_min": 180.0,
        "mask_max": 330.0,
    }
    ds = load_goes_abi_render_dataset(goes_file, layer_config)

    assert ds is not None
    assert "unknown" in ds.data_vars
    value = float(ds["unknown"].values[0, 0])
    assert 180.0 <= value <= 330.0


def test_load_goes_abi_render_dataset_returns_none_without_projection(tmp_path):
    path = tmp_path / "bad_goes.nc"
    ds = xr.Dataset(
        data_vars={"Rad": (("y", "x"), np.ones((2, 2), dtype=np.float32))},
        coords={
            "x": xr.DataArray(np.array([-0.02, 0.02], dtype=np.float64), attrs={"units": "rad"}),
            "y": xr.DataArray(np.array([0.02, -0.02], dtype=np.float64), attrs={"units": "rad"}),
        },
    )
    ds.to_netcdf(path)
    ds.close()

    result = load_goes_abi_render_dataset(path, {"source_type": "goes_abi"})

    assert result is None


def test_reproject_goes_abi_to_web_mercator_returns_target_shape(tmp_path):
    goes_file = tmp_path / "goes.nc"
    _write_goes_file(goes_file, variable_name="CMI", data=np.array([[0.2, 0.4], [0.6, 0.8]], dtype=np.float32))

    prepared = load_goes_abi_render_dataset(
        goes_file,
        {
            "variable_name": "CMI",
            "fallback_variable_names": ["Rad"],
            "source_type": "goes_abi",
        },
    )
    assert prepared is not None

    transform = rasterio.transform.from_bounds(-14471533.8, 2273030.9, -6679169.5, 7361866.1, 20, 10)
    projected = reproject_goes_abi_to_web_mercator(prepared, shape=(10, 20), transform=transform)

    assert projected is not None
    assert projected["unknown"].shape == (10, 20)


def test_load_reproject_goes_array_resolves_configured_resampling(monkeypatch, tmp_path):
    """The production array path must not pass ``None`` to rasterio."""
    from affine import Affine
    from rasterio.enums import Resampling
    from EWMRS.render import goes_transform

    monkeypatch.setattr(
        goes_transform,
        "_load_goes_abi_render_payload",
        lambda *_args, **_kwargs: {"data": np.zeros((2, 2)), "transform": Affine.identity(), "crs": "EPSG:4326"},
    )
    observed = []

    def reproject_payload(*_args, **kwargs):
        observed.append(kwargs["resampling"])
        return {"data": np.zeros((2, 2)), "x": np.zeros(2), "y": np.zeros(2)}

    monkeypatch.setattr(goes_transform, "_reproject_goes_payload_to_web_mercator", reproject_payload)
    monkeypatch.setattr(goes_transform, "goes_transform_resampling", lambda: Resampling.lanczos)

    result = load_reproject_goes_abi_render_array(
        tmp_path / "goes.nc", {}, shape=(2, 2), transform=Affine.identity()
    )

    assert result is not None
    assert observed == [Resampling.lanczos]


def test_goes_config_wires_to_expected_paths_and_metadata():
    from EWMRS.render.config import get_goes_file_list
    import util.file as fs

    layers = get_goes_file_list()
    by_name = {layer["name"]: layer for layer in layers}

    expected = {
        "GOES_ABI_C01_Reflectance": (fs.GOES_ABI_VISIBLE_BLUE_DIR, fs.GUI_GOES_C01_DIR, "reflectance_from_rad"),
        "GOES_ABI_C02_Reflectance": (fs.GOES_ABI_VISIBLE_RED_DIR, fs.GUI_GOES_C02_DIR, "reflectance_from_rad"),
        "GOES_ABI_C03_Reflectance": (fs.GOES_ABI_VEGGIE_DIR, fs.GUI_GOES_C03_DIR, "reflectance_from_rad"),
        "GOES_ABI_C04_Reflectance": (fs.GOES_ABI_CIRRUS_DIR, fs.GUI_GOES_C04_DIR, "reflectance_from_rad"),
        "GOES_ABI_C05_Reflectance": (fs.GOES_ABI_SNOW_ICE_DIR, fs.GUI_GOES_C05_DIR, "reflectance_from_rad"),
        "GOES_ABI_C06_Reflectance": (fs.GOES_ABI_PARTICLE_SIZE_DIR, fs.GUI_GOES_C06_DIR, "reflectance_from_rad"),
        "GOES_ABI_C07_BrightnessTemp": (fs.GOES_ABI_SHORTWAVE_IR_DIR, fs.GUI_GOES_C07_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C08_BrightnessTemp": (fs.GOES_ABI_UPPER_LEVEL_WV_DIR, fs.GUI_GOES_C08_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C09_BrightnessTemp": (fs.GOES_ABI_MID_LEVEL_WV_DIR, fs.GUI_GOES_C09_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C10_BrightnessTemp": (fs.GOES_ABI_LOWER_LEVEL_WV_DIR, fs.GUI_GOES_C10_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C11_BrightnessTemp": (fs.GOES_ABI_CLD_TOP_PHASE_DIR, fs.GUI_GOES_C11_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C12_BrightnessTemp": (fs.GOES_ABI_OZONE_DIR, fs.GUI_GOES_C12_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C13_BrightnessTemp": (fs.GOES_ABI_CLEAN_LWIR_DIR, fs.GUI_GOES_C13_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C14_BrightnessTemp": (fs.GOES_ABI_LONGWAVE_IR_DIR, fs.GUI_GOES_C14_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C15_BrightnessTemp": (fs.GOES_ABI_DIRTY_LWIR_DIR, fs.GUI_GOES_C15_DIR, "brightness_temp_from_rad"),
        "GOES_ABI_C16_BrightnessTemp": (fs.GOES_ABI_CO2_LWIR_DIR, fs.GUI_GOES_C16_DIR, "brightness_temp_from_rad"),
    }

    assert set(expected).issubset(by_name)
    for name, (filepath, outdir, value_transform) in expected.items():
        layer = by_name[name]
        assert layer["source_type"] == "goes_abi"
        assert layer["filepath"] == filepath
        assert layer["outdir"] == outdir
        assert layer["value_transform"] == value_transform

    assert by_name["GOES_ABI_C01_Reflectance"]["colormap_key"] == "GOES_RGB_Raw"
    assert by_name["GOES_ABI_C02_Reflectance"]["colormap_key"] == "GOES_RGB_Raw"
    assert by_name["GOES_ABI_C03_Reflectance"]["colormap_key"] == "GOES_RGB_Raw"
    assert by_name["GOES_ABI_C12_BrightnessTemp"]["colormap_key"] == "GOES_ABI_C12_BrightnessTemp"
    assert by_name["GOES_ABI_C13_BrightnessTemp"]["colormap_key"] == "GOES_IR"
    assert by_name["GOES_ABI_C14_BrightnessTemp"]["colormap_key"] == "GOES_IR"
    assert by_name["GOES_ABI_C15_BrightnessTemp"]["colormap_key"] == "GOES_IR"
    assert by_name["GOES_ABI_C16_BrightnessTemp"]["colormap_key"] == "GOES_ABI_C16_BrightnessTemp"


def test_goes_readiness_distinguishes_abi_from_glm_only(monkeypatch, tmp_path):
    import types
    from common.pipeline.goes_readiness import check_local_glm_ready, check_local_goes_ready

    target_dt = datetime(2026, 4, 19, 0, 10, tzinfo=timezone.utc)

    glm_dir = tmp_path / "GLM"
    glm_dir.mkdir(parents=True)
    (glm_dir / "OR_GLM-L2-LCFA_G19_s20261090010000.nc").write_bytes(b"glm")

    abi_dir = tmp_path / "VisibleRed"
    abi_dir.mkdir(parents=True)

    goes_ready, goes_path = check_local_goes_ready(
        target_dt,
        specs=[{"name": "GOES_ABI_C02_Reflectance", "filepath": abi_dir}],
    )
    glm_ready, glm_path = check_local_glm_ready(
        target_dt,
        specs=[types.SimpleNamespace(outdir=glm_dir, is_glm=True)],
    )

    assert goes_ready is False
    assert goes_path is None
    assert glm_ready is True
    assert glm_path is not None
