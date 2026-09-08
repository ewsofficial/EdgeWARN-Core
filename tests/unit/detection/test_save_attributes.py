import pytest
import numpy as np
import xarray as xr
from EdgeWARN.process.detect.tools.save import CellDataSaver

def test_attribute_unification():
    """
    Test that attributes (num_gates, max_refl, centroid) use the expanded grid.
    """
    lats = np.array([30.0, 31.0, 32.0, 33.0, 34.0])
    lons = np.array([260.0, 261.0, 262.0, 263.0, 264.0])
    
    # 1. Create Radar Data (Uniform 50 dBZ)
    refl_data = np.full((5, 5), 50.0)
    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data)},
        coords={'latitude': lats, 'longitude': lons}
    )
    
    # 2. Mapped Grid (Seed at 0,0)
    mapped_grid = np.zeros((5, 5), dtype=np.int32)
    mapped_grid[0, 0] = 1
    mapped_ds = xr.Dataset({'PolygonID': (('latitude', 'longitude'), mapped_grid)})
    
    # 3. Expanded Grid (Covers row 0 - gates (0,0) to (0,2))
    # This simulates a watershed expansion where the cell grew.
    expanded_grid = np.zeros((5, 5), dtype=np.int32)
    expanded_grid[0, 0:3] = 1 # 3 gates
    expanded_ds = xr.Dataset({'PolygonID': (('latitude', 'longitude'), expanded_grid)})
    
    # 4. Initialize Saver
    # bbox doesn't matter for attribute calculation but needs to be provided
    bboxes = {1: [[30.0, 260.0], [30.0, 262.0]]}
    
    saver = CellDataSaver(bboxes, radar_ds, mapped_ds, expanded_ds, ps_ds=None, preciptype_ds=None)
    
    # 5. Execute
    results = saver.create_entry()
    entry = results[0]
    
    # 6. Assertions
    # num_gates should be 3 (from expanded_ds), not 1 (from mapped_ds)
    assert entry['num_gates'] == 3, f"Expected 3 gates, got {entry['num_gates']}"
    
    # centroid should be at the middle of the 3 gates (row 0, col 1) -> (30.0, 261.0)
    # Since reflectivity is uniform (50), the centroid is purely geometric.
    assert pytest.approx(entry['centroid'][0]) == 30.0
    assert pytest.approx(entry['centroid'][1]) == 261.0
    
    # max_refl should be 50.0
    assert entry['max_refl'] == 50.0

def test_centroid_weighted():
    """
    Test that centroid is correctly weighted by intensity.
    """
    lats = np.array([30.0, 31.0])
    lons = np.array([260.0, 261.0])
    
    # High refl at (0,0), low refl at (0,1)
    refl_data = np.array([[60.0, 20.0], [0.0, 0.0]])
    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), refl_data)},
        coords={'latitude': lats, 'longitude': lons}
    )
    
    expanded_grid = np.array([[1, 1], [0, 0]], dtype=np.int32)
    expanded_ds = xr.Dataset({'PolygonID': (('latitude', 'longitude'), expanded_grid)})
    
    bboxes = {1: [[30.0, 260.0], [30.0, 261.0]]}
    
    saver = CellDataSaver(bboxes, radar_ds, mapped_ds=None, expanded_ds=expanded_ds, ps_ds=None, preciptype_ds=None)
    results = saver.create_entry()
    centroid = results[0]['centroid']
    
    # Centroid should be heavily biased toward (0,0) -> (30.0, 260.0)
    assert centroid[1] < 260.1 # Very close to 260.0
