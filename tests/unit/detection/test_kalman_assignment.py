"""
Unit Tests for Kalman Filter Assignment Module

Tests the measurement assignment functionality including:
- Mahalanobis distance calculation
- Validation gating
- Cost function computation
- Pre-filtering
- Hungarian algorithm assignment
"""

import dataclasses

import pytest
import numpy as np
from datetime import datetime

from EdgeWARN.process.detect.kalman import (
    KalmanFilter,
    KalmanObservation,
    AssignmentConfig,
    default_assignment_config,
    haversine_distance,
)

from EdgeWARN.process.detect.kalman.assignment import (
    AssignmentCostCalculator,
    AssignmentResult,
    build_cost_matrix,
    build_filtered_cost_matrix,
    solve_assignment,
    run_hybrid_assignment,
    run_greedy_assignment,
)


class TestMahalanobisDistance:
    """Tests for Mahalanobis distance methods in KalmanFilter."""
    
    @pytest.fixture
    def initialized_filter(self):
        """Create an initialized Kalman filter."""
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0)
        return kf
    
    def test_get_innovation_covariance_shape(self, initialized_filter):
        """Test that innovation covariance has correct shape."""
        S = initialized_filter.get_innovation_covariance()
        assert S.shape == (2, 2)
    
    def test_get_innovation_covariance_symmetric(self, initialized_filter):
        """Test that innovation covariance is symmetric."""
        S = initialized_filter.get_innovation_covariance()
        assert np.allclose(S, S.T)
    
    def test_get_innovation_covariance_positive_definite(self, initialized_filter):
        """Test that innovation covariance is positive definite."""
        S = initialized_filter.get_innovation_covariance()
        eigenvalues = np.linalg.eigvalsh(S)
        assert np.all(eigenvalues > 0)
    
    def test_get_innovation_covariance_regularization(self, initialized_filter):
        """Test that regularization is applied."""
        S = initialized_filter.get_innovation_covariance(regularization=1e-4)
        # Should not raise any errors
        assert S.shape == (2, 2)
    
    def test_get_mahalanobis_distance_at_center(self, initialized_filter):
        """Test Mahalanobis distance at predicted position is near zero."""
        # At the predicted position, distance should be very small
        d_m = initialized_filter.get_mahalanobis_distance(35.0, -97.0)
        assert d_m < 0.01
    
    def test_get_mahalanobis_distance_away_from_center(self, initialized_filter):
        """Test Mahalanobis distance increases away from predicted position."""
        d_m_center = initialized_filter.get_mahalanobis_distance(35.0, -97.0)
        d_m_away = initialized_filter.get_mahalanobis_distance(35.1, -97.0)
        
        assert d_m_away > d_m_center
    
    def test_get_mahalanobis_distance_uninitialized(self):
        """Test Mahalanobis distance for uninitialized filter."""
        kf = KalmanFilter()
        d_m = kf.get_mahalanobis_distance(35.0, -97.0)
        assert d_m == float('inf')
    
    def test_is_within_gate_accept(self, initialized_filter):
        """Test that close point is accepted."""
        # At predicted position should be accepted
        is_valid = initialized_filter.is_within_gate(
            35.0, -97.0, threshold=6.0, min_radius_km=2.0
        )
        assert is_valid is True
    
    def test_is_within_gate_reject(self, initialized_filter):
        """Test that far point is rejected."""
        # Very far point should be rejected
        is_valid = initialized_filter.is_within_gate(
            40.0, -90.0, threshold=6.0, min_radius_km=2.0
        )
        assert is_valid is False
    
    def test_is_within_gate_fallback(self, initialized_filter):
        """Test minimum radius fallback for collapsed covariance."""
        # With very small covariance, Mahalanobis distance can be very large
        # but we should still accept points within min_radius_km
        initialized_filter.covariance = initialized_filter.covariance.from_diagonal(
            [1e-10, 1e-10, 1.0, 1.0, 0.1, 0.1]
        )
        
        # Point 1 km away should be accepted via fallback
        is_valid = initialized_filter.is_within_gate(
            35.01, -97.0, threshold=6.0, min_radius_km=2.0
        )
        assert is_valid is True


class TestAssignmentConfig:
    """Tests for AssignmentConfig dataclass."""

    def test_yaml_supplies_every_value(self):
        """The dataclass declares no defaults, so all values come from YAML."""
        config = default_assignment_config()

        assert config.prefilter_radius_km == 16.0
        assert config.gating_threshold == 6.0
        assert config.min_gating_radius_km == 2.0
        assert config.method == "greedy"
        assert config.weight_position == 1.0

    def test_replace_overrides_a_single_value(self):
        """Callers override by deriving from the loaded config, not by rebuilding."""
        config = dataclasses.replace(
            default_assignment_config(),
            prefilter_radius_km=20.0,
            gating_threshold=5.0,
        )

        assert config.prefilter_radius_km == 20.0
        assert config.gating_threshold == 5.0
        # Untouched fields keep the YAML values.
        assert config.min_gating_radius_km == 2.0
        assert config.weight_position == 1.0

    def test_cannot_be_constructed_without_all_fields(self):
        with pytest.raises(TypeError):
            AssignmentConfig(prefilter_radius_km=20.0)


class TestAssignmentCostCalculator:
    """Tests for AssignmentCostCalculator class."""
    
    @pytest.fixture
    def calculator(self):
        """Create a cost calculator with default config."""
        return AssignmentCostCalculator()
    
    @pytest.fixture
    def sample_track(self):
        """Create a sample track dictionary."""
        return {
            'id': 1,
            'centroid': [35.0, -97.0],
            'max_refl': 45.0,
            'num_gates': 100,
            'kalman_state': {
                'lat': 35.0,
                'lon': -97.0,
                'u': 10.0,
                'v': 5.0,
            }
        }
    
    @pytest.fixture
    def sample_detection(self):
        """Create a sample detection dictionary."""
        return {
            'id': 101,
            'centroid': [35.05, -97.05],
            'max_refl': 50.0,
            'num_gates': 120,
        }
    
    @pytest.fixture
    def kalman_filter(self):
        """Create an initialized Kalman filter."""
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0)
        return kf
    
    def test_compute_cost_returns_float(self, calculator, sample_track, 
                                         sample_detection, kalman_filter):
        """Test that compute_cost returns a float."""
        cost = calculator.compute_cost(
            sample_track, sample_detection, kalman_filter
        )
        assert isinstance(cost, float)
    
    def test_compute_cost_lower_for_closer_detection(self, calculator, sample_track,
                                                      kalman_filter):
        """Test that closer detection has lower cost."""
        close_detection = {
            'id': 101,
            'centroid': [35.01, -97.01],
            'max_refl': 45.0,
            'num_gates': 100,
        }
        far_detection = {
            'id': 102,
            'centroid': [35.5, -97.5],
            'max_refl': 45.0,
            'num_gates': 100,
        }
        
        close_cost = calculator.compute_cost(
            sample_track, close_detection, kalman_filter
        )
        far_cost = calculator.compute_cost(
            sample_track, far_detection, kalman_filter
        )
        
        assert close_cost < far_cost
    
    def test_prefilter_candidates_returns_subset(self, calculator, sample_track):
        """Test that prefilter returns only nearby detections."""
        detections = [
            {'id': 1, 'centroid': [35.0, -97.0]},  # Close
            {'id': 2, 'centroid': [35.1, -97.0]},  # Close (~11 km)
            {'id': 3, 'centroid': [40.0, -90.0]},  # Far
        ]
        
        candidates = calculator.prefilter_candidates(sample_track, detections)
        
        assert len(candidates) == 2
        assert all(d['id'] in [1, 2] for d in candidates)
    
    def test_prefilter_candidates_empty_for_far_detections(self, calculator, sample_track):
        """Test that prefilter returns empty list for far detections."""
        detections = [
            {'id': 1, 'centroid': [50.0, -80.0]},  # Very far
            {'id': 2, 'centroid': [45.0, -85.0]},  # Far
        ]
        
        candidates = calculator.prefilter_candidates(sample_track, detections)
        
        assert len(candidates) == 0
    
    def test_is_within_gate_true_for_close(self, calculator, sample_track,
                                            kalman_filter):
        """Test that close detection passes gating."""
        detection = {'id': 1, 'centroid': [35.01, -97.01]}
        
        is_valid = calculator.is_within_gate(sample_track, detection, kalman_filter)
        
        assert is_valid is True
    
    def test_is_within_gate_false_for_far(self, calculator, sample_track,
                                           kalman_filter):
        """Test that far detection fails gating."""
        detection = {'id': 1, 'centroid': [40.0, -90.0]}
        
        is_valid = calculator.is_within_gate(sample_track, detection, kalman_filter)
        
        assert is_valid is False


class TestCostMatrixBuilder:
    """Tests for cost matrix building functions."""
    
    @pytest.fixture
    def tracks(self):
        """Create sample tracks."""
        return [
            {'id': 1, 'centroid': [35.0, -97.0], 'max_refl': 45.0, 'num_gates': 100},
            {'id': 2, 'centroid': [36.0, -98.0], 'max_refl': 50.0, 'num_gates': 150},
        ]
    
    @pytest.fixture
    def detections(self):
        """Create sample detections close to track positions."""
        return [
            {'id': 101, 'centroid': [35.005, -97.005], 'max_refl': 48.0, 'num_gates': 110},
            {'id': 102, 'centroid': [36.005, -98.005], 'max_refl': 52.0, 'num_gates': 160},
        ]
    
    @pytest.fixture
    def kalman_filters(self):
        """Create Kalman filters for tracks."""
        filters = {}
        
        kf1 = KalmanFilter()
        kf1.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0, position_std_km=5.0)
        filters[1] = kf1
        
        kf2 = KalmanFilter()
        kf2.initialize(lat=36.0, lon=-98.0, u=12.0, v=6.0, position_std_km=5.0)
        filters[2] = kf2
        
        return filters
    
    def test_build_cost_matrix_shape(self, tracks, detections, kalman_filters):
        """Test that cost matrix has correct shape."""
        cost_matrix, track_map, det_map = build_cost_matrix(
            tracks, detections, kalman_filters
        )
        
        assert cost_matrix.shape == (2, 2)
    
    def test_build_cost_matrix_finite_values(self, tracks, detections, kalman_filters):
        """Test that cost matrix has finite values for valid pairs."""
        cost_matrix, _, _ = build_cost_matrix(
            tracks, detections, kalman_filters
        )
        
        # At least some values should be finite
        assert np.any(np.isfinite(cost_matrix))
    
    def test_build_cost_matrix_id_maps(self, tracks, detections, kalman_filters):
        """Test that ID maps are correct."""
        cost_matrix, track_map, det_map = build_cost_matrix(
            tracks, detections, kalman_filters
        )
        
        assert len(track_map) == 2
        assert len(det_map) == 2
        assert track_map[0] == 1
        assert track_map[1] == 2


class TestSolveAssignment:
    """Tests for assignment solving functions."""
    
    def test_solve_assignment_simple(self):
        """Test simple assignment case."""
        # Simple 2x2 matrix where optimal is diagonal
        cost_matrix = np.array([
            [1.0, 10.0],
            [10.0, 1.0]
        ])
        
        pairs, unmatched_rows, unmatched_cols = solve_assignment(cost_matrix)
        
        assert len(pairs) == 2
        assert (0, 0) in pairs
        assert (1, 1) in pairs
    
    def test_solve_assignment_with_inf(self):
        """Test assignment with infinite costs."""
        cost_matrix = np.array([
            [1.0, np.inf],
            [np.inf, 1.0]
        ])
        
        pairs, unmatched_rows, unmatched_cols = solve_assignment(cost_matrix)
        
        assert len(pairs) == 2
    
    def test_solve_assignment_rectangular(self):
        """Test rectangular cost matrix."""
        cost_matrix = np.array([
            [1.0, 5.0, 10.0],
            [5.0, 1.0, 10.0]
        ])
        
        pairs, unmatched_rows, unmatched_cols = solve_assignment(cost_matrix)
        
        # Should match 2 pairs
        assert len(pairs) == 2
    
    def test_solve_assignment_empty(self):
        """Test empty cost matrix."""
        cost_matrix = np.array([])
        
        pairs, unmatched_rows, unmatched_cols = solve_assignment(cost_matrix)
        
        assert len(pairs) == 0


class TestHybridAssignment:
    """Tests for hybrid assignment algorithm."""
    
    @pytest.fixture
    def tracks(self):
        """Create sample tracks."""
        return [
            {'id': 1, 'centroid': [35.0, -97.0], 'max_refl': 45.0, 'num_gates': 100,
             'tracking_mode': 'active'},
            {'id': 2, 'centroid': [36.0, -98.0], 'max_refl': 50.0, 'num_gates': 150,
             'tracking_mode': 'active'},
        ]
    
    @pytest.fixture
    def detections(self):
        """Create sample detections close to track positions."""
        return [
            {'id': 101, 'centroid': [35.005, -97.005], 'max_refl': 48.0, 'num_gates': 110},
            {'id': 102, 'centroid': [36.005, -98.005], 'max_refl': 52.0, 'num_gates': 160},
        ]
    
    @pytest.fixture
    def kalman_filters(self):
        """Create Kalman filters for tracks."""
        filters = {}
        
        kf1 = KalmanFilter()
        kf1.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0, position_std_km=5.0)
        filters[1] = kf1
        
        kf2 = KalmanFilter()
        kf2.initialize(lat=36.0, lon=-98.0, u=12.0, v=6.0, position_std_km=5.0)
        filters[2] = kf2
        
        return filters
    
    def test_run_hybrid_assignment_returns_result(self, tracks, detections, kalman_filters):
        """Test that hybrid assignment returns AssignmentResult."""
        result = run_hybrid_assignment(tracks, detections, kalman_filters)
        
        assert isinstance(result, AssignmentResult)
    
    def test_run_hybrid_assignment_matches(self, tracks, detections, kalman_filters):
        """Test that hybrid assignment produces matches."""
        result = run_hybrid_assignment(tracks, detections, kalman_filters)
        
        # Should have some matches
        assert len(result.matched) > 0
    
    def test_run_hybrid_assignment_crossed_paths(self):
        """Test that hybrid handles crossed paths correctly."""
        # Two tracks moving towards each other
        tracks = [
            {'id': 1, 'centroid': [35.0, -97.0], 'max_refl': 45.0, 'num_gates': 100,
             'tracking_mode': 'active'},
            {'id': 2, 'centroid': [35.0, -96.0], 'max_refl': 45.0, 'num_gates': 100,
             'tracking_mode': 'active'},
        ]
        
        # Detections close to each track
        detections = [
            {'id': 101, 'centroid': [35.0, -96.995], 'max_refl': 45.0, 'num_gates': 100},
            {'id': 102, 'centroid': [35.0, -96.005], 'max_refl': 45.0, 'num_gates': 100},
        ]
        
        # Create Kalman filters
        filters = {}
        kf1 = KalmanFilter()
        kf1.initialize(lat=35.0, lon=-97.0, u=10.0, v=0.0, position_std_km=5.0)
        filters[1] = kf1
        
        kf2 = KalmanFilter()
        kf2.initialize(lat=35.0, lon=-96.0, u=-10.0, v=0.0, position_std_km=5.0)
        filters[2] = kf2
        
        result = run_hybrid_assignment(tracks, detections, filters)
        
        # Should still produce matches
        assert len(result.matched) == 2


class TestGreedyAssignment:
    """Tests for greedy assignment algorithm."""
    
    @pytest.fixture
    def tracks(self):
        """Create sample tracks."""
        return [
            {'id': 1, 'centroid': [35.0, -97.0], 'max_refl': 45.0, 'num_gates': 100,
             'tracking_mode': 'active'},
        ]
    
    @pytest.fixture
    def detections(self):
        """Create sample detections close to track position."""
        return [
            {'id': 101, 'centroid': [35.005, -97.005], 'max_refl': 48.0, 'num_gates': 110},
        ]
    
    @pytest.fixture
    def kalman_filters(self):
        """Create Kalman filter for track."""
        filters = {}
        kf = KalmanFilter()
        kf.initialize(lat=35.0, lon=-97.0, u=10.0, v=5.0, position_std_km=5.0)
        filters[1] = kf
        return filters
    
    def test_run_greedy_assignment_returns_result(self, tracks, detections, kalman_filters):
        """Test that greedy assignment returns AssignmentResult."""
        result = run_greedy_assignment(tracks, detections, kalman_filters)
        
        assert isinstance(result, AssignmentResult)
    
    def test_run_greedy_assignment_matches(self, tracks, detections, kalman_filters):
        """Test that greedy assignment produces matches."""
        result = run_greedy_assignment(tracks, detections, kalman_filters)
        
        assert len(result.matched) == 1
        assert result.matched[0] == (1, 101)


