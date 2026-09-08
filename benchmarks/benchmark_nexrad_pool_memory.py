"""Benchmark NEXRAD worker pool vs subprocess memory usage.

Measures the real production difference:
- subprocess: spawns fresh Python interpreter via subprocess.run (no shared memory, ~114 MB import baseline)
- pool: forks from parent via ProcessPoolExecutor (copy-on-write shared memory)

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_pool_memory.py
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import resource
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import psutil
import xarray as xr


RADARS = ("KTLH", "KDGX", "KTLX", "KMXX")
SIMULTANEOUS_COUNTS = (1, 2, 4)
AZIMUTH_COUNT = 720
RANGE_COUNT = 1832
VARIABLE_SPECS = (
    ("DBZH", 1.0),
    ("VRADH", 10.0),
    ("WRADH", 20.0),
    ("RHOHV", 30.0),
)

WORKER_SCRIPT = """
import sys, json, resource, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])

import numpy as np
import xarray as xr

from common.ingest.nexrad.parser import RawSweep, RawVolume
import common.ingest.nexrad.worker as worker

site = sys.argv[2]
volume_id = sys.argv[3]
output_root = sys.argv[4]
volume_path = sys.argv[5]
result_file = sys.argv[6]

AZIMUTH_COUNT = 720
RANGE_COUNT = 1832
VARIABLE_SPECS = (("DBZH", 1.0), ("VRADH", 10.0), ("WRADH", 20.0), ("RHOHV", 30.0))

azimuth = np.linspace(0.0, 359.5, AZIMUTH_COUNT, dtype=np.float32)
ranges = np.linspace(250.0, 250.0 * RANGE_COUNT, RANGE_COUNT, dtype=np.float32)
time_values = np.array([np.datetime64("2026-05-19T15:00:00") + np.timedelta64(i, "ms") for i in range(AZIMUTH_COUNT)])

data_vars = {}
base_grid = np.arange(AZIMUTH_COUNT * RANGE_COUNT, dtype=np.float32).reshape(AZIMUTH_COUNT, RANGE_COUNT)
for name, offset in VARIABLE_SPECS:
    values = (base_grid % 256).astype(np.float32, copy=False) + np.float32(offset)
    data_vars[name] = (("azimuth", "range"), values)

ds = xr.Dataset(
    data_vars=data_vars,
    coords={"azimuth": azimuth, "range": ranges, "time": ("azimuth", time_values)},
    attrs={"waveform_type": "surveillance"},
)
ds["sweep_fixed_angle"] = xr.DataArray(np.float32(0.5))

class FakeNode:
    def __init__(self, ds):
        self.ds = ds
        self.attrs = {"waveform_type": "surveillance"}
    def to_dataset(self):
        return self.ds

class FakeTree:
    def __init__(self, groups, attrs):
        self._groups = groups
        self.groups = list(groups)
        self.attrs = attrs
    def __getitem__(self, key):
        return self._groups[key]

tree = FakeTree({"/sweep_0": FakeNode(ds)}, {"scan_name": "VCP-212"})
raw_volume = RawVolume(
    volume_header=b"AR2V" + b"\\x00" * 20,
    site=site,
    sweeps=[
        RawSweep(index=0, group_name="/sweep_0", elevation_number=1, fixed_angle=0.5,
                 first_timestamp="2026-05-19T15:00:00Z", last_timestamp="2026-05-19T15:00:59Z",
                 radial_count=AZIMUTH_COUNT, complete=True),
    ],
)

worker.parse_raw_volume_file = lambda _path: raw_volume

result = worker.parse_and_export(
    volume_path=volume_path, output_root=output_root, site=site, volume_id=volume_id,
    scan_timestamp="20260519-150000", seen_elevation_keys=set(),
)

ru = resource.getrusage(resource.RUSAGE_SELF)
payload = {"site": site, "child_rss_kb": ru.ru_maxrss, "visible_sweeps": result.visible_sweeps,
           "saved_elevations": len(result.saved_elevations), "parse_error": result.parse_error}
Path(result_file).write_text(json.dumps(payload))
"""


def _pool_initializer() -> None:
    import numpy as np  # noqa: F401
    import xarray as xr  # noqa: F401
    import scipy  # noqa: F401
    import pandas as pd  # noqa: F401
    import dask  # noqa: F401
    import netCDF4  # noqa: F401
    import botocore  # noqa: F401


def _pool_parse_task(src_root: str, site: str, volume_id: str, output_root: str, volume_path: str) -> dict:
    import resource
    from common.ingest.nexrad.parser import RawSweep, RawVolume
    import common.ingest.nexrad.worker as worker

    azimuth = np.linspace(0.0, 359.5, AZIMUTH_COUNT, dtype=np.float32)
    ranges = np.linspace(250.0, 250.0 * RANGE_COUNT, RANGE_COUNT, dtype=np.float32)
    time_values = np.array(
        [np.datetime64("2026-05-19T15:00:00") + np.timedelta64(i, "ms") for i in range(AZIMUTH_COUNT)]
    )

    data_vars = {}
    base_grid = np.arange(AZIMUTH_COUNT * RANGE_COUNT, dtype=np.float32).reshape(AZIMUTH_COUNT, RANGE_COUNT)
    for name, offset in VARIABLE_SPECS:
        values = (base_grid % 256).astype(np.float32, copy=False) + np.float32(offset)
        data_vars[name] = (("azimuth", "range"), values)

    ds = xr.Dataset(
        data_vars=data_vars,
        coords={"azimuth": azimuth, "range": ranges, "time": ("azimuth", time_values)},
        attrs={"waveform_type": "surveillance"},
    )
    ds["sweep_fixed_angle"] = xr.DataArray(np.float32(0.5))

    class FakeNode:
        def __init__(self, ds):
            self.ds = ds
            self.attrs = {"waveform_type": "surveillance"}
        def to_dataset(self):
            return self.ds

    class FakeTree:
        def __init__(self, groups, attrs):
            self._groups = groups
            self.groups = list(groups)
            self.attrs = attrs
        def __getitem__(self, key):
            return self._groups[key]

    tree = FakeTree({"/sweep_0": FakeNode(ds)}, {"scan_name": "VCP-212"})
    raw_volume = RawVolume(
        volume_header=b"AR2V" + b"\x00" * 20,
        site=site,
        sweeps=[
            RawSweep(index=0, group_name="/sweep_0", elevation_number=1, fixed_angle=0.5,
                     first_timestamp="2026-05-19T15:00:00Z", last_timestamp="2026-05-19T15:00:59Z",
                     radial_count=AZIMUTH_COUNT, complete=True),
        ],
    )

    worker.parse_raw_volume_file = lambda _path: raw_volume

    result = worker.parse_and_export(
        volume_path=volume_path, output_root=output_root, site=site, volume_id=volume_id,
        scan_timestamp="20260519-150000", seen_elevation_keys=set(),
    )

    ru = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "site": site,
        "child_rss_kb": ru.ru_maxrss,
        "visible_sweeps": result.visible_sweeps,
        "saved_elevations": len(result.saved_elevations),
        "parse_error": result.parse_error,
    }


def run_subprocess_benchmark(simultaneous_volumes: int, src_root: str) -> dict:
    """Simulates production: each parse spawns a fresh Python interpreter."""
    with tempfile.TemporaryDirectory(prefix=f"nexrad_sub_{simultaneous_volumes}_", dir="/tmp/kilo") as tmp_dir:
        base = Path(tmp_dir)
        output_root = base / "output"
        output_root.mkdir()

        processes: list[subprocess.Popen] = []
        result_files: list[Path] = []
        started = time.perf_counter()

        for index in range(simultaneous_volumes):
            site = RADARS[index]
            volume_id = f"VOL{index + 1:03d}"
            volume_path = base / f"{site}_{volume_id}.ar2v"
            volume_path.write_bytes(b"AR2V" + b"\x00" * 20)
            result_file = base / f"result_{site}.json"
            result_files.append(result_file)

            proc = subprocess.Popen(
                [sys.executable, "-c", WORKER_SCRIPT, src_root, site, volume_id, str(output_root), str(volume_path), str(result_file)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(proc)

        for proc in processes:
            proc.wait(timeout=60)

        ended = time.perf_counter()

        results = []
        for rf in result_files:
            if rf.exists():
                results.append(json.loads(rf.read_text()))

        ended = time.perf_counter()

    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]
    return {
        "simultaneous_volumes": simultaneous_volumes,
        "radars": list(RADARS[:simultaneous_volumes]),
        "duration_s": ended - started,
        "total_child_rss_mb": sum(child_rss_mb),
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


def run_pool_benchmark(simultaneous_volumes: int, src_root: str) -> dict:
    """Uses ProcessPoolExecutor with fork (copy-on-write)."""
    with tempfile.TemporaryDirectory(prefix=f"nexrad_pool_{simultaneous_volumes}_", dir="/tmp/kilo") as tmp_dir:
        base = Path(tmp_dir)
        output_root = base / "output"
        output_root.mkdir()

        tasks = []
        for index in range(simultaneous_volumes):
            site = RADARS[index]
            volume_id = f"VOL{index + 1:03d}"
            volume_path = base / f"{site}_{volume_id}.ar2v"
            volume_path.write_bytes(b"AR2V" + b"\x00" * 20)
            tasks.append((src_root, site, volume_id, str(output_root), str(volume_path)))

        started = time.perf_counter()

        with ProcessPoolExecutor(
            max_workers=simultaneous_volumes,
            initializer=_pool_initializer,
        ) as executor:
            futures = [executor.submit(_pool_parse_task, *task) for task in tasks]
            results = [f.result() for f in futures]

        ended = time.perf_counter()

    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]
    return {
        "simultaneous_volumes": simultaneous_volumes,
        "radars": list(RADARS[:simultaneous_volumes]),
        "duration_s": ended - started,
        "total_child_rss_mb": sum(child_rss_mb),
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


def main() -> int:
    src_root = str(Path(__file__).parent.parent.parent / "src")

    print("NEXRAD worker pool vs subprocess memory comparison")
    print(f"Source root: {src_root}")
    print()
    print("Key difference:")
    print("  subprocess = fresh Python interpreter per parse (no shared memory)")
    print("  pool       = forked workers with copy-on-write (shared import pages)")
    print()

    all_sub = []
    all_pool = []

    for count in SIMULTANEOUS_COUNTS:
        print(f"--- n={count} subprocess ---")
        sub = run_subprocess_benchmark(count, src_root)
        all_sub.append(sub)
        print(f"  mean_child_mb: {sub['mean_child_rss_mb']:.1f}, total_child_mb: {sub['total_child_rss_mb']:.1f}, duration: {sub['duration_s']:.2f}s")

        print(f"--- n={count} pool ---")
        pool = run_pool_benchmark(count, src_root)
        all_pool.append(pool)
        print(f"  mean_child_mb: {pool['mean_child_rss_mb']:.1f}, total_child_mb: {pool['total_child_rss_mb']:.1f}, duration: {pool['duration_s']:.2f}s")

        savings = sub["mean_child_rss_mb"] - pool["mean_child_rss_mb"]
        pct = (savings / sub["mean_child_rss_mb"]) * 100 if sub["mean_child_rss_mb"] > 0 else 0
        total_savings = sub["total_child_rss_mb"] - pool["total_child_rss_mb"]
        print(f"  savings: {savings:.1f} MB per child, {total_savings:.1f} MB total ({pct:.1f}%)")
        print()

    print("=" * 80)
    print("SUMMARY: subprocess vs pool")
    print(" n | subprocess_mean_mb | pool_mean_mb | savings_per_mb | savings_pct ")
    print("---+--------------------+--------------+----------------+------------")
    for sub, pool in zip(all_sub, all_pool):
        savings = sub["mean_child_rss_mb"] - pool["mean_child_rss_mb"]
        pct = (savings / sub["mean_child_rss_mb"]) * 100 if sub["mean_child_rss_mb"] > 0 else 0
        print(
            f" {sub['simultaneous_volumes']:>1} | "
            f"{sub['mean_child_rss_mb']:>18.1f} | "
            f"{pool['mean_child_rss_mb']:>12.1f} | "
            f"{savings:>14.1f} | "
            f"{pct:>10.1f}%"
        )

    print()
    print(json.dumps({"subprocess": all_sub, "pool": all_pool}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
