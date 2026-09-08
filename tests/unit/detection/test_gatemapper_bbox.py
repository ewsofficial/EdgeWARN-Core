from dataclasses import replace

import pytest
import numpy as np
import xarray as xr
from EdgeWARN.process.detect.config import DetectionConfig
from EdgeWARN.process.detect.tools.gatemapper import GateMapper

class MockIOManager:
    def write_debug(self, msg):
        pass
    def write_warning(self, msg):
        pass

@pytest.fixture
def mock_mapper():
    radar_ds = xr.Dataset(
        {'unknown': (('latitude', 'longitude'), np.zeros((10, 10))),
         'latitude': np.arange(10, dtype=float),
         'longitude': np.arange(10, dtype=float)}
    )
    # Every contour step pinned to 1: these tests assert on the exact traced
    # points, so any downsampling would drop points they check for.
    config = DetectionConfig.from_yaml()
    config = replace(
        config,
        gatemapper=replace(
            config.gatemapper,
            contour_downsample=1,
            contour_keep_all_step=1,
            contour_coarse_step=1,
        ),
    )
    return GateMapper(radar_ds, None, MockIOManager(), config)

def test_draw_bbox_basic_square(mock_mapper):
    """Test a simple square polygon."""
    lats = np.arange(10, dtype=float)
    lons = np.arange(10, dtype=float)
    polygon_grid = np.zeros((10, 10), dtype=np.int32)
    polygon_grid[3:6, 3:6] = 1 # 3x3 square at (3,3)

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)

    assert 1 in bboxes
    coords = bboxes[1]
    assert len(coords) >= 4
    # Points should roughly trace the square [3,3] to [5,5] (indices)
    # Remember padded_mask logic shifts everything by +1, finds contour, then shifts back.
    # The output coordinates are (lat, lon).
    for lat, lon in coords:
        assert 2.0 <= lat <= 6.0
        assert 2.0 <= lon <= 6.0

def test_draw_bbox_single_pixel(mock_mapper):
    """Test a single pixel polygon."""
    lats = np.arange(10, dtype=float)
    lons = np.arange(10, dtype=float)
    polygon_grid = np.zeros((10, 10), dtype=np.int32)
    polygon_grid[5, 5] = 1

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)

    assert 1 in bboxes
    coords = bboxes[1]
    assert len(coords) > 0
    # Should be centered around (5,5)
    for lat, lon in coords:
        assert 4.0 <= lat <= 6.0
        assert 4.0 <= lon <= 6.0

def test_draw_bbox_touching_edges(mock_mapper):
    """Test polygon touching all 4 edges."""
    # Touching top-left corner (0,0)
    # Touching bottom-right corner (9,9)
    # We'll make one big diagonal line or frame

    lats = np.arange(10, dtype=float)
    lons = np.arange(10, dtype=float)
    polygon_grid = np.zeros((10, 10), dtype=np.int32)

    # Fill border
    polygon_grid[0, :] = 1
    polygon_grid[-1, :] = 1
    polygon_grid[:, 0] = 1
    polygon_grid[:, -1] = 1

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)
    assert 1 in bboxes
    coords = bboxes[1]

    lats_out = [p[0] for p in coords]
    lons_out = [p[1] for p in coords]

    # Should reach 0.0 and 9.0
    assert min(lats_out) <= 0.5
    assert max(lats_out) >= 8.5
    assert min(lons_out) <= 0.5
    assert max(lons_out) >= 8.5

def test_draw_bbox_disjoint_blobs(mock_mapper):
    """Test that it picks the largest blob (current behavior)."""
    lats = np.arange(20, dtype=float)
    lons = np.arange(20, dtype=float)
    polygon_grid = np.zeros((20, 20), dtype=np.int32)

    # Small blob
    polygon_grid[2:4, 2:4] = 1 # 2x2 = 4 pixels

    # Large blob
    polygon_grid[10:15, 10:15] = 1 # 5x5 = 25 pixels

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)
    assert 1 in bboxes
    coords = bboxes[1]

    # Should conform to the large blob (indices 10-15)
    # Not the small one (indices 2-4)
    lats_out = [p[0] for p in coords]
    lons_out = [p[1] for p in coords]

    assert min(lats_out) >= 9.0
    assert min(lons_out) >= 9.0

def test_draw_bbox_hole(mock_mapper):
    """Test donut shape (hole inside). Should pick outer contour."""
    lats = np.arange(10, dtype=float)
    lons = np.arange(10, dtype=float)
    polygon_grid = np.zeros((10, 10), dtype=np.int32)

    # 5x5 block
    polygon_grid[2:7, 2:7] = 1
    # Remove center (hole)
    polygon_grid[4, 4] = 0

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)
    coords = bboxes[1]

    # The outer boundary should cover 2-7.
    # The inner hole boundary would be smaller.
    # We expect the larger one.

    # Bounding box of points
    min_lat = min(p[0] for p in coords)
    max_lat = max(p[0] for p in coords)

    # Outer bound should be near 2 and 7 (indices)
    assert min_lat <= 2.5
    assert max_lat >= 5.5

def test_draw_bbox_empty(mock_mapper):
    """Test empty grid."""
    lats = np.arange(10, dtype=float)
    lons = np.arange(10, dtype=float)
    polygon_grid = np.zeros((10, 10), dtype=np.int32)

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)
    assert bboxes == {}

def test_draw_bbox_coordinate_mapping(mock_mapper):
    """Test precise coordinate mapping."""
    # Use non-integer coordinates to verify lookup
    lats = np.linspace(30.0, 31.0, 11) # 0.1 deg step
    lons = np.linspace(-100.0, -99.0, 11)

    polygon_grid = np.zeros((11, 11), dtype=np.int32)
    # Point at index (5,5) corresponds to 30.5, -99.5
    polygon_grid[5, 5] = 1

    dataset = xr.Dataset(
        {'PolygonID': (('latitude', 'longitude'), polygon_grid),
         'latitude': lats,
         'longitude': lons}
    )

    bboxes = mock_mapper.draw_bbox(dataset)
    coords = bboxes[1]

    # Centroid of bbox should be roughly 30.5, -99.5
    avg_lat = np.mean([p[0] for p in coords])
    avg_lon = np.mean([p[1] for p in coords])

    assert abs(avg_lat - 30.5) < 0.1
    assert abs(avg_lon - (-99.5)) < 0.1
