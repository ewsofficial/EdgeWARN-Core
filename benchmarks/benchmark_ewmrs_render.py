"""Synthetic benchmark for EWMRS render throughput.

Run with:
    PYTHONPATH=src python benchmarks/benchmark_ewmrs_render.py
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

import util.file as fs
from EWMRS.pipeline import _render_layer
from EWMRS.render.render import GUILayerRenderer, _COLORMAP_CACHE


@dataclass
class BenchmarkResult:
    label: str
    timings: list[float]

    @property
    def median_s(self) -> float:
        return statistics.median(self.timings)

    @property
    def p95_s(self) -> float:
        if len(self.timings) == 1:
            return self.timings[0]
        return statistics.quantiles(self.timings, n=20)[-1]


class _FakeDataArray:
    def __init__(self, data: np.ndarray):
        self._data = data

    @property
    def values(self) -> np.ndarray:
        return self._data


class _FakeDataset:
    def __init__(self, data: np.ndarray):
        self._data = data

    def __getitem__(self, key: str) -> _FakeDataArray:
        if key != "unknown":
            raise KeyError(key)
        return _FakeDataArray(self._data)


class BaselineRenderer(GUILayerRenderer):
    """Preserves the pre-optimization tiled render path for A/B comparison."""

    def convert_to_png(self, tile_output: bool = True):
        data = self.ds["unknown"].values
        thresholds, colors, _colors_uint8, interpolate = self._get_cmap()
        flat_data = data.ravel()
        rgba_flat = np.empty((flat_data.shape[0], 4), dtype=np.uint8)

        if interpolate:
            rgba_flat[:, 0] = np.interp(flat_data, thresholds, colors[:, 0]).astype(np.uint8)
            rgba_flat[:, 1] = np.interp(flat_data, thresholds, colors[:, 1]).astype(np.uint8)
            rgba_flat[:, 2] = np.interp(flat_data, thresholds, colors[:, 2]).astype(np.uint8)
        else:
            indices = np.digitize(flat_data, thresholds) - 1
            indices = np.clip(indices, 0, len(colors) - 1)
            rgba_flat[:, :3] = colors.astype(np.uint8)[indices]

        rgba_flat[:, 3] = np.where((flat_data < thresholds[0]) | np.isnan(flat_data), 0, 255).astype(np.uint8)
        rgba = rgba_flat.reshape((data.shape[0], data.shape[1], 4))

        dt = time.strptime("20260317-200000", "%Y%m%d-%H%M%S")
        timestamp = time.strftime("%Y%m%d-%H%M00", dt)
        self.outdir.mkdir(parents=True, exist_ok=True)
        if tile_output:
            img = Image.fromarray(rgba, mode="RGBA")
            return self._save_tiles_from_image(img, timestamp), timestamp
        png_file = self.outdir / f"{self.file_name}_{timestamp}.png"
        Image.fromarray(rgba, mode="RGBA").save(png_file, compress_level=1)
        return [png_file], timestamp

    def _save_tiles_from_image(self, img: Image.Image, timestamp: str):
        from EWMRS.render.config import TILE_SIZE

        width, height = img.size
        grid_cols = width // TILE_SIZE
        grid_rows = height // TILE_SIZE
        tile_dir = self.outdir / timestamp
        tile_dir.mkdir(parents=True, exist_ok=True)
        tile_paths = []
        for tile_y in range(grid_rows):
            for tile_x in range(grid_cols):
                left = tile_x * TILE_SIZE
                right = left + TILE_SIZE
                top = (grid_rows - 1 - tile_y) * TILE_SIZE
                bottom = top + TILE_SIZE
                tile = img.crop((left, top, right, bottom))
                tile_path = tile_dir / f"tile_{tile_x}_{tile_y}.png"
                tile.save(tile_path, compress_level=1)
                tile_paths.append(tile_path)
        return tile_paths


def _run_renderer_benchmark(renderer_cls, ds, out_root: Path, runs: int = 3, warmups: int = 1) -> BenchmarkResult:
    timings = []
    for idx in range(warmups + runs):
        renderer = renderer_cls(ds, out_root / f"run_{idx}", "Bench", "Layer", "2026-03-17T20:00:00")
        start = time.perf_counter()
        renderer.convert_to_png(tile_output=True)
        elapsed = time.perf_counter() - start
        if idx >= warmups:
            timings.append(elapsed)
    return BenchmarkResult(renderer_cls.__name__, timings)


def _run_layer_benchmark(source_dir: Path, out_dir: Path, cached_runs: int = 3) -> tuple[float, BenchmarkResult]:
    layer = {"name": "BenchLayer", "colormap_key": "Bench", "filepath": source_dir, "outdir": out_dir}
    start = time.perf_counter()
    _render_layer(layer)
    fresh_timing = time.perf_counter() - start

    cached_timings = []
    for _ in range(cached_runs):
        start = time.perf_counter()
        _render_layer(layer)
        cached_timings.append(time.perf_counter() - start)
    return fresh_timing, BenchmarkResult("cached_render_layer", cached_timings)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_dir = Path(td)
        cmap_path = temp_dir / "colormaps.json"
        cmap_path.write_text(json.dumps([
            {"colormaps": [{"name": "Bench", "interpolate": True, "thresholds": [
                {"value": 0, "rgb": [0, 0, 0]},
                {"value": 50, "rgb": [128, 128, 128]},
                {"value": 100, "rgb": [255, 255, 255]},
            ]}]}
        ]))

        original_cmap = fs.GUI_COLORMAP_JSON
        fs.GUI_COLORMAP_JSON = cmap_path
        _COLORMAP_CACHE.clear()

        data = np.linspace(0, 100, 3500 * 7000, dtype=np.float32).reshape(3500, 7000)
        ds = _FakeDataset(data)
        baseline = _run_renderer_benchmark(BaselineRenderer, ds, temp_dir / "baseline")
        optimized = _run_renderer_benchmark(GUILayerRenderer, ds, temp_dir / "optimized")

        source_dir = temp_dir / "source"
        source_dir.mkdir()
        import xarray as xr

        lat = np.linspace(20, 55, 700, dtype=np.float32)
        lon = np.linspace(-130, -60, 1400, dtype=np.float32)
        source_ds = xr.Dataset({"unknown": (("latitude", "longitude"), np.linspace(0, 100, lat.size * lon.size, dtype=np.float32).reshape(lat.size, lon.size))}, coords={"latitude": lat, "longitude": lon})
        source_ds.to_netcdf(source_dir / "sample_20260317-200000.nc")
        fresh_layer_timing, cached = _run_layer_benchmark(source_dir, temp_dir / "layer_out")

        fs.GUI_COLORMAP_JSON = original_cmap

    print(json.dumps({
        "baseline_renderer": {"median_s": baseline.median_s, "p95_s": baseline.p95_s, "runs": baseline.timings},
        "optimized_renderer": {"median_s": optimized.median_s, "p95_s": optimized.p95_s, "runs": optimized.timings},
        "renderer_improvement_pct": (baseline.median_s - optimized.median_s) / baseline.median_s * 100.0,
        "fresh_render_layer_s": fresh_layer_timing,
        "cached_render_layer": {"median_s": cached.median_s, "p95_s": cached.p95_s, "runs": cached.timings},
    }, indent=2))


if __name__ == "__main__":
    main()
