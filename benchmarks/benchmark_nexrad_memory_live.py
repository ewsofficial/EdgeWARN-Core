"""Benchmark live NEXRAD full-volume parse memory.

This benchmark:
- fetches live radar station metadata
- selects distinct radars with recent complete chunked volumes
- downloads full real AR2V volumes for those radars
- parses/exports those full volumes concurrently via the worker path
- samples total RSS across parent and child processes

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_memory_live.py
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time

import psutil

from common.ingest.nexrad.config import allowed_vcps
from common.ingest.nexrad.s3_async import async_list_recent_volume_ids, async_list_volume_chunks, get_unsigned_s3_client_async
from common.ingest.nexrad.weather_api import fetch_radar_station_vcps


SIMULTANEOUS_COUNTS = (1, 2, 4, 8)
SAMPLE_INTERVAL_SECONDS = 0.02
VOLUME_CANDIDATES_PER_SITE = 5
SITE_CANDIDATE_LIMIT = 40


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
    return selected


def _child_entry(volume_path: str, output_root: str, site: str, volume_id: str, result_queue) -> None:
    from common.ingest.nexrad.worker import parse_and_export

    started = time.perf_counter()
    result = parse_and_export(
        volume_path=volume_path,
        output_root=output_root,
        site=site,
        volume_id=volume_id,
        scan_timestamp=None,
        seen_elevation_keys=set(),
    )
    result_queue.put(
        {
            "site": site,
            "volume_id": volume_id,
            "duration_s": time.perf_counter() - started,
            "visible_sweeps": result.visible_sweeps,
            "saved_elevations": len(result.saved_elevations),
            "child_rss_kb": result.child_rss_kb,
            "parse_error": result.parse_error,
        }
    )


def run_parse_benchmark(selected: list[dict], simultaneous_volumes: int, root: Path) -> dict:
    ctx = mp.get_context("spawn")
    parent = psutil.Process(os.getpid())
    baseline_mb = _total_rss_mb(parent)
    output_root = root / f"out_{simultaneous_volumes}"
    output_root.mkdir(parents=True, exist_ok=True)
    result_queue = ctx.Queue()
    processes = []

    for entry in selected[:simultaneous_volumes]:
        process = ctx.Process(
            target=_child_entry,
            args=(entry["volume_path"], str(output_root), entry["site"], entry["volume_id"], result_queue),
        )
        process.start()
        processes.append(process)

    started = time.perf_counter()
    peak_mb = baseline_mb
    while any(process.is_alive() for process in processes):
        peak_mb = max(peak_mb, _total_rss_mb(parent))
        time.sleep(SAMPLE_INTERVAL_SECONDS)

    for process in processes:
        process.join(timeout=300)

    peak_mb = max(peak_mb, _total_rss_mb(parent))
    ended = time.perf_counter()
    results = [result_queue.get(timeout=30) for _ in processes]
    child_rss_mb = [float(entry["child_rss_kb"]) / 1024.0 for entry in results if entry.get("child_rss_kb")]

    return {
        "simultaneous_volumes": simultaneous_volumes,
        "radars": [entry["site"] for entry in selected[:simultaneous_volumes]],
        "volume_ids": [entry["volume_id"] for entry in selected[:simultaneous_volumes]],
        "chunk_counts": [entry["chunk_count"] for entry in selected[:simultaneous_volumes]],
        "duration_s": ended - started,
        "baseline_total_rss_mb": baseline_mb,
        "peak_total_rss_mb": peak_mb,
        "delta_total_rss_mb": peak_mb - baseline_mb,
        "max_child_rss_mb": max(child_rss_mb) if child_rss_mb else 0.0,
        "mean_child_rss_mb": sum(child_rss_mb) / len(child_rss_mb) if child_rss_mb else 0.0,
        "results": sorted(results, key=lambda item: item["site"]),
    }


async def _async_main() -> list[dict]:
    max_count = max(SIMULTANEOUS_COUNTS)
    with tempfile.TemporaryDirectory(prefix="nexrad_live_mem_", dir="/tmp/kilo") as tmp_dir:
        root = Path(tmp_dir)
        selected = await select_live_volumes(max_count)
        selected = await download_live_volumes(selected, root)

        summaries = []
        for count in SIMULTANEOUS_COUNTS:
            summaries.append(run_parse_benchmark(selected, count, root))
        return summaries


def main() -> int:
    summaries = asyncio.run(_async_main())

    print("Live NEXRAD simultaneous full-volume memory benchmark")
    print("Memory samples include parent and child parser processes.")
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
