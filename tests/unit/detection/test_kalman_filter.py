"""
Unit Tests for Kalman Filter Module

Tests the core Kalman filter functionality for storm cell tracking.
"""

import pytest
import numpy as np
from datetime import datetime

from EdgeWARN.process.detect.kalman import (
    KalmanFilter,
    KalmanObservation,
    StateVector,
    CovarianceMatrix,
    ConfidenceCalculator,
    PredictionState,
    TrackingConfig,
    KalmanConfig,
    haversine_distance,
    latlon_to_meters,
    meters_to_latlon,
)


class TestStateVector:
    """Tests for StateVector class."""
    
    def test_state_vector_to_array(self):
        """Test conversion to numpy array."""
        state = StateVector(lat=33.5, lon=-97.2, u=12.5, v=-6.7)
        arr = state.to_array()
        
        assert arr.shape == (6,)
        assert arr[0] == 33.5
        assert arr[1] == -97.2
        assert arr[2] == 12.5
        assert arr[3] == -6.7
    
    def test_state_vector_from_array(self):
        """Test creation from numpy array."""
        arr = np.array([33.5, -97.2, 12.5, -6.7, 0.1, -0.2])
        state = StateVector.from_array(arr)
        
        assert state.lat == 33.5
        assert state.lon == -97.2
        assert state.u == 12.5
        assert state.v == -6.7
        assert state.a_lat == 0.1
        assert state.a_lon == -0.2
    
    def test_get_speed(self):
        """Test speed calculation."""
        state = StateVector(u=3.0, v=4.0)
        assert state.get_speed() == 5.0
    
    def test_get_bearing(self):
        """Test bearing calculation."""
        # North
        state = StateVector(u=0.0, v=10.0)
        assert state.get_bearing() == 0.0
        
        # East
        state = StateVector(u=10.0, v=0.0)
        assert state.get_bearing() == 90.0


class TestCovarianceMatrix:
    """Tests for CovarianceMatrix class."""
    
    def test_from_diagonal(self):
        """Test creation from diagonal variances."""
        variances = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        cov = CovarianceMatrix.from_diagonal(variances)
        arr = cov.to_array()
        
        assert np.allclose(np.diag(arr), variances)
    
    def test_from_position_uncertainty(self):
        """Test creation from position uncertainty."""
        cov = CovarianceMatrix.from_position_uncertainty(
            1.0, velocity_variance=100.0, acceleration_variance=1.0
        )
        var_lat, var_lon = cov.get_position_variance()

        # Should be approximately (1/111)^2 for latitude
        assert var_lat > 0
        assert var_lon > 0
        assert cov.get_velocity_variance() == (100.0, 100.0)


class TestCoordinateConversion:
    """Tests for coordinate conversion functions."""
    
    def test_latlon_to_meters_zero(self):
        """Test conversion at reference point."""
        x, y = latlon_to_meters(35.0, -97.0, 35.0, -97.0)
        assert abs(x) < 1.0  # Within 1 meter
        assert abs(y) < 1.0
    
    def test_latlon_to_meters_north(self):
        """Test conversion for northward displacement."""
        # 1 degree latitude ≈ 111 km
        x, y = latlon_to_meters(36.0, -97.0, 35.0, -97.0)
        assert abs(x) < 100  # Small x (east-west)
        assert y > 110000  # ~111 km northward
    
    def test_meters_to_latlon_roundtrip(self):
        """Test roundtrip conversion."""
        ref_lat, ref_lon = 35.0, -97.0
        x, y = 5000.0, 10000.0  # 5 km east, 10 km north
        
        lat, lon = meters_to_latlon(x, y, ref_lat, ref_lon)
        x2, y2 = latlon_to_meters(lat, lon, ref_lat, ref_lon)
        
        assert abs(x - x2) < 10  # Within 10 meters
        assert abs(y - y2) < 10
    
    def test_haversine_distance(self):
        """Test haversine distance calculation."""
        # Distance from NYC to LA is approximately 3940 km
        nyc = (40.7128, -74.0060)
        la = (34.0522, -118.2437)
        
        distance = haversine_distance(nyc[0], nyc[1], la[0], la[1])
        
        # Allow 5% error due to approximation
        assert 3700 < distance < 4200


class TestKalmanFilter:
    """Tests for KalmanFilter class."""
    
    def test_initialization(self):
        """Test Kalman filter initialization."""
        kf = KalmanFilter()
        kf.initialize(lat=33.5, lon=-97.2, u=12.5, v=-6.7)
        
        assert kf.state.lat == 33.5
        assert kf.state.lon == -97.2
        assert kf.state.u == 12.5
        assert kf.state.v == -6.7
        assert kf._initialized
    
    def test_predict_step(self):
        """Test prediction step."""
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0)
        
        # Predict 2 minutes forward
        predicted = kf.predict(dt=120.0)
        
        # Position should change based on velocity
        # 10 m/s * 120 s = 1200 m ≈ 0.011 degrees
        assert predicted.lat != 35.0
        assert predicted.lon != -97.0
    
    def test_update_step(self):
        """Test update step with observation."""
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=0.0, v=0.0)
        
        # Update with observation slightly offset
        obs = KalmanObservation(lat=35.01, lon=-97.01)
        updated = kf.update(obs)
        
        # State should move toward observation
        assert abs(updated.lat - 35.01) < abs(35.0 - 35.01)
        assert abs(updated.lon - -97.01) < abs(-97.0 - -97.01)
    
    def test_predict_update_cycle(self):
        """Test full predict-update cycle."""
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0)
        
        # Predict
        kf.predict(dt=120.0)
        
        # Update with observation at predicted location
        pred_lat, pred_lon = kf.state.lat, kf.state.lon
        obs = KalmanObservation(lat=pred_lat, lon=pred_lon)
        kf.update(obs)
        
        # State should be close to observation
        assert abs(kf.state.lat - pred_lat) < 0.001
        assert abs(kf.state.lon - pred_lon) < 0.001
    
    def test_control_input(self):
        """Test prediction with StormCast control input."""
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=0.0, v=0.0)
        
        # Predict with control input (StormCast velocity)
        predicted = kf.predict(dt=120.0, control_u=15.0, control_v=10.0)
        
        # Velocity should be updated to control input
        assert predicted.u == 15.0
        assert predicted.v == 10.0
    
    def test_get_state_dict(self):
        """Test state serialization."""
        kf = KalmanFilter()
        kf.initialize(lat=33.5, lon=-97.2, u=12.5, v=-6.7)
        
        state_dict = kf.get_state_dict()
        
        assert state_dict['lat'] == 33.5
        assert state_dict['lon'] == -97.2
        assert state_dict['u'] == 12.5
        assert state_dict['v'] == -6.7
        assert 'P' in state_dict
    
    def test_from_state_dict(self):
        """Test state deserialization."""
        state_dict = {
            'lat': 33.5,
            'lon': -97.2,
            'u': 12.5,
            'v': -6.7,
            'a_lat': 0.1,
            'a_lon': -0.2,
            'P': np.eye(6).tolist()
        }
        
        kf = KalmanFilter.from_state_dict(state_dict)
        
        assert kf.state.lat == 33.5
        assert kf.state.lon == -97.2
        assert kf.state.u == 12.5
        assert kf.state.v == -6.7


class TestConfidenceCalculator:
    """Tests for ConfidenceCalculator class."""
    
    def test_initial_confidence(self):
        """Test initial confidence calculation."""
        calc = ConfidenceCalculator()
        confidence = calc.calculate(scans_predicted=0, time_predicted_seconds=0)
        
        assert confidence == 1.0
    
    def test_confidence_decay(self):
        """Test confidence decay over scans."""
        calc = ConfidenceCalculator()
        
        conf_0 = calc.calculate(scans_predicted=0, time_predicted_seconds=0)
        conf_1 = calc.calculate(scans_predicted=1, time_predicted_seconds=120)
        conf_2 = calc.calculate(scans_predicted=2, time_predicted_seconds=240)
        
        # Confidence should decrease
        assert conf_0 > conf_1 > conf_2
    
    def test_should_terminate_confidence(self):
        """Test termination based on confidence."""
        calc = ConfidenceCalculator()
        
        # High confidence - should not terminate
        should_term, reason = calc.should_terminate(
            confidence=0.8, time_predicted_seconds=120, scans_predicted=1
        )
        assert not should_term
        
        # Low confidence - should terminate
        should_term, reason = calc.should_terminate(
            confidence=0.3, time_predicted_seconds=120, scans_predicted=1
        )
        assert should_term
        assert "threshold" in reason.lower()
    
    def test_should_terminate_time(self):
        """Test termination based on time limit."""
        calc = ConfidenceCalculator()
        
        # Exceed time limit
        should_term, reason = calc.should_terminate(
            confidence=0.8, time_predicted_seconds=700, scans_predicted=6
        )  # > 10 minutes
        assert should_term
        assert "limit" in reason.lower() or "exceeds" in reason.lower()
    
class TestPredictionState:
    """Tests for PredictionState class."""
    
    def test_increment(self):
        """Test prediction state increment."""
        state = PredictionState()
        state.increment(dt_seconds=120.0, new_confidence=0.7, 
                       predicted_position=(35.1, -97.1))
        
        assert state.scan_count == 1
        assert state.total_time_seconds == 120.0
        assert state.confidence == 0.7
        assert len(state.predicted_positions) == 1
    
    def test_reset(self):
        """Test prediction state reset."""
        state = PredictionState()
        state.increment(120.0, 0.7, (35.1, -97.1))
        state.reset()
        
        assert state.scan_count == 0
        assert state.total_time_seconds == 0.0
        assert state.confidence == 1.0
        assert len(state.predicted_positions) == 0
    
    def test_serialization(self):
        """Test prediction state serialization."""
        state = PredictionState(
            scan_count=2,
            total_time_seconds=240.0,
            confidence=0.5,
            start_timestamp="2026-01-01T00:00:00"
        )
        
        data = state.to_dict()
        restored = PredictionState.from_dict(data)
        
        assert restored.scan_count == 2
        assert restored.total_time_seconds == 240.0
        assert restored.confidence == 0.5


class TestKalmanFilterWithCell:
    """Tests for Kalman filter initialization from storm cell data."""
    
    def test_initialize_from_cell(self):
        """Test initialization from basic cell data."""
        cell = {
            'centroid': [35.0, -97.0],
            'dx': 1500.0,  # meters
            'dy': 800.0,
            'dt': 120.0    # seconds
        }
        
        kf = KalmanFilter()
        kf.initialize_from_cell(cell)
        
        assert kf.state.lat == 35.0
        assert kf.state.lon == -97.0
        # Velocity = dx/dt = 1500/120 = 12.5 m/s
        assert abs(kf.state.u - 12.5) < 0.1
        assert abs(kf.state.v - 800/120) < 0.1
    
    def test_initialize_from_cell_with_stormcast(self):
        """Test initialization with StormCast velocity."""
        cell = {
            'centroid': [35.0, -97.0],
            'dx': 1500.0,
            'dy': 800.0,
            'dt': 120.0,
            'modules': {
                'StormCast': {
                    'status': 'success',
                    'u': 15.0,
                    'v': 10.0
                }
            }
        }
        
        kf = KalmanFilter()
        kf.initialize_from_cell(cell)
        
        # Should use StormCast velocity
        assert kf.state.u == 15.0
        assert kf.state.v == 10.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
