"""Live NEXRAD full-volume parse: subprocess vs pool memory.

Measures system-level memory (not per-process RSS) to properly account
for copy-on-write page sharing in the pool approach.

Opt-in live benchmark: downloads real Level-II volumes from AWS, so it
requires network access and never runs in the default suite. The unified
``benchmarks/benchmark_nexrad.py`` sampler covers the synthetic path; this
script keeps the live path with the same explicit ``--output-dir`` contract.

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_live_pool_memory.py --output-dir /tmp/nexrad_live_pool
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import resource
import subprocess
import sys
from argparse import ArgumentParser
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import tempfile
import time

import psutil

from common.ingest.nexrad.config import allowed_vcps
from common.ingest.nexrad.s3_async import async_list_recent_volume_ids, async_list_volume_chunks, get_unsigned_s3_client_async
from common.ingest.nexrad.weather_api import fetch_radar_station_vcps


SIMULTANEOUS_COUNTS = (1, 2, 4)
SAMPLE_INTERVAL_SECONDS = 0.02
VOLUME_CANDIDATES_PER_SITE = 5
SITE_CANDIDATE_LIMIT = 40

SUBPROCESS_WORKER = """
import sys, json, resource
sys.path.insert(0, sys.argv[1])
from common.ingest.nexrad.worker import parse_and_export

volume_path = sys.argv[2]
output_root = sys.argv[3]
site = sys.argv[4]
volume_id = sys.argv[5]
result_file = sys.argv[6]

result = parse_and_export(
    volume_path=volume_path, output_root=output_root, site=site, volume_id=volume_id,
    scan_timestamp=None, seen_elevation_keys=set(),
)
ru = resource.getrusage(resource.RUSAGE_SELF)
payload = {
    "site": site, "volume_id": volume_id,
    "child_rss_kb": ru.ru_maxrss,
    "visible_sweeps": result.visible_sweeps,
    "saved_elevations": len(result.saved_elevations),
    "parse_error": result.parse_error,
}
from pathlib import Path
Path(result_file).write_text(json.dumps(payload))
"""


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


def _is_complete_volume(chunks) -> bool:
    return bool(chunks) and any(chunk.chunk_type == "E" for chunk in chunks)


async def _discover_candidate(site: str, station, *, s3_client):
    if not str(site).upper().startswith("K"):
        return None
    if station.vcp not in allowed_vcps():
        return None

    volume_ids = await async_list_recent_volume_ids(
        site,
        limit=VOLUME_CANDIDATES_PER_SITE,
        s3_client=s3_client,
    )
    for volume_id in volume_ids:
        chunks = await async_list_volume_chunks(site, volume_id, s3_client=s3_client)
        if _is_complete_volume(chunks):
            return {
                "site": str(site).upper(),
                "vcp": station.vcp,
                "volume_id": str(volume_id),
                "chunk_count": len(chunks),
                "chunks": chunks,
            }
    return None


async def select_live_volumes(required_count: int) -> list[dict]:
    stations = await asyncio.to_thread(fetch_radar_station_vcps)
    candidates = sorted(
        (
            (site, station)
            for site, station in stations.items()
            if station is not None and station.vcp in allowed_vcps() and str(site).upper().startswith("K")
        ),
        key=lambda item: item[0],
    )[:SITE_CANDIDATE_LIMIT]

    discovered: list[dict] = []
    async with get_unsigned_s3_client_async() as s3_client:
        semaphore = asyncio.Semaphore(8)

        async def _run(site, station):
            async with semaphore:
                try:
                    return await _discover_candidate(site, station, s3_client=s3_client)
                except Exception:
                    return None

        results = await asyncio.gather(*(_run(site, station) for site, station in candidates))

    for result in results:
        if result is None:
            continue
        discovered.append(result)
        if len(discovered) >= required_count:
            break

    if len(discovered) < required_count:
        raise RuntimeError(f"Only found {len(discovered)} complete live volumes, need {required_count}")
    return discovered[:required_count]


async def download_live_volumes(selected: list[dict], root: Path) -> list[dict]:
    download_root = root / "downloads"
    download_root.mkdir(parents=True, exist_ok=True)

    async with get_unsigned_s3_client_async() as s3_client:
        for entry in selected:
            volume_path = download_root / f"{entry['site']}_{entry['volume_id']}.ar2v"
            with volume_path.open("wb") as handle:
                for chunk in entry["chunks"]:
                    response = await s3_client.get_object(Bucket="unidata-nexrad-level2-chunks", Key=chunk.key)
                    body = response["Body"]
                    async for data in body.iter_chunks():
                        handle.write(data)
                    if hasattr(body, "close"):
                        maybe_close = body.close()
                        if asyncio.iscoroutine(maybe_close):
                            await maybe_close
            entry["volume_path"] = str(volume_path)
            entry["volume_size_mb"] = volume_path.stat().st_size / (1024.0 * 1024.0)
    return selected


def _pool_initializer() -> None:
    import numpy as np  # noqa: F401
    import xarray as xr  # noqa: F401
    import scipy  # noqa: F401
    import pandas as pd  # noqa: F401
    import dask  # noqa: F401
    import netCDF4  # noqa: F401
    import botocore  # noqa: F401


def _pool_parse_task(src_root: str, volume_path: str, output_root: str, site: str, volume_id: str) -> dict:
    import sys
    sys.path.insert(0, src_root)
    import resource
    from common.ingest.nexrad.worker import parse_and_export

    result = parse_and_export(
        volume_path=volume_path,
        output_root=output_root,
        site=site,
        volume_id=volume_id,
        scan_timestamp=None,
        seen_elevation_keys=set(),
    )

    ru = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "site": site,
        "volume_id": volume_id,
        "child_rss_kb": ru.ru_maxrss,
        "visible_sweeps": result.visible_sweeps,
        "saved_elevations": len(result.saved_elevations),
        "parse_error": result.parse_error,
    }


def run_subprocess_benchmark(selected: list[dict], simultaneous_volumes: int, src_root: str, root: Path) -> dict:
    """Each parse spawns a fresh Python interpreter (no shared memory)."""
    parent = psutil.Process(os.getpid())
    baseline_mb = _total_rss_mb(parent)
    output_root = root / "sub_out"
    output_root.mkdir(parents=True, exist_ok=True)

    processes: list[subprocess.Popen] = []
    result_files: list[Path] = []
    started = time.perf_counter()

    for entry in selected[:simultaneous_volumes]:
        result_file = root / f"sub_result_{entry['site']}.json"
        result_files.append(result_file)

        proc = subprocess.Popen(
            [sys.executable, "-c", SUBPROCESS_WORKER, src_root, entry["volume_path"], str(output_root), entry["site"], entry["volume_id"], str(result_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(proc)

    peak_mb = baseline_mb
    while any(p.poll() is None for p in processes):
        peak_mb = max(peak_mb, _total_rss_mb(parent))
        time.sleep(SAMPLE_INTERVAL_SECONDS)

    for proc in processes:
        proc.wait(timeout=300)

    peak_mb = max(peak_mb, _total_rss_mb(parent))
    ended = time.perf_counter()

    results = []
    for rf in result_files:
        if rf.exists():
            results.append(json.loads(rf.read_text()))

    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]
    return {
        "simultaneous_volumes": simultaneous_volumes,
        "radars": [entry["site"] for entry in selected[:simultaneous_volumes]],
        "volume_ids": [entry["volume_id"] for entry in selected[:simultaneous_volumes]],
        "chunk_counts": [entry["chunk_count"] for entry in selected[:simultaneous_volumes]],
        "volume_sizes_mb": [entry["volume_size_mb"] for entry in selected[:simultaneous_volumes]],
        "duration_s": ended - started,
        "baseline_total_rss_mb": baseline_mb,
        "peak_total_rss_mb": peak_mb,
        "delta_total_rss_mb": peak_mb - baseline_mb,
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


def run_pool_benchmark(selected: list[dict], simultaneous_volumes: int, src_root: str, root: Path) -> dict:
    """Uses ProcessPoolExecutor with fork (copy-on-write shared memory)."""
    parent = psutil.Process(os.getpid())
    baseline_mb = _total_rss_mb(parent)
    output_root = root / "pool_out"
    output_root.mkdir(parents=True, exist_ok=True)

    tasks = []
    for entry in selected[:simultaneous_volumes]:
        tasks.append((src_root, entry["volume_path"], str(output_root), entry["site"], entry["volume_id"]))

    peak_mb = baseline_mb
    started = time.perf_counter()

    with ProcessPoolExecutor(
        max_workers=simultaneous_volumes,
        initializer=_pool_initializer,
    ) as executor:
        futures = [executor.submit(_pool_parse_task, *task) for task in tasks]

        while any(not f.done() for f in futures):
            peak_mb = max(peak_mb, _total_rss_mb(parent))
            time.sleep(SAMPLE_INTERVAL_SECONDS)

        results = [f.result() for f in futures]

    peak_mb = max(peak_mb, _total_rss_mb(parent))
    ended = time.perf_counter()

    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]
    return {
        "simultaneous_volumes": simultaneous_volumes,
        "radars": [entry["site"] for entry in selected[:simultaneous_volumes]],
        "volume_ids": [entry["volume_id"] for entry in selected[:simultaneous_volumes]],
        "chunk_counts": [entry["chunk_count"] for entry in selected[:simultaneous_volumes]],
        "volume_sizes_mb": [entry["volume_size_mb"] for entry in selected[:simultaneous_volumes]],
        "duration_s": ended - started,
        "baseline_total_rss_mb": baseline_mb,
        "peak_total_rss_mb": peak_mb,
        "delta_total_rss_mb": peak_mb - baseline_mb,
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


async def _async_main(output_dir: Path) -> tuple[list[dict], list[dict]]:
    max_count = max(SIMULTANEOUS_COUNTS)
    src_root = str(Path(__file__).parent.parent.parent / "src")

    with tempfile.TemporaryDirectory(prefix="nexrad_live_pool_", dir=str(output_dir)) as tmp_dir:
        root = Path(tmp_dir)
        print(f"Discovering {max_count} live volumes...")
        selected = await select_live_volumes(max_count)
        print(f"Downloading {len(selected)} volumes...")
        selected = await download_live_volumes(selected, root)

        for entry in selected:
            print(f"  {entry['site']} {entry['volume_id']}: {entry['volume_size_mb']:.1f} MB ({entry['chunk_count']} chunks)")
        print()

        sub_summaries = []
        pool_summaries = []

        for count in SIMULTANEOUS_COUNTS:
            print(f"--- n={count} subprocess (fresh Python) ---")
            sub = run_subprocess_benchmark(selected, count, src_root, root)
            sub_summaries.append(sub)
            print(f"  mean_child_mb: {sub['mean_child_rss_mb']:.1f}, peak_total_mb: {sub['peak_total_rss_mb']:.1f}, duration: {sub['duration_s']:.2f}s")
            for r in sub["results"]:
                print(f"    {r['site']}: {r['child_rss_kb']/1024:.1f} MB, {r['visible_sweeps']} sweeps, {r['saved_elevations']} elevations, {r['parse_error']}")

            print(f"--- n={count} pool (forked, shared imports) ---")
            pool = run_pool_benchmark(selected, count, src_root, root)
            pool_summaries.append(pool)
            print(f"  mean_child_mb: {pool['mean_child_rss_mb']:.1f}, peak_total_mb: {pool['peak_total_rss_mb']:.1f}, duration: {pool['duration_s']:.2f}s")
            for r in pool["results"]:
                print(f"    {r['site']}: {r['child_rss_kb']/1024:.1f} MB, {r['visible_sweeps']} sweeps, {r['saved_elevations']} elevations, {r['parse_error']}")

            savings = sub["mean_child_rss_mb"] - pool["mean_child_rss_mb"]
            pct = (savings / sub["mean_child_rss_mb"]) * 100 if sub["mean_child_rss_mb"] > 0 else 0
            total_savings = sub["delta_total_rss_mb"] - pool["delta_total_rss_mb"]
            print(f"  savings: {savings:.1f} MB per child ({pct:.1f}%), total delta savings: {total_savings:.1f} MB")
            print()

        return sub_summaries, pool_summaries


def main() -> int:
    parser = ArgumentParser(description="Live NEXRAD full-volume parse: subprocess vs pool memory")
    parser.add_argument("--output-dir", required=True, type=Path, help="Root directory for benchmark artifacts")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sub_summaries, pool_summaries = asyncio.run(_async_main(args.output_dir))

    print("=" * 90)
    print("SUMMARY: subprocess vs pool (live data)")
    print()
    print(" n | subprocess_mean_mb | pool_mean_mb | savings/child | savings_pct | sub_duration | pool_duration ")
    print("---+--------------------+--------------+---------------+-------------+--------------+--------------")
    for sub, pool in zip(sub_summaries, pool_summaries):
        savings = sub["mean_child_rss_mb"] - pool["mean_child_rss_mb"]
        pct = (savings / sub["mean_child_rss_mb"]) * 100 if sub["mean_child_rss_mb"] > 0 else 0
        speedup = sub["duration_s"] / pool["duration_s"] if pool["duration_s"] > 0 else 0
        print(
            f" {sub['simultaneous_volumes']:>1} | "
            f"{sub['mean_child_rss_mb']:>18.1f} | "
            f"{pool['mean_child_rss_mb']:>12.1f} | "
            f"{savings:>13.1f} | "
            f"{pct:>11.1f}% | "
            f"{sub['duration_s']:>12.2f} | "
            f"{pool['duration_s']:>12.2f} ({speedup:.1f}x)"
        )

    print()
    print(json.dumps({"subprocess": sub_summaries, "pool": pool_summaries}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
