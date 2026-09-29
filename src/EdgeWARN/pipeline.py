import json
import asyncio
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import xarray as xr

import EdgeWARN.process.detect.main as detect
import EdgeWARN.process.integrate.main as integration
import util.file as fs
from common.ingest.manifest import CycleInputManifest
from common.ingest.replay import protect_runtime_inputs, commit_ingest_report
from common.ingest.mrms.config import get_goes_modifiers, get_mrms_modifiers
from common.ingest.mrms.pipeline import get_output_dirs
from common.pipeline.coordinator import run_staged_ingest_cycle
from EdgeWARN.api_integration.config import (
    initialize_at_startup_realtime,
    remove_old_cells_historical,
)
from EdgeWARN.api_integration.index_manager import APIIndexManager
from EdgeWARN.historical_config import historical_cleanup_max_files
from util.io import IOManager, QueueWriter
from util.performance import tracker as perf_tracker

# Suppress cfgrib/xarray compatibility warnings.
xr.set_options(use_new_combine_kwarg_defaults=True)

# Some users report issues with DNS resolution with aiodns.
sys.modules.pop("aiodns", None)


def initialize_runtime(base_dir=None, io_manager=None, initialize_indexes=None):
    runtime_io = io_manager or IOManager("[Pipeline]")

    if base_dir:
        fs.initialize_filesystem(base_dir)

    runtime_io.write_info(
        f"Runtime filesystem initialized: base_dir={fs.BASE_DIR} rap_dir={fs.RAP_DIR}"
    )

    if initialize_indexes is None:
        initialize_indexes = initialize_at_startup_realtime()
    if not initialize_indexes:
        return

    try:
        index_manager = APIIndexManager(runtime_io)
        index_manager.initialize_indexes()
    except Exception as exc:
        runtime_io.write_error(f"Failed to initialize API indexes: {exc}")


def parse_utc_time(time_str):
    dt = datetime.fromisoformat(time_str)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _write_profile_summary(log):
    lines = ["", "=" * 50, f"{'Component':<35} | {'Time (s)':<10}", "-" * 50]
    for name, duration in perf_tracker.get_timings().items():
        lines.append(f"{name:<35} | {duration:.4f}")
    lines.append("=" * 50)
    log("\n".join(lines))


def _cleanup_historical_data_dirs(pipeline_io):
    protected_dirs = {fs.CELL_DIR.resolve(), fs.STORMCELL_DIR.resolve()}
    cleanup_dirs = get_output_dirs(
        get_mrms_modifiers(),
        goes_modifiers=get_goes_modifiers(),
    )
    cleanup_dirs.append(fs.RAP_DIR)

    seen_dirs = set()
    for directory in cleanup_dirs:
        resolved_dir = directory.resolve()
        if resolved_dir in seen_dirs or resolved_dir in protected_dirs:
            continue

        seen_dirs.add(resolved_dir)
        fs.clean_old_files(directory, max_files=historical_cleanup_max_files())

    pipeline_io.write_debug(
        "Historical cleanup applied to ingest data directories only; "
        "cell and stormcell folders were excluded."
    )


def _prepare_realtime_detection_inputs(log, input_manifest=None):
    if input_manifest is not None:
        def frame_pair(product):
            records = input_manifest.records_for_product(product)
            current = [record for record in records if record.role == "current"]
            previous = [record for record in records if record.role == "previous"]
            # The detector's legacy loader accepts string paths.  Keep the
            # manifest record itself path-safe/immutable, but cross this
            # boundary explicitly rather than leaking ``Path`` instances
            # into the loader.
            current_path = str(current[-1].local_path) if current else None
            previous_path = str(previous[-1].local_path) if previous else None
            if previous_path is None:
                return current_path, None
            return previous_path, current_path

        radar_old, radar_new = frame_pair(
            "MergedReflectivityQCComposite_00.50"
        )
        ps_old, ps_new = frame_pair("ProbSevere")
        pt_old, pt_new = frame_pair("PrecipFlag_00.00")
        selected = (
            radar_old,
            radar_new,
            ps_old,
            ps_new,
            pt_old,
            pt_new,
        )
        log(
            "INFO: Pinned detection inputs: "
            + ", ".join(str(path) for path in selected if path is not None)
        )
        return selected

    try:
        filepath_old, filepath_new = fs.latest_files(fs.MRMS_COMPOSITE_DIR, 2)
        ps_old, ps_new = fs.latest_files(fs.MRMS_PROBSEVERE_DIR, 2)
        pt_old, pt_new = fs.latest_files(fs.MRMS_PRECIPTYP_DIR, 2)
        return filepath_old, filepath_new, ps_old, ps_new, pt_old, pt_new
    except (RuntimeError, ValueError):
        comp_files = fs.latest_files(fs.MRMS_COMPOSITE_DIR, 1)
        ps_files = fs.latest_files(fs.MRMS_PROBSEVERE_DIR, 1)
        pt_files = fs.latest_files(fs.MRMS_PRECIPTYP_DIR, 1)

        log("INFO: Not enough files for tracking, falling back to single-frame mode")
        return (
            comp_files[-1] if comp_files else None,
            None,
            ps_files[-1] if ps_files else None,
            None,
            pt_files[-1] if pt_files else None,
            None,
        )


def run_edgewarn_detection_phase(
    log,
    lat_limits,
    lon_limits,
    detection_config,
    disable_tracking=False,
    disable_polygon_expansion=False,
    input_manifest: CycleInputManifest | None = None,
):
    """Run only the realtime detection phase using already-ingested local files."""
    try:
        detection_inputs = _prepare_realtime_detection_inputs(
            log,
            input_manifest,
        )
    except Exception as exc:
        log(f"ERROR: Failed to prepare detection inputs: {exc}")
        return None

    if detection_inputs is None:
        log("ERROR: Detection inputs are unavailable")
        return None

    generated_file, _ = detect.main(
        *detection_inputs,
        lat_limits,
        lon_limits,
        detection_config,
        disable_tracking=disable_tracking,
        disable_polygon_expansion=disable_polygon_expansion,
    )
    return generated_file


def run_edgewarn_integration_phase(
    log,
    generated_file,
    remove_old_cells=None,
    disable_ctam=False,
    disable_ctam_modules=False,
    disable_stormprob=False,
    frozen_discovery=None,
    mrms_core_only=False,
    input_manifest: CycleInputManifest | None = None,
    final_input_provider=None,
):
    """Run only the integration phase from an existing detection artifact."""
    if not generated_file:
        log("WARN: No detection artifact was produced; skipping integration")
        return False

    extra = {"final_input_provider": final_input_provider} if final_input_provider is not None else {}
    integration.main(
        generated_file,
        remove_old_cells=remove_old_cells,
        disable_ctam=disable_ctam,
        disable_ctam_modules=disable_ctam_modules,
        disable_stormprob=disable_stormprob,
        frozen_discovery=frozen_discovery,
        mrms_core_only=mrms_core_only,
        input_manifest=input_manifest,
        **extra,
    )
    return True


def edgewarn_cycle_worker(
    log_queue,
    shared_state,
    detection_ready_event,
    integration_ready_event,
    dt,
    lat_limits,
    lon_limits,
    detection_config,
    profile=False,
    disable_ctam=False,
    disable_ctam_modules=False,
    disable_tracking=False,
    disable_polygon_expansion=False,
    mrms_core_only=False,
    optional_complete_event=None,
    disable_stormprob=False,
    frozen_discovery=None,
):
    """Process target for staged EdgeWARN execution within the tandem runner."""
    sys.stdout = QueueWriter(log_queue)
    sys.stderr = QueueWriter(log_queue)

    def log(message):
        log_queue.put(message)

    def publish_stage(status, *, artifacts=(), errors=()):
        shared_state["edgewarn_stage"] = {
            "status": str(status),
            "produced_artifacts": [str(path) for path in artifacts],
            "errors": [str(error) for error in errors],
        }

    if profile:
        perf_tracker.set_enabled(True)
    perf_tracker.reset()
    perf_tracker.start("Total Pipeline")

    try:
        registry_generation = shared_state.get("mrms_registry")
        if registry_generation is not None:
            fs.initialize_filesystem(
                registry_generation["base_dir"],
                config_dir=registry_generation["config_dir"],
                expected_mrms_fingerprint=registry_generation["fingerprint"],
            )
        log(f"INFO: EdgeWARN worker waiting for detection inputs for {dt}")
        detection_ready_event.wait()

        if not shared_state.get("detection_inputs_ready", False):
            message = "Detection inputs were not staged successfully"
            publish_stage("unavailable", errors=(message,))
            log(f"ERROR: {message}; skipping EdgeWARN pipeline")
            return

        input_manifest = CycleInputManifest.from_dict(
            shared_state.get("detection_manifest", shared_state.get("input_manifest"))
        )
        if input_manifest is None:
            message = "Cycle input manifest was not published"
            publish_stage("failed", errors=(message,))
            log(f"ERROR: {message}")
            return

        perf_tracker.start("Detection")
        generated_file = run_edgewarn_detection_phase(
            log,
            lat_limits,
            lon_limits,
            detection_config,
            disable_tracking=disable_tracking,
            disable_polygon_expansion=disable_polygon_expansion,
            input_manifest=input_manifest,
        )
        perf_tracker.stop("Detection")
        shared_state["edgewarn_generated_file"] = str(generated_file) if generated_file else ""
        if not generated_file or not Path(generated_file).is_file():
            message = "Detection did not produce a valid stormcell artifact"
            publish_stage("failed", errors=(message,))
            log(f"ERROR: {message}")
            return

        log("INFO: EdgeWARN detection phase complete; waiting for integration inputs")
        integration_ready_event.wait()

        if not shared_state.get("edgewarn_integration_inputs_ready", False):
            message = "EdgeWARN integration inputs were not staged successfully"
            publish_stage(
                "unavailable",
                artifacts=(generated_file,),
                errors=(message,),
            )
            log(f"ERROR: {message}; skipping integration")
            return

        input_manifest = CycleInputManifest.from_dict(
            shared_state.get("integration_manifest", shared_state.get("input_manifest"))
        )
        if input_manifest is None:
            message = "Cycle input manifest was unavailable at integration release"
            publish_stage(
                "failed",
                artifacts=(generated_file,),
                errors=(message,),
            )
            log(f"ERROR: {message}")
            return

        def final_input_provider():
            optional_complete_event.wait()
            if not shared_state.get("optional_inputs_complete", False):
                if not disable_ctam and not disable_stormprob:
                    from EdgeWARN.ctam.preflight import StormProbDependencyError
                    raise StormProbDependencyError(
                        "WARNING: Cannot continue Core: StormProb optional input acquisition "
                        "did not reach a final snapshot. Core is exiting nonzero."
                    )
                raise RuntimeError("Optional acquisition did not finish cleanly")
            return CycleInputManifest.from_dict(shared_state.get("ctam_manifest"))

        extra = ({"final_input_provider": final_input_provider}
                 if optional_complete_event is not None else {})
        perf_tracker.start("Integration")
        integrated = run_edgewarn_integration_phase(
            log,
            shared_state.get("edgewarn_generated_file") or None,
            disable_ctam=disable_ctam,
            disable_ctam_modules=disable_ctam_modules,
            disable_stormprob=disable_stormprob,
            frozen_discovery=frozen_discovery,
            mrms_core_only=mrms_core_only,
            input_manifest=input_manifest,
            **extra,
        )
        perf_tracker.stop("Integration")
        if not integrated or not Path(generated_file).is_file():
            message = "EdgeWARN integration did not validate its required artifact"
            publish_stage(
                "failed",
                artifacts=(generated_file,),
                errors=(message,),
            )
            log(f"ERROR: {message}")
            return

        publish_stage("completed", artifacts=(generated_file,))
        log("INFO: EdgeWARN worker completed successfully")
    except Exception as exc:
        from EdgeWARN.ctam.preflight import StormProbDependencyError
        if isinstance(exc, StormProbDependencyError):
            shared_state["fatal_dependency"] = str(exc)
        publish_stage("failed", errors=(str(exc),))
        log(f"ERROR: EdgeWARN tandem worker failed: {exc}")
        log(traceback.format_exc())
        raise
    finally:
        try:
            perf_tracker.stop("Total Pipeline")
        except Exception:
            pass

        if profile:
            _write_profile_summary(log)


def _find_historical_file(directory, target_dt, io_manager):
    directory_path = Path(directory)
    if not directory_path.exists():
        io_manager.write_debug(f"Dir not found: {directory}")
        return None

    primary_pattern = f"*{target_dt.strftime('%Y%m%d-%H%M')}*"
    files = sorted(directory_path.glob(primary_pattern))

    if not files:
        fallback_pattern = f"*{target_dt.strftime('%Y%m%d_%H%M')}*"
        files = sorted(directory_path.glob(fallback_pattern))
    else:
        fallback_pattern = None

    if not files:
        io_manager.write_debug(
            f"No match for {primary_pattern}"
            f"{f' or {fallback_pattern}' if fallback_pattern else ''} in {directory}"
        )
        return None

    io_manager.write_debug(f"Found {files[-1]}")
    return str(files[-1])


@protect_runtime_inputs
def historical_pipeline(
    dt,
    lat_limits,
    lon_limits,
    detection_config,
    profile=False,
    cached_objs=(None, None, None),
    io_manager=None,
    disable_ctam=False,
    disable_ctam_modules=False,
    disable_stormprob=False,
    frozen_discovery=None,
    disable_tracking=False,
    disable_polygon_expansion=False,
):
    pipeline_io = io_manager or IOManager("[HistoricalProcess]")

    try:
        if profile:
            perf_tracker.set_enabled(True)
        perf_tracker.reset()
        perf_tracker.start("Total Pipeline")

        pipeline_io.write_info(f"Starting staged ingest for timestamp {dt}")
        perf_tracker.start("Ingestion")
        _cleanup_historical_data_dirs(pipeline_io)
        cycle_state = asyncio.run(
            run_staged_ingest_cycle(
                dt,
                lambda message: pipeline_io.write_info(message),
                include_goes=False,
                include_ewmrs=False,
            )
        )
        if (getattr(cycle_state, "ingest_report", None)
                and cycle_state.edgewarn_integration_inputs_ready):
            report_path = commit_ingest_report(fs.BASE_DIR, cycle_state.ingest_report, historical=True)
            cycle_state.input_manifest = CycleInputManifest.from_dict(
                json.loads(report_path.read_text())["snapshots"]["ctam"])
        perf_tracker.stop("Ingestion")

        if not cycle_state.detection_inputs_ready:
            if not disable_ctam and not disable_stormprob:
                from EdgeWARN.ctam.preflight import StormProbDependencyError
                raise StormProbDependencyError(
                    "WARNING: Cannot continue historical Core: StormProb protected "
                    "MRMS detection inputs are unavailable. Core is exiting nonzero."
                )
            pipeline_io.write_warning("Detection inputs were not staged successfully; skipping historical pipeline")
            perf_tracker.stop("Total Pipeline")
            return None, (None, None, None)

        input_manifest = cycle_state.input_manifest
        if input_manifest is None:
            if not disable_ctam and not disable_stormprob:
                from EdgeWARN.ctam.preflight import StormProbDependencyError
                raise StormProbDependencyError(
                    "WARNING: Cannot continue historical Core: StormProb final input "
                    "snapshot is missing. Core is exiting nonzero."
                )
            pipeline_io.write_error(
                "Historical ingest did not publish an input manifest"
            )
            return None, (None, None, None)
        if (not disable_ctam and not disable_stormprob
                and not cycle_state.optional_inputs_complete):
            from EdgeWARN.ctam.preflight import StormProbDependencyError
            raise StormProbDependencyError(
                "WARNING: Cannot continue historical Core: StormProb optional input "
                "acquisition did not complete. Core is exiting nonzero."
            )

        if "mrms_integration_ingest" in cycle_state.errors or "rap_ingest" in cycle_state.errors:
            pipeline_io.write_warning(
                "Historical integration inputs are incomplete after staged ingest; "
                "detection will run but integration may be skipped"
            )

        pipeline_io.write_info("Starting Storm Cell Detection")

        perf_tracker.start("Detection")
        generated_file = run_edgewarn_detection_phase(
            pipeline_io.write_info,
            lat_limits,
            lon_limits,
            detection_config,
            disable_tracking=disable_tracking,
            disable_polygon_expansion=disable_polygon_expansion,
            input_manifest=input_manifest,
        )
        perf_tracker.stop("Detection")
        if not generated_file or not Path(generated_file).is_file():
            pipeline_io.write_error(
                "Historical detection did not produce a valid artifact"
            )
            perf_tracker.stop("Total Pipeline")
            return None, (None, None, None)

        can_integrate = (
            "mrms_integration_ingest" not in cycle_state.errors
            and "rap_ingest" not in cycle_state.errors
        )

        if can_integrate:
            pipeline_io.write_info("Starting Integration")
            perf_tracker.start("Integration")
            integrated = run_edgewarn_integration_phase(
                pipeline_io.write_info,
                generated_file,
                remove_old_cells=remove_old_cells_historical(),
                disable_ctam=disable_ctam,
                disable_ctam_modules=disable_ctam_modules,
                disable_stormprob=disable_stormprob,
                frozen_discovery=frozen_discovery,
                input_manifest=input_manifest,
            )
            perf_tracker.stop("Integration")
            if not integrated or not Path(generated_file).is_file():
                pipeline_io.write_error(
                    "Historical integration did not validate its required artifact"
                )
                perf_tracker.stop("Total Pipeline")
                return None, (None, None, None)
        else:
            if not disable_ctam and not disable_stormprob:
                from EdgeWARN.ctam.preflight import StormProbDependencyError
                raise StormProbDependencyError(
                    "WARNING: Cannot continue historical Core: StormProb required "
                    "RAP/integration inputs are unavailable. Core is exiting nonzero."
                )
            pipeline_io.write_warning(
                "Staged historical integration inputs were unavailable; "
                "the timestamp remains incomplete"
            )
            perf_tracker.stop("Total Pipeline")
            return None, (None, None, None)

        perf_tracker.stop("Total Pipeline")
        pipeline_io.write_info("Pipeline completed successfully")

        if profile:
            perf_tracker.print_summary()

        return generated_file, (None, None, None)
    except Exception as exc:
        from EdgeWARN.ctam.preflight import StormProbDependencyError
        if isinstance(exc, StormProbDependencyError):
            raise
        pipeline_io.write_error(f"Pipeline failed: {exc}")
        traceback.print_exc()
        return None, (None, None, None)
