"""Connected detection tests using small, deterministic weather inputs."""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import xarray as xr

from EdgeWARN.process.detect.config import DetectionConfig
from EdgeWARN.process.detect.detect import detect_cells


def _weather_inputs(tmp_path):
    lats = np.linspace(34.0, 36.0, 21)
    lons = np.linspace(262.0, 264.0, 21)
    reflectivity = np.zeros((21, 21), dtype=np.float32)
    reflectivity[6:15, 6:15] = 50.0
    radar = xr.Dataset(
        {"unknown": (("latitude", "longitude"), reflectivity)},
        coords={"latitude": lats, "longitude": lons},
    )
    radar_path = tmp_path / "MRMS_MergedReflectivityQC_3D_20260317-200000.nc"
    radar.to_netcdf(radar_path)

    probsevere = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"ID": 17},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-97.4, 34.6], [-96.6, 34.6], [-96.6, 35.4],
                    [-97.4, 35.4], [-97.4, 34.6],
                ]],
            },
        }],
    }
    probsevere_path = tmp_path / "MRMS_PROBSEVERE_20260317-200000.json"
    probsevere_path.write_text(json.dumps(probsevere))
    return radar_path, probsevere_path


def test_netcdf_reflectivity_and_probsevere_decode_into_detected_geometry(tmp_path):
    """Real loaders, gate mapping, expansion, and saving produce cell 17."""
    radar_path, probsevere_path = _weather_inputs(tmp_path)

    result = detect_cells(
        str(radar_path), str(probsevere_path), None, MagicMock(),
        34.0, 36.0, 262.0, 264.0,
        detection_config=DetectionConfig.from_yaml(),
    )

    assert [cell["id"] for cell in result] == [17]
    assert result[0]["num_gates"] > 5
    assert result[0]["max_refl"] == 50.0
    assert 34.6 <= result[0]["centroid"][0] <= 35.4
    assert 262.6 <= result[0]["centroid"][1] <= 263.4
    assert len(result[0]["bbox"]) >= 4


def test_disable_polygon_expansion_selects_probsevere_geometry_path():
    """The orchestration option bypasses GateMapper and configures the saver."""
    radar = xr.Dataset(
        {"unknown": (("latitude", "longitude"), np.full((2, 2), 50.0))},
        coords={"latitude": [35.0, 35.1], "longitude": [263.0, 263.1]},
    )
    probsevere = {"type": "FeatureCollection", "features": []}

    with (
        patch("EdgeWARN.process.detect.detect.GateMapper") as mapper,
        patch("EdgeWARN.process.detect.detect.CellDataSaver") as saver,
    ):
        saver.return_value.create_entry.return_value = []
        result = detect_cells(
            None, None, None, MagicMock(), 34, 36, 262, 264,
            radar_obj=radar,
            ps_obj=probsevere,
            disable_polygon_expansion=True,
            detection_config=DetectionConfig.from_yaml(),
        )

    assert result == []
    mapper.assert_not_called()
    assert saver.call_args.kwargs["use_probsevere_geometry"] is True
