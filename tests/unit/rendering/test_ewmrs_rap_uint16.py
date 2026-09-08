import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from EWMRS.rap.config import get_rap_uint16_layers, uint16_nodata
from EWMRS.rap.uint16_pipeline import run_rap_uint16_pipeline, scale_to_uint16


def test_default_config_defines_rap_uint16_layers():
    layers = get_rap_uint16_layers()
    layer_by_name = {layer["name"]: layer for layer in layers}

    assert "RAP_Temperature_2m" in layer_by_name
    assert all("outdir" in layer for layer in layers)
    assert all("scale" in layer for layer in layers)
    assert all("colormap_key" in layer for layer in layers if layer.get("colormap_key") is not None)
    # Check colormap key mappings
    for layer in layers:
        name = layer["name"]
        if layer.get("colormap_key") is None:
            continue
        if name in ("RAP_CAPE_Surface", "RAP_MLCAPE", "RAP_MUCAPE", "RAP_CAPE_0_3km"):
            assert layer["colormap_key"] == "RAP_CAPE"
        elif name in ("RAP_SRH_0-3km", "RAP_SRH_0-1km"):
            assert layer["colormap_key"] == "RAP_SRH"
        elif name.startswith("RAP_Temperature_"):
            if name in ("RAP_Temperature_2m", "RAP_Temperature_Surface", "RAP_Temperature_925mb", "RAP_Temperature_850mb", "RAP_Temperature_700mb"):
                assert layer["colormap_key"] == "RAP_Temperature_LL"
            else:
                assert layer["colormap_key"] == "RAP_Temperature_HL"
        elif name.startswith("RAP_RelativeHumidity_"):
            assert layer["colormap_key"] == "RAP_RelativeHumidity"
        elif name.startswith("RAP_UWind_") or name.startswith("RAP_VWind_"):
            # Wind colormap assignment handled by _wind_colormap_key
            expected = "RAP_Wind_LL" if name.endswith("_10m") or name.endswith("_925mb") or name.endswith("_850mb") else \
                       "RAP_Wind_ML" if name.endswith("_700mb") or name.endswith("_500mb") else \
                       "RAP_Wind_HL"
            assert layer["colormap_key"] == expected
        else:
            assert layer["colormap_key"] == name
    assert all(not layer["outdir"].name.startswith("RAP_") for layer in layers)
    assert layer_by_name["RAP_Temperature_2m"]["outdir"].name == "Temperature_2m"

    expected_instability_layers = {
        "RAP_CAPE_Surface": {
            "short_names": ["cape"],
            "filter": {"typeOfLevel": "surface", "level": 0},
            "scale": {"min": 0.0, "max": 6000.0},
            "colormap_key": "RAP_CAPE",
        },
        "RAP_CIN_Surface": {
            "short_names": ["cin"],
            "filter": {"typeOfLevel": "surface", "level": 0},
            "scale": {"min": -1000.0, "max": 0.0},
            "colormap_key": "RAP_CIN_Surface",
        },
        "RAP_SRH_0-3km": {
            "short_names": ["hlcy"],
            "filter": {"typeOfLevel": "heightAboveGroundLayer", "level": 3000},
            "scale": {"min": -500.0, "max": 1000.0},
            "colormap_key": "RAP_SRH",
        },
        "RAP_SRH_0-1km": {
            "short_names": ["hlcy"],
            "filter": {"typeOfLevel": "heightAboveGroundLayer", "level": 1000},
            "scale": {"min": -500.0, "max": 1000.0},
            "colormap_key": "RAP_SRH",
        },
        "RAP_MLCAPE": {
            "short_names": ["cape"],
            "filter": {"typeOfLevel": "pressureFromGroundLayer", "level": 9000},
            "scale": {"min": 0.0, "max": 6000.0},
            "colormap_key": "RAP_CAPE",
        },
        "RAP_MLCIN": {
            "short_names": ["cin"],
            "filter": {"typeOfLevel": "pressureFromGroundLayer", "level": 9000},
            "scale": {"min": -1000.0, "max": 0.0},
            "colormap_key": "RAP_MLCIN",
        },
        "RAP_MUCAPE": {
            "short_names": ["cape"],
            "filter": {"typeOfLevel": "pressureFromGroundLayer", "level": 25500},
            "scale": {"min": 0.0, "max": 6000.0},
            "colormap_key": "RAP_CAPE",
        },
        "RAP_MUCIN": {
            "short_names": ["cin"],
            "filter": {"typeOfLevel": "pressureFromGroundLayer", "level": 25500},
            "scale": {"min": -1000.0, "max": 0.0},
            "colormap_key": "RAP_MUCIN",
        },
        "RAP_CAPE_0_3km": {
            "short_names": ["cape"],
            "filter": {"typeOfLevel": "heightAboveGroundLayer", "level": 0},
            "scale": {"min": 0.0, "max": 6000.0},
            "colormap_key": "RAP_CAPE",
        },
        "RAP_LiftedIndex_Surface_500_1000mb": {
            "short_names": ["lftx"],
            "filter": {"typeOfLevel": "isobaricLayer", "level": 500},
            "scale": {"min": -15.0, "max": 15.0},
        },
        "RAP_AbsoluteVorticity_500mb": {
            "short_names": ["absv"],
            "filter": {"typeOfLevel": "isobaricInhPa", "level": 500},
            "scale": {"min": -0.0002, "max": 0.0002},
        },
    }
    for name, expected in expected_instability_layers.items():
        layer = layer_by_name[name]
        assert layer["short_names"] == expected["short_names"]
        assert layer["filter"] == expected["filter"]
        assert layer["scale"] == expected["scale"]

    expected_surface_layers = {
        "RAP_ThetaE_Surface": {"short_names": ["papt"], "filter": {"typeOfLevel": "surface", "level": 0}},
        "RAP_MSLP_Surface": {"short_names": ["prmsl"], "filter": {"typeOfLevel": "surface", "level": 0}},
        "RAP_SnowDepth_Surface": {"short_names": ["sde"], "filter": {"typeOfLevel": "surface", "level": 0}},
        "RAP_WetBulbZeroHeight": {"short_names": ["gh"], "filter": {"typeOfLevel": "lowestLevelWetBulb0", "level": 0}},
    }
    for name, expected in expected_surface_layers.items():
        layer = layer_by_name[name]
        assert layer["short_names"] == expected["short_names"]
        assert layer["filter"] == expected["filter"]

    for removed_name in (
        "RAP_PrecipitationRate_Surface",
        "RAP_TotalPrecipitation_Surface",
        "RAP_FreezingRain_Surface",
    ):
        assert removed_name not in layer_by_name

    pressure_levels = (925, 850, 700, 500, 250)
    for level in pressure_levels:
        temperature_name = f"RAP_Temperature_{level}mb"
        temperature_layer = layer_by_name[temperature_name]
        assert temperature_layer["short_names"] == ["t"]
        assert temperature_layer["filter"] == {"typeOfLevel": "isobaricInhPa", "level": level}
        assert temperature_layer["scale"] == {"min": 180.0, "max": 330.0}

        rh_name = f"RAP_RelativeHumidity_{level}mb"
        rh_layer = layer_by_name[rh_name]
        assert rh_layer["short_names"] == ["r"]
        assert rh_layer["filter"] == {"typeOfLevel": "isobaricInhPa", "level": level}
        assert rh_layer["scale"] == {"min": 0.0, "max": 100.0}

        for component, short_name in (("U", "u"), ("V", "v")):
            name = f"RAP_{component}Wind_{level}mb"
            layer = layer_by_name[name]
            assert layer["short_names"] == [short_name]
            assert layer["filter"] == {"typeOfLevel": "isobaricInhPa", "level": level}
            assert layer["scale"] == {"min": -80.0, "max": 80.0}
            assert layer["outdir"].name == name.removeprefix("RAP_")


def test_rap_colormap_keys_exist_in_colormaps_catalog():
    layers = get_rap_uint16_layers()
    colormaps_path = Path(__file__).resolve().parents[3] / "src" / "EWMRS" / "colormaps.json"
    colormaps = json.loads(colormaps_path.read_text(encoding="utf-8"))[0]["colormaps"]
    colormap_names = {entry["name"] for entry in colormaps}

    for layer in layers:
        key = layer.get("colormap_key")
        if key is None:
            continue
        assert key in colormap_names



def test_scale_to_uint16_clips_and_reserves_nodata():
    values = np.array([[-10.0, 0.0, 50.0, 100.0, 150.0, np.nan]])

    encoded = scale_to_uint16(values, {"min": 0.0, "max": 100.0})

    assert encoded.dtype == np.dtype("<u2")
    assert encoded.tolist() == [[0, 0, 32767, 65534, 65534, uint16_nodata()]]


def test_rap_uint16_pipeline_writes_entire_grid_array_and_metadata(tmp_path):
    rap_file = tmp_path / "RAP.20260427-13z.awp130pgrbf00.grib2"
    rap_file.write_bytes(b"grib")
    layer = {
        "name": "RAP_TestLayer",
        "colormap_key": "RAP_TestLayer",
        "short_names": ["2t"],
        "filter": {"typeOfLevel": "heightAboveGround", "level": 2},
        "units": "K",
        "scale": {"min": 180.0, "max": 330.0},
        "outdir": tmp_path / "gui" / "RAP" / "TestLayer",
    }

    def mock_codes_get_string(gid, key):
        if key == "shortName":
            return "2t"
        if key == "typeOfLevel":
            return "heightAboveGround"
        raise KeyError(key)

    def mock_codes_get_long(gid, key):
        values = {"level": 2, "Ni": 3, "Nj": 2}
        if key in values:
            return values[key]
        raise KeyError(key)

    def mock_codes_get_double_array(gid, key):
        if key == "values":
            return np.array([180.0, 210.0, 240.0, 270.0, 330.0, np.nan])
        raise KeyError(key)

    with patch("EWMRS.rap.uint16_pipeline.eccodes.codes_grib_multi_support_on"), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_grib_new_from_file", side_effect=["msg", None]), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_get_string", side_effect=mock_codes_get_string), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_get_long", side_effect=mock_codes_get_long), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_get_double_array", side_effect=mock_codes_get_double_array), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_get_double", side_effect=KeyError("missingValue")), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_release"):
        timings = {}
        results = run_rap_uint16_pipeline(
            rap_file,
            dt=datetime(2026, 4, 27, 14, 0, tzinfo=timezone.utc),
            layers=[layer],
            timings=timings,
        )

    data_path = layer["outdir"] / "20260427-130000" / "data.u16"
    metadata_path = layer["outdir"] / "20260427-130000" / "metadata.json"
    index_path = layer["outdir"] / "index.json"

    assert results == {"RAP_TestLayer": data_path}
    assert data_path.is_file()
    written_values = np.fromfile(data_path, dtype="<u2")
    assert written_values.size == 6
    assert written_values.tolist() == [0, 13107, 26214, 39320, 65534, uint16_nodata()]

    metadata = json.loads(metadata_path.read_text())
    assert metadata["shape"] == [2, 3]
    assert metadata["grid"] == {"ni": 3, "nj": 2, "point_count": 6}
    assert metadata["dtype"] == "uint16"
    assert metadata["byte_order"] == "little_endian"
    assert metadata["grib"] == {"shortName": "2t", "typeOfLevel": "heightAboveGround", "level": 2}
    assert metadata["colormap_key"] == "RAP_TestLayer"
    assert metadata["timestamp"] == "20260427-130000"
    assert metadata["source_file"] == rap_file.name

    index = json.loads(index_path.read_text())
    assert index["timestamps"] == ["20260427-130000"]
    assert index["missing_value"] == uint16_nodata()
    assert metadata["conversion_time_seconds"] >= 0.0
    assert timings["RAP_TestLayer"]["status"] == "converted"
    assert timings["RAP_TestLayer"]["point_count"] == 6


def test_rap_uint16_pipeline_reports_missing_layer_without_failing(tmp_path):
    rap_file = tmp_path / "RAP.20260427-13z.awp130pgrbf00.grib2"
    rap_file.write_bytes(b"grib")
    layer = {
        "name": "RAP_MissingLayer",
        "short_names": ["2t"],
        "filter": {"typeOfLevel": "heightAboveGround", "level": 2},
        "scale": {"min": 180.0, "max": 330.0},
        "outdir": tmp_path / "gui" / "RAP" / "MissingLayer",
    }

    with patch("EWMRS.rap.uint16_pipeline.eccodes.codes_grib_multi_support_on"), \
         patch("EWMRS.rap.uint16_pipeline.eccodes.codes_grib_new_from_file", return_value=None):
        timings = {}
        results = run_rap_uint16_pipeline(rap_file, layers=[layer], timings=timings)

    assert results == {"RAP_MissingLayer": None}
    assert not (layer["outdir"] / "index.json").exists()
    assert timings["RAP_MissingLayer"] == {
        "status": "missing",
        "seconds": None,
        "output_path": None,
    }
