"""
Kalman Filter Implementation

Core Kalman filter for storm cell tracking with 6-dimensional state.
"""

from dataclasses import dataclass, field
from typing import Tuple, Optional, Dict, Any
import numpy as np
from datetime import datetime

from .state import StateVector, CovarianceMatrix, latlon_to_meters, meters_to_latlon, haversine_distance
from .config import KalmanConfig, default_kalman_config


@dataclass
class KalmanObservation:
    """Observation data for Kalman filter update."""
    
    lat: float
    lon: float
    timestamp: Optional[datetime] = None
    
    # Optional velocity observation (from StormProb or historical motion)
    u: Optional[float] = None
    v: Optional[float] = None


@dataclass
class KalmanFilter:
    """
    6-dimensional Kalman Filter for storm cell tracking.
    
    State vector: [lat, lon, u, v, a_lat, a_lon]^T
    - lat, lon: Position in degrees
    - u, v: Velocity in m/s
    - a_lat, a_lon: Acceleration in m/s²
    
    The filter uses a constant acceleration model for state transition.
    """
    
    # Configuration
    config: KalmanConfig = field(default_factory=default_kalman_config)
    
    # State
    state: StateVector = field(default_factory=StateVector)
    covariance: CovarianceMatrix = field(default_factory=CovarianceMatrix)
    
    # Reference point for coordinate conversion
    ref_lat: float = 35.0
    ref_lon: float = -97.0
    
    # Internal state (in meters for numerical stability)
    _x: float = 0.0  # Eastward position in meters
    _y: float = 0.0  # Northward position in meters
    _initialized: bool = False
    _last_timestamp: Optional[datetime] = None
    
    # Process noise matrix (Q)
    # No _Q field: predict() builds Q per step from dt via
    # _build_process_noise_matrix. A stored constant Q was assigned here and read
    # by nothing, which made two now-deleted config keys look load-bearing.

    # Measurement matrix (H) - observes position only
    _H: np.ndarray = field(default_factory=lambda: np.array([
        [1, 0, 0, 0, 0, 0],
        [0, 1, 0, 0, 0, 0]
    ], dtype=np.float64))
    
    # Measurement noise matrix (R)
    _R: Optional[np.ndarray] = None
    
    def __post_init__(self):
        """Initialize noise matrices."""
        self._initialize_noise_matrices()
    
    def _initialize_noise_matrices(self):
        """Initialize the measurement noise matrix.

        Process noise is not precomputed: it depends on dt, so predict() calls
        _build_process_noise_matrix per step.
        """
        cfg = self.config

        # Measurement noise matrix R (2x2 for position-only observations)
        # Convert km to degrees (approximate)
        pos_noise_deg = cfg.measurement_noise_position / 111.0
        self._R = np.diag([
            pos_noise_deg**2,
            pos_noise_deg**2
        ]).astype(np.float64)
    
    def initialize(self, lat: float, lon: float,
                   u: float = 0.0, v: float = 0.0,
                   position_std_km: Optional[float] = None,
                   timestamp: Optional[datetime] = None) -> None:
        """
        Initialize the Kalman filter with an initial state.

        Args:
            lat: Initial latitude in degrees
            lon: Initial longitude in degrees
            u: Initial eastward velocity in m/s (default 0)
            v: Initial northward velocity in m/s (default 0)
            position_std_km: Initial position uncertainty in km; falls back to
                the configured ``filter_internals`` value when not supplied
            timestamp: Initial timestamp
        """
        internals = self.config.internals
        if position_std_km is None:
            position_std_km = internals.initial_position_uncertainty_km

        self.state = StateVector(lat=lat, lon=lon, u=u, v=v)
        self.covariance = CovarianceMatrix.from_position_uncertainty(
            position_std_km,
            velocity_variance=internals.initial_velocity_variance,
            acceleration_variance=internals.initial_acceleration_variance,
        )
        
        # Set reference point for coordinate conversion
        self.ref_lat = lat
        self.ref_lon = lon if lon <= 180 else lon - 360
        
        # Initialize internal meter coordinates
        self._x = 0.0
        self._y = 0.0
        
        self._initialized = True
        self._last_timestamp = timestamp
    
    def initialize_from_cell(self, cell: Dict[str, Any], 
                             timestamp: Optional[datetime] = None) -> None:
        """
        Initialize the Kalman filter from a storm cell entry.
        
        Args:
            cell: Storm cell dictionary with 'centroid' and optionally 'dx', 'dy', 'dt'
            timestamp: Initial timestamp
        """
        centroid = cell.get('centroid', [0, 0])
        lat, lon = centroid[0], centroid[1]
        
        # Try to get velocity from historical motion
        u, v = 0.0, 0.0
        dx = cell.get('dx')
        dy = cell.get('dy')
        dt = cell.get('dt')
        
        if dx is not None and dy is not None and dt is not None and dt > 0:
            u = dx / dt  # m/s
            v = dy / dt  # m/s
        
        # Use the previous-cycle StormProb 15-minute forecast when available.
        from EdgeWARN.stormprob.deployment import promoted, rollback
        modules = cell.get('modules', {})
        if rollback():
            legacy = modules.get('StormCast', {})
            if legacy.get('status') == 'success' and legacy.get('u') is not None and legacy.get('v') is not None:
                u, v = float(legacy['u']), float(legacy['v'])
        stormprob = modules.get('StormProb', {}) if promoted() else {}
        if stormprob.get('status') == 'success':
            lead = next((item for item in stormprob.get('leads', [])
                         if item.get('lead_minutes') == 15), None)
            if lead and lead.get('east_km') is not None and lead.get('north_km') is not None:
                u, v = float(lead['east_km']) * 1000 / 900, float(lead['north_km']) * 1000 / 900
        
        self.initialize(lat, lon, u, v, timestamp=timestamp)
    
    def predict(self, dt: float, 
                control_u: Optional[float] = None,
                control_v: Optional[float] = None) -> StateVector:
        """
        Perform prediction step of Kalman filter.
        
        Uses constant acceleration model to predict state forward.
        
        Args:
            dt: Time step in seconds
                control_u: Optional control input for velocity (from StormProb)
                control_v: Optional control input for velocity (from StormProb)
        
        Returns:
            Predicted state vector
        """
        if not self._initialized:
            return self.state
        
        # Build state transition matrix F
        F = self._build_transition_matrix(dt)
        
        # Get current state as array
        x = self.state.to_array()
        P = self.covariance.to_array()
        
        # Apply control input if provided (StormProb velocity)
        if control_u is not None and control_v is not None:
            # Update velocity in state to match StormCast prediction
            x[2] = control_u
            x[3] = control_v
        
        # Predict state: x' = F * x
        x_pred = F @ x
        
        # Predict covariance: P' = F * P * F^T + Q
        # Use proper discrete-time process noise matrix
        Q_scaled = self._build_process_noise_matrix(dt)
        P_pred = F @ P @ F.T + Q_scaled
        
        # Update state and covariance
        self.state = StateVector.from_array(x_pred)
        self.covariance = CovarianceMatrix(_matrix=P_pred)
        
        return self.state
    
    def update(self, observation: KalmanObservation) -> StateVector:
        """
        Perform update step of Kalman filter with observation.
        
        Args:
            observation: Observation with lat/lon position
        
        Returns:
            Updated state vector
        """
        if not self._initialized:
            # Initialize with observation if not yet initialized
            self.initialize(observation.lat, observation.lon)
            return self.state
        
        # Get current state and covariance
        x = self.state.to_array()
        P = self.covariance.to_array()
        
        # Observation vector z (position only)
        z = np.array([observation.lat, observation.lon], dtype=np.float64)
        
        # Innovation (measurement residual): y = z - H * x
        y = z - self._H @ x
        
        # Innovation covariance: S = H * P * H^T + R
        S = self._H @ P @ self._H.T + self._R
        
        # Kalman gain: K = P * H^T * S^-1
        K = P @ self._H.T @ np.linalg.inv(S)
        
        # Updated state: x' = x + K * y
        x_updated = x + K @ y
        
        # Updated covariance: P' = (I - K * H) * P
        I = np.eye(6, dtype=np.float64)
        P_updated = (I - K @ self._H) @ P
        
        # Update state and covariance
        self.state = StateVector.from_array(x_updated)
        self.covariance = CovarianceMatrix(_matrix=P_updated)
        
        # Update timestamp
        if observation.timestamp:
            self._last_timestamp = observation.timestamp
        
        return self.state
    
    def _build_transition_matrix(self, dt: float) -> np.ndarray:
        """
        Build state transition matrix for constant acceleration model.
        
        The state transition follows:
        - Position: p' = p + v*dt + 0.5*a*dt²
        - Velocity: v' = v + a*dt
        - Acceleration: a' = a (constant)
        
        Longitude conversion includes cos(lat) correction to account for
        the convergence of meridians at higher latitudes.
        
        Args:
            dt: Time step in seconds
        
        Returns:
            6x6 state transition matrix
        """
        # Approximate conversion: 1 degree lat ~ 111 km ~ 111000 m
        lat_scale = dt / 111000
        lat_scale2 = 0.5 * dt**2 / 111000
        
        # M2 Fix: Apply cos(lat) correction for longitude
        cos_lat = np.cos(np.radians(self.state.lat))
        if cos_lat > 1e-6:
            lon_scale = dt / (111000 * cos_lat)
            lon_scale2 = 0.5 * dt**2 / (111000 * cos_lat)
        else:
            lon_scale = lat_scale
            lon_scale2 = lat_scale2
        
        F = np.array([
            [1, 0, lat_scale, 0, lat_scale2, 0],                  # lat' = lat + v_lat*dt + 0.5*a_lat*dt²
            [0, 1, 0, lon_scale, 0, lon_scale2],                   # lon' = lon + v_lon*dt + 0.5*a_lon*dt²  (cos(lat) corrected)
            [0, 0, 1, 0, dt, 0],                                   # u' = u + a_lat*dt
            [0, 0, 0, 1, 0, dt],                                   # v' = v + a_lon*dt
            [0, 0, 0, 0, 1, 0],                                    # a_lat' = a_lat
            [0, 0, 0, 0, 0, 1]                                     # a_lon' = a_lon
        ], dtype=np.float64)
        
        return F
    
    def _build_process_noise_matrix(self, dt: float) -> np.ndarray:
        """
        Build discrete-time process noise Q for constant-acceleration model.
        
        Uses the piecewise-constant white-noise jerk model.  The coupling
        between position, velocity, and acceleration via powers of dt
        prevents under-estimation of positional noise for large dt values.
        
        Args:
            dt: Time step in seconds
        
        Returns:
            6x6 process noise matrix
        """
        dt2 = dt * dt
        dt3 = dt2 * dt
        dt4 = dt3 * dt
        dt5 = dt4 * dt
        
        q_a = self.config.process_noise_acceleration
        
        # Per-axis 3×3 block for [pos, vel, acc] driven by jerk noise
        blk = np.array([
            [dt5 / 20, dt4 / 8, dt3 / 6],
            [dt4 / 8,  dt3 / 3, dt2 / 2],
            [dt3 / 6,  dt2 / 2, dt      ],
        ], dtype=np.float64) * q_a
        
        # State layout is interleaved: [lat, lon, u, v, a_lat, a_lon]
        # Lat axis → indices 0, 2, 4     Lon axis → indices 1, 3, 5
        Q = np.zeros((6, 6), dtype=np.float64)
        lat_idx = [0, 2, 4]
        lon_idx = [1, 3, 5]
        for ri in range(3):
            for ci in range(3):
                Q[lat_idx[ri], lat_idx[ci]] = blk[ri, ci]
                Q[lon_idx[ri], lon_idx[ci]] = blk[ri, ci]
        
        return Q
    
    def get_predicted_position(self, dt: float) -> Tuple[float, float]:
        """
        Get predicted position after dt seconds without updating state.
        
        Args:
            dt: Time step in seconds
        
        Returns:
            (lat, lon) predicted position
        """
        F = self._build_transition_matrix(dt)
        x = self.state.to_array()
        x_pred = F @ x
        return (float(x_pred[0]), float(x_pred[1]))
    
    def get_position_uncertainty_km(self) -> Tuple[float, float]:
        """
        Get current position uncertainty in km.
        
        Returns:
            (std_lat_km, std_lon_km)
        """
        return self.covariance.get_position_std_km(self.ref_lat)
    
    def get_state_dict(self) -> Dict[str, Any]:
        """
        Get state as dictionary for serialization.
        
        Returns:
            Dictionary with state components
        """
        return {
            'lat': self.state.lat,
            'lon': self.state.lon,
            'u': self.state.u,
            'v': self.state.v,
            'a_lat': self.state.a_lat,
            'a_lon': self.state.a_lon,
            'P': self.covariance.to_array().tolist()
        }
    
    @classmethod
    def from_state_dict(cls, state_dict: Dict[str, Any], 
                        config: Optional[KalmanConfig] = None) -> "KalmanFilter":
        """
        Create Kalman filter from state dictionary.
        
        Args:
            state_dict: Dictionary with state components
            config: Optional configuration
        
        Returns:
            KalmanFilter instance
        """
        kf = cls(config=config or default_kalman_config())
        
        kf.state = StateVector(
            lat=state_dict.get('lat', 0.0),
            lon=state_dict.get('lon', 0.0),
            u=state_dict.get('u', 0.0),
            v=state_dict.get('v', 0.0),
            a_lat=state_dict.get('a_lat', 0.0),
            a_lon=state_dict.get('a_lon', 0.0)
        )
        
        P = state_dict.get('P')
        if P is not None:
            kf.covariance = CovarianceMatrix(_matrix=np.array(P, dtype=np.float64))
        
        kf._initialized = True
        return kf
    
    def get_innovation_covariance(self, regularization: Optional[float] = None) -> np.ndarray:
        """
        Compute innovation covariance S = H * P * H^T + R.

        The innovation covariance represents the uncertainty in the measurement
        prediction, combining state uncertainty (P) propagated through the
        observation model (H) with measurement noise (R).

        Args:
            regularization: Small value added to diagonal for numerical stability;
                falls back to the configured ``filter_internals`` value

        Returns:
            2x2 innovation covariance matrix
        """
        if regularization is None:
            regularization = self.config.internals.innovation_covariance_regularization

        if not self._initialized:
            # Return identity if not initialized
            return np.eye(2, dtype=np.float64)

        P = self.covariance.to_array()
        S = self._H @ P @ self._H.T + self._R
        
        # Add regularization for numerical stability
        S += np.eye(2) * regularization
        
        return S
    
    def get_mahalanobis_distance(self, lat: float, lon: float) -> float:
        """
        Calculate Mahalanobis distance to a measurement.
        
        The Mahalanobis distance measures how many standard deviations away
        a point is from the predicted distribution, accounting for the
        covariance structure. This provides a scale-invariant distance metric
        that respects the uncertainty ellipse of the Kalman filter.
        
        Formula: d_M = sqrt((z - H*x)^T * S^-1 * (z - H*x))
        
        Args:
            lat: Measurement latitude in degrees
            lon: Measurement longitude in degrees
        
        Returns:
            Mahalanobis distance (dimensionless, ~chi-squared distributed)
        """
        if not self._initialized:
            return float('inf')
        
        x = self.state.to_array()
        z_pred = self._H @ x  # Predicted measurement (2D: lat, lon)
        z = np.array([lat, lon], dtype=np.float64)
        
        # Innovation (measurement residual)
        y = z - z_pred
        
        # Innovation covariance
        S = self.get_innovation_covariance()
        
        # Mahalanobis distance: sqrt(y^T * S^-1 * y)
        # Use np.linalg.solve for numerical stability instead of explicit inverse
        try:
            # Solve S * x = y for x, which gives x = S^-1 * y
            x = np.linalg.solve(S, y)
            d_m_squared = y.T @ x
            # Handle numerical issues
            if d_m_squared < 0:
                return float('inf')
            return float(np.sqrt(d_m_squared))
        except np.linalg.LinAlgError:
            # Singular matrix - add more regularization and retry
            try:
                S_regularized = S + np.eye(2) * self.config.internals.singular_retry_regularization
                x = np.linalg.solve(S_regularized, y)
                d_m_squared = y.T @ x
                if d_m_squared < 0:
                    return float('inf')
                return float(np.sqrt(d_m_squared))
            except np.linalg.LinAlgError:
                # Still singular - return infinity
                return float('inf')
    
    def is_within_gate(self, lat: float, lon: float,
                       threshold: float,
                       min_radius_km: float) -> bool:
        """
        Check if measurement is within validation gate.

        Uses Mahalanobis distance for physics-aware gating that respects
        the covariance ellipse. Falls back to minimum radius for cases
        where the covariance has collapsed (very small uncertainty).

        Both limits are required rather than defaulted: they are the same
        numbers as ``assignment.gating_threshold``/``min_gating_radius_km``,
        and a default here would be a second copy of them.

        Args:
            lat: Measurement latitude in degrees
            lon: Measurement longitude in degrees
            threshold: Mahalanobis distance threshold (~95% chi-squared)
            min_radius_km: Minimum radius in km for collapsed covariance fallback

        Returns:
            True if measurement is within the validation gate
        """
        if not self._initialized:
            return False
        
        # Check Mahalanobis distance first
        d_m = self.get_mahalanobis_distance(lat, lon)
        if d_m <= threshold:
            return True
        
        # Fallback for collapsed covariance
        # If the covariance is very small, the Mahalanobis distance can be
        # very large even for physically close points
        pred_lat, pred_lon = self.state.get_position()
        euclidean_dist = haversine_distance(lat, lon, pred_lat, pred_lon)
        
        return euclidean_dist <= min_radius_km
