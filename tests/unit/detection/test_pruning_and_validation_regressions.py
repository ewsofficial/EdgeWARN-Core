"""
Unit tests for low-priority audit fixes L1–L4.

L1: Buffer scan interval default (300 → 120)
L2: Hardcoded latitude in covariance initialization
L3: Greedy assignment cost-based sorting
L4: Collinear polygon guard in overlap calculation
"""

import pytest
from math import cos, radians

from EdgeWARN.process.detect.lineage.buffer import LineageBuffer, PendingMerge
from EdgeWARN.process.detect.lineage import buffer as buffer_module
from EdgeWARN.process.detect.kalman.state import CovarianceMatrix
from EdgeWARN.process.detect.kalman.assignment import (
    run_greedy_assignment,
    AssignmentCostCalculator,
)
from EdgeWARN.process.detect.kalman.filter import KalmanFilter
from EdgeWARN.process.detect.lineage.spatial import calculate_overlap_ratio


# ─── L1: Buffer Scan Interval ────────────────────────────────────────────────


class TestL1BufferScanInterval:
    """L1: scan_interval_seconds should default to 120s (MRMS cadence)."""

    def test_buffer_default_scan_interval(self):
        """Default scan_interval_seconds should be 120, not 300."""
        buf = LineageBuffer()
        assert buf.scan_interval_seconds == 120.0

    @pytest.mark.parametrize(
        ("age", "is_pruned"),
        [(599.999, False), (600.0, False), (600.001, True)],
        ids=["just-before-expiry", "exactly-at-expiry", "just-after-expiry"],
    )
    def test_pending_merge_pruning_boundary(self, monkeypatch, age, is_pruned):
        """Pending events expire only after the configured inactivity window."""
        now = 10_000.0
        monkeypatch.setattr(buffer_module.time, "time", lambda: now)
        buf = LineageBuffer(prune_after_scans=5, scan_interval_seconds=120)
        buf.pending_merges[7] = PendingMerge(
            child_id=7,
            parent_ids={1, 2},
            last_seen=now - age,
        )

        assert buf.prune_inactive() == int(is_pruned)
        assert (7 not in buf.pending_merges) is is_pruned


# ─── L2: Hardcoded Latitude ──────────────────────────────────────────────────


class TestL2HardcodedLatitude:
    """L2: from_position_uncertainty should accept ref_lat."""

    @staticmethod
    def _covariance(position_std_km, **kwargs):
        return CovarianceMatrix.from_position_uncertainty(
            position_std_km,
            velocity_variance=100.0,
            acceleration_variance=1.0,
            **kwargs,
        )

    def test_from_position_uncertainty_uses_ref_lat(self):
        """Longitude variance should differ between lat=25° and lat=45°."""
        cov_25 = self._covariance(5.0, ref_lat=25.0)
        cov_45 = self._covariance(5.0, ref_lat=45.0)

        _, var_lon_25 = cov_25.get_position_variance()
        _, var_lon_45 = cov_45.get_position_variance()

        # cos(25°) > cos(45°), so lon_std at 25° < lon_std at 45°
        # ⇒ var_lon at 25° < var_lon at 45°
        assert var_lon_25 < var_lon_45

    def test_from_position_uncertainty_backward_compat(self):
        """Calling without ref_lat should use 35° (backward compatible)."""
        cov_default = self._covariance(5.0)
        cov_35 = self._covariance(5.0, ref_lat=35.0)

        _, var_lon_default = cov_default.get_position_variance()
        _, var_lon_35 = cov_35.get_position_variance()

        assert var_lon_default == pytest.approx(var_lon_35)


# ─── L3: Greedy Assignment ───────────────────────────────────────────────────


class TestL3GreedyAssignment:
    """L3: Greedy should assign by lowest cost, not highest reflectivity."""

    def test_greedy_assigns_by_cost_not_reflectivity(self):
        """A close low-refl detection should beat a far high-refl detection."""
        # One track at (35.0, 262.0)
        tracks = [
            {'id': 1, 'centroid': [35.0, 262.0], 'max_refl': 55, 'num_gates': 100,
             'kalman_state': {'lat': 35.0, 'lon': 262.0}},
        ]

        # Two detections: one close + low refl, one far + high refl
        detections = [
            {'id': 10, 'centroid': [35.01, 262.01], 'max_refl': 40, 'num_gates': 80},  # close
            {'id': 20, 'centroid': [35.5, 262.5], 'max_refl': 70, 'num_gates': 200},    # far
        ]

        # KF at the track position
        kf = KalmanFilter()
        kf.initialize(35.0, 262.0)
        kalman_filters = {1: kf}

        result = run_greedy_assignment(tracks, detections, kalman_filters)

        # Track 1 should match with the close detection (10), not the high-refl one (20)
        matched_det_ids = {det_id for _, det_id in result.matched}
        assert 10 in matched_det_ids
        # Detection 20 should be unmatched (too far for gating) or at least not preferred
        assert (1, 10) in result.matched


# ─── L4: Collinear Polygon Guard ─────────────────────────────────────────────


class TestL4CollinearPolygon:
    """L4: Degenerate polygons should return 0.0 overlap."""

    def test_overlap_collinear_points(self):
        """Three collinear points as parent → 0.0 overlap."""
        collinear = [[35.0, 262.0], [35.1, 262.0], [35.2, 262.0]]
        normal = [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]]

        assert calculate_overlap_ratio(collinear, normal) == 0.0

    def test_overlap_degenerate_child_polygon(self):
        """Valid parent, collinear child → 0.0 overlap."""
        normal = [[35.0, 262.0], [35.0, 262.2], [35.2, 262.2], [35.2, 262.0]]
        collinear = [[35.0, 262.0], [35.1, 262.0], [35.2, 262.0]]

        assert calculate_overlap_ratio(normal, collinear) == 0.0
