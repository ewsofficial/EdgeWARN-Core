import pytest
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch
from EdgeWARN.api_integration.index_manager import APIIndexManager

@pytest.fixture
def index_manager(mock_io_manager, mock_fs):
    """Fixture for APIIndexManager pointing to mock fs."""
    # We must patch the fs constants in index_manager to point to our temp paths
    with patch("EdgeWARN.api_integration.index_manager.fs.STORMCELL_DIR", mock_fs / "stormcell"), \
         patch("EdgeWARN.api_integration.index_manager.fs.CELL_DIR", mock_fs / "cell"):
        
        manager = APIIndexManager(mock_io_manager)
        yield manager

def test_initialize_stormcell_index(index_manager, mock_fs):
    """Test creation of stormcell index from existing files."""
    storm_dir = mock_fs / "stormcell"
    # Create sample files
    (storm_dir / "stormcells_20230101-120000.json").touch()
    (storm_dir / "stormcells_20230101-120500.json").touch()
    (storm_dir / "ignore_me.txt").touch()
    
    index_manager.initialize_indexes()
    
    index_path = storm_dir / "stormcell_index.json"
    assert index_path.exists()
    
    with open(index_path) as f:
        data = json.load(f)
        assert "timestamps" in data
        assert len(data["timestamps"]) == 2
        assert "20230101-120000" in data["timestamps"]
        assert "20230101-120500" in data["timestamps"]

def test_initialize_cell_index(index_manager, mock_fs):
    """Test creation of cell index from existing files."""
    cell_dir = mock_fs / "cell"
    # Create sample cell files
    (cell_dir / "101.json").touch()
    (cell_dir / "102.json").touch()
    (cell_dir / "not_a_number.json").touch()
    
    index_manager.initialize_indexes()
    
    index_path = cell_dir / "cell_index.json"
    assert index_path.exists()
    
    with open(index_path) as f:
        data = json.load(f)
        assert "cellIds" in data
        assert len(data["cellIds"]) == 2
        assert 101 in data["cellIds"]
        assert 102 in data["cellIds"]

def test_cleanup_inactive_cells(index_manager, mocker):
    """Test that cleanup updates indexes."""
    # Spy on _initial_scan_cell_index to ensure it's called after cleanup
    spy_init = mocker.spy(index_manager, "_initial_scan_cell_index")
    
    # Mock the write_cell_index method to track calls
    mock_write = mocker.patch.object(index_manager, "_write_cell_index")
    
    index_manager.cleanup_inactive_cells()
    
    # Verify that index is updated (write_cell_index is called)
    spy_init.assert_called_once()
    assert mock_write.called


def test_cleanup_inactive_cells_preserves_files_when_disabled(mock_io_manager, mock_fs):
    cell_dir = mock_fs / "cell"
    old_cell_file = cell_dir / "101.json"
    old_cell_file.write_text("[]")

    old_time = datetime.now() - timedelta(hours=3)
    os.utime(old_cell_file, (old_time.timestamp(), old_time.timestamp()))

    with patch("EdgeWARN.api_integration.index_manager.fs.STORMCELL_DIR", mock_fs / "stormcell"), \
         patch("EdgeWARN.api_integration.index_manager.fs.CELL_DIR", cell_dir):
        manager = APIIndexManager(mock_io_manager, remove_old_cells=False)
        manager.cleanup_inactive_cells()

    assert old_cell_file.exists()


def test_database_projection_is_reused_for_both_indexes(
    mock_io_manager, mock_fs, monkeypatch
):
    projection = (["20230101-120000"], {"101": 123.0})
    calls = []

    class FakeRepository:
        path = mock_fs / "stormprob.sqlite3"

        def index_projection(self):
            calls.append(True)
            return projection

    FakeRepository.path.touch()
    (mock_fs / "stormcell" / "stormcells_20230101-120000.json").touch()
    (mock_fs / "cell" / "101.json").touch()
    monkeypatch.setattr(
        "EdgeWARN.stormprob.database.StormProbRepository", FakeRepository
    )

    with patch("EdgeWARN.api_integration.index_manager.fs.STORMCELL_DIR", mock_fs / "stormcell"), \
         patch("EdgeWARN.api_integration.index_manager.fs.CELL_DIR", mock_fs / "cell"):
        manager = APIIndexManager(mock_io_manager)
        manager.initialize_indexes()

    assert len(calls) == 1
    assert manager.projection_reused is True
    assert manager.stormcell_timestamps == {"20230101-120000"}
    assert manager.cell_timestamps == {"101": 123.0}


def test_publish_cycle_writes_each_index_once_after_cleanup(mock_io_manager, mock_fs, monkeypatch):
    from EdgeWARN.api_integration import index_manager as module

    storm_dir = mock_fs / "stormcell"
    cell_dir = mock_fs / "cell"
    (storm_dir / "stormcells_20230101-120000.json").touch()
    (cell_dir / "101.json").write_text("[]")
    (cell_dir / "102.json").write_text("[]")
    writes = []
    original_write = module.atomic_write_json

    def recorded_write(path, payload, **kwargs):
        writes.append((path.name, payload))
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(module, "atomic_write_json", recorded_write)
    monkeypatch.setattr(module, "inactive_cell_max_age_minutes", lambda: 1)
    with patch.object(module.fs, "STORMCELL_DIR", storm_dir), patch.object(module.fs, "CELL_DIR", cell_dir):
        manager = APIIndexManager(mock_io_manager, remove_old_cells=True)
        monkeypatch.setattr(manager, "_load_database_projection", lambda: ([], {"102": 0.0}))
        manager.publish_cycle_indexes("20230101-120000", [101])

    assert [name for name, _ in writes] == ["cell_index.json", "stormcell_index.json"]
    assert writes[0][1]["cellIds"] == [101]
    assert writes[1][1]["timestamps"] == ["20230101-120000"]
    assert not (cell_dir / "102.json").exists()


def test_failed_cleanup_keeps_existing_cell_listed(mock_io_manager, mock_fs, monkeypatch):
    from EdgeWARN.api_integration import index_manager as module

    cell_dir = mock_fs / "cell"
    storm_dir = mock_fs / "stormcell"
    old_file = cell_dir / "102.json"
    old_file.write_text("[]")
    monkeypatch.setattr(module, "inactive_cell_max_age_minutes", lambda: 1)
    original_unlink = type(old_file).unlink

    def fail_unlink(path, *args, **kwargs):
        if path == old_file:
            raise PermissionError("injected")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(old_file), "unlink", fail_unlink)
    with patch.object(module.fs, "STORMCELL_DIR", storm_dir), patch.object(module.fs, "CELL_DIR", cell_dir):
        manager = APIIndexManager(mock_io_manager, remove_old_cells=True)
        monkeypatch.setattr(manager, "_load_database_projection", lambda: ([], {"102": 0.0}))
        manager.publish_cycle_indexes("missing", [])

    assert old_file.exists()
    assert json.loads((cell_dir / "cell_index.json").read_text())["cellIds"] == [102]


def test_historical_old_scan_keeps_inactive_files_and_rebuilds_corrupt_indexes(
    mock_io_manager, mock_fs, monkeypatch
):
    from EdgeWARN.api_integration import index_manager as module

    cell_dir = mock_fs / "cell"
    storm_dir = mock_fs / "stormcell"
    (cell_dir / "102.json").write_text("[]")
    (cell_dir / "cell_index.json").write_text("broken")
    (storm_dir / "stormcells_20230101-115500.json").write_text("{}")
    (storm_dir / "stormcell_index.json").write_text("broken")
    with patch.object(module.fs, "STORMCELL_DIR", storm_dir), patch.object(module.fs, "CELL_DIR", cell_dir):
        manager = APIIndexManager(mock_io_manager, remove_old_cells=False)
        monkeypatch.setattr(manager, "_load_database_projection", lambda: ([], {"102": 0.0}))
        manager.publish_cycle_indexes("20230101-115500", [])

    assert (cell_dir / "102.json").exists()
    assert json.loads((cell_dir / "cell_index.json").read_text())["cellIds"] == [102]
    assert json.loads((storm_dir / "stormcell_index.json").read_text())["timestamps"] == ["20230101-115500"]
