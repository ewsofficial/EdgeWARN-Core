"""NEXRAD GUI serialization (owned by the NEXRAD service).

Reads grouped elevation AR2V artifacts and writes the binary .bin.gz layers
plus per-site render manifests under ``gui/NEXRAD/``. The colormap mapping is
read lazily so importing this module does not load the EWMRS render stack.
"""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path

import numpy as np
from util.atomic import atomic_write_bytes, atomic_write_json

import util.file as fs
from common.ingest.nexrad.parser import MSG_31_BLOCK_POINTERS, MSG_HEADER_LEN, MSG_31_PREFIX_LEN, iter_sweep_records, parse_grouped_ar2v_file_mmap


NEXRAD_FIELD_MAGIC = b"EWFFv1S0"
RAW_NEXRAD_BLOCK_VARIABLE_NAMES = {
    "DREF": "DBZH",
    "DVEL": "VRADH",
    "DSW ": "WRADH",
    "DZDR": "ZDR",
    "DPHI": "PHIDP",
    "DRHO": "RHOHV",
    "DCFP": "CCORH",
}
RAW_NEXRAD_BLOCK_MASKS = {
    "DZDR": np.uint16(0x07FF),
    "DPHI": np.uint16(0x03FF),
}
_GENERIC_DATA_BLOCK_STRUCT = struct.Struct(">I H h h h h B B f f")


def nexrad_render_site_dir(site: str) -> Path:
    return fs.GUI_NEXRAD_DIR / str(site).upper()


def nexrad_render_manifest_dir(site: str) -> Path:
    return nexrad_render_site_dir(site) / "render"


def nexrad_render_elevation_dir(site: str, elevation_label: str) -> Path:
    return nexrad_render_site_dir(site) / str(elevation_label)


def nexrad_render_variable_bin_name(site: str, scan_timestamp: str, elevation_label: str, variable_name: str) -> str:
    return f"{str(site).upper()}_{variable_name}_{elevation_label}_{scan_timestamp}.bin.gz"


def nexrad_render_variable_bin_path(site: str, scan_timestamp: str, elevation_label: str, variable_name: str) -> Path:
    return nexrad_render_elevation_dir(site, elevation_label) / nexrad_render_variable_bin_name(
        site,
        scan_timestamp,
        elevation_label,
        variable_name,
    )


def _should_serialize_variable(sweep, variable_name: str) -> bool:
    waveform = str(getattr(sweep, "waveform", "") or "").lower()
    if waveform == "contiguous_doppler" and variable_name == "DBZH":
        return False
    return True


def _write_nexrad_variable_bin(path: Path, dense_data: np.ndarray, azimuths: np.ndarray, ranges: np.ndarray) -> None:
    data = np.asarray(dense_data, dtype="<f2")
    azimuth_values = np.asarray(azimuths, dtype="<f4")
    range_values = np.asarray(ranges, dtype="<f4")
    counts = np.asarray([azimuth_values.shape[0], range_values.shape[0]], dtype="<u4")

    payload = b"".join(
        [
            NEXRAD_FIELD_MAGIC,
            counts.tobytes(order="C"),
            data.tobytes(order="C"),
            azimuth_values.tobytes(order="C"),
            range_values.tobytes(order="C"),
        ]
    )
    # mtime=0 makes identical fields reproducible and keeps no filename in
    # the gzip header (mirrors EWMRS save_float16_chunk).
    compressed = gzip.compress(payload, mtime=0)
    # Verify the completed stream can be read before exposing it.
    if gzip.decompress(compressed)[: len(NEXRAD_FIELD_MAGIC)] != NEXRAD_FIELD_MAGIC:
        raise ValueError("invalid NEXRAD binary magic")
    atomic_write_bytes(path, compressed)


def _normalize_azimuth_axis(dense_data: np.ndarray, azimuths: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    azimuth_values = np.asarray(azimuths, dtype=np.float32)
    if azimuth_values.ndim != 1 or azimuth_values.shape[0] <= 1:
        return dense_data, azimuth_values

    normalized = np.mod(azimuth_values, np.float32(360.0))
    order = np.argsort(normalized, kind="stable")
    if np.array_equal(order, np.arange(order.shape[0])):
        return dense_data, azimuth_values
    return dense_data[:, order], azimuth_values[order]


def _read_msg31_azimuth(record: bytes) -> float | None:
    azimuth_offset = 12 + MSG_HEADER_LEN + 12
    if len(record) < azimuth_offset + 4:
        return None
    return float(struct.unpack_from(">f", record, azimuth_offset)[0])


def _iter_msg31_data_blocks(record: bytes):
    msg31_start = 12 + MSG_HEADER_LEN
    pointer_start = msg31_start + MSG_31_PREFIX_LEN
    pointer_end = pointer_start + 4 + MSG_31_BLOCK_POINTERS * 4
    if len(record) < pointer_end:
        return

    offsets = struct.unpack(
        ">" + "I" * MSG_31_BLOCK_POINTERS,
        record[pointer_start + 4:pointer_end],
    )
    body_end = len(record) - msg31_start
    for index, offset in enumerate(offsets):
        if offset <= 0:
            continue
        next_offsets = [candidate for candidate in offsets[index + 1:] if candidate > offset]
        next_offset = min(next_offsets) if next_offsets else body_end
        block_start = msg31_start + offset
        block_end = msg31_start + next_offset
        if block_start + 4 > block_end or block_end > len(record):
            continue
        yield record[block_start:block_start + 4].decode("ascii", errors="ignore"), block_start + 4, block_end


def _decode_grouped_ar2v_sweep(records, sweep):
    azimuths: list[float] = []
    moments: dict[str, dict[str, object]] = {}

    for ray_index, record in enumerate(records):
        azimuth = _read_msg31_azimuth(record)
        if azimuth is None:
            continue
        azimuths.append(azimuth)

        for raw_block_name, block_header_start, block_end in _iter_msg31_data_blocks(record) or ():
            variable_name = RAW_NEXRAD_BLOCK_VARIABLE_NAMES.get(raw_block_name)
            if variable_name is None:
                continue
            header_end = block_header_start + _GENERIC_DATA_BLOCK_STRUCT.size
            if header_end > block_end:
                continue

            (
                _reserved,
                ngates,
                first_gate,
                gate_spacing,
                _thresh,
                _snr_thres,
                _flags,
                word_size,
                scale,
                offset,
            ) = _GENERIC_DATA_BLOCK_STRUCT.unpack_from(record, block_header_start)
            width = {8: 1, 16: 2}.get(int(word_size))
            if width is None or ngates <= 0 or float(scale) == 0.0:
                continue

            data_start = header_end
            data_end = data_start + int(ngates) * width
            if data_end > block_end or data_end > len(record):
                continue

            raw_values = np.frombuffer(record[data_start:data_end], dtype=f">u{width}")
            mask = RAW_NEXRAD_BLOCK_MASKS.get(raw_block_name)
            if mask is not None:
                raw_values = raw_values & mask
            decoded = (raw_values.astype(np.float32, copy=False) - float(offset)) / float(scale)

            moment = moments.setdefault(
                variable_name,
                {
                    "first_gate": int(first_gate),
                    "gate_spacing": int(gate_spacing),
                    "max_ngates": 0,
                    "rays": [],
                },
            )
            moment["max_ngates"] = max(int(moment["max_ngates"]), int(ngates))
            rays = moment["rays"]
            assert isinstance(rays, list)
            rays.append((ray_index, decoded))

    if not azimuths:
        return None

    decoded_moments = {}
    azimuth_array = np.asarray(azimuths, dtype=np.float32)
    for variable_name, moment in moments.items():
        max_ngates = int(moment["max_ngates"])
        if max_ngates <= 0:
            continue
        dense_data = np.full((max_ngates, azimuth_array.shape[0]), np.nan, dtype=np.float16)
        rays = moment["rays"]
        assert isinstance(rays, list)
        for ray_index, decoded in rays:
            dense_data[:decoded.shape[0], ray_index] = decoded.astype(np.float16, copy=False)
        first_gate = int(moment["first_gate"])
        gate_spacing = int(moment["gate_spacing"])
        ranges = first_gate + np.arange(max_ngates, dtype=np.float32) * gate_spacing
        decoded_moments[variable_name] = (ranges, dense_data)

    return azimuth_array, decoded_moments


def _serialize_direct_grouped_ar2v_artifact(site: str, volume_id: str, artifact, artifact_timestamp: str):
    artifact_path = getattr(artifact, "ar2v_path", None)
    if not artifact_path:
        return None

    grouped_volume = parse_grouped_ar2v_file_mmap(artifact_path)
    sweeps_by_group = {sweep.group_name: sweep for sweep in grouped_volume.sweeps}
    # Grouped elevation artifacts can be renumbered locally when written, so
    # render every sweep present in the elevation file instead of trusting
    # upstream per-volume sweep indexes.
    requested_groups = sorted(sweeps_by_group)

    manifest_layers = []
    from EWMRS.render.config import nexrad_variable_colormaps

    colormap_keys = nexrad_variable_colormaps()
    elevation_label = artifact.elevation
    elevation_dir = nexrad_render_elevation_dir(site, elevation_label)
    elevation_dir.mkdir(parents=True, exist_ok=True)

    for group_name in requested_groups:
        sweep = sweeps_by_group.get(group_name)
        if sweep is None:
            continue
        decoded = _decode_grouped_ar2v_sweep(iter_sweep_records(grouped_volume, sweep), sweep)
        if decoded is None:
            continue
        azimuths, decoded_moments = decoded
        sweep_index = int(group_name.split("_")[-1]) if "_" in group_name else 0
        for variable_name, (ranges, dense_data) in decoded_moments.items():
            if not _should_serialize_variable(sweep, variable_name):
                continue
            dense_data, ordered_azimuths = _normalize_azimuth_axis(dense_data, azimuths)
            layer_name = f"NEXRAD_{variable_name}_SWEEP_{sweep_index:02d}"
            bin_path = nexrad_render_variable_bin_path(site, artifact_timestamp, elevation_label, variable_name)
            if not bin_path.exists():
                _write_nexrad_variable_bin(bin_path, dense_data, ordered_azimuths, ranges)

            manifest_layers.append(
                {
                    "name": layer_name,
                    "site": str(site).upper(),
                    "volume_id": str(volume_id),
                    "scan_timestamp": artifact_timestamp,
                    "sweep_index": sweep_index,
                    "sweep_group": group_name,
                    "canonical_elevation": elevation_label,
                    "bin_path": str(bin_path),
                    "variable_name": variable_name,
                    "colormap_key": colormap_keys.get(variable_name),
                    "data_shape": [int(dense_data.shape[0]), int(dense_data.shape[1])],
                    "azimuth_count": int(ordered_azimuths.shape[0]),
                    "range_count": int(ranges.shape[0]),
                }
            )

    return manifest_layers


def serialize_nexrad_elevation_artifacts(
    site: str,
    volume_id: str,
    scan_timestamp: str,
    elevation_artifacts: list,
) -> Path:
    """Serialize render intermediates from grouped elevation artifacts.

    Reads grouped elevation AR2V files and produces GUI bin.gz outputs:
        gui/NEXRAD/<SITE>/<ELEVATION>/<SITE>_<VARIABLE>_<ELEVATION>_<TIMESTAMP>.bin.gz
    """
    render_dir = nexrad_render_manifest_dir(site)
    render_dir.mkdir(parents=True, exist_ok=True)

    manifest_layers = []
    manifest_timestamp = scan_timestamp
    for artifact in elevation_artifacts:
        artifact_timestamp = artifact.elevation_timestamp or artifact.scan_timestamp or scan_timestamp
        if artifact_timestamp is None:
            continue
        manifest_timestamp = manifest_timestamp or artifact_timestamp

        artifact_path = Path(artifact.ar2v_path) if getattr(artifact, "ar2v_path", None) else None
        if artifact_path is None or not artifact_path.exists():
            continue

        if artifact_path.suffix != ".ar2v":
            continue

        try:
            direct_layers = _serialize_direct_grouped_ar2v_artifact(site, volume_id, artifact, artifact_timestamp)
        except Exception:
            direct_layers = None
        if direct_layers is not None:
            manifest_layers.extend(direct_layers)

    manifest_path = render_dir / f"{str(site).upper()}_{manifest_timestamp or 'unknown'}_{volume_id}.json"
    manifest_payload = {
        "site": str(site).upper(),
        "volume_id": str(volume_id),
        "scan_timestamp": manifest_timestamp,
        "source": "elevation_artifacts",
        "layers": manifest_layers,
    }
    atomic_write_json(manifest_path, manifest_payload, indent=2)
    return manifest_path
