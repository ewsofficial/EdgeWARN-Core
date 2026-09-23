from EdgeWARN.process.detect.tools.utils import DetectionDataHandler
from pathlib import Path
import copy
from EdgeWARN.process.detect.tools.save import CellDataSaver
from EdgeWARN.process.detect.tools.vecmath import StormVectorCalculator
from EdgeWARN.process.detect.tools.alert_matcher import match_alerts_to_cells
from EdgeWARN.process.detect.track import StormCellTracker
from EdgeWARN.process.detect.kalman.config import (
    TrackingConfig,
    AssignmentConfig,
    KalmanConfig,
)
from EdgeWARN.process.detect.detect import detect_cells
from EdgeWARN.process.detect.config import DetectionConfig
from util.io import IOManager
import util.file as fs
import json as js
from datetime import datetime
from util.performance import tracker as perf_tracker

io_manager = IOManager("[CellDetection]")


def _detect_with_optional_probsevere(
    radar_path,
    ps_path,
    pt_path,
    lat_min,
    lat_max,
    lon_min,
    lon_max,
    need_probsevere,
    detection_config,
    disable_polygon_expansion=False,
    radar_obj=None,
    ps_obj=None,
    pt_obj=None,
    return_datasets=False,
):
    if need_probsevere:
        return detect_cells(
            radar_path,
            ps_path,
            pt_path,
            io_manager,
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            return_probsevere=True,
            disable_polygon_expansion=disable_polygon_expansion,
            detection_config=detection_config,
            radar_obj=radar_obj,
            ps_obj=ps_obj,
            preciptype_obj=pt_obj,
            return_datasets=return_datasets,
        )

    result = detect_cells(
        radar_path,
        ps_path,
        pt_path,
        io_manager,
        lat_min,
        lat_max,
        lon_min,
        lon_max,
        disable_polygon_expansion=disable_polygon_expansion,
        detection_config=detection_config,
        radar_obj=radar_obj,
        ps_obj=ps_obj,
        preciptype_obj=pt_obj,
        return_datasets=return_datasets,
    )

    if return_datasets:
        entries, dataset_context = result
        return entries, None, dataset_context

    return result, None


def main(
    radar_old,
    radar_new,
    ps_old,
    ps_new,
    pt_old,
    pt_new,
    lat_bounds: tuple,
    lon_bounds: tuple,
    detection_config: DetectionConfig | None = None,
    radar_old_obj=None,
    ps_old_obj=None,
    pt_old_obj=None,
    disable_tracking=False,
    disable_polygon_expansion=False,
    cleanup_stormcells=True,
):
    # Pipeline entry points pass their already-resolved config so CLI overrides
    # keep their precedence.  Keep direct callers compatible by loading the
    # same YAML-backed config when none was supplied.
    if detection_config is None:
        detection_config = DetectionConfig.from_yaml()

    if cleanup_stormcells:
        fs.clean_files_by_age(
            fs.STORMCELL_DIR,
            max_age_minutes=detection_config.stormcell_cleanup_max_age_minutes,
        )
    lat_min, lat_max = lat_bounds
    lon_min, lon_max = lon_bounds

    # === Input Data Safeguards ===
    # Check "New" data existence
    if radar_new and not Path(radar_new).exists():
        io_manager.write_warning(f"New radar file not found: {radar_new}")
        radar_new = None
    if ps_new and not Path(ps_new).exists():
        io_manager.write_warning(f"New ProbSevere file not found: {ps_new}")
        ps_new = None
    if pt_new and not Path(pt_new).exists():
        io_manager.write_warning(f"New PrecipType file not found: {pt_new}")
        pt_new = None

    # Check "Old" data existence
    if radar_old and not Path(radar_old).exists():
        io_manager.write_warning(f"Old radar file not found: {radar_old}")
        radar_old = None
    if ps_old and not Path(ps_old).exists():
        io_manager.write_warning(f"Old ProbSevere file not found: {ps_old}")
        ps_old = None
    if pt_old and not Path(pt_old).exists():
        io_manager.write_warning(f"Old PrecipType file not found: {pt_old}")
        pt_old = None

    # A current radar frame is sufficient for degraded single-frame detection.
    # ProbSevere and precipitation type enrich a detection; their absence must
    # never cause a newer valid radar frame to be discarded.
    single_frame = radar_new is None or ps_new is None or pt_new is None
    if single_frame:
        io_manager.write_debug("No new scan specified — running single-frame detection mode")

    # === Calculate Timestamp Early ===
    current_radar = radar_new if radar_new is not None else radar_old
    if current_radar is None:
        io_manager.write_warning("No valid radar input data found. Aborting detection.")
        return None, None # Modified return signature
        
    ts_str = DetectionDataHandler.find_timestamp(current_radar)
    try:
        dt = datetime.fromisoformat(ts_str)
        # dt = dt.replace(second=0, microsecond=0) # Removed: Keep seconds
        final_ts = dt.strftime("%Y%m%d-%H%M%S") # Format for filename with seconds
        json_ts = dt.isoformat() # Format for JSON content
        
    except ValueError:
        final_ts = ts_str
        json_ts = ts_str

    ps_old_data = None

    # === Load or create previous entries ===
    data_old = None
    try:
        from EdgeWARN.stormprob.database import StormProbRepository
        repository = StormProbRepository()
        pending = repository.pending_projection_cycles()
        restored = repository.recover_projections()
        if pending or restored:
            from EdgeWARN.api_integration.index_manager import APIIndexManager
            APIIndexManager(io_manager, include_pending=True).initialize_indexes()
            for cycle_id in pending:
                repository.mark_projection_published(cycle_id)
        previous = repository.latest_cycle_before(json_ts)
        if previous:
            data_old = {"features": previous,
                        "latest_timestamp": previous[0].get("timestamp")}
            io_manager.write_debug("Loaded previous cells from committed StormProb database")
    except FileNotFoundError:
        pass
    except Exception as exc:
        io_manager.write_error(f"Error loading previous database cycle: {exc}")

    try:
        # Find the most recent stormcells_*.json file in the stormcell directory
        stormcell_dir = fs.STORMCELL_DIR
        if data_old is None and stormcell_dir.exists():
            latest_json = None
            for candidate in stormcell_dir.glob("stormcells_*.json"):
                if candidate.stem < f"stormcells_{final_ts}":
                    if latest_json is None or candidate.stem > latest_json.stem:
                        latest_json = candidate

            if latest_json is not None:
                io_manager.write_debug(f"Loading previous cells from {latest_json}")
                with open(latest_json, 'r') as f:
                    data_old = js.load(f)
    except Exception as e:
        io_manager.write_error(f"Error loading previous data: {e}")

    entries_old = None
    if data_old:
        try:
            if isinstance(data_old, dict) and "features" in data_old:
                entries_old = data_old["features"]
            elif isinstance(data_old, list):
                entries_old = data_old
            
            if entries_old:
                io_manager.write_debug(f"Loaded {len(entries_old)} cells from previous file")
            else:
                io_manager.write_warning("Loaded data contained no features")
        except Exception as e:
            io_manager.write_error(f"Error parsing loaded data: {e}")
            entries_old = None

    # Get old timestamp for dt calculation
    old_ts_str = None
    if data_old and isinstance(data_old, dict):
        old_ts_str = data_old.get("latest_timestamp")

    if not entries_old and not single_frame:
        io_manager.write_info("No valid previous storm cell data found, detecting from old scan ...")
        perf_tracker.start("Detection - Old Scan Fallback")
        # Use cached objects if available for OLD scan
        entries_old, ps_old_data = _detect_with_optional_probsevere(
            radar_old,
            ps_old,
            pt_old,
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            need_probsevere=not single_frame,
            detection_config=detection_config,
            disable_polygon_expansion=disable_polygon_expansion,
            radar_obj=radar_old_obj, # Pass cached object
            ps_obj=ps_old_obj,
            pt_obj=pt_old_obj,
        )
        perf_tracker.stop("Detection - Old Scan Fallback")


    # === If single-frame mode, just update/save ===
    if single_frame:
        io_manager.write_info("Detecting available radar frame in single-frame mode.")
        current_ps = ps_new if radar_new is not None else ps_old
        current_pt = pt_new if radar_new is not None else pt_old
        entries, current_ps_data = _detect_with_optional_probsevere(
            current_radar,
            current_ps,
            current_pt,
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            need_probsevere=False,
            detection_config=detection_config,
            disable_polygon_expansion=disable_polygon_expansion,
            radar_obj=radar_old_obj if radar_new is None else None,
            ps_obj=ps_old_obj if radar_new is None else None,
            pt_obj=pt_old_obj if radar_new is None else None,
        )
        saver = CellDataSaver(None, current_radar, None, None, current_ps_data, None)

        # Apply timestamp (all cells are "current" in single frame)
        for cell in entries:
            cell["timestamp"] = json_ts

        entries = StormVectorCalculator.calculate_vectors(entries)

        # Match convective/flood alerts to cells
        entries = match_alerts_to_cells(entries, fs.MRMS_NWS_DIR, target_timestamp=json_ts)

        output_data = saver.create_json_structure(json_ts, entries)
        
        # Save to stormcell directory
        stormcell_dir = fs.STORMCELL_DIR
        stormcell_dir.mkdir(parents=True, exist_ok=True)
        output_file = stormcell_dir / f"stormcells_{final_ts}.json"
        
        from util.atomic import atomic_write_json
        atomic_write_json(output_file, output_data, indent=2, default=str)
        
        io_manager.write_info(f"Saved single-frame results to {output_file}")

        # This is an integration input, not a public committed cycle. The
        # integration publisher updates the index after its SQLite commit.
        return output_file, None

    # === Dual-frame mode ===
    io_manager.write_debug("Detecting cells in new scan ...")
    perf_tracker.start("Detection - New Scan")

    radar_new_obj = None
    ps_new_obj = None
    pt_new_obj = None
    
    entries_new, ps_new_data, dataset_context = _detect_with_optional_probsevere(
        radar_new,
        ps_new,
        pt_new,
        lat_min,
        lat_max,
        lon_min,
        lon_max,
        need_probsevere=True,
        detection_config=detection_config,
        disable_polygon_expansion=disable_polygon_expansion,
        radar_obj=radar_new_obj,
        ps_obj=ps_new_obj,
        pt_obj=pt_new_obj,
        return_datasets=True,
    )

    radar_new_obj, ps_new_obj, pt_new_obj = dataset_context
    
    perf_tracker.stop("Detection - New Scan")
    io_manager.write_debug(f"Detected {len(entries_new)} cells in new scan")

    io_manager.write_info("Matching and updating cell data")
    if ps_old_data is None:
        perf_tracker.start("Detection - Load ProbSevere Old")
        ps_old_data = DetectionDataHandler(
            radar_old,
            ps_old,
            pt_old,
            io_manager,
            lat_min,
            lat_max,
            lon_min,
            lon_max,
            radar_obj=radar_old_obj, # Use cached
            ps_obj=ps_old_obj,
            preciptype_obj=pt_old_obj
        ).load_probsevere()
        perf_tracker.stop("Detection - Load ProbSevere Old")

    perf_tracker.start("Detection - Tracking")
    saver = CellDataSaver(None, radar_new, None, None, ps_new_data, None)
    vector_previous_entries = copy.deepcopy(entries_old) if entries_old else None

    stormcell_dir = fs.STORMCELL_DIR
    stormcell_dir.mkdir(parents=True, exist_ok=True)

    if disable_tracking:
        io_manager.write_info("Tracking disabled: skipping lineage detection and Kalman tracking")
        entries = entries_new
        for cell in entries:
            cell["timestamp"] = json_ts
            cell.setdefault("tracking_mode", detection_config.tracking_disabled_mode)
            cell.setdefault(
                "prediction_count", detection_config.tracking_disabled_prediction_count
            )
            cell.setdefault("event_type", detection_config.tracking_disabled_event_type)
    else:
        # Load Kalman configurations
        tracking_config = TrackingConfig.from_yaml()
        assignment_config = AssignmentConfig.from_yaml()
        kalman_config = KalmanConfig.from_yaml()

        tracker = StormCellTracker(
            ps_old_data,
            ps_new_data,
            io_manager,
            tracking_config=tracking_config,
            assignment_config=assignment_config,
            kalman_config=kalman_config
        )

        # Lineage detection (merge/split events)
        lineage = tracker.detect_lineage_events(entries_old, entries_new, stormcell_dir)

        # Calculate dt for Kalman filter
        fallback_dt = detection_config.fallback_dt_seconds
        dt_seconds = fallback_dt
        if old_ts_str:
            try:
                old_dt = datetime.fromisoformat(old_ts_str)
                current_dt = datetime.fromisoformat(json_ts)
                dt_seconds = (current_dt - old_dt).total_seconds()
                if dt_seconds <= 0:
                    io_manager.write_warning(f"Calculated dt={dt_seconds}s is non-positive. Defaulting to {fallback_dt}s.")
                    dt_seconds = fallback_dt
            except Exception as e:
                io_manager.write_warning(f"Failed to calculate dt from timestamps: {e}. Defaulting to {fallback_dt}s.")

        # Pass timestamp, dt, and lineage to tracker
        entries = tracker.update_cells(
            entries_old,
            entries_new,
            timestamp=json_ts,
            dt_seconds=dt_seconds,
            lineage=lineage
        )

        # Save lineage buffer state for next scan
        tracker.save_lineage_buffer(stormcell_dir)

    perf_tracker.stop("Detection - Tracking")
    
    perf_tracker.start("Detection - Vector Calc")
    entries = StormVectorCalculator.calculate_vectors(entries, previous_entries=vector_previous_entries)
    perf_tracker.stop("Detection - Vector Calc")

    # Match convective/flood alerts to cells
    perf_tracker.start("Detection - Alert Matching")
    entries = match_alerts_to_cells(entries, fs.MRMS_NWS_DIR, target_timestamp=json_ts)
    perf_tracker.stop("Detection - Alert Matching")

    perf_tracker.start("Detection - Save")
    output_data = saver.create_json_structure(json_ts, entries)
    
    output_file = stormcell_dir / f"stormcells_{final_ts}.json"

    from util.atomic import atomic_write_json
    atomic_write_json(output_file, output_data, indent=2, default=str)
    perf_tracker.stop("Detection - Save")
        
    io_manager.write_info(f"Saved detection results to {output_file}")
    
    # Publication/index update happens after integration commits SQLite.
    
    # Return output file AND the new dataset objects for next iteration
    return output_file, (radar_new_obj, ps_new_obj, pt_new_obj)
