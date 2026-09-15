#!/usr/bin/env python3
"""Visual verification helper for rendered GOES tile sets.

This script reconstructs a tiled EWMRS GOES layer into a single Web Mercator
image, overlays CONUS state boundaries from ``assets/nws_zones``, and writes a
PNG for manual alignment checks.

Example:
  python scripts/verify_goes_tiles_conus.py \
    --product-dir ~/EdgeWARN_input/gui/GOES_ABI_C02_Reflectance \
    --output /tmp/goes_c02_alignment.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import unary_union

try:
    from EWMRS.pipeline import GOES_WEB_MERCATOR_BOUNDS as WEB_MERCATOR_BOUNDS
except Exception:
    WEB_MERCATOR_BOUNDS = (-13914936.349159198, 2814454.7323097703, -7402746.137752692, 6360130.74092142)


def _parse_tile_name(tile_path: Path):
    stem = tile_path.stem
    if not stem.startswith("tile_"):
        return None

    parts = stem.split("_")
    if len(parts) != 3:
        return None

    try:
        tile_x = int(parts[1])
        tile_y = int(parts[2])
        return tile_x, tile_y
    except ValueError:
        return None


def _load_tile_grid(product_dir: Path):
    index_path = product_dir / "index.json"
    if not index_path.exists():
        return None

    data = json.loads(index_path.read_text())
    if isinstance(data, dict):
        return data.get("tile_grid")
    return None


def _select_timestamp_dir(product_dir: Path, timestamp: str | None) -> Path:
    if timestamp:
        target = product_dir / timestamp
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Timestamp directory not found: {target}")
        return target

    candidates = [entry for entry in product_dir.iterdir() if entry.is_dir() and not entry.name.startswith(".")]
    if not candidates:
        raise FileNotFoundError(f"No timestamp directories found under {product_dir}")

    return sorted(candidates, key=lambda p: p.name)[-1]


def _reconstruct_rgba(tile_dir: Path, tile_grid: dict | None):
    tile_files = sorted(tile_dir.glob("tile_*.png"))
    if not tile_files:
        raise FileNotFoundError(f"No tile PNG files found under {tile_dir}")

    parsed = []
    for tile_path in tile_files:
        coord = _parse_tile_name(tile_path)
        if coord is None:
            continue
        parsed.append((coord[0], coord[1], tile_path))

    if not parsed:
        raise ValueError(f"No parseable tile filenames found under {tile_dir}")

    first_tile = np.array(Image.open(parsed[0][2]).convert("RGBA"))
    tile_size = first_tile.shape[0]

    if tile_grid:
        rows = int(tile_grid.get("rows", 0))
        cols = int(tile_grid.get("cols", 0))
        if rows <= 0 or cols <= 0:
            raise ValueError(f"Invalid tile_grid in index.json: {tile_grid}")
    else:
        cols = max(tile_x for tile_x, _, _ in parsed) + 1
        rows = max(tile_y for _, tile_y, _ in parsed) + 1

    rgba = np.zeros((rows * tile_size, cols * tile_size, 4), dtype=np.uint8)

    for tile_x, tile_y, tile_path in parsed:
        tile = np.array(Image.open(tile_path).convert("RGBA"))
        top = (rows - 1 - tile_y) * tile_size
        left = tile_x * tile_size
        rgba[top : top + tile_size, left : left + tile_size] = tile

    return rgba


def _load_state_boundaries(nws_zones_root: Path):
    state_dirs = [entry for entry in nws_zones_root.iterdir() if entry.is_dir()]
    lonlat_boundaries = []

    for state_dir in sorted(state_dirs, key=lambda p: p.name):
        zones_file = state_dir / "zones.json"
        if not zones_file.exists():
            continue

        try:
            zones = json.loads(zones_file.read_text())
        except Exception:
            continue

        polygons = []
        for zone in zones:
            rings = zone.get("Polygon")
            if not isinstance(rings, list) or not rings:
                continue

            shell = rings[0]
            holes = rings[1:] if len(rings) > 1 else None
            try:
                polygon = Polygon(shell, holes=holes)
            except Exception:
                continue

            if polygon.is_empty:
                continue
            polygons.append(polygon)

        if not polygons:
            continue

        try:
            merged = unary_union(polygons)
            lonlat_boundaries.append(merged.boundary)
        except Exception:
            continue

    return lonlat_boundaries


def _plot_boundaries_web_mercator(ax, boundaries_lonlat):
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

    for geometry in boundaries_lonlat:
        if geometry.is_empty:
            continue

        parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
        for part in parts:
            try:
                x_coords, y_coords = part.xy
                x_3857, y_3857 = transformer.transform(x_coords, y_coords)
                ax.plot(x_3857, y_3857, color="#101010", linewidth=0.25, alpha=0.55)
            except Exception:
                continue


def main():
    parser = argparse.ArgumentParser(description="Reconstruct GOES tiles and overlay CONUS state boundaries")
    parser.add_argument(
        "--product-dir",
        default=None,
        help="Path to GOES GUI product directory (for example, ~/EdgeWARN_input/gui/GOES_ABI_C02_Reflectance)",
    )
    parser.add_argument(
        "--base-dir",
        default=str(Path.home() / "EdgeWARN_input"),
        help="Runtime base directory containing gui/GOES_ABI_* outputs",
    )
    parser.add_argument(
        "--product",
        default="GOES_ABI_C02",
        help="GOES product directory name under <base-dir>/gui when --product-dir is omitted",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Timestamp directory to inspect (defaults to latest available)",
    )
    parser.add_argument(
        "--nws-zones-root",
        default=str(Path(__file__).resolve().parents[1] / "assets" / "nws_zones"),
        help="Path to assets/nws_zones root",
    )
    parser.add_argument(
        "--output",
        default="goes_conus_alignment_check.png",
        help="Output PNG path",
    )
    args = parser.parse_args()

    if args.product_dir:
        product_dir = Path(args.product_dir).expanduser().resolve()
    else:
        product_dir = (Path(args.base_dir).expanduser().resolve() / "gui" / args.product)

    if not product_dir.exists() or not product_dir.is_dir():
        raise FileNotFoundError(f"Product directory not found: {product_dir}")

    timestamp_dir = _select_timestamp_dir(product_dir, args.timestamp)
    tile_grid = _load_tile_grid(product_dir)
    rgba = _reconstruct_rgba(timestamp_dir, tile_grid)

    zones_root = Path(args.nws_zones_root).expanduser().resolve()
    if not zones_root.exists() or not zones_root.is_dir():
        raise FileNotFoundError(f"NWS zones root not found: {zones_root}")

    boundaries_lonlat = _load_state_boundaries(zones_root)

    fig, ax = plt.subplots(figsize=(14, 8), constrained_layout=True)
    extent = [
        WEB_MERCATOR_BOUNDS[0],
        WEB_MERCATOR_BOUNDS[2],
        WEB_MERCATOR_BOUNDS[1],
        WEB_MERCATOR_BOUNDS[3],
    ]

    ax.imshow(rgba, extent=extent, origin="upper")
    _plot_boundaries_web_mercator(ax, boundaries_lonlat)

    ax.set_title(f"GOES Tile Alignment Check: {product_dir.name} / {timestamp_dir.name}")
    ax.set_xlabel("Web Mercator X (m)")
    ax.set_ylabel("Web Mercator Y (m)")
    ax.set_xlim(WEB_MERCATOR_BOUNDS[0], WEB_MERCATOR_BOUNDS[2])
    ax.set_ylim(WEB_MERCATOR_BOUNDS[1], WEB_MERCATOR_BOUNDS[3])

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    print(str(output_path))


if __name__ == "__main__":
    main()
