from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import rasterio.transform
import rioxarray  # noqa: F401  Ensures xarray .rio accessor is registered.
from rasterio.enums import Resampling

from EWMRS.render.config import (
    get_file_list,
    get_goes_file_list,
    get_mrms_file_list,
)
from EWMRS.render.tools import WEB_MERCATOR_BOUNDS, configure_proj_runtime
from EWMRS.pipeline_config import (
    goes_cleanup_max_age_minutes,
    goes_cleanup_min_interval_seconds,
    gui_cleanup_max_age_minutes,
    numeric_thread_cap_value,
    numeric_thread_cap_variables,
    render_cleanup_after,
    render_phase_name,
    tile_index_cache_entries,
    worker_budget_mb,
    worker_max_workers,
    worker_psutil_fallback_max,
    worker_reserve_mb,
)
import util.file as fs
from common.ingest.manifest import CycleInputManifest
from util.atomic import atomic_write_json
from util.io import IOManager, QueueWriter

RenderOutput = Optional[list[Path]]
_CHUNK_FILENAME_RE = re.compile(r"^chunk_(\d+)_(\d+)\.f16\.gz$")

io_manager = IOManager("[Pipeline]")

WEB_MERCATOR_SHAPE = (3500, 7000)
WEB_MERCATOR_TRANSFORM = rasterio.transform.from_bounds(*WEB_MERCATOR_BOUNDS, WEB_MERCATOR_SHAPE[1], WEB_MERCATOR_SHAPE[0])

# GOES previews/renders are clipped to a CONUS-focused Web Mercator extent.
# Derived from lon/lat bounds: -125.0..-66.5, 24.5..49.5 (EPSG:4326).
GOES_WEB_MERCATOR_BOUNDS = (
    -13914936.349159198,
    2814454.7323097703,
    -7402746.137752692,
    6360130.74092142,
)
GOES_WEB_MERCATOR_SHAPE = WEB_MERCATOR_SHAPE
GOES_WEB_MERCATOR_TRANSFORM = rasterio.transform.from_bounds(
    *GOES_WEB_MERCATOR_BOUNDS,
    GOES_WEB_MERCATOR_SHAPE[1],
    GOES_WEB_MERCATOR_SHAPE[0],
)
_RUNTIME_CONFIGURED = False
_LAST_GOES_GUI_CLEANUP_S = 0.0
_LAST_GOES_GUI_CLEANUP_FUNC_ID: int | None = None


@lru_cache(maxsize=tile_index_cache_entries())
def _load_timestamp_chunk_index_cached(
    index_path_str: str,
    mtime_ns: int,
) -> tuple[list[list[int]], dict, dict] | None:
    """Cached read of a schema-versioned chunk index keyed on (path, mtime).

    The mtime is part of the cache key, so any rewrite of index.json
    invalidates the entry automatically and the next call re-reads from
    disk. ``index_path_str`` and ``mtime_ns`` are passed in to keep all
    cache key components hashable primitives.
    """
    with open(index_path_str, "r") as f:
        data = json.load(f)

    if not isinstance(data, dict) or data.get("schema_version") != 2 or data.get("representation") != "binary_chunks":
        return None
    chunks = data.get("chunks")
    tile_grid = data.get("tile_grid")
    chunk_format = data.get("chunk_format")
    if not isinstance(chunks, list) or not isinstance(tile_grid, dict) or not isinstance(chunk_format, dict):
        return None
    if chunk_format.get("encoding") != "float16" or chunk_format.get("file_suffix") != ".f16.gz" or chunk_format.get("compression") != "gzip" or chunk_format.get("bytes_per_component") != 2 or chunk_format.get("channels") not in {1, 3}:
        return None
    return chunks, tile_grid, chunk_format


def _load_timestamp_chunk_index(timestamp_dir: Path) -> tuple[list[list[int]], dict, dict] | None:
    index_file = timestamp_dir / "index.json"
    try:
        stat_result = index_file.stat()
    except FileNotFoundError:
        return None

    return _load_timestamp_chunk_index_cached(str(index_file), stat_result.st_mtime_ns)


def _ensure_dt(dt_in) -> datetime:
    if isinstance(dt_in, datetime):
        dt = dt_in
    elif isinstance(dt_in, str):
        dt = datetime.fromisoformat(dt_in)
    else:
        raise TypeError("dt must be a datetime or ISO-format string")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _configure_numerical_thread_caps() -> None:
    cap = str(numeric_thread_cap_value())
    for env_var in numeric_thread_cap_variables():
        os.environ.setdefault(env_var, cap)


def _adaptive_process_worker_count(layer_count: int, phase_name: str) -> int:
    if layer_count <= 1:
        return 1

    cpu_cap = min(layer_count, max(1, os.cpu_count() or 1), worker_max_workers())
    if cpu_cap <= 1:
        return 1

    budget_mb = worker_budget_mb(phase_name)
    reserve_mb = worker_reserve_mb()

    try:
        import psutil

        available_mb = psutil.virtual_memory().available / (1024.0 * 1024.0)
        usable_mb = max(0.0, available_mb - reserve_mb)
        memory_cap = max(1, int(usable_mb // max(1.0, budget_mb)))
        return max(1, min(cpu_cap, memory_cap))
    except Exception:
        return max(1, min(cpu_cap, worker_psutil_fallback_max()))


def _render_layer(layer) -> tuple[str, RenderOutput]:
    """Render a single layer. Returns (name, png_path or None)."""
    from EWMRS.render.render import GUIArrayRenderer, GUIValueWriter, GUILayerRenderer
    from EWMRS.render.goes_transform import (
        extract_goes_timestamp_iso,
        load_reproject_goes_abi_render_array,
    )
    from EWMRS.render.tools import TransformUtils
    from util.io import IOManager

    io_mgr = IOManager("[Pipeline]")
    _ensure_runtime_configured()

    name = layer.get("name")
    colormap_key = layer.get("colormap_key")
    source_path = layer.get("filepath")
    output_path = layer.get("outdir")
    source_type = str(layer.get("source_type", "mrms")).lower()
    pinned_input_path = layer.get("input_path")

    if source_path is None or output_path is None:
        io_mgr.write_error(f"Layer {name} is missing filepath/outdir configuration")
        return name, None

    src_dir = Path(source_path)
    out_dir = Path(output_path)

    try:
        if not src_dir.exists():
            io_mgr.write_warning(f"Source directory missing for {name}: {src_dir}")
            return name, None

        if layer.get("input_manifest_bound"):
            latest_file = (
                Path(pinned_input_path)
                if pinned_input_path is not None
                else None
            )
            if latest_file is not None and ".part" in latest_file.name.lower():
                io_mgr.write_warning(
                    f"Skipping {name}: pinned source is a partial file ({latest_file.name})"
                )
                return name, None
        else:
            latest_file = _latest_source_file(src_dir)
        if latest_file is None:
            io_mgr.write_warning(f"No source files found for {name} in {src_dir}")
            return name, None

        if source_type == "goes_abi":
            timestamp_iso = extract_goes_timestamp_iso(latest_file)
        else:
            timestamp_iso = TransformUtils.find_timestamp(str(latest_file))

        cached_render = _current_render_paths(out_dir, timestamp_iso)
        if cached_render is not None:
            io_mgr.write_info(f"Reusing existing render for {name}: {timestamp_iso}")
            return name, cached_render

        source_label = (
            "pinned source file"
            if layer.get("input_manifest_bound")
            else "latest source file"
        )
        io_mgr.write_info(f"Using {source_label} for {name}: {latest_file}")

        if source_type == "goes_abi":
            payload = load_reproject_goes_abi_render_array(
                latest_file,
                layer,
                shape=GOES_WEB_MERCATOR_SHAPE,
                transform=GOES_WEB_MERCATOR_TRANSFORM,
            )
            if payload is None:
                io_mgr.write_error(f"Failed to reproject GOES ABI dataset for {latest_file}")
                return name, None

            io_mgr.write_info(f"Reprojected {name} GOES ABI fixed grid to EPSG:3857 (CONUS clip)")
            renderer = GUIArrayRenderer(payload["data"], out_dir, colormap_key, name, timestamp_iso)
            png_path, _px_timestamp = renderer.convert_to_png(tile_output=True)
            return name, png_path
        else:
            ds = TransformUtils.load_ds(latest_file)
            if ds is None:
                io_mgr.write_error(f"Failed to load dataset for {latest_file}")
                return name, None

            if "MergedAzShear" in name and ds.latitude.values.shape[0] > 3510:
                ds = ds.coarsen(latitude=2, longitude=2, boundary="trim", coord_func="mean").reduce(np.max)
                io_mgr.write_info(f"Downsampled {name} to 0.01 deg grid")

            if "latitude" in ds.coords and "longitude" in ds.coords:
                ds.rio.write_crs("EPSG:4326", inplace=True)
                ds = ds.rio.reproject(
                    "EPSG:3857",
                    shape=WEB_MERCATOR_SHAPE,
                    transform=WEB_MERCATOR_TRANSFORM,
                    resampling=Resampling.nearest,
                )
                io_mgr.write_info(f"Reprojected {name} to EPSG:3857 (Crisp nearest-neighbor, Precise bounds)")

        renderer = GUILayerRenderer(ds, out_dir, colormap_key, name, timestamp_iso)
        png_path, _px_timestamp = renderer.convert_to_png(tile_output=True)

        return name, png_path

    except Exception as exc:
        io_mgr.write_error(f"Error processing layer {name}: {exc}")
        return name, None


def _ensure_runtime_configured() -> None:
    global _RUNTIME_CONFIGURED
    _configure_numerical_thread_caps()
    if not _RUNTIME_CONFIGURED:
        configure_proj_runtime()
        _RUNTIME_CONFIGURED = True


def _worker_initializer() -> None:
    _ensure_runtime_configured()


def _latest_source_file(src_dir: Path) -> Optional[Path]:
    latest = fs.latest_files(src_dir, 1)
    if not latest:
        return None
    return Path(latest[-1])


def _current_render_paths(out_dir: Path, timestamp_iso: str) -> RenderOutput:
    try:
        timestamp = _normalize_render_timestamp(timestamp_iso)
        timestamp_dir = out_dir / timestamp
        chunk_dir = timestamp_dir / "chunks"
        if not chunk_dir.is_dir():
            return None

        index_file = out_dir / "index.json"
        tile_grid = None
        if index_file.exists():
            with open(index_file, "r") as f:
                data = json.load(f)

            if not isinstance(data, dict) or data.get("schema_version") != 2 or data.get("representation") != "binary_chunks":
                return None
            timestamps = data.get("timestamps", [])
            if timestamp not in timestamps:
                return None
            if not isinstance(data, list):
                tile_grid = data.get("tile_grid")

        timestamp_index = _load_timestamp_chunk_index(timestamp_dir)
        if timestamp_index is None:
            return None

        indexed_tiles, timestamp_tile_grid, chunk_format = timestamp_index
        if timestamp_tile_grid is not None:
            tile_grid = timestamp_tile_grid

        tile_paths: list[tuple[int, int, Path]] = []
        for tile in indexed_tiles:
            if not isinstance(tile, list) or len(tile) != 2:
                return None

            tile_x, tile_y = tile
            if not isinstance(tile_x, int) or not isinstance(tile_y, int):
                return None

            if tile_grid is not None:
                rows = tile_grid.get("rows")
                cols = tile_grid.get("cols")
                if isinstance(rows, int) and isinstance(cols, int):
                    if tile_x < 0 or tile_x >= cols or tile_y < 0 or tile_y >= rows:
                        return None

            channels = chunk_format.get("channels")
            if not isinstance(channels, int) or channels not in {1, 3}:
                return None
            tile_path = chunk_dir / f"chunk_{tile_x}_{tile_y}.f16.gz"
            if not tile_path.is_file() or tile_path.stat().st_size <= 0:
                return None

            tile_paths.append((tile_y, tile_x, tile_path))

        tile_paths.sort(key=lambda item: (item[0], item[1]))
        return [path for _, _, path in tile_paths]
    except Exception:
        return None
def _normalize_render_timestamp(timestamp_iso: str) -> str:
    dt = datetime.fromisoformat(timestamp_iso)
    return dt.strftime(r"%Y%m%d-%H%M00")


def cleanup_old_gui_files(max_age_minutes: int | None = None):
    """Remove old files/folders from GUI output directories."""
    import shutil

    if max_age_minutes is None:
        max_age_minutes = gui_cleanup_max_age_minutes()

    now = time.time()
    max_age_seconds = max_age_minutes * 60
    total_removed = 0

    candidate_dirs = []
    for layer in get_file_list():
        output_path = layer.get("outdir")
        if output_path is None:
            continue

        candidate_dirs.append(Path(output_path))

    for out_dir in candidate_dirs:
        if not out_dir.exists():
            continue

        existing_timestamps = set()

        for png_file in out_dir.glob("*.png"):
            try:
                file_age = now - png_file.stat().st_mtime
                if file_age > max_age_seconds:
                    png_file.unlink()
                    total_removed += 1
                else:
                    stem = png_file.stem
                    if "_" in stem:
                        existing_timestamps.add(stem.split("_")[-1])
            except Exception as exc:
                io_manager.write_warning(f"Failed to process {png_file}: {exc}")

        for ts_folder in out_dir.iterdir():
            if ts_folder.is_dir() and not ts_folder.name.startswith("."):
                try:
                    folder_age = now - ts_folder.stat().st_mtime
                    if folder_age > max_age_seconds:
                        shutil.rmtree(ts_folder)
                        total_removed += 1
                        io_manager.write_debug(f"Removed old timestamp folder: {ts_folder}")
                    else:
                        existing_timestamps.add(ts_folder.name)
                except Exception as exc:
                    io_manager.write_warning(f"Failed to process folder {ts_folder}: {exc}")

        index_file = out_dir / "index.json"
        if index_file.exists():
            try:
                with open(index_file, "r") as f:
                    data = json.load(f)

                if isinstance(data, list):
                    timestamps = data
                    tile_grid = None
                else:
                    timestamps = data.get("timestamps", [])
                    tile_grid = data.get("tile_grid")

                timestamps = [ts for ts in timestamps if ts in existing_timestamps]

                if isinstance(data, dict) and data.get("schema_version") == 2 and data.get("representation") == "binary_chunks":
                    output_data = {**data, "timestamps": timestamps}
                else:
                    output_data = {"timestamps": timestamps, "tile_grid": tile_grid} if tile_grid is not None else timestamps
                atomic_write_json(index_file, output_data)
            except Exception as exc:
                io_manager.write_warning(f"Failed to update index.json in {out_dir}: {exc}")

    if total_removed > 0:
        io_manager.write_info(f"Cleaned up {total_removed} old GUI files/folders (>{max_age_minutes} min)")


def run_render_pipeline(
    dt,
    max_entries: int | None = None,
    layers=None,
    phase_name: str | None = None,
    cleanup_after: bool | None = None,
    input_manifest: CycleInputManifest | None = None,
) -> Dict[str, RenderOutput]:
    """Render configured EWMRS layers from already staged local files.

    ``max_entries`` is accepted and ignored. It is part of the GOES render task
    tuple that ``util.runtime.cycle`` queues and ``util.runtime.background``
    unpacks, so the parameter has to stay, but nothing downstream of here reads
    it: layer selection comes from the catalogs, not a count. Its only owner is
    ``runtime.yaml goes_coordination.render_task_max_entries``, which is why
    ``ewmrs_pipeline.yaml`` does not carry a second copy.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    if phase_name is None:
        phase_name = render_phase_name()
    if cleanup_after is None:
        cleanup_after = render_cleanup_after()

    dt = _ensure_dt(dt)
    results: Dict[str, RenderOutput] = {}

    layers = get_file_list() if layers is None else list(layers)
    if input_manifest is not None:
        pinned_layers = []
        for layer in layers:
            pinned_layer = dict(layer)
            source_path = pinned_layer.get("filepath")
            if source_path is not None:
                record = input_manifest.latest_for_directory(source_path)
                pinned_layer["input_path"] = (
                    str(record.local_path) if record is not None else None
                )
                pinned_layer["input_manifest_bound"] = True
            pinned_layers.append(pinned_layer)
        layers = pinned_layers
    if not layers:
        io_manager.write_info(f"{phase_name} render phase has no configured layers")
        return results

    max_workers = _adaptive_process_worker_count(len(layers), phase_name)
    io_manager.write_info(
        f"Rendering {len(layers)} {phase_name} layers across {max_workers} CPU cores for {dt.isoformat()}..."
    )
    rendered_layers: list[str] = []
    failed_layers: list[str] = []
    with ProcessPoolExecutor(max_workers=max_workers, initializer=_worker_initializer) as executor:
        futures = {executor.submit(_render_layer, layer): layer for layer in layers}
        for future in as_completed(futures):
            name, png_path = future.result()
            results[name] = png_path
            if png_path:
                rendered_layers.append(name)
                io_manager.write_info(f"Rendered layer: {name}")
            else:
                failed_layers.append(name)

    io_manager.write_info(
        f"{phase_name} rendered layers: {', '.join(rendered_layers) if rendered_layers else 'none'}"
    )
    if failed_layers:
        io_manager.write_warning(f"{phase_name} failed layers: {', '.join(failed_layers)}")

    if cleanup_after:
        cleanup_old_gui_files(max_age_minutes=gui_cleanup_max_age_minutes())
    return results


def mrms_required_layer_failures(results) -> tuple[list[str], list[str]]:
    """Split rendered MRMS results into failed required and optional layers.

    Only required layers gate an EWMRS stage; optional-layer failures are
    surfaced as warnings but never fail a cycle.
    """
    required_layers = {
        layer["name"]
        for layer in get_mrms_file_list()
        if layer.get("required", True)
    }
    failed_required = sorted(
        str(layer_name)
        for layer_name, output in results.items()
        if output is None and str(layer_name) in required_layers
    )
    failed_optional = sorted(
        str(layer_name)
        for layer_name, output in results.items()
        if output is None and str(layer_name) not in required_layers
    )
    return failed_required, failed_optional


def run_mrms_render_pipeline(
    dt,
    max_entries: int | None = None,
    input_manifest: CycleInputManifest | None = None,
) -> Dict[str, RenderOutput]:
    """Run the MRMS-backed EWMRS render phase."""
    return run_render_pipeline(
        dt,
        max_entries=max_entries,
        layers=get_mrms_file_list(),
        phase_name="MRMS",
        input_manifest=input_manifest,
    )


def _maybe_cleanup_goes_gui_files(max_age_minutes: int | None = None) -> None:
    global _LAST_GOES_GUI_CLEANUP_S, _LAST_GOES_GUI_CLEANUP_FUNC_ID

    if max_age_minutes is None:
        max_age_minutes = goes_cleanup_max_age_minutes()

    min_interval_s = goes_cleanup_min_interval_seconds()
    now_s = time.perf_counter()
    current_cleanup_func_id = id(cleanup_old_gui_files)
    if (
        _LAST_GOES_GUI_CLEANUP_FUNC_ID == current_cleanup_func_id
        and min_interval_s > 0
        and (now_s - _LAST_GOES_GUI_CLEANUP_S) < min_interval_s
    ):
        io_manager.write_debug(
            f"Skipping GOES GUI cleanup: last run was {(now_s - _LAST_GOES_GUI_CLEANUP_S):.1f}s ago"
        )
        return

    cleanup_old_gui_files(max_age_minutes=max_age_minutes)
    _LAST_GOES_GUI_CLEANUP_S = now_s
    _LAST_GOES_GUI_CLEANUP_FUNC_ID = current_cleanup_func_id


def run_goes_render_pipeline(
    dt,
    max_entries: int | None = None,
    input_manifest: CycleInputManifest | None = None,
) -> Dict[str, RenderOutput]:
    """Run the GOES-backed EWMRS render phase for raw ABI channels.

    EWMRS serves the raw ABI channel values; GoES RGB composites are a
    client-side derivation and are not rendered server-side.
    """
    layers = get_goes_file_list()
    if not layers:
        io_manager.write_info("GOES render phase is a no-op: no GOES layers configured")
        return {}

    pipeline_start_s = time.perf_counter()
    results = run_render_pipeline(
        dt,
        max_entries=max_entries,
        layers=layers,
        phase_name="GOES",
        cleanup_after=False,
        input_manifest=input_manifest,
    )
    _maybe_cleanup_goes_gui_files()

    io_manager.write_info(f"GOES render pipeline completed in {time.perf_counter() - pipeline_start_s:.3f}s")

    return results
def run_rap_uint16_pipeline(rap_file, dt=None):
    """Run the EWMRS RAP Uint16Array conversion pipeline for one RAP GRIB2 file."""
    from EWMRS.rap.uint16_pipeline import run_rap_uint16_pipeline as _run_rap_uint16_pipeline

    return _run_rap_uint16_pipeline(rap_file, dt=dt)


def _summarize_results(results: Dict[str, RenderOutput]) -> str:
    successful_layers = sum(1 for output_path in results.values() if output_path is not None)
    total_layers = len(results)
    return f"{successful_layers}/{total_layers} layers succeeded"


def ewmrs_goes_worker(
    log_queue,
    dt,
    max_entries: int | None = None,
    input_manifest=None,
):
    """Process target for decoupled GOES rendering outside tandem completion."""
    sys.stdout = QueueWriter(log_queue)
    sys.stderr = QueueWriter(log_queue)

    def log(msg: str):
        log_queue.put(str(msg))

    try:
        if isinstance(input_manifest, dict):
            input_manifest = CycleInputManifest.from_dict(input_manifest)
        if input_manifest is None:
            log("ERROR: EWMRS GOES render skipped because no pinned input manifest was provided")
            return
        log(f"INFO: Starting EWMRS GOES render phase for {dt}")
        results = run_goes_render_pipeline(
            dt,
            max_entries=max_entries,
            input_manifest=input_manifest,
        )
        log(f"INFO: EWMRS GOES render completed: {_summarize_results(results)}")
    except Exception as exc:
        log(f"ERROR: EWMRS GOES worker failed - {exc}")
