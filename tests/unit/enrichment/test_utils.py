"""
Tests for integration utilities module
"""

import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from EdgeWARN.process.integrate.utils import (
    StormIntegrationUtils
)


class TestStormIntegrationUtils:
    """Tests for StormIntegrationUtils"""

    def test_create_cell_polygon_from_bbox(self):
        """Test creating polygon from bbox"""
        cell = {
            "bbox": [[34.9, -97.1], [34.9, -96.9], [35.1, -96.9], [35.1, -97.1]]
        }
        
        poly = StormIntegrationUtils.create_cell_polygon(cell)
        
        # Should return a Shapely Polygon
        assert poly is not None
        assert hasattr(poly, 'exterior')

    def test_create_cell_polygon_normalizes_360_longitudes(self):
        """Test bbox longitudes are normalized into the signed frame."""
        cell = {
            "bbox": [[34.9, 262.9], [34.9, 263.1], [35.1, 263.1], [35.1, 262.9]]
        }

        poly = StormIntegrationUtils.create_cell_polygon(cell)

        assert poly is not None
        lon_values = [coord[0] for coord in poly.exterior.coords]
        assert max(lon_values) < 180.0
        assert min(lon_values) < 0.0

    def test_create_cell_polygon_from_centroid(self):
        """Test creating polygon from centroid"""
        cell = {
            "centroid": [35.0, -97.0]
        }
        
        poly = StormIntegrationUtils.create_cell_polygon(cell)
        
        # Should return a Shapely Polygon
        assert poly is not None
        assert hasattr(poly, 'exterior')

    def test_create_cell_polygon_missing_geometry(self):
        """Test handling when geometry is missing"""
        cell = {
            "id": 101
        }
        
        poly = StormIntegrationUtils.create_cell_polygon(cell)
        
        # Should return None
        assert poly is None
