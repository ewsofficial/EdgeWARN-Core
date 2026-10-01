"""Convert selected RAP GRIB2 layers to Uint16Array-compatible files.

Each layer is converted and published independently so a consumer can
acknowledge a successful layer, retry only the failed ones, and reuse the same
pinned raw analysis across those jobs. Shared product-index updates are
serialized between processes, and an older completion can never move a product's
latest timestamp backward.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import eccodes
import numpy as np

from EWMRS.rap.config import (
    get_rap_uint16_layers,
    rap_uint16_force,
    rap_uint16_max_timestamps,
    rap_uint16_timestamp_format,
    uint16_nodata,
    uint16_valid_max,
)
from util.io import IOManager
import util.file as fs
from util.atomic import atomic_write_bytes, atomic_write_json

io_manager = IOManager("[RAPUint16]")


@contextmanager
def _serialized_index_update(out_dir: Path):
    """Serialize the read-modify-write of one product index across processes.

    Per-layer jobs for the same analysis can finish in any order in separate
    render workers; without this, two concurrent writers would drop a timestamp
    from the published index.
    """
    from util.runtime.handoff import _AdvisoryFileLock

    lock_root = out_dir.parent / ".index-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock = _AdvisoryFileLock(lock_root / f"{out_dir.name}.lock")
    while True:
        try:
            lock.acquire()
            break
        except OSError:
            time.sleep(0.01)
    try:
        yield
    finally:
        lock.release()


def rap_timestamp_label(rap_file: str | Path, dt=None) -> str:
    """The published output timestamp for a pinned analysis, resolved once."""
    return _timestamp_label(dt, Path(rap_file))


def run_rap_uint16_pipeline(
    rap_file: str | Path,
    dt=None,
    layers=None,
    *,
    timings: dict[str, dict[str, Any]] | None = None,
    force: bool | None = None,
    cleanup: bool = True,
) -> dict[str, Path | None]:
    """Convert configured RAP messages into one raw uint16 file per layer.

    ``layers`` selects an explicit subset, so a consumer can convert, publish,
    and acknowledge one layer while the others are still retrying against the
    same pinned analysis. ``force=None`` means "no preference", deferring to the
    catalog; ``True`` and ``False`` are both explicit caller choices, which is
    why the default is not ``False``.
    """
    rap_path = Path(rap_file)
    if force is None:
        force = rap_uint16_force()
    selected_layers = get_rap_uint16_layers() if layers is None else list(layers)
    timestamp = _timestamp_label(dt, rap_path)
    results: dict[str, Path | None] = {str(layer.get("name")): None for layer in selected_layers}

    if not selected_layers:
        io_manager.write_info("RAP Uint16Array conversion has no configured layers")
        return results
    if not rap_path.is_file():
        raise FileNotFoundError(f"RAP file not found: {rap_path}")

    pending = {str(layer["name"]): _normalize_layer(layer) for layer in selected_layers}
    io_manager.write_info(f"Converting {len(pending)} RAP layer(s) to Uint16Array files from {rap_path.name}")
    converted_layers: list[str] = []
    reused_layers: list[str] = []
    missing_layers: list[str] = []

    eccodes.codes_grib_multi_support_on()
    with open(rap_path, "rb") as file_obj:
        while pending:
            gid = eccodes.codes_grib_new_from_file(file_obj)
            if gid is None:
                break

            try:
                message = _message_descriptor(gid)
                layer = _matching_layer(message, pending.values())
                if layer is None:
                    continue

                output_path = _output_data_path(layer, timestamp)
                metadata_path = output_path.with_name("metadata.json")
                if output_path.is_file() and metadata_path.is_file() and not force:
                    _update_product_index(Path(layer["outdir"]), timestamp)
                    results[str(layer["name"])] = output_path
                    reused_layers.append(str(layer["name"]))
                    _record_timing(
                        timings,
                        layer["name"],
                        status="skipped_existing",
                        seconds=0.0,
                        output_path=output_path,
                    )
                    pending.pop(str(layer["name"]), None)
                    continue

                layer_start = time.perf_counter()
                values, grid = _read_message_values(gid)
                missing_value = _try_get_double(gid, "missingValue")
                encoded = scale_to_uint16(values, layer["scale"], missing_value=missing_value)

                output_path.parent.mkdir(parents=True, exist_ok=True)
                payload = encoded.tobytes(order="C")
                expected_size = int(encoded.size) * encoded.dtype.itemsize
                if len(payload) != expected_size:
                    raise ValueError("unexpected RAP uint16 payload length")
                atomic_write_bytes(output_path, payload)

                metadata = _build_metadata(
                    layer=layer,
                    message=message,
                    rap_path=rap_path,
                    timestamp=timestamp,
                    grid=grid,
                )
                elapsed_seconds = time.perf_counter() - layer_start
                metadata["conversion_time_seconds"] = elapsed_seconds
                atomic_write_json(metadata_path, metadata, indent=2)
                _update_product_index(Path(layer["outdir"]), timestamp)

                results[str(layer["name"])] = output_path
                converted_layers.append(str(layer["name"]))
                _record_timing(
                    timings,
                    layer["name"],
                    status="converted",
                    seconds=elapsed_seconds,
                    output_path=output_path,
                    point_count=grid["point_count"],
                    shape=[grid["nj"], grid["ni"]],
                )
                pending.pop(str(layer["name"]), None)
            finally:
                eccodes.codes_release(gid)

    for missing_name in pending:
        missing_layers.append(str(missing_name))
        _record_timing(timings, missing_name, status="missing", seconds=None, output_path=None)

    removed_count = 0
    if cleanup:
        removed_count = cleanup_old_rap_uint16_layers(keep=(timestamp,))
    if removed_count:
        io_manager.write_info(
            f"RAP Uint16 cleanup summary: removed={removed_count} old layer directories"
        )
    io_manager.write_info(
        f"RAP Uint16 summary: converted={len(converted_layers)}, reused={len(reused_layers)}, missing={len(missing_layers)}"
    )
    if missing_layers:
        io_manager.write_warning(f"RAP missing layers: {', '.join(missing_layers)}")
    return results


def scale_to_uint16(values, scale: dict[str, float], *, missing_value=None) -> np.ndarray:
    """Scale numeric values into little-endian uint16 with 65535 reserved for no-data."""
    min_value = float(scale["min"])
    max_value = float(scale["max"])
    if max_value <= min_value:
        raise ValueError("RAP uint16 scale max must be greater than min")

    data = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(data)
    if missing_value is not None and np.isfinite(missing_value):
        valid &= data != float(missing_value)

    encoded = np.full(data.shape, uint16_nodata(), dtype=np.dtype("<u2"))
    if np.any(valid):
        clipped = np.clip(data[valid], min_value, max_value)
        scaled = np.rint((clipped - min_value) / (max_value - min_value) * uint16_valid_max())
        encoded[valid] = scaled.astype(np.dtype("<u2"), copy=False)
    return encoded


def _normalize_layer(layer: dict[str, Any]) -> dict[str, Any]:
    name = str(layer.get("name", "")).strip()
    if not name:
        raise ValueError("RAP uint16 layer is missing name")

    filter_config = dict(layer.get("filter") or {})
    type_of_level = filter_config.get("typeOfLevel")
    if not type_of_level:
        raise ValueError(f"RAP uint16 layer {name} is missing filter.typeOfLevel")

    scale = dict(layer.get("scale") or {})
    if "min" not in scale or "max" not in scale:
        raise ValueError(f"RAP uint16 layer {name} is missing scale min/max")

    short_names = layer.get("short_names") or layer.get("shortName") or layer.get("short_name")
    if isinstance(short_names, str):
        short_names = [short_names]
    if not short_names:
        raise ValueError(f"RAP uint16 layer {name} is missing short_names")

    return {
        **layer,
        "name": name,
        "short_names": {str(short_name) for short_name in short_names},
        "filter": filter_config,
        "scale": {"min": float(scale["min"]), "max": float(scale["max"])},
    }


def _message_descriptor(gid) -> dict[str, Any]:
    descriptor = {
        "shortName": eccodes.codes_get_string(gid, "shortName"),
        "typeOfLevel": eccodes.codes_get_string(gid, "typeOfLevel"),
        "level": None,
    }
    try:
        descriptor["level"] = eccodes.codes_get_long(gid, "level")
    except Exception:
        descriptor["level"] = None
    return descriptor


def _matching_layer(message: dict[str, Any], layers) -> dict[str, Any] | None:
    for layer in layers:
        filter_config = layer["filter"]
        if message["shortName"] not in layer["short_names"]:
            continue
        if message["typeOfLevel"] != filter_config.get("typeOfLevel"):
            continue
        if "level" in filter_config and message.get("level") != filter_config.get("level"):
            continue
        return layer
    return None


def _read_message_values(gid) -> tuple[np.ndarray, dict[str, int]]:
    ni = int(eccodes.codes_get_long(gid, "Ni"))
    nj = int(eccodes.codes_get_long(gid, "Nj"))
    values = np.asarray(eccodes.codes_get_double_array(gid, "values"), dtype=np.float64)
    expected_size = ni * nj
    if values.size != expected_size:
        raise ValueError(f"RAP message value count {values.size} does not match grid shape {nj}x{ni}")
    return values.reshape((nj, ni)), {"ni": ni, "nj": nj, "point_count": expected_size}


def _try_get_double(gid, key: str) -> float | None:
    try:
        return float(eccodes.codes_get_double(gid, key))
    except Exception:
        return None


def _output_data_path(layer: dict[str, Any], timestamp: str) -> Path:
    return Path(layer["outdir"]) / timestamp / "data.u16"


def _build_metadata(
    *,
    layer: dict[str, Any],
    message: dict[str, Any],
    rap_path: Path,
    timestamp: str,
    grid: dict[str, int],
) -> dict[str, Any]:
    shape = [grid["nj"], grid["ni"]]
    metadata = {
        "layer": layer["name"],
        "timestamp": timestamp,
        "source_file": rap_path.name,
        "shape": shape,
        "grid": grid,
        "dtype": "uint16",
        "byte_order": "little_endian",
        "scale": layer["scale"],
        "missing_value": uint16_nodata(),
        "units": layer.get("units"),
        "grib": {
            "shortName": message["shortName"],
            "typeOfLevel": message["typeOfLevel"],
            "level": message.get("level"),
        },
    }
    if layer.get("colormap_key"):
        metadata["colormap_key"] = layer["colormap_key"]
    if layer.get("description"):
        metadata["description"] = layer["description"]
    return metadata


def _update_product_index(out_dir: Path, timestamp: str, max_timestamps: int | None = None) -> None:
    if max_timestamps is None:
        max_timestamps = rap_uint16_max_timestamps()
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.json"
    with _serialized_index_update(out_dir):
        timestamps: list[str] = []
        if index_path.exists():
            try:
                existing = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    timestamps = [str(item) for item in existing]
                elif isinstance(existing, dict):
                    timestamps = [str(item) for item in existing.get("timestamps", [])]
            except Exception as exc:
                io_manager.write_warning(
                    f"Overwriting corrupt RAP uint16 index {index_path}: {exc}")

        # Descending order and deduplication mean an older job completing late
        # cannot move a product's latest entry backward. ``max_timestamps`` stays
        # the single retention window shared with the directory cleanup below.
        merged = sorted({timestamp, *timestamps}, reverse=True)
        index_data = {
            "timestamps": merged[:max_timestamps],
            "format": "uint16",
            "byte_order": "little_endian",
            "missing_value": uint16_nodata(),
        }
        atomic_write_json(index_path, index_data, indent=2)

def cleanup_old_rap_uint16_layers(max_timestamps: int | None = None, *, keep=()) -> int:
    """Remove old timestamp directories, keeping the configured recent window.

    ``keep`` names timestamp directories an active per-layer job is still
    writing, so retention never deletes a directory out from under a job.
    """
    if max_timestamps is None:
        max_timestamps = rap_uint16_max_timestamps()
    if not fs.GUI_RAP_DIR.exists():
        return 0
    protected = {str(value) for value in keep}
    removed_count = 0
    for layer_dir in fs.GUI_RAP_DIR.iterdir():
        if not layer_dir.is_dir():
            continue
        timestamp_dirs = [d for d in layer_dir.iterdir() if d.is_dir()]
        if len(timestamp_dirs) <= max_timestamps:
            continue
        timestamp_dirs.sort(key=lambda d: d.name, reverse=True)
        for old_dir in timestamp_dirs[max_timestamps:]:
            if old_dir.name in protected:
                continue
            try:
                shutil.rmtree(old_dir)
                removed_count += 1
            except Exception as exc:
                io_manager.write_warning(f"Failed to remove old RAP uint16 directory {old_dir}: {exc}")
    return removed_count


def _record_timing(
    timings: dict[str, dict[str, Any]] | None,
    layer_name: str,
    *,
    status: str,
    seconds: float | None,
    output_path: Path | None,
    point_count: int | None = None,
    shape: list[int] | None = None,
) -> None:
    if timings is None:
        return

    entry: dict[str, Any] = {
        "status": status,
        "seconds": seconds,
        "output_path": str(output_path) if output_path is not None else None,
    }
    if point_count is not None:
        entry["point_count"] = point_count
    if shape is not None:
        entry["shape"] = shape
    timings[str(layer_name)] = entry


def _timestamp_label(dt, rap_path: Path) -> str:
    parsed = _parse_rap_filename_timestamp(rap_path.name)
    if parsed is not None:
        dt = parsed
    elif dt is None:
        dt = datetime.fromtimestamp(rap_path.stat().st_mtime, tz=timezone.utc)
    elif isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    elif not isinstance(dt, datetime):
        raise TypeError("dt must be a datetime, ISO-format string, or None")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(rap_uint16_timestamp_format())


def _parse_rap_filename_timestamp(filename: str) -> datetime | None:
    match = re.search(r"RAP\.(\d{8})-(\d{2})z", filename)
    if match is None:
        return None
    date_part, hour_part = match.groups()
    return datetime.strptime(f"{date_part}{hour_part}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
