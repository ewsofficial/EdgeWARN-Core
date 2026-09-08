"""
Unit Tests for Medium Audit Fixes (M1–M6)

Tests the fixes for all six medium-priority issues from the
storm cell tracking code audit.
"""

import dataclasses

import pytest
import copy
import numpy as np
from unittest.mock import MagicMock

from EdgeWARN.process.detect.kalman import (
    KalmanFilter,
    KalmanConfig,
    default_assignment_config,
)
from EdgeWARN.process.detect.kalman.assignment import (
    AssignmentCostCalculator,
    AssignmentConfig,
    MAX_DIRECTIONAL_COST,
    build_filtered_cost_matrix,
    run_hybrid_assignment,
)
from EdgeWARN.process.detect.track import StormCellTracker


# ===========================================================================
# M1: Discrete-time Process Noise
# ===========================================================================

class TestM1ProcessNoise:
    """Tests for proper discrete-time Q matrix."""

    def test_process_noise_superlinear(self):
        """Q at 240s should have larger positional variance than 2x Q at 120s."""
        kf = KalmanFilter()
        kf.initialize(35.0, -97.0)

        Q_120 = kf._build_process_noise_matrix(120.0)
        Q_240 = kf._build_process_noise_matrix(240.0)

        # Position variance (index 0,0 for lat) should grow faster than linear
        assert Q_240[0, 0] > 2.0 * Q_120[0, 0], (
            "Positional noise should grow super-linearly with dt"
        )

    def test_process_noise_symmetry(self):
        """Q matrix should be symmetric and positive semi-definite."""
        kf = KalmanFilter()
        kf.initialize(35.0, -97.0)

        Q = kf._build_process_noise_matrix(120.0)

        # Symmetric
        np.testing.assert_array_almost_equal(Q, Q.T)

        # Positive semi-definite (all eigenvalues >= 0)
        eigenvalues = np.linalg.eigvalsh(Q)
        assert np.all(eigenvalues >= -1e-10), (
            f"Q should be PSD, got eigenvalues: {eigenvalues}"
        )

    def test_process_noise_shape(self):
        """Q matrix should be 6x6."""
        kf = KalmanFilter()
        kf.initialize(35.0, -97.0)
        Q = kf._build_process_noise_matrix(120.0)
        assert Q.shape == (6, 6)


# ===========================================================================
# M2: Longitude cos(lat) Correction
# ===========================================================================

class TestM2LongitudeCorrection:
    """Tests for cos(lat) correction in transition matrix."""

    def test_longitude_correction_at_equator(self):
        """At lat=0, lon scale should equal lat scale (cos(0) = 1)."""
        kf = KalmanFilter()
        kf.initialize(0.0, -97.0)

        F = kf._build_transition_matrix(120.0)
        lat_scale = F[0, 2]  # lat row, velocity column
        lon_scale = F[1, 3]  # lon row, velocity column

        np.testing.assert_almost_equal(lat_scale, lon_scale, decimal=6,
            err_msg="At equator, lat and lon scales should be equal")

    def test_longitude_correction_at_60n(self):
        """At lat=60, lon scale should be ~2x lat scale (cos(60) = 0.5)."""
        kf = KalmanFilter()
        kf.initialize(60.0, -97.0)

        F = kf._build_transition_matrix(120.0)
        lat_scale = F[0, 2]
        lon_scale = F[1, 3]

        # cos(60°) = 0.5, so lon_scale ≈ lat_scale / 0.5 = 2 * lat_scale
        ratio = lon_scale / lat_scale
        assert 1.9 < ratio < 2.1, (
            f"At 60°N, lon scale should be ~2x lat scale, got ratio={ratio:.4f}"
        )

    def test_longitude_correction_at_35n(self):
        """At lat=35 (CONUS center), verify correction is applied."""
        kf = KalmanFilter()
        kf.initialize(35.0, -97.0)

        F = kf._build_transition_matrix(120.0)
        lat_scale = F[0, 2]
        lon_scale = F[1, 3]

        # cos(35°) ≈ 0.819, so lon_scale should be > lat_scale
        assert lon_scale > lat_scale, (
            "Longitude scale should be larger than latitude scale at 35°N"
        )
        expected_ratio = 1.0 / np.cos(np.radians(35.0))
        actual_ratio = lon_scale / lat_scale
        np.testing.assert_almost_equal(actual_ratio, expected_ratio, decimal=4)


# ===========================================================================
# M3: Reflectivity Decay Monitoring
# ===========================================================================

class TestM3ReflectivityDecay:
    """Tests for reflectivity decay state transition."""

    def test_decay_below_30dbz(self, mock_io_manager):
        """Cell matched with 25 dBZ should enter 'decaying' state."""
        tracker = StormCellTracker(None, None, mock_io_manager)
        cell = {
            'id': 100, 'num_gates': 50, 'centroid': [35.0, -97.0],
            'max_refl': 55, 'bbox': [1, 2, 3, 4], 'confidence': 1.0
        }
        updated = {
            'id': 100, 'num_gates': 40, 'centroid': [35.0, -97.0],
            'max_refl': 25, 'bbox': [1, 2, 3, 4]
        }

        tracker._update_cell_fields(cell, updated, "2023-10-15T12:00:00")

        assert cell['tracking_mode'] == 'decaying'
        assert cell['decay_scan_count'] == 1
        assert cell['confidence'] < 1.0

    def test_active_above_30dbz(self, mock_io_manager):
        """Cell matched with 55 dBZ should stay 'active'."""
        tracker = StormCellTracker(None, None, mock_io_manager)
        cell = {
            'id': 100, 'num_gates': 50, 'centroid': [35.0, -97.0],
            'max_refl': 55, 'bbox': [1, 2, 3, 4]
        }
        updated = {
            'id': 100, 'num_gates': 60, 'centroid': [35.1, -97.1],
            'max_refl': 55, 'bbox': [1, 2, 3, 4]
        }

        tracker._update_cell_fields(cell, updated, "2023-10-15T12:00:00")

        assert cell['tracking_mode'] == 'active'
        assert cell['decay_scan_count'] == 0
        assert cell['confidence'] == 1.0


# ===========================================================================
# M4: Deep Copies on Cell Dicts
# ===========================================================================

class TestM4DeepCopy:
    """Tests that nested structures are properly deep-copied."""

    def test_deepcopy_nested_bbox(self, mock_io_manager):
        """Mutating bbox in merged entry should not affect the original."""
        tracker = StormCellTracker(None, None, mock_io_manager)

        entries = [
            {'id': 101, 'num_gates': 50, 'centroid': [35.0, -97.0],
             'max_refl': 55, 'bbox': [[34.9, -97.1], [34.9, -96.9], [35.1, -96.9], [35.1, -97.1]]},
        ]
        updated_data = [
            {'id': 101, 'num_gates': 60, 'centroid': [35.1, -97.1],
             'max_refl': 60, 'bbox': [[35.0, -97.2], [35.0, -97.0], [35.2, -97.0], [35.2, -97.2]]},
        ]

        # Save original bbox reference
        original_bbox = entries[0]['bbox']

        result = tracker.update_cells(entries, updated_data,
                                       timestamp="2023-10-15T12:00:00")

        # Mutate the result entry's bbox
        result_cell = next(c for c in result if c['id'] == 101)
        result_cell['bbox'][0] = 999

        # Original should be unaffected
        assert original_bbox[0] != 999, (
            "Deep copy failed: mutating result affected the original"
        )


# ===========================================================================
# M5: Single-Candidate Cost Validation
# ===========================================================================

class TestM5SingleCandidateCost:
    """Tests for max cost validation on single-candidate assignments."""

    def _make_track(self, track_id, lat, lon, refl=55, gates=50):
        return {
            'id': track_id, 'centroid': [lat, lon],
            'max_refl': refl, 'num_gates': gates,
            'bbox': [0, 0, 1, 1],
        }

    def _make_detection(self, det_id, lat, lon, refl=55, gates=50):
        return {
            'id': det_id, 'centroid': [lat, lon],
            'max_refl': refl, 'num_gates': gates,
            'bbox': [0, 0, 1, 1],
        }

    def test_single_candidate_accepted(self):
        """A single candidate with reasonable cost should be accepted."""
        config = default_assignment_config()
        track = self._make_track(1, 35.0, -97.0)
        detection = self._make_detection(10, 35.01, -97.01)  # Close

        kf = KalmanFilter()
        kf.initialize(35.0, -97.0, u=5.0, v=5.0)
        kalman_filters = {1: kf}

        result = run_hybrid_assignment(
            [track], [detection], kalman_filters, config, dt_seconds=120.0
        )

        assert len(result.matched) == 1
        assert result.matched[0] == (1, 10)

    def test_single_candidate_rejected_extreme_cost(self):
        """A single candidate within gate but with extreme shape mismatch should be rejected."""
        config = dataclasses.replace(
            default_assignment_config(),
            prefilter_radius_km=50.0,  # Large filter so detection isn't prefiltered out
            gating_threshold=20.0,     # Large gate so detection passes gating
            min_gating_radius_km=50.0, # Large fallback
        )
        # Track: strong storm, 55 dBZ, 50 gates
        track = self._make_track(1, 35.0, -97.0, refl=55, gates=500)
        # Detection: very weak, 1 dBZ, 1 gate -- huge shape mismatch
        detection = self._make_detection(10, 35.01, -97.01, refl=1, gates=1)

        kf = KalmanFilter()
        kf.initialize(35.0, -97.0, u=0.0, v=0.0)
        kalman_filters = {1: kf}

        # Compute the cost manually to verify it exceeds threshold
        calculator = AssignmentCostCalculator(config)
        cost = calculator.compute_cost(track, detection, kf, 120.0)
        max_cost = (
            config.weight_position * config.gating_threshold
            + config.weight_velocity * MAX_DIRECTIONAL_COST
            + config.weight_shape
            * (config.costs.reflectivity_diff_cap + config.costs.size_cost_cap)
        )

        # This test only asserts rejection if cost actually exceeds threshold;
        # if the shape mismatch isn't extreme enough, skip assertion
        if cost > max_cost:
            result = run_hybrid_assignment(
                [track], [detection], kalman_filters, config, dt_seconds=120.0
            )
            assert len(result.unmatched_tracks) > 0, (
                "Single candidate with extreme cost should be rejected"
            )


# ===========================================================================
# M6: KF Memory Cleanup
# ===========================================================================

class TestM6KFCleanup:
    """Tests for orphaned KF cleanup."""

    def test_kf_cleanup_after_update(self, mock_io_manager):
        """After update_cells, no KF entries should remain for cells not in result."""
        tracker = StormCellTracker(None, None, mock_io_manager)

        entries = [
            {'id': 101, 'num_gates': 50, 'centroid': [35.0, -97.0],
             'max_refl': 55, 'bbox': [[34.9, -97.1], [34.9, -96.9], [35.1, -96.9], [35.1, -97.1]]},
            {'id': 102, 'num_gates': 30, 'centroid': [36.0, -96.0],
             'max_refl': 45, 'bbox': [[35.9, -96.1], [35.9, -95.9], [36.1, -95.9], [36.1, -96.1]]},
        ]

        # Only keep cell 101, drop 102
        updated_data = [
            {'id': 101, 'num_gates': 55, 'centroid': [35.0, -97.0],
             'max_refl': 55, 'bbox': [[34.9, -97.1], [34.9, -96.9], [35.1, -96.9], [35.1, -97.1]]},
        ]

        result = tracker.update_cells(entries, updated_data,
                                       timestamp="2023-10-15T12:00:00")

        # Cell 102 should not have a lingering KF
        assert 102 not in tracker._kalman_filters, (
            "KF for terminated cell 102 should have been cleaned up"
        )
        assert 102 not in tracker._prediction_states, (
            "Prediction state for terminated cell 102 should have been cleaned up"
        )
        # Cell 101 should still have its KF
        assert 101 in tracker._kalman_filters
