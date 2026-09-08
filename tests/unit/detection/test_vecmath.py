import pytest
import json
from unittest.mock import patch, MagicMock
from EdgeWARN.process.detect.tools.vecmath import StormVectorCalculator

@pytest.fixture
def mock_fs(tmp_path):
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    return cell_dir

def test_calculate_vectors_basic(mock_fs):
    """Test basic vector calculation (dx, dy, dt)."""
    
    # 1. Setup History
    # Cell moving North-East
    # T0: (35.0, -97.0)
    # T1: (35.1, -96.9)
    # dt = 300s (5 min)
    
    t0_str = "2023-01-01T12:00:00"
    t1_str = "2023-01-01T12:05:00"
    
    history = [
        {"timestamp": t0_str, "centroid": [35.0, -97.0]}
    ]
    
    (mock_fs / "101.json").write_text(json.dumps(history))
    
    # 2. Current Entry
    current_entries = [
        {"id": 101, "timestamp": t1_str, "centroid": [35.1, -96.9]}
    ]
    
    # Patch fs.CELL_DIR
    with patch("EdgeWARN.process.detect.tools.vecmath.fs.CELL_DIR", mock_fs):
        results = StormVectorCalculator.calculate_vectors(current_entries)
    
    assert len(results) == 1
    cell = results[0]
    
    assert "dx" in cell
    assert "dy" in cell
    assert "dt" in cell
    
    assert cell["dt"] == 300.0
    
    # Lat diff = 0.1 deg ~ 11.1km
    # Lon diff = 0.1 deg ~ 11.1km * cos(35)
    
    assert cell["dy"] > 10000 
    assert cell["dx"] > 0
    assert cell["dx"] < cell["dy"] # because cos(lat) < 1

def test_calculate_vectors_no_history(mock_fs):
    """Test behavior when no history exists (new cell)."""
    current_entries = [
        {"id": 102, "timestamp": "2023-01-01T12:00:00", "centroid": [35.0, -97.0]}
    ]
    
    with patch("EdgeWARN.process.detect.tools.vecmath.fs.CELL_DIR", mock_fs):
        results = StormVectorCalculator.calculate_vectors(current_entries)
        
    cell = results[0]
    assert "dx" not in cell

def test_calculate_vectors_same_timestamp(mock_fs):
    """Test that vectors aren't calculated if timestamps match (duplicate processing)."""
    t0_str = "2023-01-01T12:00:00"
    history = [
        {"timestamp": t0_str, "centroid": [35.0, -97.0]}
    ]
    (mock_fs / "101.json").write_text(json.dumps(history))
    
    current_entries = [
        {"id": 101, "timestamp": t0_str, "centroid": [35.0, -97.0]}
    ]
    
    with patch("EdgeWARN.process.detect.tools.vecmath.fs.CELL_DIR", mock_fs):
        results = StormVectorCalculator.calculate_vectors(current_entries)
        
    assert "dt" not in results[0]

def test_calculate_vectors_invalid_history(mock_fs):
    """Test handling of corrupt history file."""
    (mock_fs / "103.json").write_text("{invalid")
    
    current_entries = [
        {"id": 103, "timestamp": "2023-01-01T12:00:00"}
    ]
    
    with patch("EdgeWARN.process.detect.tools.vecmath.fs.CELL_DIR", mock_fs):
        # Should catch exception and skip
        results = StormVectorCalculator.calculate_vectors(current_entries)
        
    assert "dx" not in results[0]


def test_calculate_vectors_uses_previous_entries_without_history_io(mock_fs):
    current_entries = [
        {"id": 101, "timestamp": "2023-01-01T12:05:00", "centroid": [35.1, -96.9]}
    ]
    previous_entries = [
        {"id": 101, "timestamp": "2023-01-01T12:00:00", "centroid": [35.0, -97.0]}
    ]

    with patch("EdgeWARN.process.detect.tools.vecmath.fs.CELL_DIR", mock_fs), \
         patch("EdgeWARN.process.detect.tools.vecmath.StormVectorCalculator._load_previous_entry_from_history") as mock_loader:
        results = StormVectorCalculator.calculate_vectors(current_entries, previous_entries=previous_entries)

    mock_loader.assert_not_called()
    assert results[0]["dt"] == 300.0
    assert "dx" in results[0]
    assert "dy" in results[0]
