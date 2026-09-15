from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Tuple, List
import json
import os
import time
import re
import threading
import numpy as np
from .tools import TransformUtils
from .tiler import save_float16_chunk
from common.config.overlay import resolve
from EWMRS.pipeline_config import TILE_THREADS_ENV, max_tile_threads
import util.file as fs
from util.atomic import atomic_write_json
from xarray import Dataset
from util.io import IOManager
from datetime import datetime

io_manager = IOManager("[Transform]")
_CHUNK_FILENAME_RE = re.compile(r"^chunk_(\d+)_(\d+)\.f16\.gz$")


def _scalar_data_to_rgba(
    data: np.ndarray,
    thresholds: np.ndarray,
    colors: np.ndarray,
    colors_uint8: np.ndarray,
    interpolate: bool,
) -> np.ndarray:
    flat_data = np.asarray(data, dtype=np.float32).ravel()

    rgba_flat = np.zeros((flat_data.shape[0], 4), dtype=np.uint8)
    valid_mask = np.isfinite(flat_data)
    safe_data = np.where(valid_mask, flat_data, thresholds[0])

    if interpolate:
        safe_data = np.clip(safe_data, thresholds[0], thresholds[-1])
        rgba_flat[:, 0] = np.interp(safe_data, thresholds, colors[:, 0]).astype(np.uint8)
        rgba_flat[:, 1] = np.interp(safe_data, thresholds, colors[:, 1]).astype(np.uint8)
        rgba_flat[:, 2] = np.interp(safe_data, thresholds, colors[:, 2]).astype(np.uint8)
        rgba_flat[:, 3] = np.interp(safe_data, thresholds, colors[:, 3]).astype(np.uint8)
        below_min_mask = valid_mask & (flat_data < thresholds[0])
        rgba_flat[below_min_mask] = 0
    else:
        indices = np.digitize(safe_data, thresholds) - 1
        indices = np.clip(indices, 0, len(colors_uint8) - 1)
        rgba_flat[:, :4] = colors_uint8[indices]

    rgba_flat[~valid_mask] = 0
    return rgba_flat.reshape((data.shape[0], data.shape[1], 4))


def _resolve_tile_workers(tile_count: int) -> int:
    if tile_count <= 1:
        return 1

    # An explicit env cap bypasses the CPU cap; the catalog value does not. That
    # asymmetry is deliberate and predates the extraction -- setting the variable
    # is how an operator overrides what the machine looks like. `yaml_value=None`
    # is what expresses it: a non-None answer can only have come from the
    # environment, since the catalog value is applied by the fallback below.
    try:
        env_cap = resolve(
            None,
            env_names=(TILE_THREADS_ENV,),
            yaml_value=None,
            value_type=int,
            key="ewmrs_pipeline.render_threads.max_tile_threads",
        )
    except ValueError as exc:
        # The only overlay site that swallows a malformed override, because it is
        # the only one with a working fallback -- the CPU cap below still yields a
        # usable pool. Failing the render over a typo in a thread hint would be a
        # worse trade. `minimum=1` is likewise not passed: the clamp below has
        # always accepted "0" as 1 rather than rejecting it.
        #
        # Warned rather than dropped silently: `resolve` records an origin only
        # after coercing, so a rejected override leaves this key absent from
        # `overlay.overrides()` entirely. Without this line an operator who
        # mistyped the variable would see neither the effect they wanted nor any
        # indication of why.
        io_manager.write_warning(f"Ignoring {TILE_THREADS_ENV}: {exc}")
        env_cap = None
    if env_cap is not None:
        return min(tile_count, max(1, env_cap))

    cpu_cap = max(1, os.cpu_count() or 1)
    return min(tile_count, max_tile_threads(), cpu_cap)


def _normalize_tile_grid(tile_grid: dict | None) -> dict | None:
    if not isinstance(tile_grid, dict):
        return None

    rows = tile_grid.get("rows")
    cols = tile_grid.get("cols")
    tile_size = tile_grid.get("tile_size")
    if not isinstance(rows, int) or not isinstance(cols, int) or not isinstance(tile_size, int):
        return None

    return {"rows": rows, "cols": cols, "tile_size": tile_size}


class GUIValueWriter:
    def __init__(self, outdir: Path, file_name: str, timestamp):
        self.outdir = outdir
        self.file_name = file_name
        self.timestamp = timestamp

    def save_values(
        self,
        values: np.ndarray,
        tile_output: bool = True,
        *,
        timing_context: dict | None = None,
    ) -> Tuple[List[Path], str]:
        from .config import tile_size

        render_start_s = time.perf_counter()
        dt = self._coerce_timestamp(self.timestamp)
        timestamp = dt.strftime(r"%Y%m%d-%H%M00")
        self.outdir.mkdir(parents=True, exist_ok=True)

        if tile_output:
            chunk_size = tile_size()
            values = np.asarray(values, dtype=np.float32)
            if values.ndim != 2:
                raise ValueError("Rendered values must be scalar [height,width]")
            if values.shape[0] % chunk_size or values.shape[1] % chunk_size:
                raise ValueError(
                    f"Rendered value dimensions {values.shape[:2]} are not divisible by chunk size {chunk_size}"
                )
            rows = values.shape[0] // chunk_size
            cols = values.shape[1] // chunk_size
            tile_grid = {"rows": rows, "cols": cols, "tile_size": chunk_size}
            artifact_paths = self._save_chunks_from_array(
                values,
                timestamp,
                tile_grid=tile_grid,
                timing_context=timing_context,
            )
            self._update_index(timestamp, tile_grid=tile_grid)
            total_render_s = time.perf_counter() - render_start_s
            io_manager.write_info(
                f"Render output for {self.file_name} completed in {total_render_s:.3f}s "
                f"({len(artifact_paths)} chunks, timestamp={timestamp})"
            )
            io_manager.write_debug(f"Saved {len(artifact_paths)} float16 value chunks for {self.file_name} at {timestamp}")
            return artifact_paths, timestamp

        raise ValueError("EWMRS no longer writes flat PNG artifacts; use tiled float16 value chunks")

    def _coerce_timestamp(self, timestamp) -> datetime:
        try:
            return datetime.fromisoformat(timestamp)
        except ValueError:
            cleaned_ts = TransformUtils.find_timestamp(timestamp)
            return datetime.fromisoformat(cleaned_ts)

    def _save_chunks_from_array(
        self,
        values: np.ndarray,
        timestamp: str,
        *,
        tile_grid: dict,
        timing_context: dict | None = None,
    ) -> List[Path]:
        tile_schedule_start_s = time.perf_counter()
        grid_cols = tile_grid["cols"]
        grid_rows = tile_grid["rows"]
        tile_size = tile_grid["tile_size"]

        timestamp_dir = self.outdir / timestamp
        chunk_dir = timestamp_dir / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        tile_specs = []
        total_grid_tiles = grid_rows * grid_cols
        for tile_y in range(grid_rows):
            for tile_x in range(grid_cols):
                left = tile_x * tile_size
                right = left + tile_size
                top = (grid_rows - 1 - tile_y) * tile_size
                bottom = top + tile_size
                chunk_path = chunk_dir / f"chunk_{tile_x}_{tile_y}.f16.gz"
                chunk_data = np.ascontiguousarray(values[top:bottom, left:right], dtype=np.float16)
                if np.any(np.isfinite(chunk_data)):
                    tile_specs.append((chunk_data, chunk_path))

        tile_schedule_s = time.perf_counter() - tile_schedule_start_s
        io_manager.write_info(
            f"Prepared chunk schedule for {self.file_name} in {tile_schedule_s:.3f}s "
            f"({len(tile_specs)}/{total_grid_tiles} non-transparent chunks)"
        )

        max_workers = _resolve_tile_workers(len(tile_specs))
        tile_write_start_s = time.perf_counter()
        first_tile_lock = threading.Lock()
        first_tile_logged = False

        def _write_chunk(spec):
            nonlocal first_tile_logged
            chunk_data, chunk_path = spec
            save_float16_chunk(chunk_data, chunk_path)
            completed_s = time.perf_counter()
            with first_tile_lock:
                if not first_tile_logged:
                    first_tile_logged = True
                    first_tile_latency_s = completed_s - tile_write_start_s
                    if timing_context is not None:
                        timing_context["chunk_schedule_s"] = tile_schedule_s
                        timing_context["first_chunk_latency_s"] = first_tile_latency_s
                        render_start_s = timing_context.get("render_start_s")
                        if render_start_s is not None:
                            timing_context["render_start_to_first_tile_s"] = completed_s - float(render_start_s)
                    render_start_s = None if timing_context is None else timing_context.get("render_start_s")
                    if render_start_s is not None:
                        render_to_first_tile_s = completed_s - float(render_start_s)
                        io_manager.write_info(
                            f"First chunk written for {self.file_name}: {chunk_path} "
                            f"({first_tile_latency_s:.3f}s chunk-write latency, "
                            f"{render_to_first_tile_s:.3f}s from render start)"
                        )
                    else:
                        io_manager.write_info(
                            f"First chunk written for {self.file_name}: {chunk_path} "
                            f"({first_tile_latency_s:.3f}s chunk-write latency)"
                        )

        if tile_specs:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(_write_chunk, tile_specs))

        tile_write_s = time.perf_counter() - tile_write_start_s
        if timing_context is not None:
            timing_context["chunk_write_s"] = tile_write_s
        io_manager.write_info(
            f"Chunk writes for {self.file_name} completed in {tile_write_s:.3f}s using {max_workers} worker(s) "
            f"({len(tile_specs)}/{total_grid_tiles} non-transparent chunks written)"
        )

        self._write_timestamp_index(timestamp_dir, tile_specs, tile_grid)
        # The index is the publication barrier.  Only remove obsolete chunks
        # after readers can discover the complete replacement set.
        published = {path.name for _, path in tile_specs}
        for existing in chunk_dir.iterdir():
            if existing.is_file() and _CHUNK_FILENAME_RE.fullmatch(existing.name) and existing.name not in published:
                existing.unlink()

        return [tile_path for _, tile_path in tile_specs]

    def _write_timestamp_index(self, timestamp_dir: Path, tile_specs: list[tuple[np.ndarray, Path]], tile_grid: dict) -> None:
        from .config import chunk_format_descriptor, chunk_schema_version
        normalized_tile_grid = _normalize_tile_grid(tile_grid)
        chunks: list[list[int]] = []
        for _, tile_path in tile_specs:
            match = _CHUNK_FILENAME_RE.fullmatch(tile_path.name)
            if match is None:
                continue
            chunks.append([int(match.group(1)), int(match.group(2))])

        chunks.sort(key=lambda item: (item[1], item[0]))
        output_data = {
            "schema_version": chunk_schema_version(),
            "timestamp": timestamp_dir.name,
            "representation": "binary_chunks",
            "chunk_format": chunk_format_descriptor(),
            "tile_grid": normalized_tile_grid,
            "chunks": chunks,
        }

        index_file = timestamp_dir / "index.json"
        try:
            atomic_write_json(index_file, output_data)
        except Exception as e:
            io_manager.write_error(f"Failed to update index.json in {timestamp_dir}: {e}")

    def _update_index(self, new_timestamp, tile_grid=None):
        from .config import chunk_format_descriptor, chunk_schema_version
        index_file = self.outdir / "index.json"
        timestamps = []
        existing_tile_grid = None

        if index_file.exists():
            try:
                with open(index_file, 'r') as f:
                    data = json.load(f)

                if isinstance(data, list):
                    timestamps = data
                else:
                    timestamps = data.get("timestamps", [])
                    existing_tile_grid = data.get("tile_grid")
            except Exception as e:
                io_manager.write_warning(f"Failed to read index.json in {self.outdir}: {e}. Creating new one.")

        if new_timestamp not in timestamps:
            timestamps.append(new_timestamp)
            timestamps.sort(reverse=True)

            try:
                output_data = {
                    "schema_version": chunk_schema_version(),
                    "timestamps": timestamps,
                    "representation": "binary_chunks",
                    "chunk_format": chunk_format_descriptor(include_media_type=True),
                    "tile_grid": tile_grid or existing_tile_grid,
                }

                atomic_write_json(index_file, output_data)
            except Exception as e:
                io_manager.write_error(f"Failed to update index.json in {self.outdir}: {e}")

class GUILayerRenderer:
    def __init__(self, dataset: Dataset, outdir: Path, colormap_key, file_name, timestamp):
        """
        Args:
            filepath (xr.Dataset): Dataset being converted to GUI png
            outdir (Path): Output directory of the converted png file
            colormap_key (str): Retained for renderer-call compatibility; values are stored raw
            file_name (str): Key of .png file name
            timestamp (str): ISO formatted timestamp string or string to parse
        """
        self.ds = dataset
        self.outdir = outdir
        self.colormap_key = colormap_key
        self.file_name = file_name
        self.timestamp = timestamp

    def _update_index(self, new_timestamp, tile_grid=None):
        writer = GUIValueWriter(self.outdir, self.file_name, self.timestamp)
        writer._update_index(new_timestamp, tile_grid=tile_grid)

    def convert_to_png(self, tile_output: bool = True, *, timing_context: dict | None = None) -> Tuple[List[Path], str]:
        """
        Converts dataset to tiled PNG files or a single PNG file.
        
        Args:
            tile_output: If True, output tiles in timestamp subdirectory.
                        If False, output single PNG file (backward compatibility).
        
        Returns:
            Tuple of (list of tile paths or [single png path], timestamp string).
        """
        # Step 1: No Reprojection needed for 1km/pixel raw render
        # We will resize the output image based on physical domain size later
        data = self.ds['unknown'].values

        render_start_s = time.perf_counter()
        values = np.asarray(data, dtype=np.float32)
        scalar_to_rgba_s = time.perf_counter() - render_start_s
        if timing_context is not None:
            timing_context["scalar_to_rgba_s"] = scalar_to_rgba_s
        io_manager.write_info(f"Value preparation for {self.file_name} completed in {scalar_to_rgba_s:.3f}s")

        writer = GUIValueWriter(self.outdir, self.file_name, self.timestamp)
        return writer.save_values(values, tile_output=tile_output, timing_context=timing_context)


class GUIArrayRenderer:
    def __init__(self, values: np.ndarray, outdir: Path, colormap_key, file_name: str, timestamp):
        self.values = np.asarray(values, dtype=np.float32)
        self.outdir = outdir
        self.colormap_key = colormap_key
        self.file_name = file_name
        self.timestamp = timestamp

    def convert_to_png(self, tile_output: bool = True, *, timing_context: dict | None = None) -> Tuple[List[Path], str]:
        scalar_to_rgba_s = 0.0
        if timing_context is not None:
            timing_context["scalar_to_rgba_s"] = scalar_to_rgba_s
        io_manager.write_info(f"Value preparation for {self.file_name} completed in {scalar_to_rgba_s:.3f}s")
        writer = GUIValueWriter(self.outdir, self.file_name, self.timestamp)
        return writer.save_values(self.values, tile_output=tile_output, timing_context=timing_context)
