"""Versioned domain-wide swept StormProb probability field (``EWSPv2U1``).

Each analysis cycle publishes one raster plane: for every cell of the configured
runtime domain at 0.01 degrees, the number of the 20 ensemble members whose swept
footprint covers that cell at any time in ``[T+15, T+60]``. Counts are raw member
counts and probability is ``count / ensemble_size``; no calibration is applied.
Nothing is composited across scans.

A file is a 64-byte plain header followed by a gzip container (level 6,
``mtime = 0``, deterministic) holding the row-major ``uint8`` member counts.
Rows ascend latitude. The header stays outside the compressed body so an empty
field is exactly the header and a client can locate the crop without inflating
the plane.
"""
from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import postprocess

MAGIC = b"EWSPv2U1"
WINDOW_START_MINUTES = postprocess.SWEEP_START_MINUTES
WINDOW_END_MINUTES = postprocess.SWEEP_END_MINUTES
ENSEMBLE_SIZE = postprocess.ENSEMBLE_SIZE
RESOLUTION_DEG = 0.01
HEADER = struct.Struct("<8sQHHHHII4f2f2I")
HEADER_SIZE = HEADER.size
FLAG_ACTIVE_CROP = 0x1
COMPRESSION_LEVEL = 6
BAND_ROWS = 256
# One band stays near 0.5 M pixels: 2 MB of uint32 accumulation plus the 0.5 MB
# uint8 count plane. A full-domain crop (7000 columns) therefore uses 74-row
# bands instead of 256.
BAND_PIXELS = 512 * 1024
CELL_REACH_MARGIN_KM = 2.0
KM_PER_DEGREE = 111.0


@dataclass(frozen=True)
class Domain:
    """Rectangular 0.01-degree grid; row 0 is the southernmost latitude band."""

    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float
    resolution_deg: float = RESOLUTION_DEG

    @property
    def rows(self) -> int:
        return max(0, int(math.floor((self.lat_max - self.lat_min) / self.resolution_deg + 1e-9)))

    @property
    def cols(self) -> int:
        return max(0, int(math.floor((self.lon_max - self.lon_min) / self.resolution_deg + 1e-9)))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Header form: ``(lat_min, lon_min, dlat, dlon)``."""
        return (self.lat_min, self.lon_min, self.resolution_deg, self.resolution_deg)


def load_domain(config_dir: str | None = None) -> Domain:
    """Read the field domain from ``runtime.run.lat_limits``/``lon_limits``."""
    from common.config.loader import load_config

    run = load_config("runtime", config_dir=config_dir)["run"]
    lat_min, lat_max = (float(value) for value in run["lat_limits"])
    lon_min, lon_max = (float(value) for value in run["lon_limits"])
    domain = Domain(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
    if domain.rows == 0 or domain.cols == 0:
        raise ValueError("StormProb field domain is empty")
    return domain


def cell_patch(radii_km, centres_km, centroid_latlon, *, domain: Domain | None = None) -> dict:
    """Rasterize one cell's swept members straight onto its domain box.

    The local offset is separable: the east axis depends only on the column,
    ``(lon_j - lon_c) * 111 * cos(lat_c)``, and the north axis only on the row.
    The cell's reach keeps a 2 km margin so a fast storm's sweep is not clipped,
    and radii are floored at the model's 0.1 km minimum. Returns the transient
    ``{"row", "col", "mask"}`` record used by publication; the mask is a
    ``uint32`` patch whose bounds are domain indices.
    """
    domain = load_domain() if domain is None else domain
    radii = np.maximum(np.asarray(radii_km, dtype=np.float32),
                       np.float32(postprocess.MIN_SAMPLED_RADIUS_KM))
    centres = np.asarray(centres_km, dtype=np.float32)
    if radii.shape != (1, ENSEMBLE_SIZE, 4, 64) or centres.shape != (1, ENSEMBLE_SIZE, 4, 2):
        raise ValueError("Invalid StormProb radii or centre shape")
    lat_c, lon_c = (float(value) for value in centroid_latlon)
    if not (math.isfinite(lat_c) and math.isfinite(lon_c)):
        raise ValueError("non-finite StormProb centroid")
    scale = KM_PER_DEGREE * math.cos(math.radians(lat_c))
    if not math.isfinite(scale) or abs(scale) < 1e-6:
        raise ValueError("invalid longitude scale for StormProb field")
    reach_km = float(np.max(np.abs(centres)) + np.max(radii)) + CELL_REACH_MARGIN_KM
    row_start = max(0, math.floor((lat_c - reach_km / KM_PER_DEGREE - domain.lat_min)
                                  / domain.resolution_deg))
    row_stop = min(domain.rows, math.ceil((lat_c + reach_km / KM_PER_DEGREE - domain.lat_min)
                                          / domain.resolution_deg) + 1)
    col_start = max(0, math.floor((lon_c - reach_km / scale - domain.lon_min)
                                  / domain.resolution_deg))
    col_stop = min(domain.cols, math.ceil((lon_c + reach_km / scale - domain.lon_min)
                                          / domain.resolution_deg) + 1)
    if row_stop <= row_start or col_stop <= col_start:
        return {"row": min(row_start, domain.rows), "col": min(col_start, domain.cols),
                "mask": np.zeros((0, 0), dtype=np.uint32)}
    north = (domain.lat_min
             + np.arange(row_start, row_stop, dtype=np.float64) * domain.resolution_deg
             - lat_c) * KM_PER_DEGREE
    east = (domain.lon_min
            + np.arange(col_start, col_stop, dtype=np.float64) * domain.resolution_deg
            - lon_c) * scale
    mask = postprocess.swept_member_mask(radii, centres, east, north)
    return {"row": row_start, "col": col_start, "mask": mask}


def encode_field(patches, analysis_time: Any, *, domain: Domain | None = None) -> bytes:
    """Serialize one cycle's swept field: 64-byte header plus gzip counts.

    Cells are combined member-wise: the ``uint32`` patches are OR-ed and the
    popcount is the number of members covering the point. The crop is limited to
    the union of the cell patches and processed in row bands, so no full-crop
    ``uint32``/``uint8`` plane is ever allocated.
    """
    domain = load_domain() if domain is None else domain
    clipped = [patch for patch in (_clip_patch(item, domain) for item in patches or ())
               if patch is not None]
    if clipped:
        row_start = min(patch["row"] for patch in clipped)
        col_start = min(patch["col"] for patch in clipped)
        row_stop = max(patch["row"] + patch["mask"].shape[0] for patch in clipped)
        col_stop = max(patch["col"] + patch["mask"].shape[1] for patch in clipped)
    else:
        row_start = col_start = row_stop = col_stop = 0
    crop_rows = row_stop - row_start
    crop_cols = col_stop - col_start
    crop_lat = domain.lat_min + row_start * domain.resolution_deg
    crop_lon = domain.lon_min + col_start * domain.resolution_deg
    header = HEADER.pack(
        MAGIC, _analysis_milliseconds(analysis_time),
        WINDOW_START_MINUTES, WINDOW_END_MINUTES, ENSEMBLE_SIZE, 0,
        len(clipped), FLAG_ACTIVE_CROP if crop_rows and crop_cols else 0,
        *domain.bounds,
        float(np.float32(crop_lat)), float(np.float32(crop_lon)),
        crop_rows, crop_cols,
    )
    if not crop_rows or not crop_cols:
        return header
    compressor = zlib.compressobj(COMPRESSION_LEVEL, zlib.DEFLATED, 31)
    payload = [header]
    band_rows = _band_rows(crop_cols)
    for band_start in range(0, crop_rows, band_rows):
        band_stop = min(crop_rows, band_start + band_rows)
        counts = np.zeros((band_stop - band_start, crop_cols), dtype=np.uint32)
        for patch in clipped:
            patch_row = patch["row"] - row_start
            patch_col = patch["col"] - col_start
            mask = patch["mask"]
            first = max(band_start, patch_row)
            last = min(band_stop, patch_row + mask.shape[0])
            if last <= first:
                continue
            counts[first - band_start:last - band_start,
                   patch_col:patch_col + mask.shape[1]] |= (
                mask[first - patch_row:last - patch_row])
        # Counts stay ``uint8`` until a reader divides by the ensemble size.
        # ``np.bitwise_count`` already returns ``uint8``; copying the plane
        # would double the band's transient memory for no dtype change.
        counted = np.bitwise_count(counts)
        if counted.dtype != np.uint8:
            counted = counted.astype(np.uint8)
        payload.append(compressor.compress(counted.tobytes()))
    payload.append(compressor.flush())
    return b"".join(payload)


def decode_header(payload: bytes) -> dict:
    """Parse the 64-byte plain header and validate its magic."""
    if len(payload) < HEADER_SIZE:
        raise ValueError("StormProb field artifact is shorter than its header")
    values = HEADER.unpack_from(payload, 0)
    magic = values[0]
    if magic != MAGIC:
        raise ValueError(f"invalid StormProb field magic {magic!r}")
    if values[4] != ENSEMBLE_SIZE:
        raise ValueError(f"unsupported StormProb field ensemble size {values[4]}")
    return {
        "magic": magic,
        "analysis_unix_ms": values[1],
        "window_start_minutes": values[2],
        "window_end_minutes": values[3],
        "ensemble_size": values[4],
        "reserved": values[5],
        "cell_count": values[6],
        "flags": values[7],
        "domain_bounds": tuple(values[8:12]),
        "crop_origin": (values[12], values[13]),
        "crop_shape": (values[14], values[15]),
    }


def decode_field(payload: bytes) -> dict:
    """Return the header plus the ``uint8`` member-count plane of one artifact."""
    header = decode_header(payload)
    crop_rows, crop_cols = header["crop_shape"]
    counts = np.zeros((0, 0), dtype=np.uint8)
    if header["flags"] & FLAG_ACTIVE_CROP:
        if crop_rows == 0 or crop_cols == 0:
            raise ValueError("StormProb field flags an empty active crop")
        inflated = zlib.decompress(payload[HEADER_SIZE:], 31)
        expected = crop_rows * crop_cols
        if len(inflated) != expected:
            raise ValueError(
                f"StormProb field body has {len(inflated)} bytes; expected {expected}")
        counts = np.frombuffer(inflated, dtype=np.uint8).reshape(crop_rows, crop_cols).copy()
        if counts.size and int(counts.max()) > header["ensemble_size"]:
            raise ValueError("StormProb field member count exceeds the ensemble size")
    return {**header, "member_count": counts}


def read_analysis_time(path: str | Path) -> str:
    """Read an artifact's analysis time as ISO-8601 UTC without inflating it."""
    with Path(path).open("rb") as handle:
        header = decode_header(handle.read(HEADER_SIZE))
    moment = datetime.fromtimestamp(header["analysis_unix_ms"] / 1000.0, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def cycle_key(value: Any) -> str:
    """Canonical ``YYYYMMDD-HHMMSS`` UTC key for a cycle or artifact name.

    Stormcell snapshots, ``stormcell_index.json``, and field artifact names all
    use this key, so one index entry resolves every artifact of a scan.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if len(text) == 15 and text[8] == "-":
            moment = datetime.strptime(text, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _band_rows(crop_cols: int) -> int:
    """Row count for one combine band under the fixed pixel budget."""
    return max(1, min(BAND_ROWS, BAND_PIXELS // max(crop_cols, 1)))


def _clip_patch(patch: Any, domain: Domain) -> dict | None:
    if not isinstance(patch, dict):
        raise ValueError("invalid StormProb field patch")
    mask = np.asarray(patch.get("mask"), dtype=np.uint32)
    if mask.ndim != 2:
        raise ValueError("invalid StormProb field patch shape")
    if mask.size == 0:
        return None
    row = int(patch.get("row", 0))
    col = int(patch.get("col", 0))
    if (row < 0 or col < 0 or row + mask.shape[0] > domain.rows
            or col + mask.shape[1] > domain.cols):
        raise ValueError("StormProb field patch escapes the configured domain")
    return {"row": row, "col": col, "mask": mask}


def _analysis_milliseconds(value: Any) -> int:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(round(moment.astimezone(timezone.utc).timestamp() * 1000))


__all__ = [
    "MAGIC", "HEADER_SIZE", "ENSEMBLE_SIZE", "RESOLUTION_DEG", "Domain",
    "load_domain", "cell_patch", "encode_field", "decode_header", "decode_field",
    "read_analysis_time", "cycle_key",
]
