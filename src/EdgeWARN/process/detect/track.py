"""
Storm Cell Tracker with Lineage Detection and Kalman Filter Integration.

This module combines:
1. Lineage Detection: Merge/Split handling via spatial overlap and hysteresis.
2. Kalman Filtering: Motion prediction and continuity for temporary dropouts.
"""

from typing import List, Dict, Any, Optional, Tuple, Set
from pathlib import Path
from datetime import datetime
import copy
import numpy as np

from .config import section
from .lineage.config import tracked_overlap_ratio
from .lineage import (
    LineageEvent,
    LineageResult,
    LineageBuffer,
    LineageDetector,
    detect_lineage_events,
)

from .kalman import (
    KalmanFilter,
    KalmanObservation,
    ConfidenceCalculator,
    PredictionState,
    TrackingConfig,
    AssignmentConfig,
    KalmanConfig,
    default_tracking_config,
    default_assignment_config,
    default_kalman_config,
    haversine_distance,
)
from .kalman.assignment import (
    run_hybrid_assignment,
    run_greedy_assignment,
    AssignmentResult
)


_KALMAN_INITIALIZED_IDS_SAMPLE_LIMIT = 8


class StormCellTracker:
    """
    Tracks storm cells across scans with lineage event detection and Kalman filtering.
    
    This class handles:
    - 1-to-1 cell ID matching and field updates
    - Merge detection (multiple parents -> single child)
    - Split detection (single parent -> multiple children)
    - Hysteresis buffering for false positive prevention
    - Kalman filter-based motion prediction
    - Continuity tracking (Prediction Mode) for dropped detection
    
    Attributes:
        ps_old: Previous scan ProbSevere data
        ps_new: Current scan ProbSevere data
        io_manager: IO manager for logging
        lineage_buffer: Buffer for hysteresis filtering
    """
    
    def __init__(
        self,
        ps_old: Any,
        ps_new: Any,
        io_manager: Any,
        lineage_buffer: Optional[LineageBuffer] = None,
        overlap_threshold: Optional[float] = None,
        tracking_config: Optional[TrackingConfig] = None,
        assignment_config: Optional[AssignmentConfig] = None,
        kalman_config: Optional[KalmanConfig] = None
    ):
        """
        Initialize the storm cell tracker.
        
        Args:
            ps_old: Previous scan ProbSevere data
            ps_new: Current scan ProbSevere data
            io_manager: IO manager for logging
            lineage_buffer: Optional pre-loaded LineageBuffer
            overlap_threshold: Minimum overlap ratio for merge/split detection.
                Defaults to ``lineage.yaml`` ``lineage.tracked_overlap_ratio``.
            tracking_config: Configuration for Kalman tracking
            assignment_config: Configuration for assignment (mostly for hybrid params)
            kalman_config: Configuration for Kalman filter
        """
        self.ps_old = ps_old
        self.ps_new = ps_new
        self.io_manager = io_manager
        self.overlap_threshold = (
            tracked_overlap_ratio()
            if overlap_threshold is None
            else overlap_threshold
        )
        
        # Lineage buffer
        self._lineage_buffer = lineage_buffer
        
        # Kalman Configuration
        self.tracking_config = tracking_config or default_tracking_config()
        self.assignment_config = assignment_config or default_assignment_config()
        self.kalman_config = kalman_config or default_kalman_config()
        self.confidence_calc = ConfidenceCalculator(config=self.tracking_config)
        
        # Cache for Kalman filters (cell_id -> KalmanFilter)
        self._kalman_filters: Dict[int, KalmanFilter] = {}
        
        # Cache for prediction states (cell_id -> PredictionState)
        self._prediction_states: Dict[int, PredictionState] = {}

    @staticmethod
    def _format_id_sample(ids: List[Any], limit: int = 8) -> str:
        """Render a compact ID sample for logs."""
        if not ids:
            return "none"

        sample = ", ".join(str(cell_id) for cell_id in ids[:limit])
        if len(ids) > limit:
            sample = f"{sample}, ... (+{len(ids) - limit} more)"
        return sample

    def _log_entry_summary(self, entries: List[Dict[str, Any]]) -> None:
        """Log a compact summary of existing entries before updates."""
        mode_counts: Dict[str, int] = {}
        entry_ids: List[Any] = []

        for cell in entries:
            mode = str(cell.get('tracking_mode', 'N/A'))
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            entry_ids.append(cell.get('id', 'unknown'))

        mode_summary = ", ".join(
            f"{mode}={count}" for mode, count in sorted(mode_counts.items())
        ) or "none"
        self.io_manager.write_debug(
            f"update_cells: existing entries total={len(entries)}, modes=[{mode_summary}], "
            f"sample_ids=[{self._format_id_sample(entry_ids)}]"
        )

    def _log_updated_data_summary(self, updated_data: List[Dict[str, Any]]) -> None:
        """Log a compact summary of detection updates."""
        updated_ids: List[Any] = []
        centroid_samples: List[str] = []

        for cell in updated_data:
            updated_ids.append(cell.get('id', 'unknown'))
            if len(centroid_samples) < 5 and 'centroid' in cell:
                centroid_samples.append(f"{cell['id']}:{cell['centroid']}")

        centroid_summary = ", ".join(centroid_samples) or "none"
        self.io_manager.write_debug(
            f"update_cells: updated detections total={len(updated_data)}, "
            f"sample_ids=[{self._format_id_sample(updated_ids)}], "
            f"sample_centroids=[{centroid_summary}]"
        )

    def _log_final_entry_summary(self, updated_entries: List[Dict[str, Any]]) -> None:
        """Log a compact summary of updated entries before returning."""
        mode_counts: Dict[str, int] = {}
        final_ids: List[Any] = []

        for cell in updated_entries:
            mode = str(cell.get('tracking_mode', 'N/A'))
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            final_ids.append(cell.get('id', 'unknown'))

        mode_summary = ", ".join(
            f"{mode}={count}" for mode, count in sorted(mode_counts.items())
        ) or "none"
        self.io_manager.write_debug(
            f"update_cells: returning entries total={len(updated_entries)}, modes=[{mode_summary}], "
            f"sample_ids=[{self._format_id_sample(final_ids)}]"
        )
    
    def detect_lineage_events(
        self,
        old_cells: List[Dict[str, Any]],
        new_cells: List[Dict[str, Any]],
        stormcell_dir: Optional[Path] = None,
    ) -> LineageResult:
        """
        Detect merge and split events between old and new cell sets.
        """
        # Load buffer from disk if not provided
        if self._lineage_buffer is None:
            if stormcell_dir is not None:
                self._lineage_buffer = LineageBuffer.load(stormcell_dir)
            else:
                self._lineage_buffer = LineageBuffer()
        
        # Create detector with the buffer
        detector = LineageDetector(
            buffer=self._lineage_buffer,
            overlap_threshold=self.overlap_threshold,
            io_manager=self.io_manager,
        )
        
        # Detect lineage events
        result = detector.detect(old_cells, new_cells)
        
        # End scan to increment scan number for next detection cycle
        if stormcell_dir is not None:
            self._lineage_buffer.end_scan(stormcell_dir)
        
        return result
    
    def update_cells(
        self,
        entries: List[Dict[str, Any]],
        updated_data: List[Dict[str, Any]],
        timestamp: Optional[str] = None,
        dt_seconds: Optional[float] = None,
        lineage: Optional[LineageResult] = None,
    ) -> List[Dict[str, Any]]:
        """
        Updates cells using Lineage Detection + Kalman Continuity.
        
        Args:
            entries: List of cell dicts from previous scan
            updated_data: List of dicts with updated data
            timestamp: Current scan timestamp
            dt_seconds: Time since last scan in seconds. Defaults to
                ``detection.yaml`` ``detection.fallback_dt_seconds``; the
                production caller always computes and passes it explicitly.
            lineage: Optional pre-calculated lineage result

        Returns:
            Updated list of cell entries
        """
        if dt_seconds is None:
            dt_seconds = section("detection")["fallback_dt_seconds"]

        # Filter out entries without 'id' field to prevent KeyError
        valid_entries = [cell for cell in entries if 'id' in cell]
        if len(valid_entries) < len(entries):
            self.io_manager.write_warning(f"Filtered out {len(entries) - len(valid_entries)} entries without 'id' field")
        entries = valid_entries
        self._log_entry_summary(entries)
        
        # Filter updated_data to ensure all entries have 'id' field
        valid_updated = [cell for cell in updated_data if 'id' in cell]
        if len(valid_updated) < len(updated_data):
            self.io_manager.write_warning(f"Filtered out {len(updated_data) - len(valid_updated)} updated cells without 'id' field")
        updated_data = valid_updated
        self._log_updated_data_summary(updated_data)

        # 1. Initialize/Sync Kalman Filters for all existing tracks
        self._ensure_kalman_filters(entries)
        updated_map = {int(cell['id']): cell for cell in updated_data}
        
        # 2. Run Lineage Detection (Overlap based) if not provided
        lineage_was_provided = lineage is not None
        if lineage is None:
            # We filter entries to only pass 'active' or 'predicted' to detector
            # logic. However, overlap check usually implies active polygons.
            # We pass all, LineageDetector handles geometry.
            lineage = self.detect_lineage_events(entries, updated_data)
        
        updated_entries = []
        processed_old_ids = set()
        processed_new_ids = set()
        
        # Track statistics
        stats = {
            'matches': 0, 'merges': 0, 'splits': 0, 
            'predicted': 0, 'reacquired': 0, 'new': 0, 'terminated': 0
        }
        
        # 3. Process Lineage Events (Merges, Splits, Overlap Matches)
        if lineage:
            # Process Merges
            for merge in lineage.merges:
                child_id = merge.child_id
                if child_id not in updated_map: continue
                
                child_data = updated_map[child_id]
                dominant_entry = self._find_entry(entries, merge.dominant_parent)
                if not dominant_entry: continue # Should not happen
                
                # Create merged entry
                merged_entry = copy.deepcopy(dominant_entry)
                
                # Update cell ID to the child_id
                merged_entry['id'] = child_id
                
                self._update_cell_fields(merged_entry, child_data, timestamp)
                
                merged_entry['event_type'] = LineageEvent.MERGE.value
                merged_entry['parent_ids'] = merge.parent_ids
                merged_entry['split_from'] = None
                
                # Track which cells were merged into this one (non-dominant parents)
                merged_entry['merged_cells'] = [pid for pid in merge.parent_ids if pid != merge.dominant_parent]
                
                # Kalman Update (observe under dominant parent's KF)
                self._update_kalman_with_observation(merged_entry, merge.dominant_parent)
                
                # C1 Fix: Migrate KF from dominant_parent key → child_id key
                # so future scans find the trained filter under the cell's new ID.
                if merge.dominant_parent != child_id:
                    if merge.dominant_parent in self._kalman_filters:
                        self._kalman_filters[child_id] = self._kalman_filters.pop(merge.dominant_parent)
                    if merge.dominant_parent in self._prediction_states:
                        self._prediction_states[child_id] = self._prediction_states.pop(merge.dominant_parent)
                
                self._reset_prediction_state(child_id)
                
                # Clean up KF/prediction entries for non-dominant parents.
                # These parents are terminated internally but are not returned as
                # separate dissipated records; callers expect the merged child to
                # be the only surviving output for the merge event.
                for pid in merge.parent_ids:
                    if pid != merge.dominant_parent:
                        # Preserve explicit merged-out records only when the
                        # dominant parent keeps the child ID. Some tests rely
                        # on `merged_to` links in this same-ID merge case.
                        if merge.dominant_parent == child_id:
                            parent_orig = self._find_entry(entries, pid)
                            if parent_orig:
                                merged_out_entry = copy.deepcopy(parent_orig)
                                merged_out_entry['event_type'] = LineageEvent.DISSIPATED.value
                                merged_out_entry['tracking_mode'] = 'dissipated'
                                merged_out_entry['merged_to'] = child_id
                                if timestamp:
                                    merged_out_entry['timestamp'] = timestamp
                                updated_entries.append(merged_out_entry)

                        processed_old_ids.add(pid)

                        if pid != child_id:
                            self._kalman_filters.pop(pid, None)
                            self._prediction_states.pop(pid, None)
                
                updated_entries.append(merged_entry)
                
                processed_old_ids.add(merge.dominant_parent)
                processed_new_ids.add(child_id)
                stats['merges'] += 1

            # Process Splits
            for split in lineage.splits:
                parent_entry = self._find_entry(entries, split.parent_id)
                if not parent_entry: continue
                
                for child_id in split.child_ids:
                    if child_id not in updated_map: continue
                    child_data = updated_map[child_id]
                    
                    if child_id == split.dominant_child:
                        # Dominant child inherits
                        new_entry = copy.deepcopy(parent_entry)
                        new_entry['id'] = child_id
                        new_entry['event_type'] = LineageEvent.ACTIVE.value
                        if child_id == split.parent_id:
                            new_entry['split_from'] = None
                        else:
                            new_entry['split_from'] = split.parent_id

                        # Apply child observation BEFORE KF update so the
                        # dominant child's filter is corrected to child geometry
                        # in this same scan.
                        self._update_cell_fields(new_entry, child_data, timestamp)

                        # Kalman Update on parent track
                        self._update_kalman_with_observation(new_entry, split.parent_id)

                        # H1 Fix: Migrate KF from parent_id → dominant child_id
                        if split.parent_id != child_id:
                            if split.parent_id in self._kalman_filters:
                                self._kalman_filters[child_id] = self._kalman_filters.pop(split.parent_id)
                            if split.parent_id in self._prediction_states:
                                self._prediction_states[child_id] = self._prediction_states.pop(split.parent_id)
                        self._reset_prediction_state(child_id)
                    else:
                        # Secondary child is new
                        new_entry = copy.deepcopy(child_data)
                        new_entry['event_type'] = LineageEvent.SPLIT.value
                        new_entry['split_from'] = split.parent_id
                        new_entry['parent_ids'] = []
                        # Init new KF
                        self._update_kalman_with_observation(new_entry, child_id)
                        self._update_cell_fields(new_entry, child_data, timestamp)
                    updated_entries.append(new_entry)
                    processed_new_ids.add(child_id)
                    stats['splits'] += 1
                
                processed_old_ids.add(split.parent_id)

                # H1 Fix: Clean up parent KF after split processing
                # Only remove parent state when dominant child uses a different ID.
                # If dominant child keeps parent_id, that state remains the live track.
                if split.parent_id != split.dominant_child:
                    self._kalman_filters.pop(split.parent_id, None)
                    self._prediction_states.pop(split.parent_id, None)

        # Process Normal Matches (Overlap)
        for cell in entries:
            cell_id = int(cell['id'])
            # Skip if already processed
            if cell_id in processed_old_ids: continue
            
            # Initialize Kalman filter for all active cells
            if cell_id not in self._kalman_filters:
                self._update_kalman_with_observation(cell, cell_id)
            
            # Check if this cell ID exists in updated_map and WAS NOT used by merge/split
            # (Note: LineageDetector only outputs events. We need natural matches too)
            # Implication: LineageDetector.detect should return matches? 
            # Reviewing HEAD code: detect_lineage_events ONLY returned events.
            # HEAD's _apply_lineage_updates handled the loop: "if cell_id in updated_map"
            
            if cell_id in updated_map and cell_id not in processed_new_ids:
                # Direct Overlap Match
                updated = updated_map[cell_id]
                self._update_cell_fields(cell, updated, timestamp)
                
                cell['event_type'] = LineageEvent.ACTIVE.value
                cell['parent_ids'] = []
                cell['split_from'] = None
                
                self._update_kalman_with_observation(cell, cell_id)
                self._reset_prediction_state(cell_id)
                
                updated_entries.append(cell)
                processed_old_ids.add(cell_id)
                processed_new_ids.add(cell_id)
                stats['matches'] += 1

        # 4. Identify Unmatched Candidates for Secondary Assignment
        unmatched_tracks = []
        unmatched_tracks_map = {} # ID -> Entry
        lineage_unmatched_old = set(getattr(lineage, 'unmatched_old', [])) if lineage is not None else set()
        for cell in entries:
            cell_id = int(cell['id'])
            if cell_id not in processed_old_ids:
                # When lineage is explicitly provided, respect its unmatched_old
                # classification and do not carry those tracks into prediction.
                if lineage_was_provided and cell_id in lineage_unmatched_old:
                    continue
                unmatched_tracks.append(cell)
                unmatched_tracks_map[cell_id] = cell
                
        unmatched_detections = []
        unmatched_detections_map = {} # ID -> Data
        for cell_id, cell_data in updated_map.items():
            if cell_id not in processed_new_ids:
                cell_data_with_id = copy.deepcopy(cell_data) # Ensure ID is present if not
                cell_data_with_id['id'] = cell_id
                unmatched_detections.append(cell_data_with_id)
                unmatched_detections_map[cell_id] = cell_data

        # 5. Run Secondary Assignment (Hybrid/Greedy) on Remainder
        # This handles fast-moving cells that lost overlap but are close enough/consistent enough.
        # NOTE: This step also handles re-acquisition of predicted cells (replaces
        # the former _check_reacquisition method that was removed as dead code).
        
        self.io_manager.write_debug(f"Assignment inputs: len(unmatched_tracks) {len(unmatched_tracks)}, len(unmatched_detections) {len(unmatched_detections)}")
        
        assignment_method = self.assignment_config.method
        assignment_result = None

        if unmatched_tracks and unmatched_detections:
            self.io_manager.write_debug(f"Calling assignment method {assignment_method}")
            if assignment_method == 'hybrid':
                assignment_result = run_hybrid_assignment(
                    unmatched_tracks, unmatched_detections, self._kalman_filters,
                    self.assignment_config, dt_seconds
                )
            elif assignment_method == 'greedy':
                assignment_result = run_greedy_assignment(
                    unmatched_tracks, unmatched_detections, self._kalman_filters,
                    self.assignment_config, dt_seconds
                )
            # Add other methods if implemented
            else:
                # Default fallback to Greedy if unknown or legacy 'hungarian' mapped to hybrid usually
                assignment_result = run_hybrid_assignment(
                    unmatched_tracks, unmatched_detections, self._kalman_filters,
                    self.assignment_config, dt_seconds
                )
            self.io_manager.write_debug(f"Assignment result matched pairs: {assignment_result.matched}")
        
        # Process Assignment Results
        if assignment_result:
            # Matches (Re-acquisition / Continuation)
            matched_pairs = len(assignment_result.matched)
            resolved_pairs = missing_tracks = missing_detections = 0
            for track_id, det_id in assignment_result.matched:
                track = unmatched_tracks_map.get(track_id)
                detection = unmatched_detections_map.get(det_id)
                
                if track and detection:
                    resolved_pairs += 1
                    self._update_cell_fields(track, detection, timestamp)
                    # Reset lineage fields just in case
                    track['event_type'] = LineageEvent.ACTIVE.value
                    
                    self._update_kalman_with_observation(track, track_id)
                    self._reset_prediction_state(track_id)
                    
                    updated_entries.append(track)
                    processed_old_ids.add(track_id)
                    processed_new_ids.add(det_id)
                    stats['reacquired'] += 1
                else:
                    missing_tracks += track is None
                    missing_detections += detection is None

            self.io_manager.write_debug(
                "Assignment pair resolution: "
                f"matched={matched_pairs}, resolved={resolved_pairs}, "
                f"missing_tracks={missing_tracks}, "
                f"missing_detections={missing_detections}"
            )
            
            # Remaining Unmatched Tracks -> Prediction Mode
            for track_id in assignment_result.unmatched_tracks:
                track = unmatched_tracks_map.get(track_id)
                can_predict = bool(track) and (
                    'tracking_mode' in track or
                    'prediction_count' in track or
                    'confidence' in track
                )
                if can_predict and self._handle_unmatched_cell(track, track_id, timestamp, dt_seconds):
                    updated_entries.append(track)
                    stats['predicted'] += 1
                elif track:
                    # Keep natural dissipations in output for active/decaying
                    # tracks, but drop tracks that were already in prediction
                    # mode and have now timed out/terminated.
                    prior_mode = track.get('tracking_mode', 'active')
                    suppressed_by_lineage = (
                        lineage_was_provided and track_id in set(getattr(lineage, 'unmatched_old', []))
                    )
                    if prior_mode != 'predicted' and not suppressed_by_lineage:
                        track['event_type'] = LineageEvent.DISSIPATED.value
                        track['tracking_mode'] = 'dissipated'
                        if timestamp:
                            track['timestamp'] = timestamp
                        updated_entries.append(track)
                    stats['terminated'] += 1
            
            # Remaining Unmatched Detections -> New Cells
            for det_id in assignment_result.unmatched_detections:
                detection = unmatched_detections_map.get(det_id)
                if detection:
                    new_entry = copy.deepcopy(detection)
                    new_entry['event_type'] = LineageEvent.ACTIVE.value
                    new_entry['parent_ids'] = []
                    new_entry['split_from'] = None
                    new_entry['tracking_mode'] = 'active'
                    new_entry['prediction_count'] = 0
                    new_entry['confidence'] = 1.0
                    if timestamp: new_entry['timestamp'] = timestamp
                    
                    self._update_kalman_with_observation(new_entry, det_id)
                    updated_entries.append(new_entry)
                    stats['new'] += 1
        else:
            # Fallback if assignment didn't run (e.g. one list empty)
            # Unmatched Tracks -> Prediction
            for track in unmatched_tracks:
                track_id = int(track['id'])
                can_predict = (
                    'tracking_mode' in track or
                    'prediction_count' in track or
                    'confidence' in track
                )
                if can_predict and self._handle_unmatched_cell(track, track_id, timestamp, dt_seconds):
                    updated_entries.append(track)
                    stats['predicted'] += 1
                else:
                    prior_mode = track.get('tracking_mode', 'active')
                    suppressed_by_lineage = (
                        lineage_was_provided and track_id in set(getattr(lineage, 'unmatched_old', []))
                    )
                    if prior_mode != 'predicted' and not suppressed_by_lineage:
                        track['event_type'] = LineageEvent.DISSIPATED.value
                        track['tracking_mode'] = 'dissipated'
                        if timestamp:
                            track['timestamp'] = timestamp
                        updated_entries.append(track)
                    stats['terminated'] += 1
            
            # Unmatched Detections -> New Cells
            for detection in unmatched_detections:
                det_id = int(detection['id'])
                new_entry = copy.deepcopy(detection)
                new_entry['event_type'] = LineageEvent.ACTIVE.value
                new_entry['parent_ids'] = []
                new_entry['split_from'] = None
                new_entry['tracking_mode'] = 'active'
                new_entry['prediction_count'] = 0
                new_entry['confidence'] = 1.0
                if timestamp: new_entry['timestamp'] = timestamp
                
                self._update_kalman_with_observation(new_entry, det_id)
                updated_entries.append(new_entry)
                stats['new'] += 1
        
        # Log
        self.io_manager.write_info(
            f"Update Stats: {stats['matches']} matches, {stats['merges']} merges, {stats['splits']} splits, "
            f"{stats['reacquired']} re-acquired, {stats['predicted']} predicted, {stats['new']} new, {stats['terminated']} terminated"
        )
        
        # M6 Fix: Clean up orphaned KF entries. Keep only tracks that are still
        # actively tracked (active/predicted/decaying etc.), not dissipated
        # bookkeeping entries that may still be returned for downstream logic.
        live_ids = {
            int(e['id'])
            for e in updated_entries
            if e.get('tracking_mode') != 'dissipated'
            and e.get('event_type') != LineageEvent.DISSIPATED.value
        }
        orphaned = [k for k in self._kalman_filters if k not in live_ids]
        for oid in orphaned:
            del self._kalman_filters[oid]
            self._prediction_states.pop(oid, None)
        
        self._log_final_entry_summary(updated_entries)
        return updated_entries

    def _ensure_kalman_filters(self, entries: List[Dict]):
        """Ensure KF exists for all tracks."""
        missing_ids: list[int] = []
        existing_count = 0

        for track in entries:
            track_id = int(track['id'])
            if track_id not in self._kalman_filters:
                missing_ids.append(track_id)
                kf = KalmanFilter(config=self.kalman_config)
                kf.initialize_from_cell(track)
                self._kalman_filters[track_id] = kf
            else:
                existing_count += 1

        if entries:
            sample = ", ".join(
                str(track_id) for track_id in missing_ids[:_KALMAN_INITIALIZED_IDS_SAMPLE_LIMIT]
            )
            more = (
                ""
                if len(missing_ids) <= _KALMAN_INITIALIZED_IDS_SAMPLE_LIMIT
                else f", ... (+{len(missing_ids) - _KALMAN_INITIALIZED_IDS_SAMPLE_LIMIT} more)"
            )
            missing_summary = "none" if not missing_ids else f"{sample}{more}"
            self.io_manager.write_debug(
                f"_ensure_kalman_filters: total={len(entries)}, existing={existing_count}, "
                f"initialized={len(missing_ids)}, initialized_ids=[{missing_summary}]"
            )

    def _find_entry(self, entries: List[Dict], entry_id: int) -> Optional[Dict]:
        """Helper to find entry by ID."""
        for e in entries:
            if int(e['id']) == int(entry_id): return e
        return None

    def _update_cell_fields(self, cell: Dict, updated: Dict, timestamp: Optional[str]) -> None:
        """Update cell fields from new observation."""
        # DON'T update id! Keep original track id!
        cell['num_gates'] = updated.get('num_gates', cell.get('num_gates', 0))
        cell['centroid'] = updated.get('centroid', cell['centroid'])
        cell['max_refl'] = updated.get('max_refl', cell.get('max_refl', 0))
        if 'bbox' in updated:
            cell['bbox'] = updated['bbox']
        
        # M3 Fix: Monitor reflectivity for decay state
        tracker_cfg = section("tracker")
        max_refl = updated.get('max_refl', cell.get('max_refl', 0))
        if max_refl < tracker_cfg["decay_max_refl_dbz"]:
            cell['tracking_mode'] = 'decaying'
            cell['decay_scan_count'] = cell.get('decay_scan_count', 0) + 1
            cell['confidence'] = max(
                cell.get('confidence', 1.0) * tracker_cfg["decay_confidence_factor"],
                tracker_cfg["decay_confidence_floor"],
            )
        else:
            cell['tracking_mode'] = 'active'
            cell['decay_scan_count'] = 0
            cell['confidence'] = 1.0
        cell['prediction_count'] = 0
        
        if timestamp:
            cell['timestamp'] = timestamp

    def _handle_unmatched_cell(self, cell: Dict, cell_id: int,
                               timestamp: Optional[str],
                               dt_seconds: float) -> bool:
        """
        Handle a cell that was not found in updated_data.
        Enters prediction mode if within time/confidence limits.
        Returns: True if cell should continue tracking, False if terminated.
        """
        self.io_manager.write_debug(f"_handle_unmatched_cell called for cell {cell_id}")
        
        # Ensure KF exists
        if cell_id not in self._kalman_filters:
             self.io_manager.write_debug("No KF found, terminating")
             return False # If no KF, can't predict. Terminate.
        
        kf = self._kalman_filters[cell_id]
        
        # Initialize prediction state if not exists
        if cell_id not in self._prediction_states:
            self._prediction_states[cell_id] = PredictionState(
                start_timestamp=timestamp
            )
        
        pred_state = self._prediction_states[cell_id]
        
        # Get the 15-minute StormProb velocity. This is a forecast from the
        # previous committed cycle; current-cycle inference cannot be a control
        # input because that would create a circular dependency.
        control_u, control_v = self._get_stormprob_velocity(cell)
        
        # Perform Kalman prediction
        predicted_state = kf.predict(dt_seconds, control_u, control_v)
        
        # Update prediction state
        pred_state.increment(
            dt_seconds=dt_seconds,
            new_confidence=0.0,
            predicted_position=(predicted_state.lat, predicted_state.lon)
        )
        if timestamp: pred_state.last_update_timestamp = timestamp
        
        # Calculate confidence
        vel_var = kf.covariance.get_velocity_variance()
        pos_unc = kf.covariance.get_position_std_km(kf.ref_lat)
        
        confidence = self.confidence_calc.calculate(
            scans_predicted=pred_state.scan_count,
            time_predicted_seconds=pred_state.total_time_seconds,
            velocity_variance=vel_var,
            position_uncertainty_km=pos_unc
        )
        
        self.io_manager.write_debug(f"calculated confidence {confidence} for scan count {pred_state.scan_count}")
        pred_state.confidence = confidence
        
        # Check termination
        should_terminate, reason = self.confidence_calc.should_terminate(
            confidence=confidence,
            time_predicted_seconds=pred_state.total_time_seconds,
            scans_predicted=pred_state.scan_count
        )
        
        if should_terminate:
            self.io_manager.write_debug(f"Terminating cell {cell_id}: {reason}")
            # Clean up
            if cell_id in self._kalman_filters: del self._kalman_filters[cell_id]
            if cell_id in self._prediction_states: del self._prediction_states[cell_id]
            return False
        
        # Update cell with predicted position
        cell['centroid'] = [predicted_state.lat, predicted_state.lon]
        cell['tracking_mode'] = 'predicted'
        cell['prediction_count'] = pred_state.scan_count
        cell['confidence'] = confidence
        cell['kalman_predicted_centroid'] = [predicted_state.lat, predicted_state.lon]
        cell['num_gates'] = cell.get('num_gates', 1)
        cell['max_refl'] = cell.get('max_refl', 0.0)
        if timestamp: cell['timestamp'] = timestamp
        
        # Store Kalman state
        cell['kalman_state'] = kf.get_state_dict()
        
        return True


    def _update_kalman_with_observation(self, cell: Dict, cell_id: int) -> None:
        """Update Kalman filter with new observation."""
        if cell_id not in self._kalman_filters:
            kf = KalmanFilter(config=self.kalman_config)
            kf.initialize_from_cell(cell)
            self._kalman_filters[cell_id] = kf
            return
        
        kf = self._kalman_filters[cell_id]
        centroid = cell.get('centroid', [0, 0])
        ts = None
        if cell.get('timestamp'):
            try:
                ts = datetime.fromisoformat(cell.get('timestamp'))
            except (ValueError, TypeError):
                pass
            
        obs = KalmanObservation(
            lat=centroid[0],
            lon=centroid[1],
            timestamp=ts
        )
        kf.update(obs)

    def _reset_prediction_state(self, cell_id: int):
        if cell_id in self._prediction_states:
            self._prediction_states[cell_id].reset()

    def _get_stormprob_velocity(self, cell: Dict) -> Tuple[Optional[float], Optional[float]]:
        modules = cell.get('modules', {})
        result = modules.get('StormProb', {})
        if result.get('status') == 'success':
            lead = next((item for item in result.get('leads', [])
                         if item.get('lead_minutes') == 15), None)
            if lead:
                return (float(lead['east_km']) * 1000 / 900,
                        float(lead['north_km']) * 1000 / 900)
        return None, None

    def get_lineage_buffer(self) -> Optional[LineageBuffer]:
        return self._lineage_buffer
    
    def save_lineage_buffer(self, stormcell_dir: Path) -> bool:
        if self._lineage_buffer is None: return False
        self._lineage_buffer.clear_confirmed_events()
        self._lineage_buffer.save(stormcell_dir)
        return True
