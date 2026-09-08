"""Synthetic benchmark for AzShear feature integration.

Run with:
    PYTHONPATH=src python benchmarks/benchmark_azshear_integration.py
"""

from __future__ import annotations

import copy
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import xarray as xr

from EdgeWARN.process.integrate.core.integrator import StormCellIntegrator


def _make_cells(rows: int = 5, cols: int = 5) -> list[dict]:
    cells = []
    lat0 = 30.15
    lon0 = -95.85
    step = 0.12
    size = 0.09
    for row in range(rows):
        for col in range(cols):
            cy = lat0 + row * step
            cx = lon0 + col * step
            half = size / 2.0
            cells.append(
                {
                    "id": f"cell_{row}_{col}",
                    "bbox": [
                        [cy - half, cx - half],
                        [cy - half, cx + half],
                        [cy + half, cx + half],
                        [cy + half, cx - half],
                        [cy - half, cx - half],
                    ],
                    "centroid": [cy, cx],
                    "properties": {},
                }
            )
    return cells


def _add_blob(data: np.ndarray, cy: int, cx: int, radius: int, value: float) -> None:
    yy, xx = np.ogrid[: data.shape[0], : data.shape[1]]
    mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
    data[mask] = np.maximum(data[mask], value)


def _write_synthetic_datasets(root: Path) -> tuple[Path, Path]:
    lat = np.linspace(30.0, 31.0, 401)
    lon = np.linspace(-96.0, -95.0, 401)
    low = np.zeros((401, 401), dtype=np.float32)
    mid = np.zeros((401, 401), dtype=np.float32)

    centers = [
        (70, 80, 18, 10.5, 7.2),
        (110, 115, 22, 11.2, 7.4),
        (150, 140, 20, 10.1, 6.8),
        (165, 190, 16, 9.5, 6.5),
        (210, 170, 21, 10.9, 7.1),
        (240, 220, 18, 10.3, 6.9),
        (275, 250, 24, 11.0, 7.0),
        (320, 300, 20, 9.8, 6.6),
        (340, 180, 14, 8.8, 6.2),
    ]
    for cy, cx, radius, low_value, mid_value in centers:
        _add_blob(low, cy, cx, radius, low_value)
        _add_blob(mid, cy + 4, cx + 3, max(radius - 3, 8), mid_value)

    low_path = root / "azshear_low.nc"
    mid_path = root / "azshear_mid.nc"
    xr.Dataset({"unknown": (("latitude", "longitude"), low)}, coords={"latitude": lat, "longitude": lon}).to_netcdf(low_path)
    xr.Dataset({"unknown": (("latitude", "longitude"), mid)}, coords={"latitude": lat, "longitude": lon}).to_netcdf(mid_path)
    return low_path, mid_path


def run_benchmark(runs: int = 8, warmups: int = 2) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="azshear_benchmark_") as tmpdir:
        root = Path(tmpdir)
        low_path, mid_path = _write_synthetic_datasets(root)
        cells = _make_cells()
        integrator = StormCellIntegrator(MagicMock())

        for _ in range(max(warmups, 0)):
            integrator.integrate_azshear_features(str(low_path), str(mid_path), copy.deepcopy(cells))

        timings = []
        for _ in range(max(runs, 1)):
            start = time.perf_counter()
            integrator.integrate_azshear_features(str(low_path), str(mid_path), copy.deepcopy(cells))
            timings.append(time.perf_counter() - start)

        return {
            "runs": float(len(timings)),
            "mean_seconds": statistics.mean(timings),
            "median_seconds": statistics.median(timings),
            "min_seconds": min(timings),
            "max_seconds": max(timings),
        }


if __name__ == "__main__":
    results = run_benchmark()
    print("AzShear integration synthetic benchmark")
    for key, value in results.items():
        if key == "runs":
            print(f"- {key}: {int(value)}")
        else:
            print(f"- {key}: {value:.4f}")
