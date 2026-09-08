"""Detailed memory profiling of NEXRAD worker parse path.

Breaks down memory usage at each stage:
1. After parsing raw volume
2. After grouping sweeps
3. After opening synthetic datatree
4. During NetCDF write (per group)
5. Final peak

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_memory_profile.py
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import resource
import tracemalloc
from pathlib import Path
import tempfile
import time

import numpy as np
import psutil
import xarray as xr


RADARS = ("KTLH", "KDGX", "KTLX")
AZIMUTH_COUNT = 720
RANGE_COUNT = 1832
VARIABLE_SPECS = (
    ("DBZH", 1.0),
    ("VRADH", 10.0),
    ("WRADH", 20.0),
    ("RHOHV", 30.0),
)


def _rss_mb() -> float:
    """Current RSS of this process in MB."""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_maxrss / 1024.0


def _tracemalloc_mb():
    """Current tracemalloc snapshot stats."""
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")
    top = stats[:10]
    total = sum(s.size for s in stats) / (1024 * 1024)
    return total, top


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


def _child_profile(site: str, volume_id: str, output_root: str, volume_path: str, result_queue) -> None:
    from common.ingest.nexrad.parser import RawSweep, RawVolume
    import common.ingest.nexrad.worker as worker
    from common.ingest.nexrad.grouping import group_sweeps_by_elevation, elevation_group_key
    from common.ingest.nexrad.writer import (
        _write_elevation_netcdf, _sanitize_dataset, _slim_dataset_from_node,
        _empty_root_dataset, _dataset_encoding, _sanitize_attrs,
    )
    from common.ingest.nexrad.models import SweepRecord

    tracemalloc.start()

    stages = {}

    # Stage 0: baseline
    stages["baseline_rss_mb"] = _rss_mb()
    tm_total, tm_top = _tracemalloc_mb()
    stages["baseline_tracemalloc_mb"] = tm_total

    # Build synthetic data
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
                index=0, group_name="/sweep_0", elevation_number=1,
                fixed_angle=0.5, first_timestamp="2026-05-19T15:00:00Z",
                last_timestamp="2026-05-19T15:00:59Z", radial_count=AZIMUTH_COUNT, complete=True,
            ),
            RawSweep(
                index=1, group_name="/sweep_1", elevation_number=2,
                fixed_angle=0.9, first_timestamp="2026-05-19T15:01:00Z",
                last_timestamp="2026-05-19T15:01:59Z", radial_count=AZIMUTH_COUNT, complete=True,
            ),
        ],
    )

    # Stage 1: after building synthetic datatree + raw volume
    stages["after_build_datatree_rss_mb"] = _rss_mb()
    tm_total, tm_top = _tracemalloc_mb()
    stages["after_build_datatree_tracemalloc_mb"] = tm_total
    stages["after_build_datatree_top3"] = [
        {"file": str(s.traceback), "size_mb": s.size / (1024*1024)}
        for s in tm_top[:3]
    ]

    # Stage 2: parse raw volume (mocked)
    worker.parse_raw_volume_file = lambda _path: raw_volume

    raw_vol = worker.parse_raw_volume_file(volume_path)
    stages["after_parse_raw_rss_mb"] = _rss_mb()

    # Stage 3: group sweeps
    sweep_records = []
    for idx, raw_sweep in enumerate(raw_vol.sweeps):
        if raw_sweep.fixed_angle is None:
            continue
        if raw_sweep.radial_count <= 0 or not raw_sweep.complete:
            continue
        sweep_records.append(SweepRecord(
            index=idx,
            group_name=raw_sweep.group_name,
            fixed_angle=raw_sweep.fixed_angle,
            waveform=raw_sweep.waveform,
            timestamp=raw_sweep.last_timestamp,
            azimuth_count=raw_sweep.radial_count,
        ))

    elevation_groups = group_sweeps_by_elevation(sweep_records)
    stages["after_group_sweeps_rss_mb"] = _rss_mb()
    tm_total, tm_top = _tracemalloc_mb()
    stages["after_group_sweeps_tracemalloc_mb"] = tm_total

    # Stage 4: process each elevation group
    datatree = tree
    stages["after_open_datatree_rss_mb"] = _rss_mb()
    tm_total, tm_top = _tracemalloc_mb()
    stages["after_open_datatree_tracemalloc_mb"] = tm_total

    for gi, group in enumerate(elevation_groups):
        key = elevation_group_key(group)
        group_names = [m.group_name for m in group.members]
        elevation_label = str(group.canonical_angle_deg)
        first_ts = group.first_timestamp

        # Measure memory before writing
        before_rss = _rss_mb()
        before_tm, _ = _tracemalloc_mb()

        # Simulate what write_elevation_artifacts does for netcdf path
        root_attrs = {
            "site": site, "volume_id": volume_id, "scan_timestamp": "20260519-150000",
            "elevation": elevation_label, "elevation_timestamp": first_ts,
            "first_sweep_index": group.first_sweep_index,
            "last_sweep_index": group.last_sweep_index,
            "supplemental": group.supplemental,
        }

        nc_path = Path(output_root) / f"{site}_{elevation_label}.nc"
        nc_path.parent.mkdir(parents=True, exist_ok=True)

        # These are the key operations that create copies
        _empty_root_dataset(root_attrs).to_netcdf(nc_path)
        for group_name in group_names:
            node = datatree[group_name]
            slim = _slim_dataset_from_node(node)
            sanitized = _sanitize_dataset(slim)
            encoding = _dataset_encoding(sanitized)
            sanitized.to_netcdf(nc_path, mode="a", group=group_name.lstrip("/"), encoding=encoding)

        after_rss = _rss_mb()
        after_tm, _ = _tracemalloc_mb()

        stages[f"group_{gi}_write_delta_rss_mb"] = after_rss - before_rss
        stages[f"group_{gi}_write_delta_tracemalloc_mb"] = after_tm - before_tm
        stages[f"group_{gi}_after_write_rss_mb"] = after_rss

    # Stage 5: final peak
    stages["final_rss_mb"] = _rss_mb()
    tm_total, tm_top = _tracemalloc_mb()
    stages["final_tracemalloc_mb"] = tm_total
    stages["final_top5"] = [
        {"file": str(s.traceback), "size_mb": s.size / (1024*1024)}
        for s in tm_top[:5]
    ]

    tracemalloc.stop()

    result_queue.put({
        "site": site,
        "stages": stages,
    })


def run_profile():
    with tempfile.TemporaryDirectory(prefix="nexrad_profile_", dir="/tmp/kilo") as tmp_dir:
        base = Path(tmp_dir)
        result_queue = mp.Queue()
        processes = []

        for index in range(1):  # Start with 1 to isolate single-process behavior
            site = RADARS[index]
            volume_id = f"VOL{index + 1:03d}"
            volume_path = base / f"{site}_{volume_id}.ar2v"
            volume_path.write_bytes(b"benchmark")
            process = mp.Process(
                target=_child_profile,
                args=(site, volume_id, str(base / "output"), str(volume_path), result_queue),
            )
            process.start()
            processes.append(process)

        for process in processes:
            process.join(timeout=30)

        results = [result_queue.get(timeout=5) for _ in processes]
        return results


def main():
    results = run_profile()

    for result in results:
        print(f"\n=== Memory profile for {result['site']} ===")
        stages = result["stages"]
        for key, value in stages.items():
            if isinstance(value, float):
                print(f"  {key}: {value:.1f} MB")
            elif isinstance(value, list):
                print(f"  {key}:")
                for item in value:
                    if isinstance(item, dict):
                        size = item.get("size_mb", 0)
                        f = item.get("file", "")
                        # Truncate file path for readability
                        if len(f) > 80:
                            f = "..." + f[-77:]
                        print(f"    {size:.1f} MB: {f}")
            else:
                print(f"  {key}: {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
