"""Profile realtime NEXRAD pipeline memory for 120 s, sampling every 0.1 s.

Runs the NEXRAD realtime ingestion pipeline in a subprocess, monitors
the parent and all child processes, samples memory every 0.1 s, and writes
results to JSON.

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_realtime_memory.py

Output:
    /tmp/kilo/nexrad_realtime_memory_<timestamp>.json
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil


SAMPLE_INTERVAL_S = 0.1
TIMEOUT_S = 120
OUTPUT_DIR = Path("/tmp/kilo")

WRAPPER_SCRIPT = """
import sys, os
sys.path.insert(0, sys.argv[1])
from common.ingest.nexrad.pipeline import run_realtime_ingestion_pipeline
run_realtime_ingestion_pipeline(
    scan_interval_seconds=10,
    completion_interval_seconds=5,
    max_candidate_volumes_per_site=1,
)
"""


def _snapshot_memory(parent_pid: int) -> dict:
    """Capture RSS for the parent and every descendant process."""
    snapshot = {
        "parent_rss_mb": 0.0,
        "children": [],
        "total_rss_mb": 0.0,
    }

    try:
        parent = psutil.Process(parent_pid)
        rss = parent.memory_info().rss
        snapshot["parent_rss_mb"] = rss / (1024.0 * 1024.0)
        snapshot["total_rss_mb"] += snapshot["parent_rss_mb"]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    try:
        parent = psutil.Process(parent_pid)
        for child in parent.children(recursive=True):
            try:
                pid = child.pid
                name = child.name()
                rss = child.memory_info().rss
                rss_mb = rss / (1024.0 * 1024.0)
                snapshot["children"].append({
                    "pid": pid,
                    "name": name,
                    "rss_mb": round(rss_mb, 2),
                })
                snapshot["total_rss_mb"] += rss_mb
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    snapshot["total_rss_mb"] = round(snapshot["total_rss_mb"], 2)
    return snapshot


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = OUTPUT_DIR / f"nexrad_realtime_memory_{timestamp}.json"

    src_root = str(Path(__file__).parent.parent.parent / "src")

    print(f"Launching NEXRAD realtime pipeline")
    print(f"Sampling every {SAMPLE_INTERVAL_S}s for {TIMEOUT_S}s")
    print(f"Output: {output_path}")
    print()

    proc = subprocess.Popen(
        [sys.executable, "-c", WRAPPER_SCRIPT, src_root],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=src_root,
    )

    parent_pid = proc.pid
    print(f"Pipeline PID: {parent_pid}")
    print("Waiting for pipeline to initialize...")

    # Wait for pipeline to fetch stations and start its loop
    time.sleep(8)

    if proc.poll() is not None:
        print(f"Pipeline exited before sampling started (rc={proc.returncode})")
        stdout_text = proc.stdout.read() if proc.stdout else ""
        print(f"stdout: {stdout_text[:500]}")
        return 1

    print("Starting memory sampling...")
    started = time.time()
    samples: list[dict] = []
    sample_count = 0
    last_print = 0.0

    try:
        while time.time() - started < TIMEOUT_S:
            elapsed = time.time() - started

            if proc.poll() is not None:
                print(f"Pipeline exited at {elapsed:.1f}s (rc={proc.returncode})")
                break

            snapshot = _snapshot_memory(parent_pid)
            snapshot["elapsed_s"] = round(elapsed, 2)
            snapshot["sample_index"] = sample_count
            samples.append(snapshot)

            if elapsed - last_print >= 10.0:
                total = snapshot["total_rss_mb"]
                child_count = len(snapshot["children"])
                print(f"  t={elapsed:6.1f}s | total={total:8.1f} MB | children={child_count} | samples={sample_count}")
                last_print = elapsed

            sample_count += 1
            sleep_time = SAMPLE_INTERVAL_S - (time.time() - started - (sample_count - 1) * SAMPLE_INTERVAL_S)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\nInterrupted by user")

    elapsed_total = time.time() - started
    print(f"\nSampling complete: {sample_count} samples over {elapsed_total:.1f}s")

    # Stop the pipeline
    print("Stopping pipeline...")
    try:
        parent = psutil.Process(parent_pid)
        parent.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("Pipeline did not stop gracefully, sending SIGTERM...")
            parent.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Pipeline did not stop, sending SIGKILL...")
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                parent.kill()
                proc.wait(timeout=5)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    stdout_text = ""
    if proc.stdout:
        stdout_text = proc.stdout.read()

    # Build summary
    total_rss_values = [s["total_rss_mb"] for s in samples]
    parent_rss_values = [s["parent_rss_mb"] for s in samples]
    child_counts = [len(s["children"]) for s in samples]

    process_memory: dict[str, dict] = {}
    for sample in samples:
        for child in sample["children"]:
            name = child["name"]
            if name not in process_memory:
                process_memory[name] = {
                    "count": 0,
                    "max_rss_mb": 0.0,
                    "sum_rss_mb": 0.0,
                }
            pm = process_memory[name]
            pm["count"] += 1
            pm["max_rss_mb"] = max(pm["max_rss_mb"], child["rss_mb"])
            pm["sum_rss_mb"] += child["rss_mb"]

    for name, pm in process_memory.items():
        pm["mean_rss_mb"] = round(pm["sum_rss_mb"] / pm["count"], 2) if pm["count"] > 0 else 0.0
        del pm["sum_rss_mb"]

    result = {
        "metadata": {
            "timestamp": timestamp,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "timeout_s": TIMEOUT_S,
            "actual_duration_s": round(elapsed_total, 2),
            "sample_count": sample_count,
        },
        "summary": {
            "total_rss_mb": {
                "min": round(min(total_rss_values), 1) if total_rss_values else 0.0,
                "max": round(max(total_rss_values), 1) if total_rss_values else 0.0,
                "mean": round(sum(total_rss_values) / len(total_rss_values), 1) if total_rss_values else 0.0,
            },
            "parent_rss_mb": {
                "min": round(min(parent_rss_values), 1) if parent_rss_values else 0.0,
                "max": round(max(parent_rss_values), 1) if parent_rss_values else 0.0,
                "mean": round(sum(parent_rss_values) / len(parent_rss_values), 1) if parent_rss_values else 0.0,
            },
            "child_count": {
                "min": min(child_counts) if child_counts else 0,
                "max": max(child_counts) if child_counts else 0,
                "mean": round(sum(child_counts) / len(child_counts), 1) if child_counts else 0.0,
            },
            "per_process": process_memory,
        },
        "samples": samples,
        "pipeline_stdout": stdout_text[:10000] if stdout_text else "",
    }

    output_path.write_text(json.dumps(result, indent=2))
    print(f"\nResults written to {output_path}")
    print(f"File size: {output_path.stat().st_size / (1024*1024):.1f} MB")

    s = result["summary"]
    print(f"\n=== Memory Summary ===")
    print(f"Total RSS:   min={s['total_rss_mb']['min']:.1f} MB, max={s['total_rss_mb']['max']:.1f} MB, mean={s['total_rss_mb']['mean']:.1f} MB")
    print(f"Parent RSS:  min={s['parent_rss_mb']['min']:.1f} MB, max={s['parent_rss_mb']['max']:.1f} MB, mean={s['parent_rss_mb']['mean']:.1f} MB")
    print(f"Children:    min={s['child_count']['min']}, max={s['child_count']['max']}, mean={s['child_count']['mean']:.1f}")
    print(f"\nPer-process memory (sorted by max):")
    for name, pm in sorted(s["per_process"].items(), key=lambda x: x[1]["max_rss_mb"], reverse=True):
        print(f"  {name:30s}: max={pm['max_rss_mb']:8.1f} MB, mean={pm['mean_rss_mb']:8.1f} MB (seen {pm['count']} times)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
