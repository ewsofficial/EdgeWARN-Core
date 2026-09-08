"""
Unit tests for storm cell lineage detection.

Tests cover:
- Overlap calculation
- Merge detection
- Split detection
- Hysteresis buffer
- Dominant parent/child selection
"""

import pytest
import numpy as np
from pathlib import Path
import tempfile
import json

from EdgeWARN.process.detect.lineage import (
    LineageEvent,
    LineageResult,
    LineageBuffer,
    MergeEvent,
    SplitEvent,
    calculate_overlap_ratio,
    build_spatial_index,
    select_dominant_parent,
    select_dominant_child,
)
from EdgeWARN.process.detect.lineage.detector import LineageDetector
from EdgeWARN.process.detect.lineage.spatial import find_overlapping_cells


class TestOverlapCalculation:
    """Tests for polygon overlap ratio calculation."""
    
    def test_no_overlap_returns_zero(self):
        """Two disjoint polygons should return 0.0 overlap."""
        parent = [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]]
        child = [[36.0, 263.0], [36.0, 263.2], [36.2, 263.2], [36.2, 263.0]]
        
        ratio = calculate_overlap_ratio(parent, child)
        assert ratio == 0.0
    
    def test_partial_overlap_returns_ratio(self):
        """Partially overlapping polygons should return ratio between 0 and 1."""
        # Two squares that overlap by 25%
        parent = [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]]
        child = [[35.1, 262.1], [35.1, 262.3], [35.3, 262.3], [35.3, 262.1]]
        
        ratio = calculate_overlap_ratio(parent, child)
        assert 0.0 < ratio < 1.0
    
    def test_empty_bbox_returns_zero(self):
        """Empty bbox should return 0.0."""
        ratio = calculate_overlap_ratio([], [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]])
        assert ratio == 0.0
        
        ratio = calculate_overlap_ratio(None, None)
        assert ratio == 0.0
    
    def test_invalid_polygon_returns_zero(self):
        """Invalid polygon (less than 3 points) should return 0.0."""
        parent = [[35.0, 262.0], [35.0, 262.2]]  # Only 2 points
        child = [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]]
        
        ratio = calculate_overlap_ratio(parent, child)
        assert ratio == 0.0


class TestSpatialIndex:
    """Tests for spatial index building."""
    
    def test_build_index_creates_entries(self):
        """Building index should create entries for all valid cells."""
        cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            },
            {
                'id': 2,
                'bbox': [[36.0, 263.0], [36.0, 263.2], [36.2, 263.2], [36.2, 263.0]],
                'centroid': [36.1, 263.1],
                'max_refl': 60.0,
                'num_gates': 150,
            },
        ]
        
        index = build_spatial_index(cells)
        cells_data = index['cells_data']
        
        assert len(cells_data) == 2
        assert 1 in cells_data
        assert 2 in cells_data
        assert cells_data[1]['max_refl'] == 55.0
        assert cells_data[2]['num_gates'] == 150
    
    def test_build_index_skips_invalid_cells(self):
        """Building index should skip cells with invalid bbox."""
        cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'max_refl': 55.0,
            },
            {
                'id': 2,
                'bbox': [],  # Invalid
                'max_refl': 60.0,
            },
            {
                'id': 3,
                'bbox': [[35.0, 262.0]],  # Invalid - only 1 point
                'max_refl': 65.0,
            },
        ]
        
        index = build_spatial_index(cells)
        cells_data = index['cells_data']
        
        assert len(cells_data) == 1
        assert 1 in cells_data

    def test_find_overlapping_cells_requires_threshold(self):
        """A bare spatial query cannot silently select a configured fallback."""
        with pytest.raises(TypeError):
            find_overlapping_cells({}, {})


class TestDominantSelection:
    """Tests for dominant parent/child selection."""
    
    def test_select_dominant_parent_by_refl(self):
        """Should select parent with highest max_refl."""
        parent_ids = [1, 2, 3]
        cell_index = {'cells_data': {
            1: {'max_refl': 55.0, 'num_gates': 100},
            2: {'max_refl': 65.0, 'num_gates': 80},  # Highest refl
            3: {'max_refl': 60.0, 'num_gates': 120},
        }}
        
        dominant = select_dominant_parent(parent_ids, cell_index)
        assert dominant == 2
    
    def test_select_dominant_parent_by_gates_tiebreaker(self):
        """Should use num_gates as tiebreaker when max_refl is equal."""
        parent_ids = [1, 2, 3]
        cell_index = {'cells_data': {
            1: {'max_refl': 60.0, 'num_gates': 100},
            2: {'max_refl': 60.0, 'num_gates': 150},  # Same refl, more gates
            3: {'max_refl': 60.0, 'num_gates': 120},
        }}
        
        dominant = select_dominant_parent(parent_ids, cell_index)
        assert dominant == 2
    
    def test_select_dominant_child_by_refl(self):
        """Should select child with highest max_refl."""
        child_ids = [10, 20, 30]
        cell_index = {'cells_data': {
            10: {'max_refl': 50.0, 'num_gates': 100},
            20: {'max_refl': 70.0, 'num_gates': 80},  # Highest refl
            30: {'max_refl': 55.0, 'num_gates': 120},
        }}
        
        dominant = select_dominant_child(child_ids, cell_index)
        assert dominant == 20
    
    def test_select_dominant_single_candidate(self):
        """Should return the only candidate when list has one item."""
        parent_ids = [1]
        cell_index = {'cells_data': {
            1: {'max_refl': 55.0, 'num_gates': 100},
        }}
        
        dominant = select_dominant_parent(parent_ids, cell_index)
        assert dominant == 1


class TestLineageBuffer:
    """Tests for hysteresis buffer."""
    
    def test_buffer_requires_two_confirmations(self):
        """Buffer should require min_confirmations before confirming event."""
        buffer = LineageBuffer(min_confirmations=2)
        
        # First scan - not confirmed
        confirmed = buffer.record_potential_merge(100, [100, 101], 100)
        assert confirmed is False
        
        # End scan 1
        buffer.end_scan(Path("/tmp"))
        
        # Second scan - confirmed
        confirmed = buffer.record_potential_merge(100, [100, 101], 100)
        assert confirmed is True
    
    def test_buffer_persists_to_disk(self):
        """Buffer should save and load state from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stormcell_dir = Path(tmpdir)
            
            # Create and save buffer
            buffer1 = LineageBuffer(min_confirmations=2)
            buffer1.record_potential_merge(100, [100, 101], 100)
            buffer1.save(stormcell_dir)
            
            # Load buffer
            buffer2 = LineageBuffer.load(stormcell_dir)
            
            assert buffer2.min_confirmations == 2
            assert 100 in buffer2.pending_merges
            assert buffer2.pending_merges[100].parent_ids == {100, 101}

    def test_buffer_file_does_not_override_configured_thresholds(self):
        """A stale buffer file must not outrank the configured thresholds.

        `save` records the thresholds alongside the pending events. If `load`
        restored them, a buffer file written before a config change would keep
        pinning the old values, so editing the config would silently do nothing.
        Pending state must still survive the round trip.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            stormcell_dir = Path(tmpdir)

            saved = LineageBuffer(min_confirmations=2, max_pending=100)
            saved.record_potential_merge(100, [100, 101], 100)
            saved.save(stormcell_dir)

            reloaded = LineageBuffer.load(
                stormcell_dir, min_confirmations=4, max_pending=7
            )

            assert reloaded.min_confirmations == 4
            assert reloaded.max_pending == 7
            assert 100 in reloaded.pending_merges

    def test_buffer_restores_scan_counter(self):
        """The scan counter is state, not config, so it must be restored.

        Consecutive-scan detection depends on it: a counter reset to 0 would
        make the reloaded buffer treat an in-flight event as brand new.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            stormcell_dir = Path(tmpdir)

            saved = LineageBuffer(min_confirmations=2)
            saved.record_potential_merge(100, [100, 101], 100)
            saved.end_scan(stormcell_dir)
            saved.save(stormcell_dir)
            assert saved._scan_number > 0

            reloaded = LineageBuffer.load(stormcell_dir)
            assert reloaded._scan_number == saved._scan_number

    def test_buffer_clears_confirmed_events(self):
        """Buffer should clear confirmed events after processing."""
        buffer = LineageBuffer(min_confirmations=1)
        
        buffer.record_potential_merge(100, [100, 101], 100)
        assert buffer.is_merge_confirmed(100)
        
        buffer.clear_confirmed_events()
        assert not buffer.is_merge_confirmed(100)
        assert 100 not in buffer.pending_merges
    
    def test_buffer_prunes_inactive_entries(self):
        """Buffer should prune entries inactive for too long."""
        buffer = LineageBuffer(min_confirmations=5, prune_after_scans=1, scan_interval_seconds=0.1)
        
        # Add a pending merge
        buffer.record_potential_merge(100, [100, 101], 100)
        
        # Wait for prune threshold
        import time
        time.sleep(0.2)
        
        # Prune should remove the entry
        pruned = buffer.prune_inactive()
        assert pruned >= 1
        assert 100 not in buffer.pending_merges


class TestMergeDetection:
    """Tests for merge event detection."""
    
    def test_single_parent_no_merge(self):
        """Single parent overlapping child should not trigger merge."""
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            }
        ]
        new_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            }
        ]
        
        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer)
        result = detector.detect(old_cells, new_cells)
        
        assert len(result.merges) == 0
    
    def test_two_parents_merge_detected(self):
        """Two parents overlapping single child should trigger merge."""
        # Two old cells that will merge into one
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            },
            {
                'id': 2,
                'bbox': [[35.1, 262.1], [35.1, 262.3], [35.3, 262.3], [35.3, 262.1]],
                'centroid': [35.2, 262.2],
                'max_refl': 60.0,
                'num_gates': 120,
            }
        ]
        # New cell overlaps both parents
        new_cells = [
            {
                'id': 3,
                'bbox': [[35.0, 262.0], [35.0, 262.3], [35.3, 262.3], [35.3, 262.0]],
                'centroid': [35.15, 262.15],
                'max_refl': 65.0,
                'num_gates': 200,
            }
        ]
        
        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer, overlap_threshold=0.1)
        result = detector.detect(old_cells, new_cells)
        
        assert len(result.merges) == 1
        assert result.merges[0].child_id == 3
        assert 1 in result.merges[0].parent_ids
        assert 2 in result.merges[0].parent_ids

    def test_merge_into_same_id_detected(self):
        """When one ID persists, merge into same-ID child should still be detected."""
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            },
            {
                'id': 2,
                'bbox': [[35.1, 262.1], [35.1, 262.3], [35.3, 262.3], [35.3, 262.1]],
                'centroid': [35.2, 262.2],
                'max_refl': 65.0,
                'num_gates': 120,
            },
        ]
        new_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.3], [35.3, 262.3], [35.3, 262.0]],
                'centroid': [35.15, 262.15],
                'max_refl': 60.0,
                'num_gates': 200,
            }
        ]

        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer, overlap_threshold=0.1)
        result = detector.detect(old_cells, new_cells)

        assert len(result.merges) == 1
        merge = result.merges[0]
        assert merge.child_id == 1
        assert sorted(merge.parent_ids) == [1, 2]
        assert merge.dominant_parent == 1


class TestSplitDetection:
    """Tests for split event detection."""
    
    def test_single_child_no_split(self):
        """Single child overlapping parent should not trigger split."""
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            }
        ]
        new_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            }
        ]
        
        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer)
        result = detector.detect(old_cells, new_cells)
        
        assert len(result.splits) == 0
    
    def test_two_children_split_detected(self):
        """One parent overlapping two children should trigger split."""
        # One old cell that will split
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.4], [35.4, 262.4], [35.4, 262.0]],
                'centroid': [35.2, 262.2],
                'max_refl': 60.0,
                'num_gates': 200,
            }
        ]
        # Two new cells that overlap the parent
        new_cells = [
            {
                'id': 10,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            },
            {
                'id': 20,
                'bbox': [[35.2, 262.2], [35.2, 262.4], [35.4, 262.4], [35.4, 262.2]],
                'centroid': [35.3, 262.3],
                'max_refl': 50.0,
                'num_gates': 80,
            }
        ]
        
        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer, overlap_threshold=0.1)
        result = detector.detect(old_cells, new_cells)
        
        assert len(result.splits) == 1
        assert result.splits[0].parent_id == 1
        assert 10 in result.splits[0].child_ids
        assert 20 in result.splits[0].child_ids

    def test_split_with_same_id_continuation(self):
        """Parent ID should continue while different ID child is marked as split."""
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.4], [35.4, 262.4], [35.4, 262.0]],
                'centroid': [35.2, 262.2],
                'max_refl': 60.0,
                'num_gates': 200,
            }
        ]
        new_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 120,
            },
            {
                'id': 2,
                'bbox': [[35.2, 262.2], [35.2, 262.4], [35.4, 262.4], [35.4, 262.2]],
                'centroid': [35.3, 262.3],
                'max_refl': 50.0,
                'num_gates': 80,
            },
        ]

        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer, overlap_threshold=0.1)
        result = detector.detect(old_cells, new_cells)

        assert len(result.splits) == 1
        split = result.splits[0]
        assert split.parent_id == 1
        assert sorted(split.child_ids) == [1, 2]
        assert split.dominant_child == 1

        assert result.cell_events[1] == LineageEvent.ACTIVE
        assert result.cell_events[2] == LineageEvent.SPLIT
        assert result.cell_lineage[1]['split_from'] is None
        assert result.cell_lineage[2]['split_from'] == 1


class TestLineageResult:
    """Tests for LineageResult data structure."""
    
    def test_is_empty_when_no_events(self):
        """Result should be empty when no events detected."""
        result = LineageResult()
        assert result.is_empty()
    
    def test_is_not_empty_with_merges(self):
        """Result should not be empty when merges exist."""
        result = LineageResult()
        result.merges.append(MergeEvent(child_id=1, parent_ids=[1, 2], dominant_parent=1))
        assert not result.is_empty()
    
    def test_get_event_type_returns_active_default(self):
        """get_event_type should return ACTIVE for unknown cell."""
        result = LineageResult()
        assert result.get_event_type(999) == LineageEvent.ACTIVE
    
    def test_get_parent_ids_returns_empty_default(self):
        """get_parent_ids should return empty list for non-merge cell."""
        result = LineageResult()
        assert result.get_parent_ids(999) == []
    
    def test_get_split_from_returns_none_default(self):
        """get_split_from should return None for non-split cell."""
        result = LineageResult()
        assert result.get_split_from(999) is None


class TestLineageDetectorIntegration:
    """Integration tests for the full lineage detection pipeline."""
    
    def test_detect_returns_lineage_result(self):
        """detect should return a LineageResult object."""
        old_cells = []
        new_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            }
        ]
        
        detector = LineageDetector()
        result = detector.detect(old_cells, new_cells)
        
        assert isinstance(result, LineageResult)
        assert len(result.unmatched_new) == 1  # New cell detected
        assert 1 in result.unmatched_new
    
    def test_detect_identifies_dissipated_cells(self):
        """detect should identify cells that disappeared."""
        old_cells = [
            {
                'id': 1,
                'bbox': [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]],
                'centroid': [35.1, 262.1],
                'max_refl': 55.0,
                'num_gates': 100,
            }
        ]
        new_cells = []
        
        detector = LineageDetector()
        result = detector.detect(old_cells, new_cells)
        
        assert len(result.unmatched_old) == 1
        assert 1 in result.unmatched_old
        assert result.cell_events[1] == LineageEvent.DISSIPATED
