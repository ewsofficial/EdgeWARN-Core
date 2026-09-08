"""Benchmark NEXRAD worker memory for simultaneous volumes.

This harness measures resident memory while running the NEXRAD worker parse path
for N simultaneous volumes, always using different radar IDs to avoid output
collisions. The benchmark uses a synthetic datatree and synthetic raw sweep
metadata so it can stress the worker/export path without requiring live Level-II
sample files.

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_memory.py
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time

import numpy as np
import psutil
import xarray as xr


RADARS = ("KTLH", "KDGX", "KTLX", "KMXX", "KBMX", "KDOX", "KOUN", "KAMX")
SIMULTANEOUS_COUNTS = (1, 2, 4, 8)
AZIMUTH_COUNT = 720
RANGE_COUNT = 1832
VARIABLE_SPECS = (
    ("DBZH", 1.0),
    ("VRADH", 10.0),
    ("WRADH", 20.0),
    ("RHOHV", 30.0),
)
SLEEP_SECONDS = 0.25
SAMPLE_INTERVAL_SECONDS = 0.02


def _total_rss_mb(process: psutil.Process) -> float:
    total = 0
    try:
        total += process.memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0

    try:
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    return total / (1024.0 * 1024.0)


def _make_dataset(angle: float, waveform: str, sweep_index: int) -> xr.Dataset:
    azimuth = np.linspace(0.0, 359.5, AZIMUTH_COUNT, dtype=np.float32)
    ranges = np.linspace(250.0, 250.0 * RANGE_COUNT, RANGE_COUNT, dtype=np.float32)
    time_values = np.array(
        [np.datetime64(f"2026-05-19T15:{sweep_index:02d}:00") + np.timedelta64(i, "ms") for i in range(AZIMUTH_COUNT)]
    )

    data_vars = {}
    base_grid = (
        np.arange(AZIMUTH_COUNT * RANGE_COUNT, dtype=np.float32).reshape(AZIMUTH_COUNT, RANGE_COUNT)
    )
    for name, offset in VARIABLE_SPECS:
        values = (base_grid % 256).astype(np.float32, copy=False) + np.float32(offset)
        data_vars[name] = (("azimuth", "range"), values)

    ds = xr.Dataset(
        data_vars=data_vars,
        coords={
            "azimuth": azimuth,
            "range": ranges,
            "time": ("azimuth", time_values),
        },
        attrs={"waveform_type": waveform},
    )
    ds["sweep_fixed_angle"] = xr.DataArray(np.float32(angle))
    return ds


class _Node:
    def __init__(self, dataset: xr.Dataset):
        self.ds = dataset
        self.attrs = dict(dataset.attrs)

    def to_dataset(self) -> xr.Dataset:
        return self.ds


class _Tree:
    def __init__(self, groups: dict[str, xr.Dataset]):
        self._groups = {name: _Node(dataset) for name, dataset in groups.items()}
        self.groups = list(groups)
        self.attrs = {"scan_name": "VCP-212", "scan_strategy": "standard"}

    def __getitem__(self, key: str) -> _Node:
        return self._groups[key]


def _child_entry(site: str, volume_id: str, output_root: str, volume_path: str, start_event, result_queue) -> None:
    from common.ingest.nexrad.parser import RawSweep, RawVolume
    import common.ingest.nexrad.worker as worker

    groups = {
        "/sweep_0": _make_dataset(0.5, "surveillance", 0),
        "/sweep_1": _make_dataset(0.9, "contiguous_doppler", 1),
    }
    tree = _Tree(groups)
    raw_volume = RawVolume(
        volume_header=b"AR2V" + (b"\x00" * 20),
        site=site,
        sweeps=[
            RawSweep(
                index=0,
                group_name="/sweep_0",
                elevation_number=1,
                fixed_angle=0.5,
                first_timestamp="2026-05-19T15:00:00Z",
                last_timestamp="2026-05-19T15:00:59Z",
                radial_count=AZIMUTH_COUNT,
                complete=True,
            ),
            RawSweep(
                index=1,
                group_name="/sweep_1",
                elevation_number=2,
                fixed_angle=0.9,
                first_timestamp="2026-05-19T15:01:00Z",
                last_timestamp="2026-05-19T15:01:59Z",
                radial_count=AZIMUTH_COUNT,
                complete=True,
            ),
        ],
    )

    worker.parse_raw_volume_file = lambda _path: raw_volume

    start_event.wait()
    started = time.perf_counter()
    result = worker.parse_and_export(
        volume_path=volume_path,
        output_root=output_root,
        site=site,
        volume_id=volume_id,
        scan_timestamp="20260519-150000",
        seen_elevation_keys=set(),
    )
    time.sleep(SLEEP_SECONDS)
    result_queue.put(
        {
            "site": site,
            "duration_s": time.perf_counter() - started,
            "visible_sweeps": result.visible_sweeps,
            "saved_elevations": len(result.saved_elevations),
            "child_rss_kb": result.child_rss_kb,
            "parse_error": result.parse_error,
        }
    )


def run_benchmark(simultaneous_volumes: int) -> dict:
    if simultaneous_volumes > len(RADARS):
        raise ValueError("Not enough unique radar IDs configured")

    parent = psutil.Process(os.getpid())
    baseline_mb = _total_rss_mb(parent)

    with tempfile.TemporaryDirectory(prefix=f"nexrad_mem_{simultaneous_volumes}_", dir="/tmp/kilo") as tmp_dir:
        base = Path(tmp_dir)
        start_event = mp.Event()
        result_queue = mp.Queue()
        processes: list[mp.Process] = []

        for index in range(simultaneous_volumes):
            site = RADARS[index]
            volume_id = f"VOL{index + 1:03d}"
            volume_path = base / f"{site}_{volume_id}.ar2v"
            volume_path.write_bytes(b"benchmark")
            process = mp.Process(
                target=_child_entry,
                args=(site, volume_id, str(base / "output"), str(volume_path), start_event, result_queue),
            )
            process.start()
            processes.append(process)

        peak_mb = baseline_mb
        started = time.perf_counter()
        start_event.set()

        while any(process.is_alive() for process in processes):
            peak_mb = max(peak_mb, _total_rss_mb(parent))
            time.sleep(SAMPLE_INTERVAL_SECONDS)

        for process in processes:
            process.join(timeout=10)

        peak_mb = max(peak_mb, _total_rss_mb(parent))
        ended = time.perf_counter()

        results = [result_queue.get(timeout=5) for _ in processes]

    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]
    return {
        "simultaneous_volumes": simultaneous_volumes,
        "radars": list(RADARS[:simultaneous_volumes]),
        "duration_s": ended - started,
        "baseline_total_rss_mb": baseline_mb,
        "peak_total_rss_mb": peak_mb,
        "delta_total_rss_mb": peak_mb - baseline_mb,
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


def main() -> int:
    summaries = [run_benchmark(count) for count in SIMULTANEOUS_COUNTS]

    print("NEXRAD simultaneous-volume memory benchmark")
    print(
        "Synthetic worker benchmark with different radars, total RSS includes parent and child processes."
    )
    print()
    print(" n | peak_total_mb | delta_mb | mean_child_mb | max_child_mb | duration_s ")
    print("---+---------------+----------+---------------+--------------+-----------")
    for summary in summaries:
        print(
            f" {summary['simultaneous_volumes']:>1} | "
            f"{summary['peak_total_rss_mb']:>13.1f} | "
            f"{summary['delta_total_rss_mb']:>8.1f} | "
            f"{summary['mean_child_rss_mb']:>13.1f} | "
            f"{summary['max_child_rss_mb']:>12.1f} | "
            f"{summary['duration_s']:>9.2f}"
        )
    print()
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
