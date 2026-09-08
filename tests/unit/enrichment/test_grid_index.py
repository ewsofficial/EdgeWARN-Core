"""
Unit tests for grid_index module.

Tests grid type detection, regular grid indexing, and k-d tree indexing.
"""
import pytest
import numpy as np
from EdgeWARN.process.integrate.grid_index import (
    GridIndex,
    RegularGridIndexer,
    KDTreeGridIndexer,
    BaseGridIndexer,
)


class TestRegularGridIndexer:
    """Tests for RegularGridIndexer class."""
    
    def test_is_regular_returns_true_for_regular_grid(self):
        """Regular grid detection should return True for uniform lat/lon grids."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        assert RegularGridIndexer.is_regular(lat_grid, lon_grid) == True
    
    def test_is_regular_returns_false_for_irregular_grid(self):
        """Regular grid detection should return False for curvilinear grids."""
        # Create a curvilinear grid (e.g., rotated pole)
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        # Perturb the grid significantly to make it irregular
        lat_grid = lat_grid + np.sin(lon_grid * np.pi / 180) * 0.5
        
        assert RegularGridIndexer.is_regular(lat_grid, lon_grid) == False
    
    def test_is_regular_returns_false_for_1d_arrays(self):
        """Regular grid detection should return False for 1D arrays."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        
        assert RegularGridIndexer.is_regular(lats, lons) is False
    
    def test_query_returns_correct_indices(self):
        """Indexer should return correct indices for known coordinates."""
        # Create 1-degree grid from 30-40 lat, -100 to -90 lon
        lats = np.linspace(30, 40, 11)  # indices 0-10
        lons = np.linspace(-100, -90, 11)  # indices 0-10
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = RegularGridIndexer(lat_grid, lon_grid)
        
        # Query for exact grid point
        idx = indexer.query(35.0, -95.0)
        assert idx == (5, 5)
        
        # Query for point at edge
        idx = indexer.query(30.0, -100.0)
        assert idx == (0, 0)
        
        # Query for point at opposite edge
        idx = indexer.query(40.0, -90.0)
        assert idx == (10, 10)
    
    def test_query_handles_longitude_normalization(self):
        """Indexer should normalize 0-360 longitudes to -180-180."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = RegularGridIndexer(lat_grid, lon_grid)
        
        # Query with 0-360 longitude (260 = -100)
        idx_360 = indexer.query(35.0, 260.0)
        idx_180 = indexer.query(35.0, -100.0)
        
        assert idx_360 == idx_180
    
    def test_query_clamps_out_of_bounds(self):
        """Indexer should clamp indices to valid grid bounds."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = RegularGridIndexer(lat_grid, lon_grid)
        
        # Query far outside grid
        idx = indexer.query(50.0, -80.0)  # Beyond 40, -90
        assert idx == (10, 10)  # Should clamp to max indices
        
        idx = indexer.query(20.0, -110.0)  # Below 30, -100
        assert idx == (0, 0)  # Should clamp to min indices
    
    def test_query_handles_descending_coordinates(self):
        """Indexer should handle grids with descending coordinates."""
        # Create grid with descending latitude (40 to 30)
        lats = np.linspace(40, 30, 11)
        lons = np.linspace(-90, -100, 11)  # descending longitude too
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = RegularGridIndexer(lat_grid, lon_grid)
        
        # Query for middle point
        idx = indexer.query(35.0, -95.0)
        assert idx == (5, 5)
    
    def test_init_raises_on_invalid_grid(self):
        """Indexer should raise error for invalid grid dimensions."""
        with pytest.raises(ValueError, match="2D lat/lon arrays"):
            RegularGridIndexer(np.array([1, 2, 3]), np.array([4, 5, 6]))
        
        with pytest.raises(ValueError, match="at least 2 points"):
            RegularGridIndexer(np.array([[1]]), np.array([[4]]))


class TestKDTreeGridIndexer:
    """Tests for KDTreeGridIndexer class."""
    
    def test_query_finds_nearest_point(self):
        """k-d tree should find nearest grid point."""
        # Create regular grid
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = KDTreeGridIndexer(lat_grid, lon_grid)
        
        # Query for exact grid point
        idx = indexer.query(35.0, -95.0)
        assert idx == (5, 5)
    
    def test_query_finds_nearest_for_off_grid_point(self):
        """k-d tree should find nearest point for off-grid coordinates."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = KDTreeGridIndexer(lat_grid, lon_grid)
        
        # Query for point between grid cells
        idx = indexer.query(35.05, -94.95)
        # Should return nearest (35.0, -95.0) = (5, 5)
        assert idx == (5, 5)
    
    def test_query_handles_longitude_normalization(self):
        """k-d tree indexer should normalize 0-360 longitudes."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = KDTreeGridIndexer(lat_grid, lon_grid)
        
        # Query with both formats
        idx_360 = indexer.query(35.0, 260.0)
        idx_180 = indexer.query(35.0, -100.0)
        
        assert idx_360 == idx_180


class TestGridIndexFactory:
    """Tests for GridIndex factory class."""
    
    def test_create_returns_regular_indexer_for_regular_grid(self):
        """Factory should return RegularGridIndexer for regular grids."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        indexer = GridIndex.create(lat_grid, lon_grid)
        
        assert isinstance(indexer, RegularGridIndexer)
    
    def test_create_returns_kdtree_for_irregular_grid(self):
        """Factory should return KDTreeGridIndexer for irregular grids."""
        # Create a curvilinear grid
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        # Perturb to make irregular
        lat_grid = lat_grid + np.sin(lon_grid * np.pi / 180) * 0.1
        
        indexer = GridIndex.create(lat_grid, lon_grid)
        
        assert isinstance(indexer, KDTreeGridIndexer)
    
    def test_both_indexers_produce_same_results(self):
        """Both indexers should produce consistent results on regular grids."""
        lats = np.linspace(30, 40, 11)
        lons = np.linspace(-100, -90, 11)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        regular = RegularGridIndexer(lat_grid, lon_grid)
        kdtree = KDTreeGridIndexer(lat_grid, lon_grid)
        
        # Test exact grid points (both should match exactly)
        test_points = [
            (35.0, -95.0),
            (30.0, -100.0),
            (40.0, -90.0),
        ]
        
        for lat, lon in test_points:
            regular_idx = regular.query(lat, lon)
            kdtree_idx = kdtree.query(lat, lon)
            assert regular_idx == kdtree_idx, f"Mismatch at ({lat}, {lon})"
        
        # Test off-grid points (may differ by 1 due to rounding)
        off_grid_points = [
            (32.5, -97.5),
            (37.5, -92.5),
        ]
        
        for lat, lon in off_grid_points:
            regular_idx = regular.query(lat, lon)
            kdtree_idx = kdtree.query(lat, lon)
            # Allow one index difference due to rounding
            assert abs(regular_idx[0] - kdtree_idx[0]) <= 1
            assert abs(regular_idx[1] - kdtree_idx[1]) <= 1


class TestRAPRealisticGrid:
    """Tests with realistic RAP-like grid dimensions."""
    
    @pytest.fixture
    def rap_grid(self):
        """Create a realistic RAP-sized regular grid."""
        # RAP grid: approximately 337 x 451
        lats = np.linspace(20, 55, 337)
        lons = np.linspace(-140, -50, 451)
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        return lat_grid, lon_grid
    
    def test_rap_grid_detected_as_regular(self, rap_grid):
        """Realistic RAP grid should be detected as regular."""
        lat_grid, lon_grid = rap_grid
        assert RegularGridIndexer.is_regular(lat_grid, lon_grid) == True
    
    def test_rap_grid_indexing_accuracy(self, rap_grid):
        """Indexer should return accurate indices for RAP-sized grid."""
        lat_grid, lon_grid = rap_grid
        indexer = RegularGridIndexer(lat_grid, lon_grid)
        
        # Grid: 337 lats from 20-55 (step ~0.104), 451 lons from -140 to -50 (step 0.2)
        # Calculate expected indices based on grid spacing
        test_cases = [
            (37.5, -97.5),   # Center-ish of CONUS
            (40.0, -105.0),  # Colorado area
            (30.0, -85.0),   # Florida area
        ]
        
        for lat, lon in test_cases:
            idx = indexer.query(lat, lon)
            # Verify indices are within valid range
            assert 0 <= idx[0] < lat_grid.shape[0]
            assert 0 <= idx[1] < lat_grid.shape[1]
            
            # Verify we can retrieve a value from the grid
            lat_retrieved = lat_grid[idx[0], idx[1]]
            lon_retrieved = lon_grid[idx[0], idx[1]]
            
            # The retrieved point should be close to the query point
            assert abs(lat_retrieved - lat) < 0.2  # Within one grid cell
            assert abs(lon_retrieved - lon) < 0.25  # Within one grid cell


class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_empty_grid_raises_error(self):
        """Empty grid should raise error."""
        with pytest.raises(ValueError):
            RegularGridIndexer(np.array([]).reshape(0, 0), np.array([]).reshape(0, 0))
    
    def test_single_point_grid_raises_error(self):
        """Single point grid should raise error."""
        with pytest.raises(ValueError, match="at least 2 points"):
            RegularGridIndexer(np.array([[35.0]]), np.array([[-95.0]]))
    
    def test_very_small_grid(self):
        """Minimum viable grid (2x2) should work."""
        lats = np.array([[30.0, 30.0], [31.0, 31.0]])
        lons = np.array([[-100.0, -99.0], [-100.0, -99.0]])
        
        indexer = RegularGridIndexer(lats, lons)
        
        idx = indexer.query(30.5, -99.5)
        assert idx in [(0, 0), (0, 1), (1, 0), (1, 1)]
    
    def test_grid_spanning_dateline(self):
        """Grid spanning dateline should handle longitude correctly."""
        # Grid from 170 to -170 (crossing dateline)
        lats = np.linspace(50, 60, 11)
        lons = np.concatenate([np.linspace(170, 180, 6), np.linspace(-179, -170, 10)])
        lat_grid, lon_grid = np.meshgrid(lats, lons, indexing='ij')
        
        # Note: This grid IS structurally regular (each row has constant lons),
        # but the longitude discontinuity makes the regular indexer less useful.
        # For this edge case, k-d tree is more robust.
        
        # k-d tree should handle it correctly
        indexer = KDTreeGridIndexer(lat_grid, lon_grid)
        idx = indexer.query(55.0, 175.0)
        assert idx is not None
        
        # Verify the point is near 175E
        lat_retrieved = lat_grid[idx[0], idx[1]]
        lon_retrieved = lon_grid[idx[0], idx[1]]
        assert abs(lat_retrieved - 55.0) < 1.0
        assert abs(lon_retrieved - 175.0) < 2.0  # Allow for dateline discontinuity
