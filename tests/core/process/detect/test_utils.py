"""
Tests for detection utilities module
"""

import pytest
import json
import numpy as np
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from EdgeWARN.process.detect.tools.utils import DetectionDataHandler


class TestDetectionDataHandler:
    """Tests for DetectionDataHandler class"""

    @pytest.fixture
    def mock_io(self):
        """Create a mock IOManager"""
        return MagicMock()

    @pytest.fixture
    def handler(self, mock_io, tmp_path):
        """Create a DetectionDataHandler instance"""
        radar_path = tmp_path / "radar.grib2"
        ps_path = tmp_path / "probsevere.json"
        preciptype_path = tmp_path / "preciptype.grib2"
        
        return DetectionDataHandler(
            radar_path=str(radar_path),
            ps_path=str(ps_path),
            preciptype_path=str(preciptype_path),
            io_manager=mock_io,
            lat_min=30.0,
            lat_max=40.0,
            lon_min=-100.0,
            lon_max=-90.0
        )

    def test_initialization(self, handler, tmp_path):
        """Test handler initialization"""
        assert handler.radar_path == str(tmp_path / "radar.grib2")
        assert handler.ps_path == str(tmp_path / "probsevere.json")
        assert handler.preciptype_path == str(tmp_path / "preciptype.grib2")
        assert handler.lat_grid == (30.0, 40.0)
        assert handler.lon_grid == (-100.0, -90.0)

    def test_load_radar_full(self, handler, mock_io):
        """Test loading full radar dataset"""
        with patch.object(handler.file_handler, 'load_dataset') as mock_load:
            mock_ds = MagicMock()
            mock_load.return_value = mock_ds
            
            result = handler.load_radar_full()
            
            assert result == mock_ds
            mock_load.assert_called_once_with(handler.radar_path)

    def test_subset_radar(self, handler):
        """Test subsetting radar data"""
        mock_ds = MagicMock()
        
        with patch.object(handler.file_handler, 'subset_dataset') as mock_subset:
            mock_subset.return_value = MagicMock()
            
            result = handler.subset_radar(mock_ds)
            
            mock_subset.assert_called_once_with(
                mock_ds,
                lat_limits=(30.0, 40.0),
                lon_limits=(-100.0, -90.0)
            )

    def test_load_subset(self, handler):
        """Test loading subsetted radar data"""
        with patch.object(handler.file_handler, 'load_dataset') as mock_load:
            mock_load.return_value = MagicMock()
            
            result = handler.load_subset()
            
            mock_load.assert_called_once_with(
                handler.radar_path,
                lat_limits=(30.0, 40.0),
                lon_limits=(-100.0, -90.0)
            )

    def test_load_preciptype(self, handler):
        """Test loading precipitation type data"""
        with patch.object(handler.file_handler, 'load_dataset') as mock_load:
            mock_load.return_value = MagicMock()
            
            result = handler.load_preciptype()
            
            mock_load.assert_called_once_with(
                handler.preciptype_path,
                lat_limits=(30.0, 40.0),
                lon_limits=(-100.0, -90.0)
            )

    def test_load_probsevere(self, handler, tmp_path):
        """Test loading and filtering ProbSevere data"""
        # Create test ProbSevere data
        ps_data = {
            "features": [
                {
                    "properties": {"ID": 1},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-95, 35], [-95, 36], [-94, 36], [-94, 35], [-95, 35]]]
                    }
                },
                {
                    "properties": {"ID": 2},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-80, 25], [-80, 26], [-79, 26], [-79, 25], [-80, 25]]]
                    }
                }
            ]
        }
        
        ps_file = tmp_path / "probsevere.json"
        ps_file.write_text(json.dumps(ps_data))
        handler.ps_path = str(ps_file)
        
        result = handler.load_probsevere()
        
        # Should only include features within lat/lon bounds
        assert len(result["features"]) == 1
        assert result["features"][0]["properties"]["ID"] == 1

    def test_load_probsevere_no_file(self, handler, mock_io):
        """Test loading ProbSevere when file doesn't exist"""
        handler.ps_path = "/nonexistent/file.json"
        
        with patch.object(handler.file_handler, 'load_dataset') as mock_load:
            mock_load.return_value = None
            
            result = handler.load_probsevere()
            
            assert result == []

    def test_load_probsevere_empty_features(self, handler, tmp_path):
        """Test loading ProbSevere with empty features"""
        ps_data = {"features": []}
        
        ps_file = tmp_path / "probsevere.json"
        ps_file.write_text(json.dumps(ps_data))
        handler.ps_path = str(ps_file)
        
        result = handler.load_probsevere()
        
        assert result["features"] == []

    def test_load_probsevere_normalizes_longitude(self, handler, tmp_path):
        """Test that longitude is normalized to -180 to 180 range"""
        # Handler has lon limits of -100 to -90 (or 260 to 270 in 0-360)
        ps_data = {
            "features": [
                {
                    "properties": {"ID": 1},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[265, 35], [265, 36], [266, 36], [266, 35], [265, 35]]]
                    }
                }
            ]
        }
        
        ps_file = tmp_path / "probsevere.json"
        ps_file.write_text(json.dumps(ps_data))
        handler.ps_path = str(ps_file)
        
        # Adjust handler to use 0-360 longitude
        handler.lon_grid = (260.0, 270.0)
        
        result = handler.load_probsevere()
        
        assert [feature["properties"]["ID"] for feature in result["features"]] == [1]
        assert result["features"][0]["geometry"]["coordinates"] == [
            [[-95, 35], [-95, 36], [-94, 36], [-94, 35], [-95, 35]]
        ]
