
import pytest
import numpy as np
import xarray as xr
from dataclasses import replace
from unittest.mock import MagicMock
from EdgeWARN.process.detect.config import DetectionConfig
from EdgeWARN.process.detect.tools.gatemapper import GateMapper

class MockIOManager:
    def write_debug(self, msg):
        pass
    def write_warning(self, msg):
        pass

def test_connectivity_constraint():
    """
    Test that watershed expansion respects connectivity constraints.
    It should NOT jump across low-reflectivity gaps.
    """
    # 1. Setup Grid 20x20
    lats = np.arange(20)
    lons = np.arange(20)
    
    # 2. Create Radar Data (High Reflectivity Mask)
    # Refl threshold is 40.
    refl_data = np.zeros((20, 20))
    
    # Region 1: Connected to Seed (Polygon A)
    # Rectangle from (5,5) to (10,10)
    refl_data[5:11, 5:11] = 50 
    
    # Region 2: Disconnected Blob
    # Rectangle at (15,15) to (17,17)
    refl_data[15:18, 15:18] = 50
    
    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data),
         'latitude': lats,
         'longitude': lons}
    )
    
    # 3. Create Mapped Dataset (Polygons)
    # Polygon A covers a subset of Region 1: (5,5) to (7,7)
    polygon_grid = np.zeros((20, 20), dtype=np.int32)
    polygon_grid[5:8, 5:8] = 1 # ID 1
    
    mapped_ds = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )
    
    # 4. Initialize GateMapper
    mapper = GateMapper(radar_ds, None, MockIOManager(), DetectionConfig.from_yaml(refl_threshold=40.0))
    
    # 5. Run Expand Gates
    expanded_ds = mapper.expand_gates(mapped_ds)
    final_grid = expanded_ds['PolygonID'].values
    
    # 6. Verify Results
    
    # Check Region 1 (Connected) - Should be ID 1
    # Specifically check a point outside the original polygon but connected
    assert final_grid[9, 9] == 1, "Connected high-reflectivity pixel OUTSIDE polygon should be captured"
    
    # Check that expansion FILLED the connected blob
    assert final_grid[10, 10] == 1, "Extreme edge of connected blob should be captured"
    
    # Check Region 2 (Disconnected) - Should be 0
    # Even though we allowed expansion, it cannot jump the gap.
    assert final_grid[16, 16] == 0, "Disconnected high-reflectivity pixel should NOT be assigned an ID"
    
    # Check Background - Should match 0
    assert final_grid[0, 0] == 0

def test_merger_split():
    """
    Test that two nearby cells split reasonably (watershed behavior).
    """
    lats = np.arange(20)
    lons = np.arange(20)
    refl_data = np.zeros((20, 20))
    refl_data[5:15, 5:15] = 50 # Large block
    
    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data),
         'latitude': lats,
         'longitude': lons}
    )
    
    polygon_grid = np.zeros((20, 20), dtype=np.int32)
    polygon_grid[6, 6] = 1 # Seed A top-left
    polygon_grid[13, 13] = 2 # Seed B bottom-right
    
    mapped_ds = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )
    
    mapper = GateMapper(radar_ds, None, MockIOManager(), DetectionConfig.from_yaml(refl_threshold=40.0))
    expanded_ds = mapper.expand_gates(mapped_ds)
    final_grid = expanded_ds['PolygonID'].values
    
    # Both IDs should be present
    assert 1 in final_grid
    assert 2 in final_grid
    
    # Check that they cover the block
    assert np.all(final_grid[5:15, 5:15] > 0)


def test_disconnected_seeded_components_expand_independently():
    """
    Test that multiple disconnected marker-bearing islands are each processed.
    This specifically covers the component-partitioned watershed fast path.
    """
    lats = np.arange(30)
    lons = np.arange(30)
    refl_data = np.zeros((30, 30))

    # Two disconnected high-reflectivity islands.
    refl_data[3:10, 3:10] = 50
    refl_data[18:26, 18:27] = 50

    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data),
         'latitude': lats,
         'longitude': lons}
    )

    polygon_grid = np.zeros((30, 30), dtype=np.int32)
    polygon_grid[5, 5] = 1
    polygon_grid[21, 22] = 2

    mapped_ds = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    mapper = GateMapper(radar_ds, None, MockIOManager(), DetectionConfig.from_yaml(refl_threshold=40.0))
    expanded_ds = mapper.expand_gates(mapped_ds)
    final_grid = expanded_ds['PolygonID'].values

    assert np.all(final_grid[3:10, 3:10] == 1)
    assert np.all(final_grid[18:26, 18:27] == 2)
    assert np.all(final_grid[11:17, 11:17] == 0)

def test_dynamic_thresholding():
    """
    Test that dynamic thresholding applies correctly to strong vs weak cells.
    Strong cell (>= 45 max refl) drops to 40.
    Weak cell (< 45 max refl) drops to 37.5.
    """
    lats = np.arange(20)
    lons = np.arange(20)
    
    # Baseline mask is 37.5
    refl_data = np.zeros((20, 20))
    
    # Cell 1: Strong cell. Max refl = 50. Should threshold at max(40, 50-10) = 40.
    refl_data[2:8, 2:8] = 42 # Within expanded area
    refl_data[4:6, 4:6] = 50 # Core
    refl_data[2:8, 8] = 39   # Just below its allowed threshold, should NOT expand here
    
    # Cell 2: Weak cell. Max refl = 44. Should threshold at max(37.5, 44-10) = 37.5.
    refl_data[12:18, 12:18] = 38 # Within expanded area
    refl_data[14:16, 14:16] = 44 # Core
    refl_data[12:18, 18] = 37    # Below 37.5 baseline entirely

    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data),
         'latitude': lats,
         'longitude': lons}
    )
    
    polygon_grid = np.zeros((20, 20), dtype=np.int32)
    polygon_grid[5, 5] = 1 # ID 1 (Strong)
    polygon_grid[15, 15] = 2 # ID 2 (Weak)
    
    mapped_ds = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )
    
    mapper = GateMapper(radar_ds, None, MockIOManager(), DetectionConfig.from_yaml())
    expanded_ds = mapper.expand_gates(mapped_ds)
    final_grid = expanded_ds['PolygonID'].values
    
    # Verify Strong Cell (Thresh = 40)
    assert final_grid[4, 4] == 1, "Core should be included"
    assert final_grid[2, 2] == 1, "Area >= 40 should be included"
    assert final_grid[5, 8] == 0, "Area < 40 should NOT be included for strong cell"
    
    # Verify Weak Cell (Thresh = 37.5)
    assert final_grid[15, 15] == 2, "Core should be included"
    assert final_grid[13, 13] == 2, "Area >= 37.5 should be included"
    assert final_grid[15, 18] == 0, "Area < 37.5 should NOT be included for weak cell"


def test_dynamic_min_threshold_low_is_live_not_inert():
    """Raising the weak-cell floor must shrink the weak cell and leave the strong one.

    The test above pins the curve at its shipped values, which a re-hardcoded
    37.5 would also satisfy. The plan calls this out as the dangerous case: YAML
    that looks authoritative while the code ignores it.
    """
    lats = np.arange(20)
    lons = np.arange(20)
    refl_data = np.zeros((20, 20))
    refl_data[2:8, 2:8] = 42
    refl_data[4:6, 4:6] = 50
    # The weak core is 4x4 so it stays above `reject_clusters_at_or_below_gates`
    # once the skirt drops out; otherwise the cell vanishes for a second reason.
    refl_data[11:19, 11:19] = 38
    refl_data[13:17, 13:17] = 44

    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data),
         'latitude': lats,
         'longitude': lons}
    )

    polygon_grid = np.zeros((20, 20), dtype=np.int32)
    polygon_grid[5, 5] = 1
    polygon_grid[15, 15] = 2

    mapped_ds = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    config = DetectionConfig.from_yaml()
    raised = replace(config, gatemapper=replace(config.gatemapper, dynamic_min_threshold_low=40.0))

    mapper = GateMapper(radar_ds, None, MockIOManager(), raised)
    final_grid = mapper.expand_gates(mapped_ds)['PolygonID'].values

    # 38 dBZ now sits below the weak cell's floor, so only its 44 dBZ core survives.
    assert final_grid[11, 11] == 0
    assert final_grid[15, 15] == 2

    # The strong cell's branch is a different key and must be untouched.
    assert final_grid[2, 2] == 1
    assert final_grid[4, 4] == 1


def test_sparse_label_ids_and_nan_reflectivity_preserve_expansion_logic():
    """
    Ensure compact label reductions handle sparse IDs and NaN values correctly.
    """
    lats = np.arange(20)
    lons = np.arange(20)
    refl_data = np.zeros((20, 20), dtype=float)

    # Label 1 has enough finite support to expand.
    refl_data[2:6, 2:6] = 45.0
    refl_data[2, 3] = np.nan

    # Sparse label 100 has one finite seed pixel and surrounding qualifying values.
    refl_data[12:16, 12:16] = 42.0
    refl_data[13, 13] = 44.0
    refl_data[14, 14] = np.nan

    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data),
         'latitude': lats,
         'longitude': lons}
    )

    polygon_grid = np.zeros((20, 20), dtype=np.int32)
    polygon_grid[3, 3] = 1
    polygon_grid[13, 13] = 100

    mapped_ds = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    mapper = GateMapper(radar_ds, None, MockIOManager(), DetectionConfig.from_yaml())
    expanded_ds = mapper.expand_gates(mapped_ds)
    final_grid = expanded_ds['PolygonID'].values

    assert final_grid[2, 2] == 1
    assert final_grid[2, 3] == 0  # NaN pixel is excluded by post-filtering.
    assert final_grid[3, 3] == 1
    assert final_grid[12, 12] == 100
    assert final_grid[14, 14] == 0
