"""Profile realtime service memory usage.

Modes (decomposition Phase 7):

- ``single`` (default): launch one entrypoint and profile its whole PID tree
  (parent plus every descendant).
- ``direct``: launch ``run_edgewarn.py``, ``run_ewmrs.py``, and
  ``run_nexrad.py`` independently and report the aggregate across the three
  trees, per tree, and per process name.
- ``launcher``: launch the same three through ``run_all.py`` and profile the
  supervisor's tree (the launcher-performance-gate comparison target).

Samples RSS every 0.1 s and writes results to JSON.

Usage:
    PYTHONPATH=src python benchmarks/benchmark_realtime_pipeline_memory.py \
        [--mode single|direct|launcher] [--entrypoint run_edgewarn.py] [--duration 300]
"""

from __future__ import annotations

import argparse
import json
import threading
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil


SAMPLE_INTERVAL_S = 0.1
DEFAULT_OUTPUT_DIR = Path("/tmp/kilo")

_PRIMARY_FLAGS = [
    "--lat_limits", "20", "55",
    "--lon_limits", "230", "300",
    "--disable-ctam",
    "--disable-tracking",
    "--disable-goes",
]

SERVICE_ENTRYPOINTS = {
    "edgewarn": ("run_edgewarn.py", _PRIMARY_FLAGS),
    "ewmrs": ("run_ewmrs.py", []),
    "nexrad": ("run_nexrad.py", []),
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("single", "direct", "launcher"), default="single")
    parser.add_argument("--entrypoint", default="run_edgewarn.py",
                        help="Entry point profiled in single mode")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args(argv)


def _build_launch_plan(mode: str, entrypoint: str, src_root: Path):
    """Return ``{tree_name: argv}`` describing what to launch."""
    env_python = sys.executable
    if mode == "single":
        script, flags = SERVICE_ENTRYPOINTS.get(
            entrypoint, (entrypoint, [])
        )
        return {"single": [env_python, str(src_root / script), *flags]}
    if mode == "launcher":
        return {"launcher": [env_python, str(src_root / "run_all.py")]}
    return {
        name: [env_python, str(src_root / script), *flags]
        for name, (script, flags) in SERVICE_ENTRYPOINTS.items()
    }


def _snapshot_memory(parent_pids: dict[str, int]) -> dict:
    """Capture RSS for every named root tree plus the aggregate."""
    snapshot = {"trees": {}, "total_rss_mb": 0.0}
    for tree_name, pid in parent_pids.items():
        try:
            parent = psutil.Process(pid)
            rss = parent.memory_info().rss / (1024.0 * 1024.0)
            children = []
            for child in parent.children(recursive=True):
                try:
                    children.append({
                        "pid": child.pid,
                        "name": child.name(),
                        "rss_mb": child.memory_info().rss / (1024.0 * 1024.0),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            tree_total = rss + sum(child["rss_mb"] for child in children)
            snapshot["trees"][tree_name] = {
                "root_rss_mb": round(rss, 2),
                "children": children,
                "total_rss_mb": round(tree_total, 2),
            }
            snapshot["total_rss_mb"] += tree_total
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            snapshot["trees"][tree_name] = {
                "root_rss_mb": 0.0,
                "children": [],
                "total_rss_mb": 0.0,
            }
    return snapshot


def _stop(proc, grace_s: float = 10.0):
    """SIGINT first, then SIGTERM, then SIGKILL."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(sig)
        except OSError:
            return
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode_tag = "pipeline" if args.mode == "single" else args.mode
    output_path = output_root / f"{mode_tag}_memory_profile_{timestamp}.json"

    src_root = Path(__file__).resolve().parents[2] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{src_root}:{env.get('PYTHONPATH', '')}"

    plan = _build_launch_plan(args.mode, args.entrypoint, src_root)

    print(f"Mode: {args.mode}")
    for tree_name, cmd in plan.items():
        print(f"  {tree_name}: {' '.join(cmd)}")
    print(f"Sampling every {SAMPLE_INTERVAL_S}s for {args.duration}s")
    print(f"Output: {output_path}\n")

    processes = {}
    stdout_chunks: dict[str, list[str]] = {}
    drain_threads: list[threading.Thread] = []
    for tree_name, cmd in plan.items():
        # Inherited stdio in direct/launcher keeps logging out of the sampler;
        # single mode captures stdout for diagnostics as before.
        capture = args.mode == "single"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
            env=env,
            cwd=str(src_root),
        )
        processes[tree_name] = proc
        if capture:
            # Drain concurrently so a chatty child can never fill the pipe
            # buffer and stall mid-profile.
            stdout_chunks[tree_name] = []

            def _drain(name=tree_name, handle=proc.stdout):
                for line in handle:
                    stdout_chunks[name].append(line)

            thread = threading.Thread(target=_drain, daemon=True)
            thread.start()
            drain_threads.append(thread)

    time.sleep(5)  # initialization warm-up, matching the historical harness
    print("Starting memory sampling...")
    started = time.time()
    samples: list[dict] = []
    sample_count = 0
    warned_exits: set[str] = set()

    try:
        while time.time() - started < args.duration:
            elapsed = time.time() - started
            live = {name: proc.pid for name, proc in processes.items() if proc.poll() is None}
            exited = [n for n, p in processes.items() if p.poll() is not None and n not in warned_exits]
            if exited:
                warned_exits.update(exited)
                print(f"Trees exited at {elapsed:.1f}s: {', '.join(sorted(exited))}")
            if not live:
                break
                # Keep sampling the surviving trees until the duration ends.
            snapshot = _snapshot_memory(live)
            snapshot["elapsed_s"] = round(elapsed, 2)
            snapshot["sample_index"] = sample_count
            samples.append(snapshot)

            if sample_count % 100 == 0:
                total = snapshot["total_rss_mb"]
                per_tree = ", ".join(
                    f"{name}={tree['total_rss_mb']:.0f}MB"
                    for name, tree in snapshot["trees"].items()
                )
                print(f"  t={elapsed:6.1f}s | total={total:8.1f} MB | {per_tree} | samples={sample_count}")

            sample_count += 1
            sleep_time = SAMPLE_INTERVAL_S - (
                time.time() - started - (sample_count - 1) * SAMPLE_INTERVAL_S
            )
            if sleep_time > 0:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        print("\nInterrupted by user")

    elapsed_total = time.time() - started
    print(f"\nSampling complete: {sample_count} samples over {elapsed_total:.1f}s")

    print("Stopping service trees...")
    captured_stdout = {}
    for name, proc in processes.items():
        _stop(proc)
    for thread in drain_threads:
        thread.join(timeout=5)
    for name, chunks in stdout_chunks.items():
        captured_stdout[name] = "".join(chunks)[:10000]

    total_rss_values = [s["total_rss_mb"] for s in samples]

    # Aggregate per-process memory over time across all trees.
    process_memory: dict[str, dict] = {}
    for sample in samples:
        for tree in sample["trees"].values():
            for child in tree["children"]:
                name = child["name"]
                if name not in process_memory:
                    process_memory[name] = {"count": 0, "max_rss_mb": 0.0, "mean_total": 0.0}
                pm = process_memory[name]
                pm["count"] += 1
                pm["max_rss_mb"] = max(pm["max_rss_mb"], child["rss_mb"])
                pm["mean_total"] += child["rss_mb"]
    for pm in process_memory.values():
        pm["mean_rss_mb"] = pm["mean_total"] / pm["count"] if pm["count"] else 0.0
        del pm["mean_total"]

    result = {
        "metadata": {
            "timestamp": timestamp,
            "mode": args.mode,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "duration_s": args.duration,
            "actual_duration_s": round(elapsed_total, 2),
            "sample_count": sample_count,
            "launch_plan": {name: cmd for name, cmd in plan.items()},
        },
        "summary": {
            "aggregate_total_rss_mb": {
                "min": round(min(total_rss_values), 1) if total_rss_values else 0.0,
                "max": round(max(total_rss_values), 1) if total_rss_values else 0.0,
                "mean": round(sum(total_rss_values) / len(total_rss_values), 1) if total_rss_values else 0.0,
            },
            "per_process": process_memory,
        },
        "samples": samples,
        "tree_stdout": captured_stdout,
    }

    output_path.write_text(json.dumps(result, indent=2))
    print(f"\nResults written to {output_path}")
    print(f"File size: {output_path.stat().st_size / (1024*1024):.1f} MB")

    s = result["summary"]
    agg = s["aggregate_total_rss_mb"]
    print("\n=== Memory Summary ===")
    print(f"Aggregate RSS: min={agg['min']:.1f} MB, max={agg['max']:.1f} MB, mean={agg['mean']:.1f} MB")
    print("Per-process memory:")
    for name, pm in sorted(s["per_process"].items(), key=lambda x: x[1]["max_rss_mb"], reverse=True):
        print(f"  {name:30s}: max={pm['max_rss_mb']:8.1f} MB, mean={pm['mean_rss_mb']:8.1f} MB (seen {pm['count']} times)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
