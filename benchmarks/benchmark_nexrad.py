"""Unified NEXRAD worker memory benchmark (synthetic path).

Single entry point for synthetic worker-memory measurement with explicit
``--output-dir`` and subprocess/pool/compare execution. The legacy live
scripts (``benchmark_nexrad_memory_live.py``,
``benchmark_nexrad_live_pool_memory.py``) keep the opt-in S3 path with the
same output-dir contract; ``benchmark_nexrad_memory.py`` and
``benchmark_nexrad_pool_memory.py`` are thin shims over this sampler.

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad.py --output-dir /tmp/nexrad_bench --mode synthetic
"""
from __future__ import annotations

import multiprocessing as mp
import os
import tempfile
import time
from argparse import ArgumentParser
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import psutil


RADARS = ("KTLH", "KDGX", "KTLX", "KMXX", "KBMX", "KDOX", "KOUN", "KAMX")
DEFAULT_SIMULTANEOUS = (1, 2, 4)
AZIMUTH_COUNT = 720
SLEEP_SECONDS = 0.25
SAMPLE_INTERVAL_SECONDS = 0.02


def _total_rss_mb(process: psutil.Process) -> float:
    total = 0.0
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


def _build_synthetic_volume(site: str, volume_id: str):
    """Fabricate a two-sweep volume against the current worker model.

    Uses ``models.RawVolumeBuffer``/``RawSweepRange`` (the types
    ``worker.parse_and_export`` actually consumes) with empty record ranges:
    grouping and export bookkeeping run for real while no AR2V bytes are
    needed, keeping the benchmark offline and deterministic.
    """
    from common.ingest.nexrad.models import RawSweepRange, RawVolumeBuffer

    _ = volume_id
    return RawVolumeBuffer(
        volume_header=b"AR2V" + (b"\x00" * 20),
        site=site,
        record_buffer=b"",
        sweeps=[
            RawSweepRange(
                index=0,
                group_name="/sweep_0",
                elevation_number=1,
                fixed_angle=0.5,
                first_timestamp="2026-05-19T15:00:00Z",
                last_timestamp="2026-05-19T15:00:59Z",
                radial_count=AZIMUTH_COUNT,
                complete=True,
            ),
            RawSweepRange(
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


def _run_volume_once(site: str, volume_id: str, output_root: str, volume_path: str) -> dict:
    """Run the real worker/export path over a fabricated volume in-process."""
    import common.ingest.nexrad.worker as worker

    raw_volume = _build_synthetic_volume(site, volume_id)
    worker.parse_raw_volume_file_mmap = lambda _path: raw_volume

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
    return {
        "site": site,
        "duration_s": time.perf_counter() - started,
        "visible_sweeps": result.visible_sweeps,
        "saved_elevations": len(result.saved_elevations),
        "child_rss_kb": result.child_rss_kb,
        "parse_error": result.parse_error,
    }


def _child_entry_synthetic(site: str, volume_id: str, output_root: str, volume_path: str, start_event, result_queue) -> None:
    start_event.wait()
    result_queue.put(_run_volume_once(site, volume_id, output_root, volume_path))


def _pool_entry_synthetic(site: str, volume_id: str, output_root: str, volume_path: str) -> dict:
    return _run_volume_once(site, volume_id, output_root, volume_path)


def run_synthetic_subprocess(simultaneous_volumes: int, output_dir: Path) -> dict:
    if simultaneous_volumes > len(RADARS):
        raise ValueError("Not enough unique radar IDs configured")

    parent = psutil.Process(os.getpid())
    baseline_mb = _total_rss_mb(parent)

    with tempfile.TemporaryDirectory(prefix=f"nexrad_synth_sub_{simultaneous_volumes}_", dir=str(output_dir)) as tmp_dir:
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
                target=_child_entry_synthetic,
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
        "mode": "synthetic",
        "execution": "subprocess",
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


def run_synthetic_pool(simultaneous_volumes: int, output_dir: Path) -> dict:
    if simultaneous_volumes > len(RADARS):
        raise ValueError("Not enough unique radar IDs configured")

    parent = psutil.Process(os.getpid())
    baseline_mb = _total_rss_mb(parent)

    with tempfile.TemporaryDirectory(prefix=f"nexrad_synth_pool_{simultaneous_volumes}_", dir=str(output_dir)) as tmp_dir:
        base = Path(tmp_dir)
        output_root = base / "output"
        output_root.mkdir()

        tasks = []
        for index in range(simultaneous_volumes):
            site = RADARS[index]
            volume_id = f"VOL{index + 1:03d}"
            volume_path = base / f"{site}_{volume_id}.ar2v"
            volume_path.write_bytes(b"benchmark")
            tasks.append((site, volume_id, str(output_root), str(volume_path)))

        started = time.perf_counter()
        with ProcessPoolExecutor(
            max_workers=simultaneous_volumes,
            initializer=lambda: None,
        ) as executor:
            futures = [executor.submit(_pool_entry_synthetic, *task) for task in tasks]
            results = [f.result() for f in futures]
        ended = time.perf_counter()

    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]
    return {
        "mode": "synthetic",
        "execution": "pool",
        "simultaneous_volumes": simultaneous_volumes,
        "radars": list(RADARS[:simultaneous_volumes]),
        "duration_s": ended - started,
        "baseline_total_rss_mb": baseline_mb,
        "peak_total_rss_mb": baseline_mb,
        "delta_total_rss_mb": 0.0,
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


def _print_summary(label: str, summaries: list[dict]) -> None:
    print(f"\n{label}")
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


def main(argv=None) -> int:
    parser = ArgumentParser(description="Unified NEXRAD worker memory benchmark")
    parser.add_argument("--output-dir", required=True, type=Path, help="Root directory for benchmark artifacts")
    parser.add_argument("--mode", choices=("synthetic",), default="synthetic",
                        help="Data source: synthetic datatree (live S3 stays in the legacy live scripts)")
    parser.add_argument("--execution", choices=("subprocess", "pool", "compare"), default="subprocess",
                        help="Execution mode: subprocess spawn, process pool, or compare both")
    parser.add_argument("--simultaneous-volumes", type=int, nargs="+", default=list(DEFAULT_SIMULTANEOUS),
                        help="Number of simultaneous volumes to parse")
    args = parser.parse_args(argv)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"NEXRAD worker benchmark: mode={args.mode}, execution={args.execution}")
    print(f"Output directory: {output_dir}")
    print()

    if args.mode == "synthetic":
        if args.execution in ("subprocess", "compare"):
            sub_summaries = [run_synthetic_subprocess(n, output_dir) for n in args.simultaneous_volumes]
            _print_summary("Synthetic subprocess", sub_summaries)
        if args.execution in ("pool", "compare"):
            pool_summaries = [run_synthetic_pool(n, output_dir) for n in args.simultaneous_volumes]
            _print_summary("Synthetic pool", pool_summaries)
        if args.execution == "compare" and len(args.simultaneous_volumes) == len(sub_summaries):
            print("\nSavings (pool vs subprocess)")
            for sub, pool in zip(sub_summaries, pool_summaries):
                savings = sub["mean_child_rss_mb"] - pool["mean_child_rss_mb"]
                pct = (savings / sub["mean_child_rss_mb"]) * 100 if sub["mean_child_rss_mb"] > 0 else 0
                print(f"  n={sub['simultaneous_volumes']}: {savings:.1f} MB/child ({pct:.1f}%)")
    elif args.mode == "live":
        print("Live mode is not implemented in the unified sampler; "
              "use benchmarks/benchmark_nexrad_memory_live.py (or "
              "benchmark_nexrad_live_pool_memory.py) with --output-dir.")
        return 1
    else:  # pragma: no cover - argparse choices reject anything else
        print(f"Unknown mode: {args.mode}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
