import pytest
import json
from unittest.mock import patch
from EdgeWARN.process.integrate.history import CellHistoryManager

@pytest.fixture
def history_manager(mock_io_manager, mock_fs):
    """Create a CellHistoryManager with mocked dependencies."""
    with patch("EdgeWARN.process.integrate.history.fs.CELL_DIR", mock_fs / "cell"):
        yield CellHistoryManager(mock_io_manager)

# === Tests for update_cell_histories ===

def test_update_cell_histories_creates_file(history_manager, mock_fs):
    """Test that history files are created for new cells."""
    cells = [
        {"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 50}
    ]
    
    history_manager.update_cell_histories(cells)
    
    file_path = mock_fs / "cell" / "101.json"
    assert file_path.exists()
    
    with open(file_path) as f:
        data = json.load(f)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == 101

def test_update_cell_histories_appends(history_manager, mock_fs):
    """Test that updates are appended to existing history."""
    cell_dir = mock_fs / "cell"
    file_path = cell_dir / "101.json"
    
    # Pre-populate with existing history
    existing = [{"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 50}]
    with open(file_path, "w") as f:
        json.dump(existing, f)
    
    # Update with new timestamp
    cells = [
        {"id": 101, "timestamp": "2023-10-15T12:02:00", "num_gates": 55}
    ]
    
    history_manager.update_cell_histories(cells)
    
    with open(file_path) as f:
        data = json.load(f)
        assert len(data) == 2
        assert data[1]["timestamp"] == "2023-10-15T12:02:00"

def test_update_cell_histories_skips_duplicates(history_manager, mock_fs):
    """Test that duplicate timestamps refresh the latest snapshot in place."""
    cell_dir = mock_fs / "cell"
    file_path = cell_dir / "101.json"
    
    existing = [{"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 50}]
    with open(file_path, "w") as f:
        json.dump(existing, f)
    
    # Update with same timestamp (duplicate)
    cells = [
        {"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 55}
    ]
    
    history_manager.update_cell_histories(cells)
    
    with open(file_path) as f:
        data = json.load(f)
        assert len(data) == 1
        assert data[0]["num_gates"] == 55


def test_update_cell_histories_replaces_duplicate_timestamp_with_new_fields(history_manager, mock_fs):
    cell_dir = mock_fs / "cell"
    file_path = cell_dir / "101.json"

    existing = [{"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 50}]
    with open(file_path, "w") as f:
        json.dump(existing, f)

    cells = [
        {
            "id": 101,
            "timestamp": "2023-10-15T12:00:00",
            "num_gates": 55,
            "dx": 1200.0,
            "dy": 800.0,
            "dt": 120.0,
        }
    ]

    history_manager.update_cell_histories(cells)

    with open(file_path) as f:
        data = json.load(f)

    assert len(data) == 1
    assert data[0]["dx"] == 1200.0
    assert data[0]["dy"] == 800.0
    assert data[0]["dt"] == 120.0

def test_update_cell_histories_skips_no_timestamp(history_manager, mock_fs, mock_io_manager):
    """Test that cells without a timestamp key are skipped."""
    cells = [
        {"id": 101, "num_gates": 50}  # No timestamp
    ]
    
    history_manager.update_cell_histories(cells)
    
    # File should NOT be created
    file_path = mock_fs / "cell" / "101.json"
    assert not file_path.exists()

def test_update_cell_histories_empty_list(history_manager, mock_fs):
    """Test that empty cell list is a no-op."""
    history_manager.update_cell_histories([])
    
    # No files should be created
    assert len(list((mock_fs / "cell").glob("*.json"))) == 0

def test_update_cell_histories_handles_corrupt_file(history_manager, mock_fs, mock_io_manager):
    """Test graceful handling of corrupted history file."""
    cell_dir = mock_fs / "cell"
    file_path = cell_dir / "101.json"
    
    # Write invalid JSON
    with open(file_path, "w") as f:
        f.write("{invalid json")
    
    cells = [
        {"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 50}
    ]
    
    history_manager.update_cell_histories(cells)
    
    # A malformed history must not be replaced with a one-entry file: that
    # would silently destroy the accumulated history following a transient
    # partial-read/corruption event.
    mock_io_manager.write_error.assert_called()
    assert file_path.read_text() == "{invalid json"

def test_update_cell_histories_skips_no_id(history_manager, mock_fs):
    """Test that cells without an id are skipped."""
    cells = [
        {"timestamp": "2023-10-15T12:00:00", "num_gates": 50}  # No id
    ]
    
    history_manager.update_cell_histories(cells)
    
    # No files should be created
    assert len(list((mock_fs / "cell").glob("*.json"))) == 0

def test_update_cell_histories_multiple_cells(history_manager, mock_fs):
    """Test updating history for multiple cells at once."""
    cells = [
        {"id": 101, "timestamp": "2023-10-15T12:00:00", "num_gates": 50},
        {"id": 102, "timestamp": "2023-10-15T12:00:00", "num_gates": 30},
        {"id": 103, "timestamp": "2023-10-15T12:00:00", "num_gates": 40}
    ]
    
    history_manager.update_cell_histories(cells)
    
    cell_dir = mock_fs / "cell"
    assert (cell_dir / "101.json").exists()
    assert (cell_dir / "102.json").exists()
    assert (cell_dir / "103.json").exists()
