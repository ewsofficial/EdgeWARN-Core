"""
Regression tests for high-priority audit fixes (H1, H2, H3, H4).

H1: Split processing KF migration — dominant child inherits parent's KF
H2: Hysteresis buffer enforces consecutive scan detections
H3: Split overlap direction uses old cell area as denominator
H4: Bare except replaced with (ValueError, TypeError)
"""

import pytest
from unittest.mock import MagicMock
from datetime import datetime

from EdgeWARN.process.detect.track import StormCellTracker
from EdgeWARN.process.detect.kalman import KalmanFilter
from EdgeWARN.process.detect.lineage.events import (
    LineageResult,
    SplitEvent,
    LineageEvent,
)
from EdgeWARN.process.detect.lineage.buffer import LineageBuffer
from EdgeWARN.process.detect.lineage.detector import LineageDetector


@pytest.fixture
def mock_io():
    io = MagicMock()
    io.write_info = MagicMock()
    io.write_error = MagicMock()
    io.write_warning = MagicMock()
    io.write_debug = MagicMock()
    return io


@pytest.fixture
def tracker(mock_io):
    return StormCellTracker(None, None, mock_io)


def _make_cell(cell_id, lat=35.0, lon=-97.0, refl=55, gates=50):
    """Helper to create a cell dict with a proper polygon bbox."""
    return {
        "id": cell_id,
        "centroid": [lat, lon],
        "max_refl": refl,
        "num_gates": gates,
        "bbox": [
            [lat - 0.1, lon - 0.1],
            [lat + 0.1, lon - 0.1],
            [lat + 0.1, lon + 0.1],
            [lat - 0.1, lon + 0.1],
        ],
    }


# ============================================================================
# H1: Split processing migrates KF from parent_id key to dominant child_id
# ============================================================================

class TestH1SplitKFMigration:
    """Verify that after a split, the Kalman filter follows the dominant child."""

    def test_split_migrates_kf_to_dominant_child(self, tracker):
        """KF should be reachable under child_id after split, not parent_id."""
        old_100 = _make_cell(100, lat=35.0, lon=-97.0, refl=60, gates=80)
        new_300 = _make_cell(300, lat=35.02, lon=-97.0, refl=62, gates=70)  # dominant
        new_301 = _make_cell(301, lat=34.95, lon=-97.0, refl=40, gates=30)  # secondary

        entries = [old_100]

        # Pre-create a trained KF for the parent
        kf_100 = KalmanFilter()
        kf_100.initialize_from_cell(old_100)
        tracker._kalman_filters[100] = kf_100

        # Build lineage result with a confirmed split
        lineage = LineageResult()
        lineage.splits.append(SplitEvent(
            parent_id=100,
            child_ids=[300, 301],
            dominant_child=300,
        ))
        lineage.cell_events[300] = LineageEvent.ACTIVE
        lineage.cell_events[301] = LineageEvent.SPLIT

        result = tracker.update_cells(
            entries, [new_300, new_301],
            timestamp="2024-01-01T00:02:00",
            lineage=lineage,
        )

        # KF should now be under dominant child 300
        assert 300 in tracker._kalman_filters, "KF not found under dominant child_id after split"
        # KF should be the SAME object (migrated, not recreated)
        assert tracker._kalman_filters[300] is kf_100, "KF was recreated instead of migrated"

    def test_split_cleans_up_parent_kf(self, tracker):
        """Parent's KF should be removed after split processing."""
        old_100 = _make_cell(100, refl=60, gates=80)
        new_300 = _make_cell(300, refl=62, gates=70)
        new_301 = _make_cell(301, refl=40, gates=30)

        entries = [old_100]
        kf_100 = KalmanFilter()
        kf_100.initialize_from_cell(old_100)
        tracker._kalman_filters[100] = kf_100

        lineage = LineageResult()
        lineage.splits.append(SplitEvent(
            parent_id=100, child_ids=[300, 301], dominant_child=300,
        ))
        lineage.cell_events[300] = LineageEvent.ACTIVE
        lineage.cell_events[301] = LineageEvent.SPLIT

        tracker.update_cells(
            entries, [new_300, new_301],
            timestamp="2024-01-01T00:02:00",
            lineage=lineage,
        )

        # Parent KF should NOT remain
        assert 100 not in tracker._kalman_filters, "Parent KF not cleaned up after split"
        assert 100 not in tracker._prediction_states, "Parent prediction state not cleaned up"

    def test_secondary_child_gets_fresh_kf(self, tracker):
        """Non-dominant children should get their own fresh KF."""
        old_100 = _make_cell(100, refl=60, gates=80)
        new_300 = _make_cell(300, refl=62, gates=70)
        new_301 = _make_cell(301, refl=40, gates=30)

        entries = [old_100]
        kf_100 = KalmanFilter()
        kf_100.initialize_from_cell(old_100)
        tracker._kalman_filters[100] = kf_100

        lineage = LineageResult()
        lineage.splits.append(SplitEvent(
            parent_id=100, child_ids=[300, 301], dominant_child=300,
        ))
        lineage.cell_events[300] = LineageEvent.ACTIVE
        lineage.cell_events[301] = LineageEvent.SPLIT

        tracker.update_cells(
            entries, [new_300, new_301],
            timestamp="2024-01-01T00:02:00",
            lineage=lineage,
        )

        # Secondary child 301 should have its own KF (not the parent's)
        assert 301 in tracker._kalman_filters, "Secondary child missing KF"
        assert tracker._kalman_filters[301] is not kf_100, \
            "Secondary child should NOT share parent's KF"


# ============================================================================
# H3: Split overlap direction uses old cell area as denominator
# ============================================================================

class TestH3SplitOverlapDirection:
    """Verify that split detection uses parent area as denominator."""

    def test_split_overlap_uses_old_area(self):
        """Split overlap should be intersection / old_cell_area."""
        # Large parent
        old_cell = {
            "id": 100,
            "centroid": [35.0, 262.5],
            "max_refl": 55,
            "num_gates": 100,
            "bbox": [
                [34.8, 262.3], [34.8, 262.7],
                [35.2, 262.7], [35.2, 262.3],
            ],
        }
        # Small child that overlaps part of parent
        new_child = {
            "id": 200,
            "centroid": [35.1, 262.6],
            "max_refl": 50,
            "num_gates": 30,
            "bbox": [
                [35.0, 262.5], [35.0, 262.7],
                [35.2, 262.7], [35.2, 262.5],
            ],
        }

        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer, overlap_threshold=0.10)

        # _find_split_overlaps should compute ratio relative to old cell
        from EdgeWARN.process.detect.lineage.spatial import build_spatial_index
        new_index = build_spatial_index([new_child])

        overlaps = detector._find_split_overlaps(old_cell, new_index, 0.0)
        assert len(overlaps) == 1
        new_id, ratio = overlaps[0]
        assert new_id == 200
        # Intersection area / old area should be < 1.0 (child is smaller)
        assert 0.0 < ratio < 1.0, f"Expected ratio < 1.0, got {ratio}"

    def test_small_fragment_detected(self):
        """A small fragment of a large parent should be detected as a split."""
        # Very large parent
        old_cell = {
            "id": 100,
            "centroid": [35.0, 262.5],
            "max_refl": 60,
            "num_gates": 200,
            "bbox": [
                [34.5, 262.0], [34.5, 263.0],
                [35.5, 263.0], [35.5, 262.0],
            ],
        }
        # First child: large portion of parent
        child1 = {
            "id": 200,
            "centroid": [35.0, 262.5],
            "max_refl": 58,
            "num_gates": 150,
            "bbox": [
                [34.5, 262.0], [34.5, 262.8],
                [35.5, 262.8], [35.5, 262.0],
            ],
        }
        # Second child: small fragment
        child2 = {
            "id": 201,
            "centroid": [35.0, 262.9],
            "max_refl": 45,
            "num_gates": 40,
            "bbox": [
                [34.5, 262.8], [34.5, 263.0],
                [35.5, 263.0], [35.5, 262.8],
            ],
        }

        buffer = LineageBuffer(min_confirmations=1)
        detector = LineageDetector(buffer=buffer, overlap_threshold=0.10)

        # Run full detection
        result = detector.detect([old_cell], [child1, child2])

        # Should detect a split event
        assert len(result.splits) == 1, f"Expected 1 split, got {len(result.splits)}"
        assert result.splits[0].parent_id == 100


# ============================================================================
# H4: Bare except replaced with (ValueError, TypeError)
# ============================================================================

class TestH4SpecificExcept:
    """Verify that only ValueError/TypeError are caught, not KeyboardInterrupt."""

    def test_value_error_handled(self, tracker):
        """ValueError on bad timestamp should not crash."""
        cell = _make_cell(100)
        cell["timestamp"] = "not-a-real-timestamp"

        # Should not raise
        tracker._update_kalman_with_observation(cell, 100)
        assert 100 in tracker._kalman_filters

    def test_keyboard_interrupt_propagates(self, tracker):
        """KeyboardInterrupt should NOT be swallowed."""
        cell = _make_cell(100)
        cell["timestamp"] = "2024-01-01T00:00:00"

        # Pre-create KF so it enters the update path
        kf = KalmanFilter()
        kf.initialize_from_cell(cell)
        tracker._kalman_filters[100] = kf

        # Monkey-patch fromisoformat to raise KeyboardInterrupt
        original = datetime.fromisoformat

        def raise_keyboard(*args, **kwargs):
            raise KeyboardInterrupt("simulated")

        import EdgeWARN.process.detect.track as track_module
        old_datetime = track_module.datetime

        try:
            track_module.datetime = type('MockDatetime', (), {
                'fromisoformat': staticmethod(raise_keyboard)
            })()
            with pytest.raises(KeyboardInterrupt):
                tracker._update_kalman_with_observation(cell, 100)
        finally:
            track_module.datetime = old_datetime
