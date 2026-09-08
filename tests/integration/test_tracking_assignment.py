"""
Integration Tests for Storm Cell Tracking with Assignment Algorithms

Tests the integration of hybrid assignment with StormCellTracker
for various storm tracking scenarios including:
- Crossed paths
- Storm splits
- Storm mergers
- Tracking continuity
- Re-acquisition
"""

import dataclasses

import pytest
import numpy as np
from datetime import datetime
from unittest.mock import Mock, MagicMock

from EdgeWARN.process.detect.track import StormCellTracker
from EdgeWARN.process.detect.kalman import (
    KalmanFilter,
    default_tracking_config,
    default_assignment_config,
    haversine_distance,
)


def _assignment_config(method):
    """Loaded assignment config with only the algorithm swapped."""
    return dataclasses.replace(default_assignment_config(), method=method)


class MockIOManager:
    """Mock IO manager for testing."""
    
    def __init__(self):
        self.messages = []
    
    def write_info(self, msg):
        self.messages.append(('info', msg))
    
    def write_debug(self, msg):
        self.messages.append(('debug', msg))
    
    def write_warning(self, msg):
        self.messages.append(('warning', msg))
    
    def write_error(self, msg):
        self.messages.append(('error', msg))
    
    def get_info_messages(self):
        return [msg for level, msg in self.messages if level == 'info']


class TestHybridAssignmentIntegration:
    """Integration tests for hybrid assignment with StormCellTracker."""
    
    @pytest.fixture
    def io_manager(self):
        """Create a mock IO manager."""
        return MockIOManager()
    
    @pytest.fixture
    def hybrid_tracker(self, io_manager):
        """Create a tracker with hybrid assignment."""
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('hybrid')
        )
    
    @pytest.fixture
    def greedy_tracker(self, io_manager):
        """Create a tracker with greedy assignment."""
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('greedy')
        )
    
    @pytest.fixture
    def sample_entries(self):
        """Create sample storm cell entries."""
        return [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
            {
                'id': 2,
                'centroid': [36.0, -98.0],
                'max_refl': 50.0,
                'num_gates': 150,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
        ]
    
    def test_simple_update_with_hybrid(self, hybrid_tracker, sample_entries):
        """Test simple update with hybrid assignment."""
        updated_data = [
            {
                'id': 1,
                'centroid': [35.005, -97.005],
                'max_refl': 48.0,
                'num_gates': 110,
            },
            {
                'id': 2,
                'centroid': [36.005, -98.005],
                'max_refl': 52.0,
                'num_gates': 160,
            },
        ]
        
        result = hybrid_tracker.update_cells(
            sample_entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # Should have 2 active cells
        assert len(result) == 2
        assert all(c['tracking_mode'] == 'active' for c in result)
        assert {cell['id']: cell['centroid'] for cell in result} == {
            1: [35.005, -97.005],
            2: [36.005, -98.005],
        }
    
    def test_new_cell_detection(self, hybrid_tracker, sample_entries):
        """Test detection of new cells."""
        updated_data = [
            {
                'id': 1,
                'centroid': [35.005, -97.005],
                'max_refl': 48.0,
                'num_gates': 110,
            },
            {
                'id': 3,  # New cell - far from existing tracks
                'centroid': [40.0, -100.0],
                'max_refl': 40.0,
                'num_gates': 80,
            },
        ]
        
        result = hybrid_tracker.update_cells(
            sample_entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # Should have at least 2 cells (1 matched + 1 new)
        # Track 2 may or may not enter prediction mode depending on gating
        assert len(result) >= 2
        ids = [c['id'] for c in result]
        assert 3 in ids  # New cell should be added
    
    def test_prediction_mode_entry(self, hybrid_tracker, sample_entries):
        """Test that unmatched cells enter prediction mode."""
        updated_data = [
            {
                'id': 1,
                'centroid': [35.005, -97.005],
                'max_refl': 48.0,
                'num_gates': 110,
            },
            # Cell 2 not in updated_data
        ]
        
        result = hybrid_tracker.update_cells(
            sample_entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # Should have at least 1 active cell
        active = [c for c in result if c.get('tracking_mode') == 'active']
        assert len(active) >= 1
    
    def test_reacquisition_from_prediction(self, hybrid_tracker, io_manager):
        """Test re-acquisition of a predicted cell."""
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'predicted',
                'prediction_count': 2,
                'confidence': 0.7,
                'kalman_state': {
                    'lat': 35.02,
                    'lon': -97.02,
                    'u': 10.0,
                    'v': 5.0,
                },
            },
        ]
        
        # Initialize Kalman filter for the predicted cell
        kf = KalmanFilter()
        kf.initialize(lat=35.02, lon=-97.02, u=10.0, v=5.0, position_std_km=5.0)
        hybrid_tracker._kalman_filters[1] = kf
        
        updated_data = [
            {
                'id': 101,  # New ID but close to predicted position
                'centroid': [35.025, -97.025],
                'max_refl': 48.0,
                'num_gates': 110,
            },
        ]
        
        result = hybrid_tracker.update_cells(
            entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # Check that re-acquisition happened
        info_messages = io_manager.get_info_messages()
        assert any('re-acquired' in msg.lower() for msg in info_messages)
        assert [(cell['id'], cell['centroid'], cell['tracking_mode']) for cell in result] == [
            (1, [35.025, -97.025], 'active')
        ]


class TestCrossedPathsScenario:
    """Tests for crossed storm paths scenario."""
    
    @pytest.fixture
    def io_manager(self):
        """Create a mock IO manager."""
        return MockIOManager()
    
    @pytest.fixture
    def hybrid_tracker(self, io_manager):
        """Create a tracker with hybrid assignment."""
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('hybrid')
        )
    
    @pytest.fixture
    def greedy_tracker(self, io_manager):
        """Create a tracker with greedy assignment."""
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('greedy')
        )
    
    def test_crossed_paths_hybrid(self, hybrid_tracker):
        """
        Test crossed paths scenario with hybrid assignment.
        
        Track A at (35.0, -97.0) moving East -> predicted at (35.0, -96.9)
        Track B at (35.0, -96.0) moving West -> predicted at (35.0, -96.1)
        
        Detection 1 at (35.0, -96.15) - closer to B's prediction
        Detection 2 at (35.0, -96.85) - closer to A's prediction
        
        Hybrid should assign correctly based on motion consistency.
        """
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
            {
                'id': 2,
                'centroid': [35.0, -96.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
        ]
        
        # Initialize Kalman filters with opposite velocities
        kf1 = KalmanFilter()
        kf1.initialize(lat=35.0, lon=-97.0, u=15.0, v=0.0)  # Moving East
        hybrid_tracker._kalman_filters[1] = kf1
        
        kf2 = KalmanFilter()
        kf2.initialize(lat=35.0, lon=-96.0, u=-15.0, v=0.0)  # Moving West
        hybrid_tracker._kalman_filters[2] = kf2
        
        # Detections in between
        updated_data = [
            {
                'id': 101,
                'centroid': [35.0, -96.15],  # Closer to track 2's position
                'max_refl': 45.0,
                'num_gates': 100,
            },
            {
                'id': 102,
                'centroid': [35.0, -96.85],  # Closer to track 1's position
                'max_refl': 45.0,
                'num_gates': 100,
            },
        ]
        
        result = hybrid_tracker.update_cells(
            entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # Should have 2 active cells
        assert len(result) == 2
        assert all(c['tracking_mode'] == 'active' for c in result)
        assert {cell['id']: cell['centroid'] for cell in result} == {
            1: [35.0, -96.85],
            2: [35.0, -96.15],
        }


class TestStormSplitScenario:
    """Tests for storm split scenario."""
    
    @pytest.fixture
    def io_manager(self):
        return MockIOManager()
    
    @pytest.fixture
    def hybrid_tracker(self, io_manager):
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('hybrid')
        )
    
    def test_storm_split(self, hybrid_tracker):
        """
        Test storm split scenario.
        
        One track splits into two detections.
        One detection should match the original track,
        the other should become a new cell.
        """
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 55.0,
                'num_gates': 200,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
        ]
        
        # Initialize Kalman filter
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=0.0, position_std_km=5.0)
        hybrid_tracker._kalman_filters[1] = kf
        
        # Two detections from split - both close to original
        updated_data = [
            {
                'id': 101,
                'centroid': [35.0, -97.002],  # Very close to original
                'max_refl': 50.0,
                'num_gates': 100,
            },
            {
                'id': 102,
                'centroid': [35.0, -96.998],  # Also very close
                'max_refl': 45.0,
                'num_gates': 100,
            },
        ]
        
        result = hybrid_tracker.update_cells(
            entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        assert {cell['id']: cell['centroid'] for cell in result} == {
            1: [35.0, -96.998],
            101: [35.0, -97.002],
        }
        assert all(cell['tracking_mode'] == 'active' for cell in result)


class TestStormMergeScenario:
    """Tests for storm merge scenario."""
    
    @pytest.fixture
    def io_manager(self):
        return MockIOManager()
    
    @pytest.fixture
    def hybrid_tracker(self, io_manager):
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('hybrid')
        )
    
    def test_storm_merge(self, hybrid_tracker):
        """
        Test storm merge scenario.
        
        Two tracks merge into one detection.
        One track should match, the other should enter prediction mode.
        """
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
            {
                'id': 2,
                'centroid': [35.0, -96.9],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
        ]
        
        # Initialize Kalman filters
        kf1 = KalmanFilter()
        kf1.initialize(lat=35.0, lon=-97.0, u=10.0, v=0.0, position_std_km=5.0)
        hybrid_tracker._kalman_filters[1] = kf1
        
        kf2 = KalmanFilter()
        kf2.initialize(lat=35.0, lon=-96.9, u=10.0, v=0.0, position_std_km=5.0)
        hybrid_tracker._kalman_filters[2] = kf2
        
        # One merged detection - between the two tracks
        updated_data = [
            {
                'id': 101,
                'centroid': [35.0, -96.95],
                'max_refl': 55.0,
                'num_gates': 200,
            },
        ]
        
        result = hybrid_tracker.update_cells(
            entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # A merged detection can update only one track. The other track must
        # remain a distinct prediction or terminate; it must never receive the
        # same observation as a second active match.
        active = [c for c in result if c.get('tracking_mode') == 'active']
        assert [(cell['id'], cell['centroid']) for cell in active] == [
            (1, [35.0, -96.95]),
        ]
        assert len({cell['id'] for cell in result}) == len(result)


class TestTrackingContinuity:
    """Tests for tracking continuity through temporary drops."""
    
    @pytest.fixture
    def io_manager(self):
        return MockIOManager()
    
    @pytest.fixture
    def tracker(self, io_manager):
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=dataclasses.replace(
                default_tracking_config(),
                max_prediction_time_minutes=10.0,
            ),
            assignment_config=_assignment_config('hybrid')
        )
    
    def test_tracking_continuity_through_drop(self, tracker):
        """
        Test that storm remains tracked through temporary detection drop.
        
        Simulates:
        1. Initial detection
        2. Drop for one scan
        3. Re-detection
        """
        # Initial entry
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
        ]
        
        # Initialize Kalman filter
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0, position_std_km=5.0)
        tracker._kalman_filters[1] = kf
        
        # Scan 1: No detection (enter prediction mode)
        result1 = tracker.update_cells(
            entries, [], timestamp='2026-02-18T12:02:00', dt_seconds=120.0
        )
        
        assert [(cell['id'], cell['tracking_mode']) for cell in result1] == [
            (1, 'predicted'),
        ]

        # Scan 2: Re-detection has a new detector ID but remains track 1.
        updated_data = [
            {
                'id': 101,
                'centroid': [35.02, -96.98],  # Close to predicted position
                'max_refl': 48.0,
                'num_gates': 110,
            },
        ]
        
        result2 = tracker.update_cells(
            result1, updated_data, timestamp='2026-02-18T12:04:00', dt_seconds=120.0
        )

        assert [(cell['id'], cell['centroid'], cell['tracking_mode']) for cell in result2] == [
            (1, [35.02, -96.98], 'active'),
        ]
    
    def test_termination_after_timeout(self, tracker):
        """Test that storm is terminated after max prediction time."""
        # Entry in prediction mode with high prediction count
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'predicted',
                'prediction_count': 5,  # Already 5 scans predicted
                'confidence': 0.3,  # Below threshold
            },
        ]
        
        # Initialize Kalman filter
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0, position_std_km=5.0)
        tracker._kalman_filters[1] = kf
        
        # Initialize prediction state
        from EdgeWARN.process.detect.kalman import PredictionState
        pred_state = PredictionState(scan_count=5, total_time_seconds=600.0)
        tracker._prediction_states[1] = pred_state
        
        # No detection
        result = tracker.update_cells(
            entries, [], timestamp='2026-02-18T12:10:00', dt_seconds=120.0
        )
        
        # Should be terminated
        assert len(result) == 0


class TestMethodComparison:
    """Compare hybrid vs greedy assignment methods."""
    
    @pytest.fixture
    def io_manager(self):
        return MockIOManager()
    
    @pytest.fixture
    def hybrid_tracker(self, io_manager):
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('hybrid')
        )
    
    @pytest.fixture
    def greedy_tracker(self, io_manager):
        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=default_tracking_config(),
            assignment_config=_assignment_config('greedy')
        )
    
    def test_both_methods_produce_results(self, hybrid_tracker, greedy_tracker):
        """Test that both methods produce valid results."""
        entries = [
            {
                'id': 1,
                'centroid': [35.0, -97.0],
                'max_refl': 45.0,
                'num_gates': 100,
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
            },
        ]
        
        updated_data = [
            {
                'id': 101,
                'centroid': [35.05, -97.05],
                'max_refl': 48.0,
                'num_gates': 110,
            },
        ]
        
        hybrid_result = hybrid_tracker.update_cells(
            entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        greedy_result = greedy_tracker.update_cells(
            entries, updated_data, timestamp='2026-02-18T12:00:00'
        )
        
        # Both should produce results
        assert len(hybrid_result) == 1
        assert len(greedy_result) == 1
        
        # Both should have active tracking
        assert hybrid_result[0]['tracking_mode'] == 'active'
        assert greedy_result[0]['tracking_mode'] == 'active'
