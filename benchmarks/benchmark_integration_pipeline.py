#!/usr/bin/env python3
"""
Benchmark: Integration Pipeline Performance

Tests the performance of the CTAM integration pipeline with detailed
per-step memory statistics (mean RSS and max RSS).

Metrics:
- Total execution time
- Peak memory usage (overall)
- Per-step: mean RSS (MB), max RSS (MB), delta RSS (MB), duration (s)
"""

import gc
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import psutil

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_PROCESS = psutil.Process(os.getpid())
_SAMPLE_INTERVAL_S = 0.05  # 50 ms polling resolution

# ---------------------------------------------------------------------------
# Per-step memory sampler
# ---------------------------------------------------------------------------

_step_samples: dict[str, list[float]] = defaultdict(list)
_step_start_mem: dict[str, float] = {}
_step_end_mem: dict[str, float] = {}
_step_durations: dict[str, float] = {}

_active_steps: set[str] = set()
_sampler_stop = threading.Event()
_sampler_lock = threading.Lock()


def _memory_sampler():
    """Background thread: polls RSS every SAMPLE_INTERVAL_S and stores sample."""
    while not _sampler_stop.is_set():
        rss_mb = _PROCESS.memory_info().rss / 1024 / 1024
        with _sampler_lock:
            for step_name in _active_steps:
                _step_samples[step_name].append(rss_mb)
        _sampler_stop.wait(_SAMPLE_INTERVAL_S)


# ---------------------------------------------------------------------------
# Monkey-patch pipeline._run_step
# ---------------------------------------------------------------------------

def _patched_run_step(step_name: str, action):
    """Replacement for pipeline._run_step that records per-step memory."""
    gc.collect()
    start_mem = _PROCESS.memory_info().rss / 1024 / 1024
    start_t = time.perf_counter()

    with _sampler_lock:
        _active_steps.add(step_name)
        _step_start_mem[step_name] = start_mem
        _step_samples[step_name] = [start_mem]

    try:
        result = action()
    finally:
        end_t = time.perf_counter()
        end_mem = _PROCESS.memory_info().rss / 1024 / 1024

        with _sampler_lock:
            _active_steps.discard(step_name)
            _step_samples[step_name].append(end_mem)
            _step_end_mem[step_name] = end_mem
            _step_durations[step_name] = end_t - start_t

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_step_report():
    col_w = 46
    num_w = 9

    header = (
        f"\n{'=' * 90}\n"
        f"{'PER-STEP MEMORY REPORT':^90}\n"
        f"{'=' * 90}\n"
        f"{'Step':<{col_w}} {'Mean MB':>{num_w}} {'Max MB':>{num_w}} "
        f"{'Δ MB':>{num_w}} {'Time (s)':>{num_w}}\n"
        f"{'-' * 90}"
    )
    print(header)

    for step_name, samples in _step_samples.items():
        if not samples:
            continue
        mean_mb = sum(samples) / len(samples)
        max_mb = max(samples)
        start_mb = _step_start_mem.get(step_name, samples[0])
        end_mb = _step_end_mem.get(step_name, samples[-1])
        delta_mb = end_mb - start_mb
        duration = _step_durations.get(step_name, 0.0)

        delta_str = f"{delta_mb:+.1f}"
        print(
            f"{step_name:<{col_w}} {mean_mb:>{num_w}.1f} {max_mb:>{num_w}.1f} "
            f"{delta_str:>{num_w}} {duration:>{num_w}.2f}"
        )

    print("=" * 90)


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

def run_benchmark():
    import EdgeWARN.process.integrate.pipeline as _pipeline
    import util.file as fs

    # Patch _run_step before the pipeline runs
    _pipeline._run_step = _patched_run_step

    from EdgeWARN.process.integrate.main import main as integrate_main
    from util.io import IOManager

    io_manager = IOManager("[Benchmark]")

    print("=" * 60)
    print("INTEGRATION PIPELINE BENCHMARK")
    print("=" * 60)

    latest_inputs = fs.latest_files(fs.STORMCELL_DIR, 1)
    if not latest_inputs:
        print("No stormcell inputs found. Cannot run integration benchmark.")
        return

    input_file = latest_inputs[0]
    print(f"Using input file: {input_file}")

    gc.collect()
    with _sampler_lock:
        _active_steps.clear()

    # Start background memory sampler
    _sampler_stop.clear()
    sampler_thread = threading.Thread(target=_memory_sampler, daemon=True)
    sampler_thread.start()

    overall_start_mem = _PROCESS.memory_info().rss / 1024 / 1024
    start_time = time.perf_counter()

    try:
        integrate_main(json_path=input_file, remove_old_cells=False)
    except Exception as e:
        print(f"Pipeline error: {e}")
        import traceback
        traceback.print_exc()

    elapsed = time.perf_counter() - start_time
    overall_end_mem = _PROCESS.memory_info().rss / 1024 / 1024

    _sampler_stop.set()
    sampler_thread.join(timeout=2)

    # Compute overall peak from all samples
    all_samples = [s for samples in _step_samples.values() for s in samples]
    peak_mb = max(all_samples) if all_samples else overall_end_mem

    print(f"\nOverall Results:")
    print(f"  Total Time:   {elapsed:.2f}s")
    print(f"  Start Memory: {overall_start_mem:.1f} MB")
    print(f"  Peak Memory:  {peak_mb:.1f} MB")
    print(f"  End Memory:   {overall_end_mem:.1f} MB")
    print(f"  Net Delta:    {overall_end_mem - overall_start_mem:+.1f} MB")

    _print_step_report()


if __name__ == "__main__":
    run_benchmark()
