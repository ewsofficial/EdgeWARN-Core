"""Main entry point for WPC Surface Analysis ingestion."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

import util.file as fs
from common.ingest.wpc.config import (
    cleanup_glob,
    cleanup_max_age_minutes,
    previous_analysis_lookback_hours,
)
from common.ingest.wpc.parser import parse_coded_surface
from common.ingest.wpc.converter import parsed_to_geojson, save_geojson
from common.ingest.wpc.downloader import (
    download_coded_surface,
    get_output_filepath,
    get_latest_output_filepath
)
from util.io import IOManager

io_manager = IOManager("[WPC]")


def fetch_surface_analysis(dt: Optional[datetime] = None, save_timestamped: bool = False) -> Optional[Dict]:
    if dt is None:
        dt = datetime.now(timezone.utc)

    io_manager.write_info("Starting WPC surface analysis fetch...")
    result = download_coded_surface(dt)

    if result is None:
        io_manager.write_error("Failed to download WPC surface analysis")
        return None

    content, actual_time = result
    io_manager.write_info(f"Using analysis time: {actual_time.isoformat()}")

    io_manager.write_info("Parsing coded surface data...")
    try:
        parsed = parse_coded_surface(content)
    except Exception as e:
        io_manager.write_error(f"Failed to parse surface data: {e}")
        return None

    io_manager.write_info("Converting to GeoJSON...")
    geojson = parsed_to_geojson(parsed, actual_time)

    num_fronts = sum(1 for f in geojson["features"] if f["geometry"]["type"] == "LineString")
    num_centers = sum(1 for f in geojson["features"] if f["geometry"]["type"] == "Point")
    io_manager.write_info(f"Converted: {num_fronts} fronts/troughs, {num_centers} pressure centers")

    latest_path = get_latest_output_filepath()
    save_geojson(geojson, str(latest_path))
    io_manager.write_info(f"Saved latest to: {latest_path}")

    if save_timestamped:
        ts_path = get_output_filepath(actual_time)
        save_geojson(geojson, str(ts_path))
        io_manager.write_info(f"Saved timestamped copy to: {ts_path}")

    return geojson


def run_wpc_ingest(log_queue=None):
    import sys
    from util.io import QueueWriter

    if log_queue is not None:
        sys.stdout = QueueWriter(log_queue)
        sys.stderr = QueueWriter(log_queue)

    try:
        dt_now = datetime.now(timezone.utc)
        dt_prev = dt_now - timedelta(hours=previous_analysis_lookback_hours())
        fetch_surface_analysis(dt_prev, save_timestamped=True)
        result = fetch_surface_analysis(dt_now, save_timestamped=True)

        clean_old_files()

        if result:
            io_manager.write_info("WPC surface analysis ingest completed successfully")
        else:
            io_manager.write_error("WPC surface analysis ingest failed")
    except Exception as e:
        io_manager.write_error(f"WPC ingest error: {e}")


def clean_old_files(max_age_minutes: Optional[int] = None):
    import time
    import os

    sfc_dir = fs.WPC_SFC_DIR
    if not sfc_dir.exists():
        return

    if max_age_minutes is None:
        max_age_minutes = cleanup_max_age_minutes()

    now = time.time()
    max_age_seconds = max_age_minutes * 60
    removed = 0

    for f in sfc_dir.glob(cleanup_glob()):
        try:
            age = now - f.stat().st_mtime
            if age > max_age_seconds:
                os.remove(str(f))
                removed += 1
        except Exception:
            pass

    if removed > 0:
        io_manager.write_info(f"Cleaned up {removed} old WPC files")


if __name__ == "__main__":
    result = fetch_surface_analysis(save_timestamped=True)
    if result:
        print(json.dumps(result, indent=2))
