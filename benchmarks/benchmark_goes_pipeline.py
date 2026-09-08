#!/usr/bin/env python3
"""Benchmark the full GOES render pipeline with per-step timing and memory stats.

This benchmark runs in two modes:
1) Detailed single-process pass for per-layer/per-step timings and memory.
2) End-to-end GOES pipeline pass using the normal parallel executor.

Run with:
    PYTHONPATH=src python benchmarks/benchmark_goes_pipeline.py

Optional:
    PYTHONPATH=src python benchmarks/benchmark_goes_pipeline.py \
        --sample-interval 0.05 \
        --output /tmp/goes_pipeline_benchmark.json
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import psutil

import EWMRS.pipeline as ewmrs_pipeline
import EWMRS.render.tiler as tiler_module
import EWMRS.render.goes_transform as goes_transform
import EWMRS.render.render as render_module
from EWMRS.pipeline import _render_layer
from EWMRS.render.config import get_goes_file_list


@dataclass
class StepRun:
    layer_name: str
    step_name: str
    start_s: float
    end_s: float
    duration_s: float
    start_mem_mb: float
    end_mem_mb: float
    mean_mem_mb: float
    peak_mem_mb: float


class MemoryStepSampler:
    def __init__(self, sample_interval_s: float, include_children: bool = False):
        self.sample_interval_s = sample_interval_s
        self.include_children = include_children
        self._process = psutil.Process(os.getpid())

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._next_token = 1
        self._active: dict[int, dict[str, Any]] = {}
        self.completed: list[StepRun] = []
        self.pipeline_samples_mb: list[float] = []

    def _total_rss_mb(self) -> float:
        total = 0
        try:
            total += self._process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

        if self.include_children:
            try:
                for child in self._process.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return total / (1024.0 * 1024.0)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            mem_mb = self._total_rss_mb()
            with self._lock:
                self.pipeline_samples_mb.append(mem_mb)
                for entry in self._active.values():
                    entry["samples_mb"].append(mem_mb)
            self._stop_event.wait(self.sample_interval_s)

    def enter(self, layer_name: str, step_name: str) -> int:
        start_s = time.perf_counter()
        start_mb = self._total_rss_mb()
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._active[token] = {
                "layer_name": layer_name,
                "step_name": step_name,
                "start_s": start_s,
                "start_mb": start_mb,
                "samples_mb": [start_mb],
            }
        return token

    def exit(self, token: int) -> None:
        end_s = time.perf_counter()
        end_mb = self._total_rss_mb()

        with self._lock:
            entry = self._active.pop(token, None)
            if entry is None:
                return
            samples = entry["samples_mb"]
            samples.append(end_mb)

        duration_s = end_s - float(entry["start_s"])
        mean_mem_mb = float(sum(samples) / len(samples)) if samples else end_mb
        peak_mem_mb = float(max(samples)) if samples else end_mb

        self.completed.append(
            StepRun(
                layer_name=str(entry["layer_name"]),
                step_name=str(entry["step_name"]),
                start_s=float(entry["start_s"]),
                end_s=end_s,
                duration_s=duration_s,
                start_mem_mb=float(entry["start_mb"]),
                end_mem_mb=end_mb,
                mean_mem_mb=mean_mem_mb,
                peak_mem_mb=peak_mem_mb,
            )
        )


_CURRENT_LAYER = threading.local()


def _set_current_layer(layer_name: str | None) -> None:
    _CURRENT_LAYER.name = layer_name


def _get_current_layer() -> str | None:
    return getattr(_CURRENT_LAYER, "name", None)


def _channel_from_layer_config(layer_config: Any) -> str:
    if isinstance(layer_config, dict):
        value = layer_config.get("channel_id")
        if value:
            return str(value)
    return "unknown"


def _avg(values: list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), p))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _layer_output_manifest(layers: list[dict[str, Any]]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    for layer in layers:
        layer_name = str(layer["name"])
        out_dir = Path(layer["outdir"])
        layer_manifest: dict[str, Any] = {
            "tile_count": 0,
            "tile_sha256": {},
            "index_sha256": None,
        }

        index_file = out_dir / "index.json"
        if index_file.exists():
            try:
                parsed = json.loads(index_file.read_text())
                canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                layer_manifest["index_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            except Exception:
                layer_manifest["index_sha256"] = _sha256_file(index_file)

        tile_hashes: dict[str, str] = {}
        if out_dir.exists():
            for timestamp_dir in sorted(path for path in out_dir.iterdir() if path.is_dir()):
                for tile_file in sorted(timestamp_dir.glob("tile_*.png")):
                    rel_path = f"{timestamp_dir.name}/{tile_file.name}"
                    tile_hashes[rel_path] = _sha256_file(tile_file)

        layer_manifest["tile_count"] = len(tile_hashes)
        layer_manifest["tile_sha256"] = tile_hashes
        manifest[layer_name] = layer_manifest

    return manifest


def _compare_manifests(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    layer_names = sorted(set(reference) | set(candidate))
    mismatched_layers = []
    index_parity_ok = True
    tile_count_parity_ok = True
    checksum_parity_ok = True

    for layer_name in layer_names:
        ref_layer = reference.get(layer_name, {})
        cand_layer = candidate.get(layer_name, {})

        index_equal = ref_layer.get("index_sha256") == cand_layer.get("index_sha256")
        tile_count_equal = ref_layer.get("tile_count") == cand_layer.get("tile_count")
        checksum_equal = ref_layer.get("tile_sha256") == cand_layer.get("tile_sha256")

        if not index_equal:
            index_parity_ok = False
        if not tile_count_equal:
            tile_count_parity_ok = False
        if not checksum_equal:
            checksum_parity_ok = False

        if not (index_equal and tile_count_equal and checksum_equal):
            mismatched_layers.append(
                {
                    "layer": layer_name,
                    "index_match": index_equal,
                    "tile_count_match": tile_count_equal,
                    "checksum_match": checksum_equal,
                }
            )

    return {
        "index_parity": index_parity_ok,
        "tile_count_parity": tile_count_parity_ok,
        "checksum_parity": checksum_parity_ok,
        "layer_mismatches": mismatched_layers,
    }


@contextmanager
def _instrument_goes_steps(sampler: MemoryStepSampler):
    patches: list[tuple[Any, str, Any]] = []

    def patch(obj: Any, attr: str, step_name_builder: Callable[..., str]) -> None:
        original = getattr(obj, attr)

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            layer_name = _get_current_layer()
            if layer_name is None:
                return original(*args, **kwargs)

            step_name = step_name_builder(*args, **kwargs)
            token = sampler.enter(layer_name, step_name)
            try:
                return original(*args, **kwargs)
            finally:
                sampler.exit(token)

        setattr(obj, attr, wrapped)
        patches.append((obj, attr, original))

    patch(ewmrs_pipeline, "_latest_source_file", lambda *_args, **_kwargs: "latest_source_file")
    patch(goes_transform, "extract_goes_timestamp_iso", lambda *_args, **_kwargs: "extract_goes_timestamp")
    patch(
        goes_transform,
        "load_goes_abi_render_dataset",
        lambda *_args, **kwargs: f"load_goes_dataset[{_channel_from_layer_config(kwargs.get('layer_config') or (_args[1] if len(_args) > 1 else None))}]",
    )
    patch(goes_transform, "reproject_goes_abi_to_web_mercator", lambda *_args, **_kwargs: "reproject_goes_dataset")

    patch(
        goes_transform,
        "load_reproject_goes_abi_render_array",
        lambda *_args, **kwargs: f"load_reproject_channel[{_channel_from_layer_config(kwargs.get('layer_config') or (_args[1] if len(_args) > 1 else None))}]",
    )

    patch(render_module.GUILayerRenderer, "convert_to_png", lambda *_args, **_kwargs: "convert_to_png")
    patch(
        render_module.GUIValueWriter,
        "_save_chunks_from_array",
        lambda *_args, **_kwargs: "save_value_chunks",
    )
    patch(tiler_module, "save_float16_chunk", lambda *_args, **_kwargs: "save_float16_chunk")

    try:
        yield
    finally:
        for obj, attr, original in reversed(patches):
            setattr(obj, attr, original)


def _build_temp_goes_layers(out_root: Path) -> list[dict[str, Any]]:
    temp_layers: list[dict[str, Any]] = []
    for layer in get_goes_file_list():
        layer_copy = dict(layer)
        layer_copy["outdir"] = out_root / layer["name"]
        temp_layers.append(layer_copy)
    return temp_layers


def _summarize_step_runs(step_runs: list[StepRun]) -> dict[str, Any]:
    by_layer: dict[str, list[StepRun]] = defaultdict(list)
    for run in step_runs:
        by_layer[run.layer_name].append(run)

    summary: dict[str, Any] = {}
    for layer_name, runs in sorted(by_layer.items()):
        by_step: dict[str, list[StepRun]] = defaultdict(list)
        for run in runs:
            by_step[run.step_name].append(run)

        step_stats = []
        for step_name, step_calls in by_step.items():
            durations = [call.duration_s for call in step_calls]
            mean_mems = [call.mean_mem_mb for call in step_calls]
            peaks = [call.peak_mem_mb for call in step_calls]
            deltas = [call.end_mem_mb - call.start_mem_mb for call in step_calls]

            step_stats.append(
                {
                    "step": step_name,
                    "calls": len(step_calls),
                    "total_duration_s": sum(durations),
                    "avg_duration_s": _avg(durations),
                    "max_duration_s": max(durations),
                    "avg_memory_mb": _avg(mean_mems),
                    "peak_memory_mb": max(peaks),
                    "avg_memory_delta_mb": _avg(deltas),
                }
            )

        step_stats.sort(key=lambda item: item["total_duration_s"], reverse=True)
        summary[layer_name] = {
            "steps": step_stats,
        }

    return summary


def run_detailed_single_process_benchmark(
    layers: list[dict[str, Any]],
    sample_interval_s: float,
) -> dict[str, Any]:
    sampler = MemoryStepSampler(sample_interval_s=sample_interval_s, include_children=False)
    layer_status: dict[str, dict[str, Any]] = {}

    sampler.start()
    start_s = time.perf_counter()
    try:
        with _instrument_goes_steps(sampler):
            for layer in layers:
                layer_name = str(layer["name"])
                _set_current_layer(layer_name)

                layer_token = sampler.enter(layer_name, "layer_total")
                layer_start = time.perf_counter()
                error_message = None
                rendered_output = None

                try:
                    _, rendered_output = _render_layer(layer)
                except Exception as exc:  # pragma: no cover - defensive benchmark harness
                    error_message = str(exc)
                finally:
                    layer_elapsed = time.perf_counter() - layer_start
                    sampler.exit(layer_token)
                    _set_current_layer(None)

                output_count = len(rendered_output) if rendered_output else 0
                layer_status[layer_name] = {
                    "duration_s": layer_elapsed,
                    "success": bool(rendered_output),
                    "output_count": output_count,
                    "error": error_message,
                }
    finally:
        total_s = time.perf_counter() - start_s
        sampler.stop()
        _set_current_layer(None)

    summary = _summarize_step_runs(sampler.completed)

    for layer_name, status in layer_status.items():
        layer_summary = summary.setdefault(layer_name, {"steps": []})
        layer_summary["layer"] = {
            "duration_s": status["duration_s"],
            "success": status["success"],
            "output_count": status["output_count"],
            "error": status["error"],
        }

    avg_mem = _avg(sampler.pipeline_samples_mb)
    peak_mem = max(sampler.pipeline_samples_mb) if sampler.pipeline_samples_mb else 0.0
    layer_latencies = [
        layer_info.get("layer", {}).get("duration_s", 0.0)
        for layer_info in summary.values()
        if layer_info.get("layer")
    ]

    return {
        "mode": "detailed_single_process",
        "total_duration_s": total_s,
        "avg_memory_mb": avg_mem,
        "peak_memory_mb": peak_mem,
        "latency_p50_s": _percentile(layer_latencies, 50),
        "latency_p95_s": _percentile(layer_latencies, 95),
        "layer_latencies_s": layer_latencies,
        "layer_count": len(layers),
        "layers": summary,
    }


def run_parallel_pipeline_benchmark(
    layers: list[dict[str, Any]],
    sample_interval_s: float,
) -> dict[str, Any]:
    sampler = MemoryStepSampler(sample_interval_s=sample_interval_s, include_children=True)

    original_get_goes_file_list = ewmrs_pipeline.get_goes_file_list
    original_cleanup_old_gui_files = ewmrs_pipeline.cleanup_old_gui_files
    original_load_reproject = goes_transform.load_reproject_goes_abi_render_array
    original_load_payload = goes_transform._load_goes_abi_render_payload
    original_reproject_payload = goes_transform._reproject_goes_payload_to_web_mercator
    original_save_chunks_from_array = render_module.GUIValueWriter._save_chunks_from_array
    original_save_float16_chunk = tiler_module.save_float16_chunk

    benchmark_state = {
        "pipeline_start_s": None,
        "payload_by_channel_s": defaultdict(list),
        "reprojection_by_channel_s": defaultdict(list),
        "chunk_batch_s": [],
        "chunk_save_s": [],
        "first_chunk_latency_s": None,
        "first_chunk_finished_s": None,
    }
    benchmark_channel = threading.local()

    def _current_channel() -> str:
        return getattr(benchmark_channel, "channel_id", "unknown")

    @functools.wraps(original_load_reproject)
    def wrapped_load_reproject(path, layer_config, *args, **kwargs):
        previous_channel = getattr(benchmark_channel, "channel_id", None)
        benchmark_channel.channel_id = _channel_from_layer_config(layer_config)
        try:
            return original_load_reproject(path, layer_config, *args, **kwargs)
        finally:
            benchmark_channel.channel_id = previous_channel

    @functools.wraps(original_load_payload)
    def wrapped_load_payload(*args, **kwargs):
        channel_id = _current_channel()
        start_s = time.perf_counter()
        try:
            return original_load_payload(*args, **kwargs)
        finally:
            benchmark_state["payload_by_channel_s"][channel_id].append(time.perf_counter() - start_s)

    @functools.wraps(original_reproject_payload)
    def wrapped_reproject_payload(*args, **kwargs):
        channel_id = _current_channel()
        start_s = time.perf_counter()
        try:
            return original_reproject_payload(*args, **kwargs)
        finally:
            benchmark_state["reprojection_by_channel_s"][channel_id].append(time.perf_counter() - start_s)

    @functools.wraps(original_save_chunks_from_array)
    def wrapped_save_chunks_from_array(self, rgba, timestamp, *args, **kwargs):
        start_s = time.perf_counter()
        try:
            return original_save_chunks_from_array(self, rgba, timestamp, *args, **kwargs)
        finally:
            benchmark_state["chunk_batch_s"].append(time.perf_counter() - start_s)

    def wrapped_save_float16_chunk(chunk_data, output_path):
        start_s = time.perf_counter()
        try:
            return original_save_float16_chunk(chunk_data, output_path)
        finally:
            finished_s = time.perf_counter()
            benchmark_state["chunk_save_s"].append(finished_s - start_s)
            if benchmark_state["first_chunk_latency_s"] is None and benchmark_state["pipeline_start_s"] is not None:
                benchmark_state["first_chunk_latency_s"] = finished_s - float(benchmark_state["pipeline_start_s"])
                benchmark_state["first_chunk_finished_s"] = finished_s

    ewmrs_pipeline.get_goes_file_list = lambda: layers
    ewmrs_pipeline.cleanup_old_gui_files = lambda max_age_minutes=120: None
    goes_transform.load_reproject_goes_abi_render_array = wrapped_load_reproject
    goes_transform._load_goes_abi_render_payload = wrapped_load_payload
    goes_transform._reproject_goes_payload_to_web_mercator = wrapped_reproject_payload
    render_module.GUIValueWriter._save_chunks_from_array = wrapped_save_chunks_from_array
    tiler_module.save_float16_chunk = wrapped_save_float16_chunk

    sampler.start()
    start_s = time.perf_counter()
    benchmark_state["pipeline_start_s"] = start_s
    results = {}
    try:
        results = ewmrs_pipeline.run_goes_render_pipeline(datetime.now(timezone.utc), max_entries=10)
    finally:
        total_s = time.perf_counter() - start_s
        sampler.stop()
        ewmrs_pipeline.get_goes_file_list = original_get_goes_file_list
        ewmrs_pipeline.cleanup_old_gui_files = original_cleanup_old_gui_files
        goes_transform.load_reproject_goes_abi_render_array = original_load_reproject
        goes_transform._load_goes_abi_render_payload = original_load_payload
        goes_transform._reproject_goes_payload_to_web_mercator = original_reproject_payload
        render_module.GUIValueWriter._save_chunks_from_array = original_save_chunks_from_array
        tiler_module.save_float16_chunk = original_save_float16_chunk

    avg_mem = _avg(sampler.pipeline_samples_mb)
    peak_mem = max(sampler.pipeline_samples_mb) if sampler.pipeline_samples_mb else 0.0
    successful_layers = sum(1 for rendered in results.values() if rendered)
    total_tiles_written = sum(len(rendered) for rendered in results.values() if rendered)
    throughput_tiles_per_s = (total_tiles_written / total_s) if total_s > 0 else 0.0

    def _summarize_channel_timings(values_by_channel: dict[str, list[float]]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for channel_id, samples in sorted(values_by_channel.items()):
            if not samples:
                continue
            summary[channel_id] = {
                "calls": len(samples),
                "total_duration_s": sum(samples),
                "avg_duration_s": _avg(samples),
                "max_duration_s": max(samples),
            }
        return summary

    first_tile_finished_s = benchmark_state["first_chunk_finished_s"]

    return {
        "mode": "parallel_pipeline",
        "total_duration_s": total_s,
        "avg_memory_mb": avg_mem,
        "peak_memory_mb": peak_mem,
        "total_tiles_written": total_tiles_written,
        "throughput_tiles_per_s": throughput_tiles_per_s,
        "render_start_to_first_tile_s": float(benchmark_state["first_chunk_latency_s"] or 0.0),
        "tile_write_total_s": sum(benchmark_state["chunk_batch_s"]),
        "avg_tile_save_s": _avg(benchmark_state["chunk_save_s"]),
        "reprojection_by_channel": _summarize_channel_timings(benchmark_state["reprojection_by_channel_s"]),
        "payload_load_by_channel": _summarize_channel_timings(benchmark_state["payload_by_channel_s"]),
        "queue_lag_s": None,
        "layer_count": len(layers),
        "successful_layers": successful_layers,
        "failed_layers": len(layers) - successful_layers,
    }


def _aggregate_runs(run_reports: list[dict[str, Any]]) -> dict[str, Any]:
    all_latencies: list[float] = []
    throughputs: list[float] = []
    peak_memories: list[float] = []
    first_tile_latencies: list[float] = []
    total_phase_times: list[float] = []

    for run_report in run_reports:
        detailed = run_report["detailed_single_process"]
        parallel = run_report["parallel_pipeline"]
        all_latencies.extend(detailed.get("layer_latencies_s", []))
        throughputs.append(float(parallel.get("throughput_tiles_per_s", 0.0)))
        peak_memories.append(float(parallel.get("peak_memory_mb", 0.0)))
        first_tile_latencies.append(float(parallel.get("render_start_to_first_tile_s", 0.0)))
        total_phase_times.append(float(parallel.get("total_duration_s", 0.0)))

    return {
        "latency_per_render_s": {
            "p50": _percentile(all_latencies, 50),
            "p95": _percentile(all_latencies, 95),
            "samples": len(all_latencies),
        },
        "throughput_tiles_per_s": {
            "p50": _percentile(throughputs, 50),
            "p95": _percentile(throughputs, 95),
            "samples": len(throughputs),
        },
        "render_start_to_first_tile_s": {
            "p50": _percentile(first_tile_latencies, 50),
            "p95": _percentile(first_tile_latencies, 95),
            "samples": len(first_tile_latencies),
        },
        "total_goes_phase_s": {
            "p50": _percentile(total_phase_times, 50),
            "p95": _percentile(total_phase_times, 95),
            "samples": len(total_phase_times),
        },
        "peak_memory_mb": {
            "p50": _percentile(peak_memories, 50),
            "p95": _percentile(peak_memories, 95),
            "max": max(peak_memories) if peak_memories else 0.0,
            "samples": len(peak_memories),
        },
    }


def _print_console_summary(report: dict[str, Any]) -> None:
    print("=" * 90)
    print("GOES RENDER PIPELINE BENCHMARK")
    print("=" * 90)

    aggregate = report["aggregate_metrics"]
    correctness = report["correctness_parity"]
    print(f"\nRuns: {report['runs']}")
    print("\nMandatory Metrics")
    print(
        f"  Latency per render (layer_total): "
        f"p50={aggregate['latency_per_render_s']['p50']:.2f}s, "
        f"p95={aggregate['latency_per_render_s']['p95']:.2f}s"
    )
    print(
        f"  Render start to first tile: "
        f"p50={aggregate['render_start_to_first_tile_s']['p50']:.2f}s, "
        f"p95={aggregate['render_start_to_first_tile_s']['p95']:.2f}s"
    )
    print(
        f"  Throughput (tiles/sec): "
        f"p50={aggregate['throughput_tiles_per_s']['p50']:.2f}, "
        f"p95={aggregate['throughput_tiles_per_s']['p95']:.2f}"
    )
    print(
        f"  Total GOES phase: "
        f"p50={aggregate['total_goes_phase_s']['p50']:.2f}s, "
        f"p95={aggregate['total_goes_phase_s']['p95']:.2f}s"
    )
    print(
        f"  Peak memory (MB): "
        f"p50={aggregate['peak_memory_mb']['p50']:.1f}, "
        f"p95={aggregate['peak_memory_mb']['p95']:.1f}, "
        f"max={aggregate['peak_memory_mb']['max']:.1f}"
    )

    print("\nCorrectness Parity")
    print(f"  index.json parity:   {correctness['index_parity_all_runs']}")
    print(f"  tile count parity:   {correctness['tile_count_parity_all_runs']}")
    print(f"  tile checksum parity:{correctness['checksum_parity_all_runs']}")

    baseline = report["run_reports"][0]
    detailed = baseline["detailed_single_process"]
    parallel = baseline["parallel_pipeline"]
    print("\nBaseline Run (run 1)")
    print(f"  Detailed total duration: {detailed['total_duration_s']:.2f}s")
    print(f"  Parallel total duration: {parallel['total_duration_s']:.2f}s")
    print(f"  First tile latency:      {parallel['render_start_to_first_tile_s']:.2f}s")
    print(f"  Parallel throughput:     {parallel['throughput_tiles_per_s']:.2f} tiles/sec")
    print(f"  Parallel peak memory:    {parallel['peak_memory_mb']:.1f} MB")
    if parallel.get("reprojection_by_channel"):
        slowest_channel = max(
            parallel["reprojection_by_channel"].items(),
            key=lambda item: item[1].get("total_duration_s", 0.0),
        )
        print(
            f"  Slowest reprojection:    {slowest_channel[0]} "
            f"({slowest_channel[1]['total_duration_s']:.2f}s total)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark GOES rendering pipeline")
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0.1,
        help="Memory sampling interval in seconds (default: 0.1)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of repeated benchmark runs (default: 5)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path",
    )
    args = parser.parse_args()

    runs = max(1, int(args.runs))
    run_reports: list[dict[str, Any]] = []
    parity_checks: list[dict[str, Any]] = []
    reference_manifest: dict[str, Any] | None = None

    with tempfile.TemporaryDirectory(prefix="goes_render_benchmark_") as tmp_dir:
        out_root = Path(tmp_dir)
        for run_idx in range(1, runs + 1):
            run_dir = out_root / f"run_{run_idx}"
            detailed_layers = _build_temp_goes_layers(run_dir / "detailed")
            parallel_layers = _build_temp_goes_layers(run_dir / "parallel")

            detailed = run_detailed_single_process_benchmark(detailed_layers, args.sample_interval)
            parallel = run_parallel_pipeline_benchmark(parallel_layers, args.sample_interval)

            manifest = _layer_output_manifest(parallel_layers)
            if reference_manifest is None:
                reference_manifest = manifest
                parity = {
                    "index_parity": True,
                    "tile_count_parity": True,
                    "checksum_parity": True,
                    "layer_mismatches": [],
                }
            else:
                parity = _compare_manifests(reference_manifest, manifest)

            run_reports.append(
                {
                    "run": run_idx,
                    "detailed_single_process": detailed,
                    "parallel_pipeline": parallel,
                    "correctness_parity": parity,
                }
            )
            parity_checks.append(parity)

    aggregate_metrics = _aggregate_runs(run_reports)
    correctness_parity = {
        "index_parity_all_runs": all(check.get("index_parity", False) for check in parity_checks),
        "tile_count_parity_all_runs": all(check.get("tile_count_parity", False) for check in parity_checks),
        "checksum_parity_all_runs": all(check.get("checksum_parity", False) for check in parity_checks),
        "run_mismatches": [
            {
                "run": run_report["run"],
                "layer_mismatches": run_report["correctness_parity"].get("layer_mismatches", []),
            }
            for run_report in run_reports
            if run_report["correctness_parity"].get("layer_mismatches")
        ],
    }

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "sample_interval_s": args.sample_interval,
        "runs": runs,
        "run_reports": run_reports,
        "aggregate_metrics": aggregate_metrics,
        "correctness_parity": correctness_parity,
        "detailed_single_process": run_reports[0]["detailed_single_process"],
        "parallel_pipeline": run_reports[0]["parallel_pipeline"],
    }

    _print_console_summary(report)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"\nWrote benchmark JSON: {args.output}")
    else:
        print("\nJSON report")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
