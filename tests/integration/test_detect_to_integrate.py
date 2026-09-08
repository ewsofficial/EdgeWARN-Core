"""
Integration tests for detection to integration workflow
"""

import pytest
import numpy as np
from unittest.mock import MagicMock, patch

from EdgeWARN.process.detect.config import DetectionConfig


class TestDetectToIntegrateWorkflow:
    """Tests for the detection to integration workflow"""

    @pytest.fixture
    def mock_io(self):
        """Create a mock IOManager"""
        return MagicMock()

    @pytest.fixture
    def sample_cell_data(self):
        """Create sample cell data for testing"""
        return [
            {
                "id": 101,
                "centroid": [35.0, -97.0],
                "bbox": [[34.9, -97.1], [34.9, -96.9], [35.1, -96.9], [35.1, -97.1]],
                "num_gates": 50,
                "max_refl": 55.0,
                "timestamp": "2023-10-15T14:30:00",
                "properties": {}
            },
            {
                "id": 102,
                "centroid": [36.0, -96.0],
                "bbox": [[35.9, -96.1], [35.9, -95.9], [36.1, -95.9], [36.1, -96.1]],
                "num_gates": 30,
                "max_refl": 45.0,
                "timestamp": "2023-10-15T14:30:00",
                "properties": {}
            }
        ]

    def test_detect_creates_valid_cell_structure(self, mock_io):
        """Test that detection produces valid cell structure"""
        from EdgeWARN.process.detect.detect import detect_cells
        
        # This is a simplified test - in reality would need actual data files
        # For integration test, we mock the dependencies
        with patch('EdgeWARN.process.detect.detect.DetectionDataHandler') as mock_handler, \
             patch('EdgeWARN.process.detect.detect.GateMapper') as mock_mapper, \
             patch('EdgeWARN.process.detect.detect.CellDataSaver') as mock_saver:
            
            # Setup mocks
            mock_handler_instance = MagicMock()
            mock_handler.return_value = mock_handler_instance
            
            mock_ds = MagicMock()
            mock_handler_instance.load_subset.return_value = mock_ds
            mock_handler_instance.load_probsevere.return_value = {"features": []}
            mock_handler_instance.load_preciptype.return_value = None
            
            mock_mapper_instance = MagicMock()
            mock_mapper.return_value = mock_mapper_instance
            mock_mapper_instance.map_gates_to_polygons.return_value = MagicMock()
            mock_mapper_instance.expand_gates.return_value = MagicMock()
            mock_mapper_instance.draw_bbox.return_value = {1: [[0, 0], [1, 1]]}
            
            mock_saver_instance = MagicMock()
            mock_saver.return_value = mock_saver_instance
            mock_saver_instance.create_entry.return_value = [
                {"id": 1, "centroid": [35.0, -97.0], "properties": {}}
            ]
            
            # Run detection
            result = detect_cells(
                "radar.grib2", "ps.json", "pt.grib2", mock_io,
                30, 40, -100, -90,
                detection_config=DetectionConfig.from_yaml(),
            )
            
            # Verify structure
            assert isinstance(result, list)
            if result:
                assert "id" in result[0]
                assert "centroid" in result[0]

    def test_integrate_glm_adds_flash_data(self, sample_cell_data, tmp_path):
        """Test that GLM integration adds flash count and energy"""
        import xarray as xr
        from EdgeWARN.process.integrate.integrate_glm import integrate_glm
        from EdgeWARN.process.integrate.utils import StormIntegrationUtils
        
        # Create synthetic GLM data
        lats = np.array([35.0, 35.01, 36.0, 30.0])
        lons = np.array([-97.0, -97.01, -96.0, -90.0])
        energies = np.array([100.0, 50.0, 75.0, 1000.0])
        
        glm_ds = xr.Dataset({
            "flash_lat": (("number_of_flashes",), lats),
            "flash_lon": (("number_of_flashes",), lons),
            "flash_energy": (("number_of_flashes",), energies)
        })
        
        glm_file = tmp_path / "glm.nc"
        glm_ds.to_netcdf(glm_file)
        
        # Mock create_cell_polygon to return proper polygons
        def mock_create_poly(cell):
            from shapely.geometry import Polygon
            coords = [(lon, lat) for lat, lon in cell['bbox']]
            return Polygon(coords)
        
        with patch.object(StormIntegrationUtils, 'create_cell_polygon', side_effect=mock_create_poly):
            result = integrate_glm(sample_cell_data, str(glm_file))
        
        # Verify GLM data was added
        for cell in result:
            assert "GLM_FLASH_COUNT" in cell["properties"]
            assert "GLM_TOTAL_ENERGY" in cell["properties"]
            assert isinstance(cell["properties"]["GLM_FLASH_COUNT"], int)
            assert isinstance(cell["properties"]["GLM_TOTAL_ENERGY"], float)

    def test_integrate_rap_adds_wind_data(self, sample_cell_data):
        """Test that RAP integration adds wind data"""
        from EdgeWARN.process.integrate.integrate_rap import integrate_rap
        
        mock_io = MagicMock()
        
        with patch('EdgeWARN.process.integrate.integrate_rap.RAPPointExtractor') as MockExtractor:
            mock_instance = MockExtractor.return_value
            mock_instance.extract_batch.return_value = {
                "wind_field.u850": {101: 10.0, 102: 10.0},
                "wind_field.v850": {101: 5.0, 102: 5.0},
                "wind_field.u500": {101: 30.0, 102: 30.0},
                "wind_field.v500": {101: 15.0, 102: 15.0}
            }
            result = integrate_rap(sample_cell_data, "dummy_path", mock_io)
        
        # Verify wind data was added
        for cell in result:
            assert "wind_field" in cell["properties"]
            wind = cell["properties"]["wind_field"]
            assert "u850" in wind
            assert "v850" in wind
            assert "u500" in wind
            assert "v500" in wind
