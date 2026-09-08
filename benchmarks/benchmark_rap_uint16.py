"""Benchmark EWMRS RAP Uint16Array conversion per configured layer.

Run with:
    python -m pytest benchmarks/benchmark_rap_uint16.py -v -s
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _sample_rap_path() -> str | None:
    import util.file as fs

    try:
        files = fs.latest_files(fs.RAP_DIR, 1)
        return files[-1] if files else None
    except Exception:
        return None


def _benchmark_layers(tmp_path: Path) -> list[dict]:
    from EWMRS.rap.config import get_rap_uint16_layers

    layers = []
    for layer in get_rap_uint16_layers():
        benchmark_layer = dict(layer)
        benchmark_layer["outdir"] = tmp_path / "gui" / "RAP" / str(layer["name"])
        layers.append(benchmark_layer)
    return layers


def _print_timing_table(timings: dict[str, dict]) -> None:
    print("\nRAP Uint16Array per-layer benchmark")
    print("=" * 92)
    print(f"{'Layer':32} {'Status':18} {'Seconds':>10} {'Points':>12} {'Shape':>14}")
    print("-" * 92)
    for layer_name, timing in timings.items():
        seconds = timing.get("seconds")
        seconds_text = "n/a" if seconds is None else f"{seconds:.3f}"
        point_count = timing.get("point_count", "n/a")
        shape = timing.get("shape", "n/a")
        print(f"{layer_name:32} {timing.get('status', 'unknown'):18} {seconds_text:>10} {str(point_count):>12} {str(shape):>14}")
    print("=" * 92)


def test_rap_uint16_layer_benchmark(tmp_path):
    sample_rap = _sample_rap_path()
    if sample_rap is None:
        pytest.skip("No sample RAP file available in fs.RAP_DIR")

    from EWMRS.rap.uint16_pipeline import run_rap_uint16_pipeline

    timings: dict[str, dict] = {}
    results = run_rap_uint16_pipeline(
        sample_rap,
        layers=_benchmark_layers(tmp_path),
        timings=timings,
        force=True,
    )

    _print_timing_table(timings)

    converted = [layer for layer, path in results.items() if path is not None]
    assert converted, "No configured RAP layers were converted from the sample RAP file"
