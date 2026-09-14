import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import time

import common.ingest.mrms.config as mrms_config
from EdgeWARN import historical_pipeline, initialize_runtime, parse_utc_time
from EdgeWARN.api_integration.config import initialize_at_startup_historical
from EdgeWARN.historical_config import (
    historical_step_minutes,
    historical_throttle_seconds,
)
from EdgeWARN.process.detect.config import DetectionConfig
from EdgeWARN.schedule.scheduler import MRMSUpdateChecker
from util.io import TimestampedOutput, IOManager

sys.stdout = TimestampedOutput(sys.stdout)
sys.stderr = TimestampedOutput(sys.stderr)

io_manager = IOManager("[HistoricalProcess]")


def _validated_historical_output(path: Path, requested_time: datetime) -> bool:
    """Confirm this iteration produced an artifact for its own scan minute."""
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp = payload.get("latest_timestamp")
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        expected = requested_time.astimezone(timezone.utc).replace(second=0, microsecond=0)
        return observed.astimezone(timezone.utc).replace(second=0, microsecond=0) == expected
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return False

def main():
    """Historical scheduler: iterate through time range and process each available timestamp."""
    args = io_manager.get_historical_args()

    # Initialize custom filesystem if provided
    initialize_runtime(
        base_dir=args.base_dir,
        io_manager=io_manager,
        initialize_indexes=initialize_at_startup_historical(),
    )

    try:
        start_time = parse_utc_time(args.start)
        end_time = parse_utc_time(args.end)
    except ValueError as e:
        io_manager.write_error(f"Invalid timestamp format: {e}")
        return

    lat_limits = tuple(args.lat)
    lon_limits = tuple(args.lon)

    # Initialize scheduler
    checker = MRMSUpdateChecker(verbose=True)
    
    current_time = start_time
    last_processed_timestamp = None  # Track the actual data timestamp that was processed
    
    io_manager.write_info(f"Starting historical processing from {start_time} to {end_time}")
    if args.disable_ctam:
        io_manager.write_info("CTAM execution disabled via --disable-ctam")
    elif getattr(args, "disable_ctam_modules", False):
        io_manager.write_info("External CTAM modules disabled; built-in StormProb remains enabled")
    if args.disable_tracking:
        io_manager.write_info("Tracking disabled via --disable-tracking")
    if args.disable_polygon_expansion:
        io_manager.write_info("Polygon expansion disabled via --disable-polygon-expansion; using original ProbSevere polygons")
    io_manager.write_info(
        "Detection thresholds: "
        f"disable_polygon_expansion={args.disable_polygon_expansion}, "
        f"refl_threshold={args.refl_threshold}, "
        f"min_seed_percentage={args.min_seed_percentage}, "
        f"drop_offset={args.drop_offset}"
    )

    detection_config = DetectionConfig.from_yaml(
        config_dir=args.config_dir,
        refl_threshold=args.refl_threshold,
        min_seed_percentage=args.min_seed_percentage,
        drop_offset=args.drop_offset,
    )

    step = timedelta(minutes=historical_step_minutes())
    throttle_seconds = historical_throttle_seconds()

    while current_time <= end_time:
        io_manager.write_info(f"\n{'='*60}")
        io_manager.write_info(f"Checking for data near: {current_time.isoformat()}")
        
        # Check modifiers dynamically
        check_modifiers = mrms_config.get_check_modifiers()
        
        # Find latest common timestamp on S3 within 1 hour of current_time
        latest_common = checker.latest_common_minute_1h(check_modifiers, reference_dt=current_time)
        
        if latest_common is None:
            io_manager.write_warning(f"No common timestamp found near {current_time}")
            current_time += step
            continue
        
        # Check if this is the same timestamp we already processed
        if latest_common == last_processed_timestamp:
            io_manager.write_info(f"Timestamp {latest_common} already processed, skipping")
            current_time += step
            continue
        
        io_manager.write_info(f"Processing timestamp: {latest_common.isoformat()}")
        
        # Run the pipeline
        try:
            result = historical_pipeline(
                latest_common,
                lat_limits,
                lon_limits,
                profile=args.profile,
                io_manager=io_manager,
                disable_ctam=args.disable_ctam,
                disable_ctam_modules=getattr(args, "disable_ctam_modules", False),
                disable_tracking=args.disable_tracking,
                disable_polygon_expansion=args.disable_polygon_expansion,
                detection_config=detection_config,
            )

            generated_file = result[0] if isinstance(result, tuple) else result
            generated_path = Path(generated_file) if generated_file else None
            if generated_path is None or not _validated_historical_output(
                generated_path, latest_common
            ):
                raise RuntimeError(
                    "Historical pipeline did not produce a validated artifact for its requested timestamp"
                )

            last_processed_timestamp = latest_common
            io_manager.write_info(f"✓ Output saved to {generated_path}")
                
        except Exception as e:
            io_manager.write_error(f"Pipeline failed for {latest_common}: {e}")
            io_manager.write_warning(
                f"Timestamp {latest_common} remains unprocessed and may be retried"
            )

        current_time += step

        # Only iterations that reached the pipeline are throttled; the two early
        # `continue` branches above skip this.
        time.sleep(throttle_seconds)
    
    io_manager.write_info(f"\n{'='*60}")
    io_manager.write_info("Historical processing complete.")

if __name__ == "__main__":
    main()
