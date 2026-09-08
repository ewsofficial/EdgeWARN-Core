"""
Integration Tests for Kalman Filter Tracking

Tests the integration of Kalman filter with StormCellTracker
for storm continuity tracking.
"""

import dataclasses

import pytest
from datetime import datetime
from unittest.mock import Mock, MagicMock

from EdgeWARN.process.detect.track import StormCellTracker
from EdgeWARN.process.detect.kalman import (
    default_tracking_config,
    default_kalman_config,
    haversine_distance,
)


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


class TestStormCellTrackerKalman:
    """Tests for StormCellTracker with Kalman filter integration."""
    
    @pytest.fixture
    def tracker(self):
        """Create a tracker instance for testing."""
        io_manager = MockIOManager()
        config = dataclasses.replace(
            default_tracking_config(),
            confidence_decay_factor=0.8,
            # Lower the threshold so the cell doesn't terminate after 2 scans.
            confidence_threshold=0.1,
        )

        # Acceleration is the only process-noise knob: the jerk model derives
        # every block of Q from it. The position and velocity overrides that used
        # to sit here fed a matrix nothing read.
        kalman_config = dataclasses.replace(
            default_kalman_config(),
            process_noise_acceleration=1e-12,
        )

        return StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager,
            tracking_config=config,
            kalman_config=kalman_config
        )
    
    @pytest.fixture
    def active_cells(self):
        """Create sample active storm cells."""
        return [
            {
                'id': 1001,
                'centroid': [35.0, -97.0],
                'num_gates': 150,
                'max_refl': 55.0,
                'bbox': [[34.9, -97.1], [35.1, -96.9]],
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
                'dx': 1200.0,
                'dy': 600.0,
                'dt': 120.0,
                'modules': {
                    'StormCast': {
                        'status': 'success',
                        'u': 10.0,
                        'v': 5.0
                    }
                }
            },
            {
                'id': 1002,
                'centroid': [36.0, -96.0],
                'num_gates': 200,
                'max_refl': 60.0,
                'bbox': [[35.9, -96.1], [36.1, -95.9]],
                'tracking_mode': 'active',
                'prediction_count': 0,
                'confidence': 1.0,
                'dx': 800.0,
                'dy': 400.0,
                'dt': 120.0
            }
        ]
    
    def test_new_cell_not_within_radius(self, tracker, active_cells):
        """Test that new cell far from predicted is added as new."""
        # Put cell 1001 into prediction mode
        updated_data_1 = [
            {
                'id': 1002,
                'centroid': [36.01, -96.01],
                'num_gates': 210,
                'max_refl': 61.0,
                'bbox': [[35.91, -96.11], [36.11, -95.91]]
            }
        ]
        
        result_1 = tracker.update_cells(
            entries=active_cells,
            updated_data=updated_data_1,
            timestamp='2026-01-01T00:02:00',
            dt_seconds=120.0
        )
        
        # New detection far from predicted position (> 5km)
        new_detection = {
            'id': 9999,
            'centroid': [40.0, -90.0],  # Far away
            'num_gates': 100,
            'max_refl': 50.0,
            'bbox': [[39.9, -90.1], [40.1, -89.9]]
        }
        
        result_2 = tracker.update_cells(
            entries=result_1,
            updated_data=[new_detection, updated_data_1[0]],
            timestamp='2026-01-01T00:04:00',
            dt_seconds=120.0
        )
        
        # Should have 3 cells: 1 active (1002), 1 predicted (1001), 1 new (9999)
        assert len(result_2) == 3
        
        ids = [c['id'] for c in result_2]
        assert 1001 in ids  # Still predicted
        assert 1002 in ids  # Active
        assert 9999 in ids  # New
    
    def test_confidence_decreases_over_scans(self, tracker):
        """Test that confidence decreases over multiple prediction scans."""
        cell = {
            'id': 1001,
            'centroid': [35.0, -97.0],
            'num_gates': 150,
            'max_refl': 55.0,
            'bbox': [[34.9, -97.1], [35.1, -96.9]],
            'tracking_mode': 'active',
            'prediction_count': 0,
            'confidence': 1.0,
            'dx': 1200.0,
            'dy': 600.0,
            'dt': 120.0
        }
        
        confidences = []
        
        for i in range(3):
            print(f"DEBUG Iteration {i} with cell: {cell}")
            result = tracker.update_cells(
                entries=[cell],
                updated_data=[],  # No updates - force prediction mode
                timestamp=f'2026-01-01T00:{i*2:02d}:00',
                dt_seconds=120.0
            )
            print(f"DEBUG Result: {result}")
            
            if result:
                cell = result[0]
                confidences.append(cell['confidence'])
            else:
                # Cell was terminated
                break
            print(f"DEBUG Confidences after iteration {i}: {confidences}")
        
        # Confidence should decrease
        assert len(confidences) >= 2
        for i in range(1, len(confidences)):
            assert confidences[i-1] > confidences[i]
    
    def test_stormcast_velocity_used_for_prediction(self, tracker, active_cells):
        """Test that StormCast velocity is used for prediction."""
        # Cell 1001 has StormCast velocity
        updated_data = [
            {
                'id': 1002,
                'centroid': [36.01, -96.01],
                'num_gates': 210,
                'max_refl': 61.0,
                'bbox': [[35.91, -96.11], [36.11, -95.91]]
            }
        ]
        
        result = tracker.update_cells(
            entries=active_cells,
            updated_data=updated_data,
            timestamp='2026-01-01T00:02:00',
            dt_seconds=120.0
        )
        
        predicted = [c for c in result if c['tracking_mode'] == 'predicted'][0]
        
        # Check that Kalman filter was initialized with StormCast velocity
        kf = tracker._kalman_filters.get(1001)
        assert kf is not None
        # StormCast velocity: u=10.0, v=5.0
        assert kf.state.u == 10.0
        assert kf.state.v == 5.0


class TestTrackingStatistics:
    """Tests for tracking statistics logging."""
    
    def test_statistics_logged(self):
        """Test that tracking statistics are logged."""
        io_manager = MockIOManager()
        tracker = StormCellTracker(
            ps_old=None,
            ps_new=None,
            io_manager=io_manager
        )
        
        cells = [
            {'id': 1, 'centroid': [35.0, -97.0], 'tracking_mode': 'active', 
             'confidence': 1.0, 'dx': 1000.0, 'dy': 500.0, 'dt': 120.0}
        ]
        
        updated = [
            {'id': 1, 'centroid': [35.01, -97.01], 'num_gates': 100, 
             'max_refl': 55.0, 'bbox': []}
        ]
        
        tracker.update_cells(cells, updated, '2026-01-01T00:00:00')
        
        # Check that stats were logged
        info_msgs = [m for t, m in io_manager.messages if t == 'info']
        assert any('Update Stats' in m for m in info_msgs)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
