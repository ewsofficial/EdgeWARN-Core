import pytest
from unittest.mock import MagicMock
from EdgeWARN.process.detect.track import StormCellTracker

# Helper: generate a proper polygon bbox (list of [lat,lon] pairs)
def _bbox(lat, lon, size=0.1):
    """Create a square bbox polygon centered on (lat, lon)."""
    return [
        [lat - size, lon - size],
        [lat - size, lon + size],
        [lat + size, lon + size],
        [lat + size, lon - size],
    ]

@pytest.fixture
def tracker(mock_io_manager):
    """Create a StormCellTracker with mocked dependencies."""
    return StormCellTracker(None, None, mock_io_manager)

# === Tests for update_cells ===

def test_update_cells_updates_existing(tracker):
    """Test that existing cells are updated with new data."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)},
        {"id": 102, "num_gates": 30, "centroid": [36.0, -96.0], "max_refl": 45, "bbox": _bbox(36.0, -96.0)}
    ]
    
    updated_data = [
        {"id": 101, "num_gates": 60, "centroid": [35.1, -97.1], "max_refl": 60, "bbox": _bbox(35.1, -97.1)},
        {"id": 102, "num_gates": 35, "centroid": [36.1, -96.1], "max_refl": 50, "bbox": _bbox(36.1, -96.1)}
    ]
    
    result = tracker.update_cells(entries, updated_data, timestamp="2023-10-15T12:00:00")
    
    assert len(result) == 2
    
    cell_101 = next(c for c in result if c["id"] == 101)
    assert cell_101["num_gates"] == 60
    assert cell_101["max_refl"] == 60
    assert cell_101["timestamp"] == "2023-10-15T12:00:00"

def test_update_cells_removes_missing(tracker):
    """Test that cells not in updated_data are marked as DISSIPATED."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)},
        {"id": 102, "num_gates": 30, "centroid": [36.0, -96.0], "max_refl": 45, "bbox": _bbox(36.0, -96.0)},
        {"id": 103, "num_gates": 20, "centroid": [37.0, -95.0], "max_refl": 40, "bbox": _bbox(37.0, -95.0)}
    ]
    
    updated_data = [
        {"id": 101, "num_gates": 55, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)}
        # 102 and 103 are missing
    ]
    
    result = tracker.update_cells(entries, updated_data)
    
    # Now returns 3: 101 (active) + 102 (dissipated) + 103 (dissipated)
    assert len(result) == 3
    assert any(c["id"] == 102 and c["event_type"] == "DISSIPATED" for c in result)
    assert any(c["id"] == 103 and c["event_type"] == "DISSIPATED" for c in result)

def test_update_cells_adds_new(tracker):
    """Test that new cells from updated_data are appended."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)}
    ]
    
    updated_data = [
        {"id": 101, "num_gates": 55, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)},
        {"id": 104, "num_gates": 40, "centroid": [38.0, -94.0], "max_refl": 50, "bbox": _bbox(38.0, -94.0)}  # New cell
    ]
    
    result = tracker.update_cells(entries, updated_data, timestamp="2023-10-15T12:00:00")
    
    assert len(result) == 2
    
    new_cell = next(c for c in result if c["id"] == 104)
    assert new_cell["num_gates"] == 40
    assert new_cell["timestamp"] == "2023-10-15T12:00:00"

def test_update_cells_preserves_storm_history(tracker):
    """Test that storm_history is not overwritten."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0), "storm_history": [{"t": 1, "val": 10}]}
    ]
    
    updated_data = [
        {"id": 101, "num_gates": 60, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)}
    ]
    
    result = tracker.update_cells(entries, updated_data)
    
    assert "storm_history" in result[0]
    assert result[0]["storm_history"] == [{"t": 1, "val": 10}]

def test_update_cells_empty_entries(tracker):
    """Test updating with empty entries list."""
    entries = []
    
    updated_data = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)}
    ]
    
    result = tracker.update_cells(entries, updated_data)
    
    assert len(result) == 1
    assert result[0]["id"] == 101

def test_update_cells_empty_updated_data(tracker):
    """Test updating with empty updated_data (all cells marked as DISSIPATED)."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)},
        {"id": 102, "num_gates": 30, "centroid": [36.0, -96.0], "max_refl": 45, "bbox": _bbox(36.0, -96.0)}
    ]
    
    result = tracker.update_cells(entries, [])
    
    # Should now return all cells as DISSIPATED
    assert len(result) == 2
    assert all(c["event_type"] == "DISSIPATED" for c in result)

def test_update_cells_no_timestamp(tracker):
    """Test updating without providing a timestamp."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)}
    ]
    
    updated_data = [
        {"id": 101, "num_gates": 60, "centroid": [35.1, -97.1], "max_refl": 60, "bbox": _bbox(35.1, -97.1)}
    ]
    
    result = tracker.update_cells(entries, updated_data, timestamp=None)
    
    # Should not add timestamp key
    assert "timestamp" not in result[0] or result[0].get("timestamp") is None

def test_update_cells_logs_condensed_debug_summaries(tracker):
    """Debug logging should summarize entry/update/final state instead of logging per cell."""
    entries = [
        {"id": 101, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0), "tracking_mode": "active"},
        {"id": 102, "centroid": [36.0, -96.0], "max_refl": 45, "bbox": _bbox(36.0, -96.0), "tracking_mode": "predicted"},
    ]
    updated_data = [
        {"id": 101, "centroid": [35.1, -97.1], "max_refl": 60, "bbox": _bbox(35.1, -97.1)},
        {"id": 103, "centroid": [38.0, -94.0], "max_refl": 50, "bbox": _bbox(38.0, -94.0)},
    ]

    tracker.update_cells(entries, updated_data, timestamp="2023-10-15T12:00:00")

    debug_messages = [call.args[0] for call in tracker.io_manager.write_debug.call_args_list]

    assert any(
        "update_cells: existing entries total=2, modes=[active=1, predicted=1], sample_ids=[101, 102]" in message
        for message in debug_messages
    )
    assert any(
        "update_cells: updated detections total=2, sample_ids=[101, 103], sample_centroids=[101:[35.1, -97.1], 103:[38.0, -94.0]]" in message
        for message in debug_messages
    )
    assert any(
        "update_cells: returning entries total=3, modes=[active=2, predicted=1], sample_ids=[101, 102, 103]" in message
        for message in debug_messages
    )

def test_update_cells_handles_merge_with_links(tracker):
    """Test that merge events correctly populate merged_cells and merged_to keys."""
    from EdgeWARN.process.detect.lineage import LineageResult, MergeEvent
    
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)},
        {"id": 102, "num_gates": 30, "centroid": [35.1, -97.1], "max_refl": 45, "bbox": _bbox(35.1, -97.1)}
    ]
    
    # 101 and 102 merge into a new detection with ID 101 (101 is dominant)
    updated_data = [
        {"id": 101, "num_gates": 80, "centroid": [35.05, -97.05], "max_refl": 58, "bbox": _bbox(35.05, -97.05)}
    ]
    
    lineage = LineageResult()
    lineage.merges.append(MergeEvent(child_id=101, parent_ids=[101, 102], dominant_parent=101))
    
    result = tracker.update_cells(entries, updated_data, timestamp="2023-10-15T12:00:00", lineage=lineage)
    
    # Should have 2 entries: the child (101) and the dissipated parent (102)
    assert len(result) == 2
    
    child = next(c for c in result if c["id"] == 101 and c["event_type"] == "MERGE")
    dissipated = next(c for c in result if c["id"] == 102 and c["event_type"] == "DISSIPATED")
    
    assert child["merged_cells"] == [102]
    assert dissipated["merged_to"] == 101
    assert dissipated["timestamp"] == "2023-10-15T12:00:00"

def test_update_cells_handles_natural_dissipation(tracker):
    """Test that unmatched cells are marked as DISSIPATED if they don't enter prediction."""
    entries = [
        {"id": 101, "num_gates": 50, "centroid": [35.0, -97.0], "max_refl": 55, "bbox": _bbox(35.0, -97.0)}
    ]
    
    # Empty updated data means 101 is unmatched
    updated_data = []
    
    # Mock _handle_unmatched_cell to return False (termination)
    tracker._handle_unmatched_cell = MagicMock(return_value=False)
    
    result = tracker.update_cells(entries, updated_data, timestamp="2023-10-15T12:00:00")
    
    assert len(result) == 1
    assert result[0]["id"] == 101
    assert result[0]["event_type"] == "DISSIPATED"
    assert result[0]["tracking_mode"] == "dissipated"


def test_update_cells_split_dominant_updates_kf_with_child_observation(tracker):
    """Dominant split child should carry a KF state updated toward child centroid."""
    from EdgeWARN.process.detect.lineage import LineageResult, SplitEvent

    entries = [
        {"id": 101, "num_gates": 80, "centroid": [35.0, -97.0], "max_refl": 58, "bbox": _bbox(35.0, -97.0)}
    ]

    updated_data = [
        {"id": 201, "num_gates": 55, "centroid": [35.3, -97.3], "max_refl": 60, "bbox": _bbox(35.3, -97.3)},
        {"id": 202, "num_gates": 30, "centroid": [34.9, -96.8], "max_refl": 47, "bbox": _bbox(34.9, -96.8)},
    ]

    lineage = LineageResult()
    lineage.splits.append(SplitEvent(parent_id=101, child_ids=[201, 202], dominant_child=201))

    result = tracker.update_cells(entries, updated_data, timestamp="2023-10-15T12:00:00", lineage=lineage)

    dominant_child = next(c for c in result if c["id"] == 201)
    assert dominant_child["split_from"] == 101

    # KF should have migrated from parent_id -> dominant child id
    assert 101 not in tracker._kalman_filters
    assert 201 in tracker._kalman_filters

    kf_state = tracker._kalman_filters[201].state
    parent_lat, parent_lon = 35.0, -97.0
    child_lat, child_lon = 35.3, -97.3

    # Regression check: state must move toward child centroid in same scan.
    assert abs(kf_state.lat - child_lat) < abs(kf_state.lat - parent_lat)
    assert abs(kf_state.lon - child_lon) < abs(kf_state.lon - parent_lon)
