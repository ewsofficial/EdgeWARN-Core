#!/usr/bin/env python3
"""Plot common GOES ABI RGB products from staged ABI-L1b-RadC data.

This script reads GOES ABI RadC files from a runtime base directory (default:
~/EdgeWARN_input), applies the same Rad->reflectance / Rad->brightness-temp
transforms used by EWMRS, and writes PNGs for:

- True-Color RGB
- Airmass RGB
- Nighttime Microphysics RGB
- Day Cloud Phase RGB
- Simple Water Vapor RGB
- Sandwich RGB
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image
from rasterio.enums import Resampling


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from common.ingest.mrms.config import get_abi_radc_channel_specs
from common.ingest.mrms.utils import extract_timestamp
from EWMRS.pipeline import GOES_WEB_MERCATOR_SHAPE, GOES_WEB_MERCATOR_TRANSFORM
from EWMRS.render.goes_transform import load_goes_abi_render_dataset, reproject_goes_abi_to_web_mercator
from EWMRS.render.tools import configure_proj_runtime


REFLECTANCE_CHANNELS = {"C01", "C02", "C03", "C04", "C05", "C06"}
REQUIRED_CHANNELS = {"C01", "C02", "C03", "C05", "C07", "C08", "C10", "C12", "C13", "C15"}


def _layer_config_for_channel(channel_id: str) -> dict:
    if channel_id in REFLECTANCE_CHANNELS:
        return {
            "source_type": "goes_abi",
            "variable_name": "CMI",
            "fallback_variable_names": ["Rad"],
            "channel_id": channel_id,
            "value_transform": "reflectance_from_rad",
            "mask_min": {channel_id: 0.0, "default": 0.0},
            "mask_max": {channel_id: 1.2, "default": 1.2},
        }

    return {
        "source_type": "goes_abi",
        "variable_name": "CMI",
        "fallback_variable_names": ["Rad"],
        "channel_id": channel_id,
        "value_transform": "brightness_temp_from_rad",
        "mask_min": {channel_id: 180.0, "default": 180.0},
        "mask_max": {channel_id: 330.0, "default": 330.0},
    }


def _load_goes_ir_colormap(colormaps_file: Path) -> tuple[np.ndarray, np.ndarray]:
    raw = json.loads(colormaps_file.read_text())
    sources = raw if isinstance(raw, list) else [raw]
    for source in sources:
        for cmap in source.get("colormaps", []):
            if str(cmap.get("name", "")) != "GOES_IR":
                continue
            thresholds = np.array([item["value"] for item in cmap["thresholds"]], dtype=np.float32)
            colors = np.array([item["rgb"] for item in cmap["thresholds"]], dtype=np.float32) / 255.0
            return thresholds, colors
    raise ValueError(f"GOES_IR colormap not found in {colormaps_file}")


def _list_channel_files(channel_dir: Path) -> list[tuple[datetime, Path]]:
    if not channel_dir.exists():
        return []

    entries = []
    for candidate in channel_dir.iterdir():
        if not candidate.is_file() or candidate.suffix.lower() == ".idx":
            continue
        timestamp = extract_timestamp(candidate.name).astimezone(UTC)
        entries.append((timestamp, candidate))

    entries.sort(key=lambda item: item[0])
    return entries


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None

    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _pick_nearest_file(files: list[tuple[datetime, Path]], target_timestamp: datetime) -> tuple[Path, float]:
    chosen_timestamp, chosen_path = min(files, key=lambda item: abs((item[0] - target_timestamp).total_seconds()))
    delta_minutes = abs((chosen_timestamp - target_timestamp).total_seconds()) / 60.0
    return chosen_path, delta_minutes


def _normalize(values: np.ndarray, min_value: float, max_value: float, *, invert: bool = False, gamma: float = 1.0) -> np.ndarray:
    if max_value <= min_value:
        raise ValueError(f"Invalid range: min={min_value}, max={max_value}")

    scaled = (values - min_value) / (max_value - min_value)
    scaled = np.clip(scaled, 0.0, 1.0)
    if invert:
        scaled = 1.0 - scaled
    if gamma != 1.0:
        scaled = np.power(scaled, 1.0 / gamma)
    return scaled.astype(np.float32)


def _to_celsius(values_kelvin: np.ndarray) -> np.ndarray:
    return (values_kelvin - np.float32(273.15)).astype(np.float32)


def _to_rgb_image(rgb: np.ndarray, valid_mask: np.ndarray) -> Image.Image:
    rgb_uint8 = np.clip(np.nan_to_num(rgb, nan=0.0) * 255.0, 0.0, 255.0).astype(np.uint8)
    alpha = np.where(valid_mask, 255, 0).astype(np.uint8)
    rgba = np.dstack((rgb_uint8, alpha))
    return Image.fromarray(rgba, mode="RGBA")


def _align_to_reference_grid(datasets: dict[str, object]) -> dict[str, np.ndarray]:
    reference_channel = "C13" if "C13" in datasets else min(
        datasets,
        key=lambda key: int(datasets[key]["unknown"].sizes["y"]) * int(datasets[key]["unknown"].sizes["x"]),
    )
    ref_da = datasets[reference_channel]["unknown"]
    ref_x = ref_da["x"]
    ref_y = ref_da["y"]

    aligned: dict[str, np.ndarray] = {}
    for channel_id, ds in datasets.items():
        da = ds["unknown"]
        if da.sizes == ref_da.sizes and np.array_equal(da["x"].values, ref_x.values) and np.array_equal(da["y"].values, ref_y.values):
            aligned[channel_id] = np.asarray(da.values, dtype=np.float32)
            continue

        interp_da = da.interp(x=ref_x, y=ref_y, method="linear")
        aligned[channel_id] = np.asarray(interp_da.values, dtype=np.float32)

    return aligned


def _colorize(values: np.ndarray, thresholds: np.ndarray, colors: np.ndarray) -> np.ndarray:
    safe = np.clip(values, thresholds[0], thresholds[-1])
    red = np.interp(safe, thresholds, colors[:, 0])
    green = np.interp(safe, thresholds, colors[:, 1])
    blue = np.interp(safe, thresholds, colors[:, 2])
    return np.dstack((red, green, blue)).astype(np.float32)


def _compute_products(channel_data: dict[str, np.ndarray], goes_ir_thresholds: np.ndarray, goes_ir_colors: np.ndarray, true_color_gamma: float) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    c01 = channel_data["C01"]
    c02 = channel_data["C02"]
    c03 = channel_data["C03"]
    c05 = channel_data["C05"]
    c07_c = _to_celsius(channel_data["C07"])
    c08_c = _to_celsius(channel_data["C08"])
    c10_c = _to_celsius(channel_data["C10"])
    c12_c = _to_celsius(channel_data["C12"])
    c13_k = channel_data["C13"]
    c13_c = _to_celsius(c13_k)
    c15_c = _to_celsius(channel_data["C15"])

    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    # True Color RGB (CIMSS synthetic green)
    green_syn = 0.45 * c02 + 0.10 * c03 + 0.45 * c01
    tc_r = _normalize(c02, 0.0, 1.0, gamma=true_color_gamma)
    tc_g = _normalize(green_syn, 0.0, 1.0, gamma=true_color_gamma)
    tc_b = _normalize(c01, 0.0, 1.0, gamma=true_color_gamma)
    tc_mask = np.isfinite(c01) & np.isfinite(c02) & np.isfinite(c03)
    out["true_color"] = (np.dstack((tc_r, tc_g, tc_b)).astype(np.float32), tc_mask)

    # Airmass RGB
    am_r = _normalize(c08_c - c10_c, -26.2, 0.6)
    am_g = _normalize(c12_c - c13_c, -43.2, 6.7)
    am_b = _normalize(c08_c, -64.65, -29.25, invert=True)
    am_mask = np.isfinite(c08_c) & np.isfinite(c10_c) & np.isfinite(c12_c) & np.isfinite(c13_c)
    out["airmass"] = (np.dstack((am_r, am_g, am_b)).astype(np.float32), am_mask)

    # Nighttime Microphysics RGB
    nm_r = _normalize(c15_c - c13_c, -6.7, 2.6)
    nm_g = _normalize(c13_c - c07_c, -3.1, 5.2)
    nm_b = _normalize(c13_c, -29.6, 19.5)
    nm_mask = np.isfinite(c15_c) & np.isfinite(c13_c) & np.isfinite(c07_c)
    out["nighttime_microphysics"] = (np.dstack((nm_r, nm_g, nm_b)).astype(np.float32), nm_mask)

    # Day Cloud Phase RGB
    dcp_r = _normalize(c13_c, -53.5, 7.5, invert=True)
    dcp_g = _normalize(c02 * 100.0, 0.0, 78.0)
    dcp_b = _normalize(c05 * 100.0, 1.0, 59.0)
    dcp_mask = np.isfinite(c13_c) & np.isfinite(c02) & np.isfinite(c05)
    out["day_cloud_phase"] = (np.dstack((dcp_r, dcp_g, dcp_b)).astype(np.float32), dcp_mask)

    # Simple Water Vapor RGB
    swv_r = _normalize(c13_c, -70.86, 5.81, invert=True)
    swv_g = _normalize(c08_c, -58.49, -30.48, invert=True)
    swv_b = _normalize(c10_c, -28.03, -12.12, invert=True)
    swv_mask = np.isfinite(c13_c) & np.isfinite(c08_c) & np.isfinite(c10_c)
    out["simple_water_vapor"] = (np.dstack((swv_r, swv_g, swv_b)).astype(np.float32), swv_mask)

    # Sandwich RGB (visible grayscale + GOES IR enhancement overlay)
    vis = _normalize(c02, 0.0, 1.0, gamma=true_color_gamma)
    vis_rgb = np.dstack((vis, vis, vis)).astype(np.float32)
    ir_rgb = _colorize(c13_k, goes_ir_thresholds, goes_ir_colors)
    overlay_alpha = _normalize(c13_c, -70.0, -10.0, invert=True)
    overlay_alpha = np.where(c13_c < -5.0, overlay_alpha, 0.0).astype(np.float32) * 0.9
    sandwich = (vis_rgb * (1.0 - overlay_alpha[..., None])) + (ir_rgb * overlay_alpha[..., None])
    sandwich_mask = np.isfinite(c02) & np.isfinite(c13_k)
    out["sandwich"] = (sandwich.astype(np.float32), sandwich_mask)

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot GOES ABI RGB products from staged ABI RadC files")
    parser.add_argument(
        "--base-dir",
        default=str(Path.home() / "EdgeWARN_input"),
        help="Runtime base directory containing data/GOES_ABI",
    )
    parser.add_argument(
        "--output-dir",
        default="goes_rgb_products",
        help="Directory to write RGB PNG outputs",
    )
    parser.add_argument(
        "--colormaps-file",
        default="src/EWMRS/colormaps.json",
        help="Path to colormaps.json (used for Sandwich GOES_IR enhancement)",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Optional target timestamp (ISO-8601, e.g. 2026-04-21T15:30:00Z)",
    )
    parser.add_argument(
        "--max-offset-minutes",
        type=float,
        default=20.0,
        help="Warn if a selected channel file is farther than this from target timestamp",
    )
    parser.add_argument(
        "--true-color-gamma",
        type=float,
        default=2.2,
        help="Gamma used for True Color and Sandwich visible channel",
    )
    args = parser.parse_args()
    configure_proj_runtime()

    base_dir = Path(args.base_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    channel_dir_names = {spec.channel_id: spec.outdir.name for spec in get_abi_radc_channel_specs() if spec.channel_id}

    files_by_channel: dict[str, list[tuple[datetime, Path]]] = {}
    missing_channels = []
    for channel_id in sorted(REQUIRED_CHANNELS):
        channel_dir = base_dir / "data" / "ABI_RadC" / channel_dir_names[channel_id]
        entries = _list_channel_files(channel_dir)
        if not entries:
            missing_channels.append(channel_id)
        files_by_channel[channel_id] = entries

    if missing_channels:
        joined = ", ".join(missing_channels)
        raise FileNotFoundError(f"No GOES ABI files found for required channels: {joined}")

    requested_timestamp = _parse_timestamp(args.timestamp)
    if requested_timestamp is None:
        latest_per_channel = [files_by_channel[channel_id][-1][0] for channel_id in sorted(REQUIRED_CHANNELS)]
        target_timestamp = min(latest_per_channel)
    else:
        target_timestamp = requested_timestamp

    selected_files: dict[str, Path] = {}
    for channel_id in sorted(REQUIRED_CHANNELS):
        selected_path, delta_minutes = _pick_nearest_file(files_by_channel[channel_id], target_timestamp)
        selected_files[channel_id] = selected_path
        if delta_minutes > args.max_offset_minutes:
            print(
                f"Warning: {channel_id} nearest file is {delta_minutes:.1f} minutes from target timestamp "
                f"({target_timestamp.isoformat()})"
            )

    print(f"Target timestamp: {target_timestamp.isoformat()}")
    for channel_id in sorted(selected_files):
        print(f"{channel_id}: {selected_files[channel_id]}")

    datasets: dict[str, object] = {}
    for channel_id, file_path in selected_files.items():
        ds = load_goes_abi_render_dataset(file_path, _layer_config_for_channel(channel_id))
        if ds is None:
            raise RuntimeError(f"Failed to load/transform GOES channel {channel_id}: {file_path}")
        ds = reproject_goes_abi_to_web_mercator(
            ds,
            shape=GOES_WEB_MERCATOR_SHAPE,
            transform=GOES_WEB_MERCATOR_TRANSFORM,
            resampling=Resampling.bilinear,
        )
        if ds is None:
            raise RuntimeError(f"Failed to reproject GOES channel {channel_id} to EPSG:3857: {file_path}")
        datasets[channel_id] = ds

    aligned_channel_data = _align_to_reference_grid(datasets)
    goes_ir_thresholds, goes_ir_colors = _load_goes_ir_colormap(Path(args.colormaps_file).resolve())

    products = _compute_products(
        aligned_channel_data,
        goes_ir_thresholds=goes_ir_thresholds,
        goes_ir_colors=goes_ir_colors,
        true_color_gamma=args.true_color_gamma,
    )

    timestamp_label = target_timestamp.strftime("%Y%m%dT%H%M%SZ")
    for product_name, (rgb, valid_mask) in products.items():
        image = _to_rgb_image(rgb, valid_mask)
        output_path = output_dir / f"{product_name}_{timestamp_label}.png"
        image.save(output_path, compress_level=1)
        print(f"Wrote {output_path}")

    metadata = {
        "target_timestamp": target_timestamp.isoformat(),
        "projection": "EPSG:3857",
        "shape": list(GOES_WEB_MERCATOR_SHAPE),
        "selected_files": {channel_id: str(path) for channel_id, path in selected_files.items()},
        "products": sorted(products.keys()),
    }
    metadata_path = output_dir / f"goes_rgb_metadata_{timestamp_label}.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))
    print(f"Wrote {metadata_path}")


if __name__ == "__main__":
    main()
