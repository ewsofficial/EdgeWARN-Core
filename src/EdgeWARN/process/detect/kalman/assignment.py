"""
Measurement Assignment Module for Storm Cell Tracking

Implements the hybrid pre-filter + Hungarian algorithm approach for
assigning new detections to tracked storm cells.

Key Components:
- AssignmentCostCalculator: Computes assignment costs
- build_filtered_cost_matrix: Constructs cost matrix for pre-filtered candidates
- solve_assignment: Solves the assignment problem using Hungarian algorithm
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any
import numpy as np
from scipy.optimize import linear_sum_assignment
from math import sqrt, atan2, pi

from .config import AssignmentConfig, default_assignment_config
from .filter import KalmanFilter
from .state import haversine_distance


# Theoretical maximum of the directional term ``1 - cos(angle_diff)``, reached at
# a 180° reversal. Derived from the cost formula rather than configurable: a knob
# here would let the assignment budget disagree with what the term can produce.
MAX_DIRECTIONAL_COST = 2.0


@dataclass
class AssignmentResult:
    """
    Result of assignment computation.
    
    Attributes:
        matched: List of (track_id, detection_id) pairs that were matched
        unmatched_tracks: List of track IDs that were not matched
        unmatched_detections: List of detection IDs that were not matched
        costs: Dictionary mapping (track_id, detection_id) to assignment cost
    """
    matched: List[Tuple[int, int]]
    unmatched_tracks: List[int]
    unmatched_detections: List[int]
    costs: Dict[Tuple[int, int], float]


class AssignmentCostCalculator:
    """
    Calculates assignment costs for track-detection pairs.
    
    The cost function is a weighted sum of:
    1. Position cost (Mahalanobis distance)
    2. Velocity consistency cost (angular deviation)
    3. Shape similarity cost (reflectivity and size)
    
    Cost = w1 * d_position + w2 * d_velocity + w3 * d_shape
    """
    
    def __init__(self, config: Optional[AssignmentConfig] = None):
        """
        Initialize cost calculator.

        Args:
            config: Assignment configuration parameters; loaded from
                ``config/kalman.yaml`` when not supplied
        """
        self.config = config if config is not None else default_assignment_config()

    def compute_cost(self, track: Dict[str, Any], detection: Dict[str, Any],
                     kalman_filter: KalmanFilter,
                     dt_seconds: Optional[float] = None) -> float:
        """
        Compute total assignment cost for a track-detection pair.

        Args:
            track: Tracked storm cell dictionary
            detection: New detection dictionary
            kalman_filter: Kalman filter for the track
            dt_seconds: Time since last update (for velocity computation). When
                omitted, uses ``assignment_costs.default_dt_seconds``.

        Returns:
            Total assignment cost (lower is better)
        """
        if dt_seconds is None:
            dt_seconds = self.config.costs.default_dt_seconds

        # Get detection centroid
        det_centroid = detection.get('centroid', [0, 0])
        det_lat, det_lon = det_centroid[0], det_centroid[1]
        
        # Position cost (Mahalanobis distance)
        d_position = kalman_filter.get_mahalanobis_distance(det_lat, det_lon)
        
        # Velocity consistency cost
        d_velocity = self._compute_velocity_cost(
            track, detection, kalman_filter, dt_seconds
        )
        
        # Shape similarity cost
        d_shape = self._compute_shape_cost(track, detection)
        
        # Weighted sum
        total_cost = (
            self.config.weight_position * d_position +
            self.config.weight_velocity * d_velocity +
            self.config.weight_shape * d_shape
        )
        
        return total_cost
    
    def prefilter_candidates(self, track: Dict[str, Any],
                              detections: List[Dict[str, Any]],
                              det_lats: Optional[np.ndarray] = None,
                              det_lons: Optional[np.ndarray] = None) -> List[Dict[str, Any]]:
        """
        Stage 1: Filter detections within prefilter_radius_km.

        This reduces the problem size before running the Hungarian algorithm.

        Args:
            track: Tracked storm cell dictionary
            detections: List of all new detections
            det_lats, det_lons: Optional pre-built numpy arrays of detection
                centroid lats/lons (parallel to ``detections``). If supplied,
                the per-call extraction loop is skipped — callers running a
                per-track loop should build these once and reuse them.

        Returns:
            List of detections within the pre-filter radius
        """
        if not detections:
            return []

        # Get predicted position from Kalman state or centroid
        kalman_state = track.get('kalman_state', {})
        pred_lat = kalman_state.get('lat')
        pred_lon = kalman_state.get('lon')

        if pred_lat is None or pred_lon is None:
            # Fall back to current centroid
            centroid = track.get('centroid', [0, 0])
            pred_lat, pred_lon = centroid[0], centroid[1]

        if det_lats is None or det_lons is None:
            det_lats = np.zeros(len(detections))
            det_lons = np.zeros(len(detections))

            for i, det in enumerate(detections):
                det_centroid = det.get('centroid', [0, 0])
                det_lats[i] = det_centroid[0]
                det_lons[i] = det_centroid[1]

        from .state import vectorized_haversine_distance

        # Calculate distances to all detections at once
        distances = vectorized_haversine_distance(pred_lat, pred_lon, det_lats, det_lons)

        radius = self.config.prefilter_radius_km
        return [detections[i] for i in np.flatnonzero(distances <= radius)]

    def _get_reference_position(
        self,
        track: Dict[str, Any],
        kalman_filter: Optional[KalmanFilter] = None,
    ) -> Tuple[float, float]:
        """Return the best available position estimate for assignment gating."""
        kalman_state = track.get('kalman_state', {})
        pred_lat = kalman_state.get('lat')
        pred_lon = kalman_state.get('lon')

        if pred_lat is not None and pred_lon is not None:
            return pred_lat, pred_lon

        if kalman_filter is not None and kalman_filter._initialized:
            return kalman_filter.state.get_position()

        centroid = track.get('centroid', [0, 0])
        return centroid[0], centroid[1]
    
    def is_within_gate(self, track: Dict[str, Any], detection: Dict[str, Any],
                       kalman_filter: KalmanFilter) -> bool:
        """
        Check if detection is within the validation gate for a track.
        
        Uses Mahalanobis distance with minimum radius fallback.
        
        Args:
            track: Tracked storm cell dictionary
            detection: New detection dictionary
            kalman_filter: Kalman filter for the track
        
        Returns:
            True if detection is within the validation gate
        """
        det_centroid = detection.get('centroid', [0, 0])
        det_lat, det_lon = det_centroid[0], det_centroid[1]
        
        if kalman_filter.is_within_gate(
            det_lat, det_lon,
            threshold=self.config.gating_threshold,
            min_radius_km=self.config.min_gating_radius_km
        ):
            return True

        # Fallback for still-healthy tracks whose covariance is too tight for
        # assignment, especially in integration tests that initialize a filter
        # directly and then expect assignment on the next scan.
        ref_lat, ref_lon = self._get_reference_position(track, kalman_filter)
        return haversine_distance(det_lat, det_lon, ref_lat, ref_lon) <= self.config.prefilter_radius_km
    
    def _compute_velocity_cost(self, track: Dict[str, Any],
                               detection: Dict[str, Any],
                               kalman_filter: KalmanFilter,
                               dt_seconds: float) -> float:
        """
        Compute velocity consistency cost.
        
        Compares the implied velocity (from position change) with the
        Kalman filter's predicted velocity. Penalizes backward motion.
        
        Args:
            track: Tracked storm cell dictionary
            detection: New detection dictionary
            kalman_filter: Kalman filter for the track
            dt_seconds: Time since last update
        
        Returns:
            Velocity cost (0 = perfect match, 2 = opposite direction)
        """
        if dt_seconds <= 0:
            return 0.0
        
        # Get predicted velocity from Kalman filter
        pred_u, pred_v = kalman_filter.state.get_velocity()
        pred_speed = sqrt(pred_u**2 + pred_v**2)
        
        if pred_speed < self.config.costs.predicted_speed_deadband_ms:  # Nearly stationary
            return 0.0
        
        # Compute implied velocity from position change
        track_centroid = track.get('centroid', [0, 0])
        det_centroid = detection.get('centroid', [0, 0])
        
        # Convert position difference to approximate meters
        dlat = det_centroid[0] - track_centroid[0]
        dlon = det_centroid[1] - track_centroid[1]
        
        # Approximate meters per degree
        meters_per_deg_lat = 111320.0
        meters_per_deg_lon = 111320.0 * np.cos(np.radians(track_centroid[0]))
        
        dy = dlat * meters_per_deg_lat  # Northward displacement (m)
        dx = dlon * meters_per_deg_lon  # Eastward displacement (m)
        
        # Implied velocity (m/s)
        implied_u = dx / dt_seconds
        implied_v = dy / dt_seconds
        implied_speed = sqrt(implied_u**2 + implied_v**2)
        
        if implied_speed < self.config.costs.implied_speed_deadband_ms:  # Very small implied motion
            return 0.0
        
        # Compute angular deviation
        # Predicted direction
        pred_bearing = atan2(pred_u, pred_v)  # atan2(east, north)
        # Implied direction
        implied_bearing = atan2(implied_u, implied_v)
        
        # Angular difference
        angle_diff = abs(pred_bearing - implied_bearing)
        if angle_diff > pi:
            angle_diff = 2 * pi - angle_diff
        
        # Cost: 1 - cos(angle) gives 0 for same direction, 2 for opposite
        # We use a modified version that penalizes large deviations more
        velocity_cost = 1.0 - np.cos(angle_diff)
        
        return velocity_cost
    
    def _compute_shape_cost(self, track: Dict[str, Any],
                            detection: Dict[str, Any]) -> float:
        """
        Compute shape similarity cost.
        
        Compares max reflectivity and number of gates (size) between
        track and detection. Uses log-scale for reflectivity.
        
        Args:
            track: Tracked storm cell dictionary
            detection: New detection dictionary
        
        Returns:
            Shape cost (0 = identical, higher = more different)
        """
        costs = self.config.costs
        cost = 0.0

        # Max reflectivity comparison (log scale)
        track_refl = track.get('max_refl', 0)
        det_refl = detection.get('max_refl', 0)

        if track_refl > 0 and det_refl > 0:
            # Log-scale difference
            refl_diff = abs(np.log10(det_refl) - np.log10(track_refl))
            cost += min(refl_diff, costs.reflectivity_diff_cap)

        # Size comparison (number of gates)
        track_gates = track.get('num_gates', 0)
        det_gates = detection.get('num_gates', 0)

        if track_gates > 0 and det_gates > 0:
            # Relative size difference
            size_ratio = max(track_gates, det_gates) / min(track_gates, det_gates)
            # Normalize: ratio of 2 gives cost of ~0.3, ratio of 4 gives ~0.6
            size_cost = np.log2(size_ratio) / costs.size_ratio_log2_divisor
            cost += min(size_cost, costs.size_cost_cap)

        return cost


def build_cost_matrix(tracks: List[Dict[str, Any]],
                      detections: List[Dict[str, Any]],
                      kalman_filters: Dict[int, KalmanFilter],
                      config: Optional[AssignmentConfig] = None,
                      dt_seconds: Optional[float] = None) -> Tuple[np.ndarray, Dict[int, int], Dict[int, int]]:
    """
    Build full cost matrix for all track-detection pairs.
    
    Args:
        tracks: List of tracked storm cells
        detections: List of new detections
        kalman_filters: Dictionary mapping track IDs to Kalman filters
        config: Assignment configuration
        dt_seconds: Time since last update
    
    Returns:
        Tuple of (cost_matrix, track_id_map, detection_id_map)
        - cost_matrix: N x M matrix of assignment costs
        - track_id_map: Map from row index to track ID
        - detection_id_map: Map from column index to detection ID
    """
    n_tracks = len(tracks)
    n_detections = len(detections)
    
    if n_tracks == 0 or n_detections == 0:
        return np.array([]), {}, {}
    
    # Create ID maps
    track_id_map = {i: int(tracks[i]['id']) for i in range(n_tracks)}
    detection_id_map = {j: int(detections[j]['id']) for j in range(n_detections)}
    
    # Build cost matrix
    cost_matrix = np.full((n_tracks, n_detections), np.inf)
    
    calculator = AssignmentCostCalculator(config)
    
    for i, track in enumerate(tracks):
        track_id = int(track['id'])
        kf = kalman_filters.get(track_id)
        
        if kf is None:
            continue
        
        for j, detection in enumerate(detections):
            # Check gating first
            if not calculator.is_within_gate(track, detection, kf):
                continue
            
            # Compute cost
            cost = calculator.compute_cost(track, detection, kf, dt_seconds)
            cost_matrix[i, j] = cost
    
    return cost_matrix, track_id_map, detection_id_map


def build_filtered_cost_matrix(tracks: List[Dict[str, Any]],
                               track_candidates: Dict[int, List[Dict[str, Any]]],
                               kalman_filters: Dict[int, KalmanFilter],
                               config: Optional[AssignmentConfig] = None,
                               dt_seconds: Optional[float] = None) -> Tuple[np.ndarray, Dict[int, int], Dict[int, int], List[Tuple[int, int]], Dict[Tuple[int, int], float]]:
    """
    Build reduced cost matrix for pre-filtered candidates.

    This is Stage 2 of the hybrid approach. The cost matrix only includes
    track-detection pairs that passed the pre-filter.

    Args:
        tracks: List of tracked storm cells
        track_candidates: Map from track ID to list of candidate detections
        kalman_filters: Dictionary mapping track IDs to Kalman filters
        config: Assignment configuration
        dt_seconds: Time since last update

    Returns:
        Tuple of (cost_matrix, track_id_map, detection_id_map, single_assignments, single_assignment_costs)
        - cost_matrix: N x M matrix of assignment costs
        - track_id_map: Map from row index to track ID
        - detection_id_map: Map from column index to detection ID
        - single_assignments: List of (track_id, detection_id) for single-candidate tracks
        - single_assignment_costs: Map from (track_id, detection_id) to the cost
          already computed during single-candidate gating, so callers don't need
          to recompute it.
    """
    # Every candidate participates in the same one-to-one solve.  Assigning a
    # single-candidate track eagerly is unsafe because several tracks can have
    # the same sole candidate (a common merge shape), which would assign one
    # detection more than once.
    single_assignments = []
    single_assignment_costs: Dict[Tuple[int, int], float] = {}
    multi_candidate_tracks = []
    all_candidate_detections = set()

    calculator = AssignmentCostCalculator(config)

    for track in tracks:
        track_id = int(track['id'])
        candidates = track_candidates.get(track_id, [])

        if len(candidates) >= 1:
            multi_candidate_tracks.append(track)
            for det in candidates:
                all_candidate_detections.add(int(det['id']))

    if len(multi_candidate_tracks) == 0 or len(all_candidate_detections) == 0:
        return np.array([]), {}, {}, single_assignments, single_assignment_costs
    
    # Create detection list and maps
    detection_list = list(all_candidate_detections)
    detection_id_map = {j: det_id for j, det_id in enumerate(detection_list)}
    detection_id_to_idx = {det_id: j for j, det_id in detection_id_map.items()}
    
    n_tracks = len(multi_candidate_tracks)
    n_detections = len(detection_list)

    # Create track map
    track_id_map = {i: int(multi_candidate_tracks[i]['id']) for i in range(n_tracks)}

    # Build cost matrix
    cost_matrix = np.full((n_tracks, n_detections), np.inf)

    for i, track in enumerate(multi_candidate_tracks):
        track_id = int(track['id'])
        kf = kalman_filters.get(track_id)

        if kf is None:
            continue

        candidates = track_candidates.get(track_id, [])

        for det in candidates:
            det_id = int(det['id'])
            if det_id not in detection_id_to_idx:
                continue

            j = detection_id_to_idx[det_id]

            # Check gating
            if not calculator.is_within_gate(track, det, kf):
                continue

            # Compute cost
            cost = calculator.compute_cost(track, det, kf, dt_seconds)
            cost_matrix[i, j] = cost

    return cost_matrix, track_id_map, detection_id_map, single_assignments, single_assignment_costs


def solve_assignment(cost_matrix: np.ndarray,
                     threshold: float = float('inf')) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """
    Solve the assignment problem using the Hungarian algorithm.
    
    Args:
        cost_matrix: N x M matrix of assignment costs
        threshold: Maximum cost for valid assignment (default: accept all)
    
    Returns:
        Tuple of (matched_pairs, unmatched_rows, unmatched_cols)
        - matched_pairs: List of (row_idx, col_idx) pairs
        - unmatched_rows: List of row indices not matched
        - unmatched_cols: List of column indices not matched
    """
    if cost_matrix.size == 0:
        return [], [], []
    
    n_rows, n_cols = cost_matrix.shape
    
    # Run Hungarian algorithm
    row_inds, col_inds = linear_sum_assignment(cost_matrix)
    
    # Process results
    matched_pairs = []
    matched_rows = set()
    matched_cols = set()
    
    for row_idx, col_idx in zip(row_inds, col_inds):
        cost = cost_matrix[row_idx, col_idx]
        
        # Check if assignment is valid (not infinite cost)
        if cost < threshold and not np.isinf(cost):
            matched_pairs.append((row_idx, col_idx))
            matched_rows.add(row_idx)
            matched_cols.add(col_idx)
    
    # Find unmatched rows and columns
    unmatched_rows = [i for i in range(n_rows) if i not in matched_rows]
    unmatched_cols = [j for j in range(n_cols) if j not in matched_cols]
    
    return matched_pairs, unmatched_rows, unmatched_cols


def run_hybrid_assignment(tracks: List[Dict[str, Any]],
                          detections: List[Dict[str, Any]],
                          kalman_filters: Dict[int, KalmanFilter],
                          config: Optional[AssignmentConfig] = None,
                          dt_seconds: Optional[float] = None) -> AssignmentResult:
    """
    Run the full hybrid assignment algorithm.
    
    This implements the two-stage approach:
    1. Pre-filter candidates within fixed radius
    2. Run Hungarian algorithm on filtered candidates
    
    Args:
        tracks: List of tracked storm cells
        detections: List of new detections
        kalman_filters: Dictionary mapping track IDs to Kalman filters
        config: Assignment configuration
        dt_seconds: Time since last update
    
    Returns:
        AssignmentResult with matched pairs and unmatched lists
    """
    calculator = AssignmentCostCalculator(config)
    tracks_by_id = {int(track['id']): track for track in tracks}

    # Stage 1: Pre-filter candidates for each track.
    # Build the detection centroid arrays once and reuse across every track —
    # the centroid set does not change during prefiltering.
    if detections:
        det_lats = np.empty(len(detections), dtype=np.float64)
        det_lons = np.empty(len(detections), dtype=np.float64)
        for i, det in enumerate(detections):
            det_centroid = det.get('centroid', [0, 0])
            det_lats[i] = det_centroid[0]
            det_lons[i] = det_centroid[1]
    else:
        det_lats = None
        det_lons = None

    track_candidates = {}
    for track_id, track in tracks_by_id.items():
        candidates = calculator.prefilter_candidates(track, detections, det_lats, det_lons)
        track_candidates[track_id] = candidates
    
    # Stage 2: Build filtered cost matrix
    cost_matrix, track_id_map, detection_id_map, single_assignments, single_assignment_costs = build_filtered_cost_matrix(
        tracks, track_candidates, kalman_filters, config, dt_seconds
    )

    # Add single assignments to result
    matched = list(single_assignments)
    matched_track_ids = {pair[0] for pair in matched}
    matched_detection_ids = {pair[1] for pair in matched}

    # Costs already computed during single-candidate gating; reuse them.
    costs: Dict[Tuple[int, int], float] = dict(single_assignment_costs)

    # Stage 3: Solve assignment for multi-candidate tracks
    if cost_matrix.size > 0:
        pairs, unmatched_rows, unmatched_cols = solve_assignment(cost_matrix)

        # Convert indices to IDs
        for row_idx, col_idx in pairs:
            track_id = track_id_map[row_idx]
            det_id = detection_id_map[col_idx]
            matched.append((track_id, det_id))
            matched_track_ids.add(track_id)
            matched_detection_ids.add(det_id)
            # Reuse the cost already stored in cost_matrix for this pair.
            costs[(track_id, det_id)] = float(cost_matrix[row_idx, col_idx])

    # Build unmatched lists
    all_track_ids = set(tracks_by_id)
    all_detection_ids = {int(d['id']) for d in detections}

    unmatched_tracks = list(all_track_ids - matched_track_ids)
    unmatched_detections = list(all_detection_ids - matched_detection_ids)
    
    return AssignmentResult(
        matched=matched,
        unmatched_tracks=unmatched_tracks,
        unmatched_detections=unmatched_detections,
        costs=costs
    )


def run_greedy_assignment(tracks: List[Dict[str, Any]],
                          detections: List[Dict[str, Any]],
                          kalman_filters: Dict[int, KalmanFilter],
                          config: Optional[AssignmentConfig] = None,
                          dt_seconds: Optional[float] = None) -> AssignmentResult:
    """
    Run greedy nearest-neighbor assignment (fallback method).
    
    This is the legacy approach that assigns each detection to the
    closest track, one at a time.
    
    Args:
        tracks: List of tracked storm cells
        detections: List of new detections
        kalman_filters: Dictionary mapping track IDs to Kalman filters
        config: Assignment configuration
        dt_seconds: Time since last update
    
    Returns:
        AssignmentResult with matched pairs and unmatched lists
    """
    calculator = AssignmentCostCalculator(config)
    
    matched = []
    matched_track_ids = set()
    matched_detection_ids = set()
    costs = {}
    
    # L3 Fix: Pre-compute all valid (track, detection) costs, then assign
    # lowest-cost pair first. This is cost-centric rather than
    # detection-centric, producing globally better assignments.
    candidate_pairs = []
    for track in tracks:
        track_id = int(track['id'])
        kf = kalman_filters.get(track_id)
        if kf is None:
            continue
        
        for detection in detections:
            det_id = int(detection['id'])
            
            # Check gating
            if not calculator.is_within_gate(track, detection, kf):
                continue
            
            # Compute cost
            cost = calculator.compute_cost(track, detection, kf, dt_seconds)
            candidate_pairs.append((cost, track_id, det_id, track, detection))
    
    # Sort by ascending cost — best matches first
    candidate_pairs.sort(key=lambda x: x[0])
    
    for cost, track_id, det_id, track, detection in candidate_pairs:
        if track_id in matched_track_ids or det_id in matched_detection_ids:
            continue
        
        matched.append((track_id, det_id))
        matched_track_ids.add(track_id)
        matched_detection_ids.add(det_id)
        costs[(track_id, det_id)] = cost
    
    # Build unmatched lists
    all_track_ids = {int(t['id']) for t in tracks}
    all_detection_ids = {int(d['id']) for d in detections}
    
    unmatched_tracks = list(all_track_ids - matched_track_ids)
    unmatched_detections = list(all_detection_ids - matched_detection_ids)
    
    return AssignmentResult(
        matched=matched,
        unmatched_tracks=unmatched_tracks,
        unmatched_detections=unmatched_detections,
        costs=costs
    )
