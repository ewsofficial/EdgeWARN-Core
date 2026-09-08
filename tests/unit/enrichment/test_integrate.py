import json

import pytest
import numpy as np
import xarray as xr
import util.file as fs
from unittest.mock import MagicMock, patch
from EdgeWARN.process.integrate.core import integrator as integrator_module
from EdgeWARN.process.integrate.config import get_datasets_config
from EdgeWARN.process.integrate.integrate import StormCellIntegrator
from EdgeWARN.process.integrate.azshear.integration import _build_search_polygons, _open_azshear_dataset
from EdgeWARN.process.integrate.azshear.metrics import compute_component_metrics
from EdgeWARN.process.integrate.geometry.cell_polygon import StormIntegrationUtils

@pytest.fixture
def mock_io_manager():
    return MagicMock()

@pytest.fixture
def integrator(mock_io_manager):
    return StormCellIntegrator(mock_io_manager)

@pytest.fixture
def synthetic_dataset(tmp_path):
    """Create a synthetic netcdf file for testing integration logic"""
    # 0.01 degree grid, covering 30-40N, -100 to -90W
    lat = np.linspace(30.0, 31.0, 101) # 0.01 spacing
    lon = np.linspace(-96.0, -95.0, 101)
    
    # Create simple pattern: Value = lat index + lon index (gradient)
    # shape (101, 101)
    data = np.zeros((101, 101))
    
    # Place a "hotspot" value of 100 at center, with 50 surrounding to pull down mean
    data[49:52, 49:52] = 50.0
    data[50, 50] = 100.0
    
    # Gradient background
    for i in range(101):
        for j in range(101):
            if data[i, j] == 0:
                data[i, j] = (i + j) / 10.0 # mostly 0-20
    
    ds = xr.Dataset(
        data_vars=dict(
            test_var=(["latitude", "longitude"], data)
        ),
        coords=dict(
            latitude=(["latitude"], lat),
            longitude=(["longitude"], lon),
        ),
        attrs=dict(description="Synthetic Test Data")
    )
    
    path = tmp_path / "synthetic_test.nc"
    ds.to_netcdf(path)
    return str(path)


@pytest.fixture
def synthetic_dataset_360(tmp_path):
    """Create a synthetic netcdf file with 0-360 longitude coordinates."""
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(264.0, 265.0, 101)

    data = np.zeros((101, 101))
    data[50, 50] = 100.0

    ds = xr.Dataset(
        data_vars=dict(
            test_var=(["latitude", "longitude"], data)
        ),
        coords=dict(
            latitude=(["latitude"], lat),
            longitude=(["longitude"], lon),
        ),
        attrs=dict(description="Synthetic 0-360 Longitude Test Data")
    )

    path = tmp_path / "synthetic_test_360.nc"
    ds.to_netcdf(path)
    return str(path)


@pytest.fixture
def synthetic_dataset_2d_coords(tmp_path):
    """Create a synthetic netcdf file with 2D latitude/longitude coordinates."""
    lat_1d = np.linspace(30.0, 31.0, 101)
    lon_1d = np.linspace(-96.0, -95.0, 101)
    lon_2d, lat_2d = np.meshgrid(lon_1d, lat_1d)

    data = np.zeros((101, 101))
    data[50, 50] = 100.0

    ds = xr.Dataset(
        data_vars=dict(
            test_var=(["y", "x"], data)
        ),
        coords=dict(
            latitude=(["y", "x"], lat_2d),
            longitude=(["y", "x"], lon_2d),
        ),
        attrs=dict(description="Synthetic 2D Coordinate Test Data")
    )

    path = tmp_path / "synthetic_test_2d.nc"
    ds.to_netcdf(path)
    return str(path)

def test_integrate_multi_stats(integrator, synthetic_dataset):
    """Test integrate_multi_stats with various statistical configs"""
    
    # Defined hotspot at idx (50, 50) -> lat=30.5, lon=-95.5
    # create_cell_polygon expects 'bbox' list of [lat, lon] tuples
    
    # Box expanded to STRICTLY contain 3x3 grid points (49, 50, 51)
    # Grid points are at .49, .50, .51
    # Box should be .485 to .515
    bbox = [
        [30.485, -95.515],
        [30.485, -95.485],
        [30.515, -95.485],
        [30.515, -95.515],
        [30.485, -95.515]
    ]
    
    cell = {
        "id": "test_cell_1",
        "bbox": bbox,
        "centroid": [30.5, -95.5],
        "properties": {}
    }
    
    cells = [cell]
    
    stats_config = [
        {"key": "p100Test", "method": "max"},
        {"key": "p90Test", "method": "percentile", "percentile": 90},
        {"key": "MeanTest", "method": "mean"}
    ]
    
    # Run integration
    result = integrator.integrate_multi_stats(synthetic_dataset, cells, stats_config)
    
    props = result[0]["properties"]
    
    # Max should be 100.0 (the hotspot pixel)
    assert props["p100Test"] == 100.0
    
    # Mean should be significantly lower as it includes neighbors
    # Neighbors are ~10.0
    assert props["MeanTest"] < 100.0
    assert props["MeanTest"] > 0.0
    
    # p90 should be close to max if the hotspot dominates, or lower if many pixels
    assert props["p90Test"] <= 100.0
    assert props["p90Test"] > 0.0
    assert props["MeanTest"] == round(props["MeanTest"], 2)
    assert props["p90Test"] == round(props["p90Test"], 2)


def test_datasets_config_includes_p90_echotop30():
    configs = get_datasets_config()

    assert {
        "name": "EchoTop30 (90th)",
        "filepath": fs.MRMS_ECHOTOP30_DIR,
        "key": "p90EchoTop30",
        "method": "percentile",
        "percentile": 90,
    } in configs


def test_datasets_config_includes_p90_echotop50_only():
    configs = get_datasets_config()

    assert {
        "name": "EchoTop50 (90th)",
        "filepath": fs.MRMS_ECHOTOP50_DIR,
        "key": "p90EchoTop50",
        "method": "percentile",
        "percentile": 90,
    } in configs
    assert not any(conf.get("key") == "maxEchoTop50" for conf in configs)


def test_integrate_ds_via_max_rounds_to_two_decimals(integrator, tmp_path):
    lat = np.array([30.0, 30.01, 30.02])
    lon = np.array([-95.02, -95.01, -95.0])
    data = np.array([
        [1.0, 2.0, 3.0],
        [4.0, 12.346, 6.0],
        [7.0, 8.0, 9.0],
    ])

    ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], data)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    path = tmp_path / "synthetic_max_rounding.nc"
    ds.to_netcdf(path)

    cell = {
        "id": "test_cell_max_rounding",
        "bbox": [
            [30.005, -95.015],
            [30.005, -95.005],
            [30.015, -95.005],
            [30.015, -95.015],
            [30.005, -95.015],
        ],
        "centroid": [30.01, -95.01],
        "properties": {},
    }

    result = integrator.integrate_ds_via_max(str(path), [cell], "MaxTest")

    assert result[0]["properties"]["MaxTest"] == 12.35


def test_integrate_multi_stats_rounds_to_two_decimals(integrator, tmp_path):
    lat = np.array([30.0, 30.01, 30.02])
    lon = np.array([-95.02, -95.01, -95.0])
    data = np.array([
        [1.111, 2.222, 3.333],
        [4.444, 5.556, 6.666],
        [7.777, 8.888, 9.999],
    ])

    ds = xr.Dataset(
        data_vars=dict(test_var=(["latitude", "longitude"], data)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    path = tmp_path / "synthetic_multi_rounding.nc"
    ds.to_netcdf(path)

    cell = {
        "id": "test_cell_multi_rounding",
        "bbox": [
            [30.005, -95.015],
            [30.005, -95.005],
            [30.015, -95.005],
            [30.015, -95.015],
            [30.005, -95.015],
        ],
        "centroid": [30.01, -95.01],
        "properties": {},
    }

    result = integrator.integrate_multi_stats(
        str(path),
        [cell],
        [
            {"key": "MaxRounded", "method": "max"},
            {"key": "MeanRounded", "method": "mean"},
            {"key": "P90Rounded", "method": "percentile", "percentile": 90},
        ],
    )

    props = result[0]["properties"]
    assert props["MaxRounded"] == 5.56
    assert props["MeanRounded"] == 5.56
    assert props["P90Rounded"] == 5.56

def test_output_decimals_is_live_at_both_integrator_sites(integrator, tmp_path, override_integration_config):
    """Raising `output.decimals` must change what both entry points write.

    The two round-to-2 tests above would still pass against a hardcoded 2, so
    they cannot tell a wired key from an inert one.
    """
    override_integration_config("output", "decimals", 4)

    lat = np.array([30.0, 30.01, 30.02])
    lon = np.array([-95.02, -95.01, -95.0])
    data = np.array([
        [1.0, 2.0, 3.0],
        [4.0, 12.3456789, 6.0],
        [7.0, 8.0, 9.0],
    ])
    bbox = [
        [30.005, -95.015],
        [30.005, -95.005],
        [30.015, -95.005],
        [30.015, -95.015],
        [30.005, -95.015],
    ]

    def cell():
        return {"id": "c", "bbox": bbox, "centroid": [30.01, -95.01], "properties": {}}

    coords = dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon))

    max_path = tmp_path / "decimals_max.nc"
    xr.Dataset(data_vars=dict(unknown=(["latitude", "longitude"], data)), coords=coords).to_netcdf(max_path)
    result = integrator.integrate_ds_via_max(str(max_path), [cell()], "MaxTest")
    assert result[0]["properties"]["MaxTest"] == 12.3457

    stats_path = tmp_path / "decimals_stats.nc"
    xr.Dataset(data_vars=dict(test_var=(["latitude", "longitude"], data)), coords=coords).to_netcdf(stats_path)
    result = integrator.integrate_multi_stats(
        str(stats_path), [cell()], [{"key": "MaxStat", "method": "max"}]
    )
    assert result[0]["properties"]["MaxStat"] == 12.3457


def test_integrate_empty_intersection(integrator, synthetic_dataset):
    """Test integration where cell is outside dataset bounds"""
    # Create cell far away (e.g., lat 40, lon -80)
    bbox = [
        [40.0, -80.0],
        [40.0, -79.9],
        [40.1, -79.9],
        [40.1, -80.0]
    ]
    
    cell = {
        "id": "test_cell_outside",
        "bbox": bbox,
        "centroid": [40.05, -79.95],
        "properties": {}
    }
    
    stats_config = [{"key": "p100Zero", "method": "max"}]
    
    result = integrator.integrate_multi_stats(synthetic_dataset, [cell], stats_config)
    
    # Should get 0
    assert result[0]["properties"]["p100Zero"] == 0


def test_integrate_multi_stats_with_360_longitudes(integrator, synthetic_dataset_360):
    """Integration should handle datasets using 0-360 longitude coordinates."""
    cell = {
        "id": "test_cell_360",
        "bbox": [
            [30.495, 264.495],
            [30.495, 264.505],
            [30.505, 264.505],
            [30.505, 264.495],
            [30.495, 264.495],
        ],
        "centroid": [30.5, 264.5],
        "properties": {}
    }

    result = integrator.integrate_multi_stats(
        synthetic_dataset_360,
        [cell],
        [{"key": "p100Test", "method": "max"}]
    )

    assert result[0]["properties"]["p100Test"] == 100.0


def test_integrate_multi_stats_with_2d_coords(integrator, synthetic_dataset_2d_coords):
    """Integration should handle datasets with 2D latitude/longitude coordinates."""
    cell = {
        "id": "test_cell_2d",
        "bbox": [
            [30.495, -95.505],
            [30.495, -95.495],
            [30.505, -95.495],
            [30.505, -95.505],
            [30.495, -95.505],
        ],
        "centroid": [30.5, -95.5],
        "properties": {}
    }

    result = integrator.integrate_multi_stats(
        synthetic_dataset_2d_coords,
        [cell],
        [{"key": "p100Test", "method": "max"}]
    )

    assert result[0]["properties"]["p100Test"] == 100.0


def test_integrate_multi_stats_reuses_spatial_lookup_cache(integrator, synthetic_dataset):
    cell = {
        "id": "test_cell_cache",
        "bbox": [
            [30.495, -95.505],
            [30.495, -95.495],
            [30.505, -95.495],
            [30.505, -95.505],
            [30.495, -95.505],
        ],
        "centroid": [30.5, -95.5],
        "properties": {},
    }
    cells = [cell]
    cell_contexts = integrator.build_cell_contexts(cells)

    with patch("EdgeWARN.process.integrate.core.integrator.build_spatial_lookup", wraps=integrator_module.build_spatial_lookup) as mock_build_lookup:
        integrator.integrate_multi_stats(
            synthetic_dataset,
            cells,
            [{"key": "MaxOne", "method": "max"}],
            cell_contexts=cell_contexts,
        )
        integrator.integrate_multi_stats(
            synthetic_dataset,
            cells,
            [{"key": "MaxTwo", "method": "max"}],
            cell_contexts=cell_contexts,
        )

    assert mock_build_lookup.call_count == 1

def test_integrate_error_handling(integrator):
    """Test handling of invalid file path"""
    cell = {
        "id": "cell_1",
        "bbox": [[0,0], [0,1], [1,1], [1,0]],
        "properties": {}
    }
    
    stats_config = [{"key": "p100Err", "method": "max"}]
    
    # Invalid path
    result = integrator.integrate_multi_stats("/invalid/path/test.nc", [cell], stats_config)
    
    assert "p100Err" not in result[0]["properties"]


@pytest.fixture
def synthetic_azshear_dataset_pair(tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))

    # Positioned just east of the storm polygon so the near-cell search halo
    # sees the signature while the raw storm polygon does not.
    low[48:53, 52] = 8.6
    low[50, 52] = 10.8
    mid[48:53, 52] = 6.4
    mid[50, 52] = 8.1

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=( ["latitude"], lat), longitude=( ["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=( ["latitude"], lat), longitude=( ["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low.nc"
    mid_path = tmp_path / "azshear_mid.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)
    return str(low_path), str(mid_path)


def _base_azshear_cell(cell_id):
    return {
        "id": cell_id,
        "bbox": [
            [30.49, -95.53],
            [30.49, -95.49],
            [30.51, -95.49],
            [30.51, -95.53],
            [30.49, -95.53],
        ],
        "centroid": [30.5, -95.51],
        "properties": {},
    }


def test_integrate_azshear_features_replaces_legacy_schema(integrator, synthetic_azshear_dataset_pair, synthetic_dataset):
    low_path, mid_path = synthetic_azshear_dataset_pair
    cell = _base_azshear_cell("test_cell_buffered_azshear")
    cell["properties"]["ExistingField"] = 123.0

    stats_result = integrator.integrate_multi_stats(
        synthetic_dataset,
        [cell],
        [{"key": "p100Test", "method": "max"}],
    )
    assert stats_result[0]["properties"]["ExistingField"] == 123.0

    result = integrator.integrate_azshear_features(low_path, mid_path, stats_result)
    props = result[0]["properties"]
    azshear = props["azshear"]

    assert props["ExistingField"] == 123.0
    assert props["p100Test"] >= 0.0
    assert azshear["buffer_km"] == 1.5
    assert "alignment" not in azshear
    assert "low_candidate_count" not in azshear
    assert "mid_candidate_count" not in azshear

    low = azshear["low"]
    mid = azshear["mid"]
    cross = azshear["cross_layer"]

    assert low["core_structure"]["component_count"] == 1
    assert low["core_structure"]["largest_component_peak_azshear"] == 10.8
    assert low["core_structure"]["largest_component_area"] > 0.0
    assert low["dominance"]["dominance_ratio"] == 1.0
    assert low["distribution"]["coverage_fraction"] > 0.0
    assert low["linearity"]["alignment_with_reflectivity_axis"] >= 0.0
    assert low["persistence"]["dominant_component_persistence"] == 0.0

    assert mid["core_structure"]["component_count"] == 1
    assert mid["core_structure"]["largest_component_peak_azshear"] == 8.1
    assert mid["distribution"]["total_azshear_area"] > 0.0

    assert cross["dominant_component_overlap_area"] > 0.0
    assert cross["dominant_component_overlap_ratio"] > 0.0
    assert cross["dominant_component_centroid_distance_km"] is not None
    assert cross["ll_ml_peak_ratio"] > 1.0
    assert cross["simultaneous_persistence"] == 0.0
    assert cross["pair_count"] == 1


def test_integrate_azshear_features_uses_fallback_buffer_for_centroid_cells(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))
    low[48:53, 51] = 8.6
    low[50, 51] = 10.4
    mid[48:53, 51] = 6.4
    mid[50, 51] = 7.8

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_fallback.nc"
    mid_path = tmp_path / "azshear_mid_fallback.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cell = {
        "id": "test_cell_centroid_fallback",
        "centroid": [30.5, -95.51],
        "properties": {},
    }

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear is not None
    assert azshear["buffer_km"] == 1.5
    assert azshear["low"]["core_structure"]["component_count"] == 1
    assert azshear["mid"]["core_structure"]["component_count"] == 1


def test_build_search_polygons_are_disjoint_for_adjacent_cells():
    cells = [
        _base_azshear_cell("left_neighbor"),
        {
            "id": "right_neighbor",
            "bbox": [
                [30.49, -95.48],
                [30.49, -95.44],
                [30.51, -95.44],
                [30.51, -95.48],
                [30.49, -95.48],
            ],
            "centroid": [30.5, -95.46],
            "properties": {},
        },
    ]

    raw_polys = [StormIntegrationUtils.create_cell_polygon(cell) for cell in cells]
    search_polys = _build_search_polygons(raw_polys)

    assert len(search_polys) == 2
    assert search_polys[0].intersection(search_polys[1]).area == pytest.approx(0.0, abs=1e-9)


def test_integrate_azshear_features_handles_missing_signal(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)
    zeros = xr.Dataset(
        data_vars=dict(unknown=( ["latitude", "longitude"], np.zeros((101, 101)))),
        coords=dict(latitude=( ["latitude"], lat), longitude=( ["longitude"], lon)),
    )
    zero_path = tmp_path / "azshear_zero.nc"
    zeros.to_netcdf(zero_path)

    cell = _base_azshear_cell("test_cell_no_azshear")

    result = integrator.integrate_azshear_features(str(zero_path), str(zero_path), [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear is None


def test_integrate_azshear_features_applies_updated_thresholds(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))
    low[48:53, 52] = 7.9
    mid[48:53, 52] = 5.9

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_threshold.nc"
    mid_path = tmp_path / "azshear_mid_threshold.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cell = _base_azshear_cell("test_cell_threshold_gate")

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear is None


def test_integrate_azshear_features_requires_minimum_gate_count(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))
    low[49:53, 52] = 8.6
    mid[49:53, 52] = 6.4

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_min_gate.nc"
    mid_path = tmp_path / "azshear_mid_min_gate.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cell = _base_azshear_cell("test_cell_min_gate_filter")

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear is None


def test_integrate_azshear_features_sets_missing_layer_to_null(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))
    low[48:53, 52] = 8.6
    low[50, 52] = 10.8

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_only.nc"
    mid_path = tmp_path / "azshear_mid_empty.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cell = _base_azshear_cell("test_cell_single_layer")

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear is not None
    assert azshear["low"] is not None
    assert azshear["low"]["core_structure"]["component_count"] == 1
    assert azshear["mid"] is None
    assert azshear["cross_layer"]["dominant_component_overlap_area"] == 0.0
    assert azshear["cross_layer"]["dominant_component_centroid_distance_km"] is None


def test_integrate_azshear_features_uses_largest_component_for_core_metrics(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))

    low[60:64, 60:64] = 8.7
    low[61, 61] = 9.2

    # Smaller but higher-peak mid component.
    mid[45:48, 45:48] = 6.4
    mid[46, 46] = 9.9
    # Larger but lower-peak mid component (should be dominant by area).
    mid[60:64, 60:64] = 6.3
    mid[61, 61] = 6.5

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_pair.nc"
    mid_path = tmp_path / "azshear_mid_pair.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cell = {
        "id": "test_cell_largest_component",
        "bbox": [
            [30.4, -95.7],
            [30.4, -95.2],
            [30.9, -95.2],
            [30.9, -95.7],
            [30.4, -95.7],
        ],
        "centroid": [30.65, -95.45],
        "properties": {},
    }

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear["low"]["core_structure"]["component_count"] == 1
    assert azshear["mid"]["core_structure"]["component_count"] == 2
    assert azshear["mid"]["core_structure"]["largest_component_peak_azshear"] == 6.5
    assert azshear["mid"]["dominance"]["secondary_core_ratio"] > 0.5
    assert azshear["cross_layer"]["dominant_component_overlap_area"] > 0.0
    assert azshear["cross_layer"]["ll_ml_peak_ratio"] > 1.0


def test_integrate_azshear_features_pairs_best_low_mid_match(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))

    low[40:43, 40:43] = 8.8
    low[41, 41] = 10.0
    low[60:64, 60:64] = 8.7
    low[61, 61] = 9.2

    mid[40:44, 40:44] = 6.4
    mid[41, 41] = 6.8
    mid[60:63, 60:64] = 6.6
    mid[61, 61] = 7.2

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_best_pair.nc"
    mid_path = tmp_path / "azshear_mid_best_pair.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cell = {
        "id": "test_cell_best_pair",
        "bbox": [
            [30.4, -95.7],
            [30.4, -95.2],
            [30.9, -95.2],
            [30.9, -95.7],
            [30.4, -95.7],
        ],
        "centroid": [30.65, -95.45],
        "properties": {},
    }

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), [cell])
    azshear = result[0]["properties"]["azshear"]
    cross = azshear["cross_layer"]

    assert azshear["low"]["core_structure"]["largest_component_peak_azshear"] == 9.2
    assert azshear["mid"]["core_structure"]["largest_component_peak_azshear"] == 6.8
    assert cross["pair_count"] == 2
    assert cross["dominant_component_overlap_area"] > 0.0
    assert cross["dominant_component_centroid_distance_km"] < 1.0
    assert cross["ll_ml_peak_ratio"] == pytest.approx(1.278, abs=0.01)


def test_integrate_azshear_features_keeps_signal_with_single_non_overlapping_owner(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 101)
    lon = np.linspace(-96.0, -95.0, 101)

    low = np.zeros((101, 101))
    mid = np.zeros((101, 101))
    low[48:53, 53] = 8.6
    low[50, 53] = 10.8
    mid[48:53, 53] = 6.4
    mid[50, 53] = 8.1

    low_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], low)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    mid_ds = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], mid)),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )

    low_path = tmp_path / "azshear_low_owned.nc"
    mid_path = tmp_path / "azshear_mid_owned.nc"
    low_ds.to_netcdf(low_path)
    mid_ds.to_netcdf(mid_path)

    cells = [
        _base_azshear_cell("left_neighbor"),
        {
            "id": "right_owner",
            "bbox": [
                [30.49, -95.48],
                [30.49, -95.44],
                [30.51, -95.44],
                [30.51, -95.48],
                [30.49, -95.48],
            ],
            "centroid": [30.5, -95.46],
            "properties": {},
        },
    ]

    result = integrator.integrate_azshear_features(str(low_path), str(mid_path), cells)

    assert result[0]["properties"]["azshear"] is None
    assert result[1]["properties"]["azshear"] is not None
    assert result[1]["properties"]["azshear"]["cross_layer"]["pair_count"] == 1


def test_integrate_azshear_features_computes_history_based_persistence(integrator, synthetic_azshear_dataset_pair, tmp_path, monkeypatch):
    low_path, mid_path = synthetic_azshear_dataset_pair
    cell = _base_azshear_cell("test_cell_persistence")

    def _entry(low_count, low_peak, mid_count, mid_peak):
        return {
            "properties": {
                "azshear": {
                    "low": {
                        "core_structure": {
                            "component_count": low_count,
                            "largest_component_peak_azshear": low_peak,
                        }
                    },
                    "mid": {
                        "core_structure": {
                            "component_count": mid_count,
                            "largest_component_peak_azshear": mid_peak,
                        }
                    },
                }
            }
        }

    history = [
        _entry(1, 8.8, 1, 6.7),  # oldest; excluded by last-5 persistence window
        _entry(1, 8.5, 1, 6.5),
        _entry(0, 0.0, 1, 5.5),
        _entry(1, 7.0, 0, 0.0),
        _entry(1, 8.2, 1, 6.1),
        _entry(0, 0.0, 0, 0.0),
    ]

    history_dir = tmp_path / "cells"
    history_dir.mkdir(parents=True, exist_ok=True)
    with open(history_dir / "test_cell_persistence.json", "w") as f:
        json.dump(history, f)

    monkeypatch.setattr(fs, "CELL_DIR", history_dir)

    result = integrator.integrate_azshear_features(low_path, mid_path, [cell])
    azshear = result[0]["properties"]["azshear"]

    assert azshear["low"]["persistence"]["dominant_component_persistence"] == 0.6
    assert azshear["low"]["persistence"]["peak_persistence"] == 0.4
    assert azshear["mid"]["persistence"]["dominant_component_persistence"] == 0.6
    assert azshear["mid"]["persistence"]["peak_persistence"] == 0.4
    assert azshear["cross_layer"]["simultaneous_persistence"] == 0.4


def test_integrate_azshear_features_returns_none_for_independent_empty_cells(integrator, tmp_path):
    lat = np.linspace(30.0, 31.0, 11)
    lon = np.linspace(-96.0, -95.0, 11)
    zeros = xr.Dataset(
        data_vars=dict(unknown=(["latitude", "longitude"], np.zeros((11, 11)))),
        coords=dict(latitude=(["latitude"], lat), longitude=(["longitude"], lon)),
    )
    zero_path = tmp_path / "azshear_zero_shared.nc"
    zeros.to_netcdf(zero_path)

    cells = [
        {
            "id": "cell_one",
            "bbox": [[30.4, -95.6], [30.4, -95.5], [30.5, -95.5], [30.5, -95.6], [30.4, -95.6]],
            "centroid": [30.45, -95.55],
            "properties": {},
        },
        {
            "id": "cell_two",
            "bbox": [[30.6, -95.4], [30.6, -95.3], [30.7, -95.3], [30.7, -95.4], [30.6, -95.4]],
            "centroid": [30.65, -95.35],
            "properties": {},
        },
    ]

    result = integrator.integrate_azshear_features(str(zero_path), str(zero_path), cells)

    assert result[0]["properties"]["azshear"] is None
    assert result[1]["properties"]["azshear"] is None


def test_open_azshear_dataset_uses_fast_grib_loader(monkeypatch):
    io_manager = MagicMock()
    integrator = StormCellIntegrator(io_manager)

    expected = xr.Dataset(
        data_vars={"unknown": (("latitude", "longitude"), np.zeros((1, 1)))},
        coords={"latitude": [30.0], "longitude": [-95.0]},
    )

    calls = {"fast": 0, "xarray": 0}

    def fake_fast(path):
        calls["fast"] += 1
        return expected

    def fake_open_dataset(*args, **kwargs):
        calls["xarray"] += 1
        raise AssertionError("xarray open_dataset should not be called for successful GRIB fast-load")

    monkeypatch.setattr("EdgeWARN.process.integrate.azshear.integration.load_grib_fast", fake_fast)
    monkeypatch.setattr("EdgeWARN.process.integrate.azshear.integration.xr.open_dataset", fake_open_dataset)

    ds, is_grib = _open_azshear_dataset(integrator, "sample.grib2")

    assert ds is expected
    assert is_grib is True
    assert calls["fast"] == 1
    assert calls["xarray"] == 0


def test_open_azshear_dataset_falls_back_when_fast_loader_fails(monkeypatch):
    io_manager = MagicMock()
    integrator = StormCellIntegrator(io_manager)

    fallback_ds = xr.Dataset(
        data_vars={"unknown": (("latitude", "longitude"), np.zeros((1, 1)))},
        coords={"latitude": [30.0], "longitude": [-95.0]},
    )

    calls = {"fast": 0, "xarray": 0}

    def fake_fast(path):
        calls["fast"] += 1
        raise RuntimeError("forced fast loader failure")

    def fake_open_dataset(path, decode_timedelta=True):
        calls["xarray"] += 1
        return fallback_ds

    monkeypatch.setattr("EdgeWARN.process.integrate.azshear.integration.load_grib_fast", fake_fast)
    monkeypatch.setattr("EdgeWARN.process.integrate.azshear.integration.xr.open_dataset", fake_open_dataset)

    ds, is_grib = _open_azshear_dataset(integrator, "sample.grib2")

    assert ds is fallback_ds
    assert is_grib is True
    assert calls["fast"] == 1
    assert calls["xarray"] == 1
    assert io_manager.write_warning.called


def test_open_azshear_dataset_uses_xarray_for_non_grib(monkeypatch):
    io_manager = MagicMock()
    integrator = StormCellIntegrator(io_manager)

    fallback_ds = xr.Dataset(
        data_vars={"unknown": (("latitude", "longitude"), np.zeros((1, 1)))},
        coords={"latitude": [30.0], "longitude": [-95.0]},
    )

    calls = {"fast": 0, "xarray": 0}

    def fake_fast(path):
        calls["fast"] += 1
        raise AssertionError("fast loader should not run for non-GRIB path")

    def fake_open_dataset(path, decode_timedelta=True):
        calls["xarray"] += 1
        return fallback_ds

    monkeypatch.setattr("EdgeWARN.process.integrate.azshear.integration.load_grib_fast", fake_fast)
    monkeypatch.setattr("EdgeWARN.process.integrate.azshear.integration.xr.open_dataset", fake_open_dataset)

    ds, is_grib = _open_azshear_dataset(integrator, "sample.nc")

    assert ds is fallback_ds
    assert is_grib is False
    assert calls["fast"] == 0
    assert calls["xarray"] == 1


def test_compute_component_metrics_uses_compact_pixel_storage():
    values = np.array(
        [
            [0.0, 8.4, 8.7],
            [0.0, 9.1, 8.9],
            [0.0, 0.0, 0.0],
        ]
    )
    component_mask = values >= 8.0
    lat_grid = np.array(
        [
            [30.0, 30.0, 30.0],
            [30.1, 30.1, 30.1],
            [30.2, 30.2, 30.2],
        ]
    )
    lon_grid = np.array(
        [
            [-96.0, -95.9, -95.8],
            [-96.0, -95.9, -95.8],
            [-96.0, -95.9, -95.8],
        ]
    )

    metrics = compute_component_metrics(component_mask, values, lat_grid, lon_grid, 1.0, 1.0, 1.0)

    assert metrics is not None
    assert metrics["pixel_count"] == 4
    assert metrics["_pixel_lats"].shape == (4,)
    assert metrics["_pixel_lons"].shape == (4,)
    assert metrics["_pixel_bbox"] == (-95.9, 30.0, -95.8, 30.1)
    assert "_component_mask" not in metrics
    assert "_lat_grid" not in metrics
    assert "_lon_grid" not in metrics


def test_probsevere_field_map_is_live_not_frozen_at_import(override_integration_config):
    """`integrator.py` bound the whole mapping at import, so it ignored --config-dir."""
    from EdgeWARN.process.integrate.config import probsevere_field_map

    override_integration_config("probsevere_field_map", "ProbSevere", "OVERRIDDEN")
    assert probsevere_field_map()["ProbSevere"] == "OVERRIDDEN"
