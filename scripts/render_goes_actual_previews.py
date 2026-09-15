#!/usr/bin/env python3
"""Render preview PNGs from staged GOES ABI files using EWMRS colormaps.

This script reads the latest staged file for each ABI channel under a base
directory, applies the same GOES transforms used by EWMRS, and writes a simple
PNG preview for visual verification of the colormaps against real data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from common.ingest.mrms.config import get_abi_radc_channel_specs
from EWMRS.render.goes_transform import load_goes_abi_render_dataset


REFLECTANCE_CHANNELS = {"C01", "C02", "C03", "C04", "C05", "C06"}
BRIGHTNESS_TEMP_LIMITS = {
    "C07": (180.0, 330.0),
    "C08": (180.0, 300.0),
    "C09": (180.0, 310.0),
    "C10": (185.0, 320.0),
    "C11": (180.0, 330.0),
    "C12": (180.0, 330.0),
    "C13": (180.0, 330.0),
    "C14": (180.0, 330.0),
    "C15": (180.0, 330.0),
    "C16": (180.0, 330.0),
}


def _load_colormaps(colormaps_path: Path) -> dict[str, tuple[np.ndarray, np.ndarray, bool]]:
    text = colormaps_path.read_text()
    try:
        raw = json.loads(text)
        # Accept either top-level list of sources or a single object
        if isinstance(raw, dict):
            raw = [raw]
    except Exception:
        # Fallback: attempt to extract the inner "colormaps" array from the file
        start = text.find('"colormaps"')
        if start == -1:
            raise
        arr_start = text.find('[', start)
        arr_end = text.find(']', arr_start)
        if arr_start == -1 or arr_end == -1:
            raise
        arr_text = text[arr_start:arr_end+1]
        # Remove trailing commas before array/object closes to make it safe for json.loads
        arr_text = arr_text.replace(',\n]', '\n]').replace(',\n}', '\n}')
        all_colormaps = json.loads(arr_text)
        raw = [{"colormaps": all_colormaps}]
    by_name: dict[str, tuple[np.ndarray, np.ndarray, bool]] = {}

    for source in raw:
        for cmap in source.get("colormaps", []):
            name = str(cmap.get("name", ""))
            thresholds = np.array([t["value"] for t in cmap["thresholds"]], dtype=np.float32)
            colors = np.array([t["rgb"] for t in cmap["thresholds"]], dtype=np.float32)
            by_name[name] = (thresholds, colors, bool(cmap.get("interpolate", True)))

    return by_name


def _colormap_name_for_channel(channel_id: str) -> str:
    if channel_id in {"C01", "C02", "C03", "C04", "C05", "C06"}:
        return "GOES_RGB_Raw"
    if channel_id in REFLECTANCE_CHANNELS:
        return f"GOES_ABI_{channel_id}_Reflectance"
    if channel_id in {"C13", "C14", "C15"}:
        return "GOES_IR"
    return f"GOES_ABI_{channel_id}_BrightnessTemp"


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

    min_temp, max_temp = BRIGHTNESS_TEMP_LIMITS[channel_id]
    return {
        "source_type": "goes_abi",
        "variable_name": "CMI",
        "fallback_variable_names": ["Rad"],
        "channel_id": channel_id,
        "value_transform": "brightness_temp_from_rad",
        "mask_min": {channel_id: min_temp, "default": min_temp},
        "mask_max": {channel_id: max_temp, "default": max_temp},
    }


def _latest_non_idx_file(directory: Path) -> Path | None:
    if not directory.exists():
        return None

    candidates = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() != ".idx"]
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]


def _rgba_from_data(data: np.ndarray, thresholds: np.ndarray, colors: np.ndarray, interpolate: bool) -> np.ndarray:
    flat = np.asarray(data, dtype=np.float32).ravel()
    rgba_flat = np.empty((flat.shape[0], 4), dtype=np.uint8)
    rgba_flat[:, 3] = 0

    valid_mask = np.isfinite(flat)
    safe = np.where(valid_mask, flat, thresholds[0])

    if interpolate:
        safe = np.clip(safe, thresholds[0], thresholds[-1])
        rgba_flat[:, 0] = np.interp(safe, thresholds, colors[:, 0]).astype(np.uint8)
        rgba_flat[:, 1] = np.interp(safe, thresholds, colors[:, 1]).astype(np.uint8)
        rgba_flat[:, 2] = np.interp(safe, thresholds, colors[:, 2]).astype(np.uint8)
    else:
        color_uint8 = colors.astype(np.uint8)
        indices = np.digitize(safe, thresholds) - 1
        indices = np.clip(indices, 0, len(color_uint8) - 1)
        rgba_flat[:, :3] = color_uint8[indices]

    rgba_flat[valid_mask & (flat >= thresholds[0]), 3] = 255
    return rgba_flat.reshape((data.shape[0], data.shape[1], 4))


def _save_preview(dataset, colormap, output_path: Path) -> None:
    thresholds, colors, interpolate = colormap
    rgba = _rgba_from_data(dataset["unknown"].values, thresholds, colors, interpolate)
    image = Image.fromarray(rgba, mode="RGBA")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, compress_level=1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render GOES actual-data preview images")
    parser.add_argument(
        "--base-dir",
        default=str(Path.home() / "EdgeWARN_input"),
        help="Runtime base directory containing data/GOES_ABI",
    )
    parser.add_argument(
        "--colormaps-file",
        default="src/EWMRS/colormaps.json",
        help="Path to colormaps.json",
    )
    parser.add_argument(
        "--output-dir",
        default="goes_actual_previews",
        help="Directory for rendered preview PNGs",
    )
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    colormaps = _load_colormaps(Path(args.colormaps_file).resolve())

    rendered = []
    for spec in get_abi_radc_channel_specs():
        if not spec.channel_id:
            continue

        latest_file = _latest_non_idx_file(base_dir / "data" / "ABI_RadC" / spec.outdir.name)
        if latest_file is None:
            continue

        colormap_name = _colormap_name_for_channel(spec.channel_id)
        if colormap_name not in colormaps:
            continue

        dataset = load_goes_abi_render_dataset(latest_file, _layer_config_for_channel(spec.channel_id))
        if dataset is None:
            continue

        output_path = output_dir / f"GOES_ABI_{spec.channel_id}.png"
        _save_preview(dataset, colormaps[colormap_name], output_path)
        rendered.append(output_path)

    print(f"Rendered {len(rendered)} GOES previews")
    for path in rendered:
        print(path)


if __name__ == "__main__":
    main()
