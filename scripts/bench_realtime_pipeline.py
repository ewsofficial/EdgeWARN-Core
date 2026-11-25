#!/usr/bin/env python3
"""Execution-time profiler and benchmark for the realtime Core cycle.

Profiles where a realtime Core cycle spends wall time, CPU and memory, down to
individual functions and call paths, and benchmarks it over repeated trials.

Each trial replays one realtime scan through the real worker that
``run_edgewarn.py`` spawns per cycle (``EdgeWARN.pipeline.edgewarn_cycle_worker``)
with detection, integration and final optional-input manifests pinned exactly
as the ingest service would publish them. Every trial runs in a fresh forked
process on its own copy of an isolated runtime root, so trials start from the
same tracking/StormProb state and nothing is written to the source tree.

Inputs come from an existing runtime tree (``--base-dir``, read only). The
benchmark selects one scan, snapshots its inputs plus the prior Core state
(earlier stormcells, lineage buffer, cell histories, alert snapshots and a
StormProb database rewound to before the scan) into a seed directory, and
copies that seed for every trial.

What is measured, per trial:

* ``process``  - whole-trial wall, user/system CPU, max RSS, page faults,
  block I/O, context switches, GC pauses, threads, instrumentation overhead.
* ``stage``    - benchmark-owned spans: process startup and the full cycle.
* ``phase``    - every ``util.performance`` tracker span the pipeline already
  emits (Detection, Integration - RAP, Integration - CTAM, ...).
* ``import``   - each first-party module imported lazily during the cycle.
* ``function`` - every Python function and method defined in the instrumented
  packages (default ``EdgeWARN``, ``common``, ``util``) plus curated
  third-party entry points (xarray, eccodes, rasterio, shapely, ONNX Runtime,
  json), keyed ``module:qualname``.
* ``callpath`` - the same functions split by call path (``a > b > c``),
  including work dispatched to thread pools, with recursion collapsed.

Function metrics: call count, inclusive/self wall and thread CPU, the
overhead-corrected inclusive/self wall, the per-call latency p50/p95/p99/max,
the Python heap (``tracemalloc``) peak above entry and net retained, and the
RSS peak above entry and net change. The Python heap peak is exact (tracemalloc
peaks are re-armed on every instrumented call boundary). RSS peaks combine
call-boundary reads with a background sampler (``--sample-ms``), so a spike
shorter than the sampling interval inside a single call may be missed.

Notes on reading the numbers:

* Inclusive times of work running concurrently in threads overlap; thread
  CPU is the CPU of the calling thread only. Stage and phase rows also report
  whole-process CPU.
* ``tracemalloc`` slows allocation-heavy Python code; run with
  ``--no-tracemalloc`` when wall-time fidelity matters more than heap peaks.
  The ``*_corr`` columns subtract the calibrated per-call wrapper overhead.
* Inputs are copied per trial and therefore page-cache warm.
* Generators, coroutines and C-implemented callables are not wrapped; their
  cost is attributed to the nearest instrumented caller's self time.

Results are summarized across trials (mean, median, p95, p99, min, max, std)
in a long-format CSV, with the raw per-trial values in ``*_trials.csv`` and
each trial's pipeline log in ``*_logs/``.

    python scripts/bench_realtime_pipeline.py --base-dir /path/to/runtime
    python scripts/bench_realtime_pipeline.py --base-dir /path/to/runtime \\
        --trials 5 --cycle 2026-11-24T01:48:00Z --output bench.csv
    python scripts/bench_realtime_pipeline.py --base-dir /path/to/runtime \\
        -- --disable-tracking          # Core flags after "--"
"""
import argparse
import csv
import dataclasses
import functools
import gc
import importlib.abc
import inspect
import json
import math
import multiprocessing
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
import traceback
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

MIB = 1024.0 * 1024.0
DEFAULT_PREFIXES = ("EdgeWARN", "common", "util")
# Modules whose functions must stay unwrapped: the tracker is re-pointed at the
# profiler, and wrapping the profiler's own collaborators would recurse.
EXCLUDED_MODULES = ("util.performance",)
INSTRUMENTED_DUNDERS = {"__init__", "__call__", "__enter__", "__exit__"}
# Curated third-party entry points that dominate Core's array, geometry, model
# and serialization work. Missing modules or attributes are ignored.
THIRD_PARTY = {
    "xarray": ("open_dataset", "load_dataset", "open_dataarray"),
    "eccodes": ("codes_grib_new_from_file", "codes_get_values", "codes_get_array",
                "codes_get", "codes_release"),
    "rasterio.features": ("rasterize", "geometry_mask", "shapes"),
    "shapely.vectorized": ("contains",),
    "shapely.ops": ("unary_union", "transform"),
    "onnxruntime": ("InferenceSession.__init__", "InferenceSession.run"),
    "json": ("load", "loads", "dump", "dumps"),
}
HIST_BINS = 100           # 10 bins per decade from 100 ns to 1000 s
HIST_FLOOR_LOG10 = -7.0
DETECTION_PRODUCT = "MergedReflectivityQCComposite_00.50"
SELECTION_CANDIDATES = 6  # newest composite scans considered for --cycle auto
WRITE_SETTLE_SECONDS = 15.0


# --------------------------------------------------------------------------
# Profiler (runs only inside a trial process)
# --------------------------------------------------------------------------

def _hist_bin(seconds):
    if seconds <= 10.0 ** HIST_FLOOR_LOG10:
        return 0
    return min(HIST_BINS - 1, int((math.log10(seconds) - HIST_FLOOR_LOG10) * 10.0))


def _hist_quantile(hist, q):
    """Log-interpolated quantile of a latency histogram (~12% bin width)."""
    total = sum(hist)
    if not total:
        return 0.0
    target = q * total
    seen = 0
    for index, count in enumerate(hist):
        if count and seen + count >= target:
            low = 10.0 ** (index / 10.0 + HIST_FLOOR_LOG10)
            high = 10.0 ** ((index + 1) / 10.0 + HIST_FLOOR_LOG10)
            return low * (high / low) ** ((target - seen) / count)
        seen += count
    return 10.0 ** (len(hist) / 10.0 + HIST_FLOOR_LOG10)


class _RssReader:
    """Current resident set size in bytes, cheap enough for every call."""

    def __init__(self):
        try:
            self._fd = os.open("/proc/self/statm", os.O_RDONLY)
            self._page = os.sysconf("SC_PAGE_SIZE")
            self.read = self._read_proc
        except (OSError, AttributeError, ValueError):
            try:
                import psutil
                process = psutil.Process()
                self.read = lambda: process.memory_info().rss
            except ImportError:
                self.read = lambda: 0

    def _read_proc(self):
        return int(os.pread(self._fd, 128, 0).split()[1]) * self._page


class _Frame:
    __slots__ = ("key", "path", "outer", "t0", "c0", "py0", "py1", "pypeak",
                 "rss0", "rss1", "rsspeak", "child_wall", "child_cpu",
                 "children", "descendants", "p0")

    def __init__(self, key, path):
        self.key = key
        self.path = path
        self.outer = True
        self.child_wall = self.child_cpu = 0.0
        self.children = self.descendants = 0
        self.p0 = 0.0


class _Stat:
    __slots__ = ("calls", "outer_calls", "wall_incl", "wall_self", "cpu_incl",
                 "cpu_self", "wall_min", "wall_max", "py_peak", "py_net", "rss_peak",
                 "rss_net", "descendants", "children", "hist")

    def __init__(self):
        self.calls = self.outer_calls = self.descendants = self.children = 0
        self.wall_incl = self.wall_self = self.cpu_incl = self.cpu_self = 0.0
        self.wall_min = math.inf
        self.wall_max = 0.0
        self.py_peak = self.rss_peak = 0
        self.py_net = self.rss_net = 0
        self.hist = [0] * HIST_BINS

    def add(self, frame, wall, cpu):
        self.calls += 1
        self.children += frame.children
        self.wall_self += wall - frame.child_wall
        self.cpu_self += cpu - frame.child_cpu
        self.wall_min = min(self.wall_min, wall)
        self.wall_max = max(self.wall_max, wall)
        self.hist[_hist_bin(wall)] += 1
        self.py_peak = max(self.py_peak, frame.pypeak - frame.py0)
        self.rss_peak = max(self.rss_peak, frame.rsspeak - frame.rss0)
        if frame.outer:
            # Recursive re-entries are already inside the outer activation.
            self.outer_calls += 1
            self.wall_incl += wall
            self.cpu_incl += cpu
            self.descendants += frame.descendants
            self.py_net += frame.py1 - frame.py0
            self.rss_net += frame.rss1 - frame.rss0

    def metrics(self, overhead):
        incl_corr = max(0.0, self.wall_incl - self.descendants * overhead)
        self_corr = max(0.0, self.wall_self - self.children * overhead)

        def quantile(q):
            # Bin interpolation can overshoot the observed range; clamp to it.
            return min(max(_hist_quantile(self.hist, q), self.wall_min), self.wall_max)

        return {
            "calls": self.calls,
            "wall_incl_s": self.wall_incl,
            "wall_incl_corr_s": incl_corr,
            "wall_self_s": self.wall_self,
            "wall_self_corr_s": self_corr,
            "cpu_incl_s": self.cpu_incl,
            "cpu_self_s": self.cpu_self,
            "cpu_util": self.cpu_incl / self.wall_incl if self.wall_incl > 0 else 0.0,
            "call_p50_s": quantile(0.50),
            "call_p95_s": quantile(0.95),
            "call_p99_s": quantile(0.99),
            "call_max_s": self.wall_max,
            "py_peak_mib": self.py_peak / MIB,
            "py_net_mib": self.py_net / MIB,
            "rss_peak_mib": self.rss_peak / MIB,
            "rss_net_mib": self.rss_net / MIB,
            "descendant_calls": self.descendants,
            "overhead_est_s": self.descendants * overhead,
        }


class _Span:
    """A named, non-nested interval (tracker phase or benchmark stage)."""

    def __init__(self):
        self.count = 0
        self.wall = self.thread_cpu = self.process_cpu = 0.0
        self.py_peak = self.rss_peak = self.rss_peak_abs = 0
        self.py_net = self.rss_net = 0

    def metrics(self):
        return {
            "count": self.count,
            "wall_s": self.wall,
            "thread_cpu_s": self.thread_cpu,
            "process_cpu_s": self.process_cpu,
            "process_cpu_util": self.process_cpu / self.wall if self.wall > 0 else 0.0,
            "py_peak_mib": self.py_peak / MIB,
            "py_net_mib": self.py_net / MIB,
            "rss_peak_mib": self.rss_peak / MIB,
            "rss_peak_abs_mib": self.rss_peak_abs / MIB,
            "rss_net_mib": self.rss_net / MIB,
        }


def _process_cpu():
    times = os.times()
    return times.user + times.system


class Profiler:
    """Function-level wall/CPU/heap/RSS attribution with call-path context."""

    def __init__(self, prefixes, *, trace_memory, sample_seconds, third_party):
        self.prefixes = tuple(prefixes)
        self.trace_memory = trace_memory
        self.sample_seconds = sample_seconds
        self.third_party = third_party
        self.lock = threading.Lock()
        self.local = threading.local()
        self.active = {}
        self.functions = {}
        self.paths = {}
        self.spans = {}
        self.open_spans = {}
        self.originals = {}       # id(original) -> (original, wrapper)
        self.instrumented = set()
        self.classes = set()
        self.skipped = 0
        self.overhead = 0.0
        self.rss = _RssReader()
        self.peak_rss = 0
        self.peak_py = 0
        self.max_threads = 1
        self.gc_pause = 0.0
        self.gc_runs = [0, 0, 0]
        self.gc_collected = 0
        self._gc_started = 0.0
        self._stop = threading.Event()
        self._sampler = None
        self._finder = None

    # -- installation -----------------------------------------------------

    def matches(self, name):
        if name == __name__ or any(name == item or name.startswith(item + ".")
                                   for item in EXCLUDED_MODULES):
            return False
        return any(name == prefix or name.startswith(prefix + ".") for prefix in self.prefixes)

    def install(self):
        if self.trace_memory:
            tracemalloc.start(1)
        if self.third_party:
            for name in THIRD_PARTY:
                module = sys.modules.get(name)
                if module is not None:
                    self._instrument_third_party(module)
        for name, module in list(sys.modules.items()):
            if module is not None and self.matches(name):
                self.instrument_module(module, sweep=False)
        self._sweep_all()
        self._finder = _InstrumentingFinder(self)
        sys.meta_path.insert(0, self._finder)
        self._patch_thread_pools()
        self._patch_tracker()
        gc.callbacks.append(self._gc_callback)
        self._sampler = threading.Thread(target=self._sample, name="bench-sampler", daemon=True)
        self._sampler.start()

    def uninstall(self):
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2.0)
        if self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        if self._gc_callback in gc.callbacks:
            gc.callbacks.remove(self._gc_callback)
        if self.trace_memory:
            tracemalloc.stop()

    def instrument_module(self, module, *, sweep=True):
        name = module.__name__
        if name in self.instrumented:
            return
        self.instrumented.add(name)
        for attr, value in list(vars(module).items()):
            if isinstance(value, types.FunctionType) and value.__module__ == name:
                wrapper = self._wrap(value, f"{name}:{value.__qualname__}")
                if wrapper is not value:
                    self._set(module, attr, wrapper)
            elif isinstance(value, type) and value.__module__ == name:
                self._instrument_class(value, name)
        if sweep:
            self._sweep(module)

    def _instrument_class(self, cls, module_name):
        if id(cls) in self.classes:
            return
        self.classes.add(id(cls))
        for attr, value in list(vars(cls).items()):
            if attr.startswith("__") and attr.endswith("__") and attr not in INSTRUMENTED_DUNDERS:
                continue
            replacement = None
            if isinstance(value, types.FunctionType):
                replacement = self._wrap(value, f"{module_name}:{value.__qualname__}")
            elif isinstance(value, staticmethod) and isinstance(value.__func__, types.FunctionType):
                function = value.__func__
                wrapped = self._wrap(function, f"{module_name}:{function.__qualname__}")
                replacement = staticmethod(wrapped) if wrapped is not function else None
            elif isinstance(value, classmethod) and isinstance(value.__func__, types.FunctionType):
                function = value.__func__
                wrapped = self._wrap(function, f"{module_name}:{function.__qualname__}")
                replacement = classmethod(wrapped) if wrapped is not function else None
            elif isinstance(value, type) and value.__module__ == module_name:
                self._instrument_class(value, module_name)
            if replacement is not None and replacement is not value:
                self._set(cls, attr, replacement)

    def _instrument_third_party(self, module):
        for dotted in THIRD_PARTY.get(module.__name__, ()):
            owner = module
            *parents, attr = dotted.split(".")
            try:
                for part in parents:
                    owner = getattr(owner, part)
                original = getattr(owner, attr)
            except AttributeError:
                continue
            if isinstance(owner, type):
                original = vars(owner).get(attr, original)
            if not callable(original) or id(original) in self.originals:
                continue
            wrapper = self._make_wrapper(original, f"[lib] {module.__name__}.{dotted}")
            self.originals[id(original)] = (original, wrapper)
            self._set(owner, attr, wrapper)

    def _wrap(self, function, key):
        known = self.originals.get(id(function))
        if known is not None and known[0] is function:
            return known[1]
        if getattr(function, "__bench_wrapper__", False):
            return function
        if (inspect.isgeneratorfunction(function) or inspect.iscoroutinefunction(function)
                or inspect.isasyncgenfunction(function)):
            # Suspension interleaves frames on one thread; keep them unwrapped.
            self.skipped += 1
            return function
        wrapper = self._make_wrapper(function, key)
        self.originals[id(function)] = (function, wrapper)
        return wrapper

    def _make_wrapper(self, function, key):
        enter, leave = self.enter, self.leave

        def wrapper(*args, **kwargs):
            frame = enter(key)
            try:
                return function(*args, **kwargs)
            finally:
                leave(frame)

        try:
            functools.update_wrapper(wrapper, function)
        except (AttributeError, TypeError):
            pass
        wrapper.__bench_wrapper__ = True
        return wrapper

    @staticmethod
    def _set(owner, attr, value):
        try:
            setattr(owner, attr, value)
        except (AttributeError, TypeError):
            pass

    def _sweep(self, module):
        """Rebind names a module imported with ``from x import f`` to wrappers."""
        for attr, value in list(vars(module).items()):
            known = self.originals.get(id(value))
            if known is not None and known[0] is value:
                self._set(module, attr, known[1])

    def _sweep_all(self):
        for name in list(self.instrumented):
            module = sys.modules.get(name)
            if module is not None:
                self._sweep(module)

    def _patch_thread_pools(self):
        from concurrent.futures import ThreadPoolExecutor

        original_submit = ThreadPoolExecutor.submit
        profiler = self

        def submit(executor, fn, /, *args, **kwargs):
            base = profiler.current_path()

            def run(*inner_args, **inner_kwargs):
                local = profiler.local
                previous = getattr(local, "base", ())
                local.base = base
                try:
                    return fn(*inner_args, **inner_kwargs)
                finally:
                    local.base = previous

            return original_submit(executor, run, *args, **kwargs)

        ThreadPoolExecutor.submit = submit

    def _patch_tracker(self):
        from util.performance import tracker

        original_start, original_stop = tracker.start, tracker.stop

        def start(name):
            self.open_span(f"phase:{name}", name)
            return original_start(name)

        def stop(name):
            result = original_stop(name)
            self.close_span(name)
            return result

        tracker.start, tracker.stop = start, stop

    # -- measurement ------------------------------------------------------

    def current_path(self):
        stack = getattr(self.local, "stack", None)
        if stack:
            return stack[-1].path
        return getattr(self.local, "base", ())

    def _memory_event(self, frame, opening):
        """Re-arm the tracemalloc peak and push peaks into every open frame."""
        with self.lock:
            if self.trace_memory:
                current, peak = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
            else:
                current = peak = 0
            rss = self.rss.read()
            for other in self.active.values():
                if peak > other.pypeak:
                    other.pypeak = peak
                if rss > other.rsspeak:
                    other.rsspeak = rss
            if peak > self.peak_py:
                self.peak_py = peak
            if rss > self.peak_rss:
                self.peak_rss = rss
            if opening:
                frame.py0 = frame.pypeak = current
                frame.rss0 = frame.rsspeak = rss
                self.active[id(frame)] = frame
            else:
                self.active.pop(id(frame), None)
                frame.py1, frame.rss1 = current, rss
                frame.pypeak = max(frame.pypeak, peak)
                frame.rsspeak = max(frame.rsspeak, rss)

    def enter(self, key):
        local = self.local
        stack = getattr(local, "stack", None)
        if stack is None:
            stack = local.stack = []
            local.depth = {}
        parent_path = stack[-1].path if stack else getattr(local, "base", ())
        frame = _Frame(key, parent_path if key in parent_path else parent_path + (key,))
        depth = local.depth
        count = depth.get(key, 0)
        frame.outer = count == 0
        depth[key] = count + 1
        self._memory_event(frame, True)
        stack.append(frame)
        frame.c0 = time.thread_time()
        frame.t0 = time.perf_counter()
        return frame

    def leave(self, frame):
        wall = time.perf_counter() - frame.t0
        cpu = time.thread_time() - frame.c0
        local = self.local
        stack = local.stack
        if stack and stack[-1] is frame:
            stack.pop()
        elif frame in stack:
            stack.remove(frame)
        local.depth[frame.key] -= 1
        self._memory_event(frame, False)
        if stack:
            parent = stack[-1]
            parent.child_wall += wall
            parent.child_cpu += cpu
            parent.children += 1
            parent.descendants += frame.descendants + 1
        with self.lock:
            stat = self.functions.get(frame.key)
            if stat is None:
                stat = self.functions[frame.key] = _Stat()
            stat.add(frame, wall, cpu)
            stat = self.paths.get(frame.path)
            if stat is None:
                stat = self.paths[frame.path] = _Stat()
            stat.add(frame, wall, cpu)

    def open_span(self, key, name=None):
        frame = _Frame(key, ())
        self._memory_event(frame, True)
        frame.p0 = _process_cpu()
        frame.c0 = time.thread_time()
        frame.t0 = time.perf_counter()
        self.open_spans.setdefault(name or key, []).append(frame)
        return frame

    def close_span(self, name):
        opened = self.open_spans.get(name)
        if not opened:
            return
        frame = opened.pop()
        wall = time.perf_counter() - frame.t0
        thread_cpu = time.thread_time() - frame.c0
        process_cpu = _process_cpu() - frame.p0
        self._memory_event(frame, False)
        with self.lock:
            span = self.spans.setdefault(frame.key, _Span())
            span.count += 1
            span.wall += wall
            span.thread_cpu += thread_cpu
            span.process_cpu += process_cpu
            span.py_peak = max(span.py_peak, frame.pypeak - frame.py0)
            span.py_net += frame.py1 - frame.py0
            span.rss_peak = max(span.rss_peak, frame.rsspeak - frame.rss0)
            span.rss_peak_abs = max(span.rss_peak_abs, frame.rsspeak)
            span.rss_net += frame.rss1 - frame.rss0

    def _sample(self):
        while not self._stop.wait(self.sample_seconds):
            rss = self.rss.read()
            threads = threading.active_count()
            with self.lock:
                for frame in self.active.values():
                    if rss > frame.rsspeak:
                        frame.rsspeak = rss
                if rss > self.peak_rss:
                    self.peak_rss = rss
                if threads > self.max_threads:
                    self.max_threads = threads

    def _gc_callback(self, phase, info):
        if phase == "start":
            self._gc_started = time.perf_counter()
            return
        self.gc_pause += time.perf_counter() - self._gc_started
        generation = info.get("generation", 0)
        if 0 <= generation < 3:
            self.gc_runs[generation] += 1
        self.gc_collected += info.get("collected", 0)

    def calibrate(self, iterations=20000, nesting=8):
        """Measure the extra cost of one wrapped call at realistic depth."""
        def noop():
            return None

        wrapped = self._make_wrapper(noop, "__bench_calibration__")
        frames = [self.enter(f"__bench_nest_{level}__") for level in range(nesting)]
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            for _ in range(iterations):
                noop()
            raw = time.perf_counter() - started
            started = time.perf_counter()
            for _ in range(iterations):
                wrapped()
            best = min(best, (time.perf_counter() - started - raw) / iterations)
        for frame in reversed(frames):
            self.leave(frame)
        with self.lock:
            self.functions.clear()
            self.paths.clear()
        self.overhead = max(0.0, best)
        return self.overhead

    def total_calls(self):
        return sum(stat.calls for stat in self.functions.values())


class _InstrumentingLoader(importlib.abc.Loader):
    def __init__(self, loader, profiler):
        self._loader = loader
        self._profiler = profiler

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        profiler = self._profiler
        name = module.__name__
        if name in THIRD_PARTY and not profiler.matches(name):
            self._loader.exec_module(module)
            profiler._instrument_third_party(module)
            profiler._sweep_all()
            return
        frame = profiler.enter(f"import:{name}")
        try:
            self._loader.exec_module(module)
        finally:
            profiler.leave(frame)
        profiler.instrument_module(module)

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _InstrumentingFinder(importlib.abc.MetaPathFinder):
    """Instrument first-party modules that the cycle imports lazily."""

    def __init__(self, profiler):
        self._profiler = profiler

    def find_spec(self, name, path, target=None):
        profiler = self._profiler
        wanted = profiler.matches(name) or (profiler.third_party and name in THIRD_PARTY)
        if not wanted:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _InstrumentingLoader(spec.loader, profiler)
        return spec


# --------------------------------------------------------------------------
# Input selection and seeding (parent process)
# --------------------------------------------------------------------------

def _utc(value):
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cycle_for(analysis_time):
    """Even-minute cycle a scan belongs to (files are stamped up to 2 min later)."""
    floored = analysis_time.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=floored.minute % 2)


def _scan(directory, predicate=None):
    from common.ingest.manifest import parse_file_analysis_time

    found = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False) or entry.name.startswith("."):
                    continue
                if predicate is not None and not predicate(entry.name):
                    continue
                analysis_time = parse_file_analysis_time(entry.name)
                if analysis_time is not None:
                    found.append((analysis_time, Path(entry.path)))
    except FileNotFoundError:
        pass
    return sorted(found)


def _aligned(cycle_time, candidates, make_record):
    from common.ingest.manifest import CycleInputManifest

    best = None
    for analysis_time, path in candidates:
        record = make_record(path, analysis_time)
        if CycleInputManifest(cycle_time=cycle_time, inputs=(record,)).validate_alignment():
            continue
        distance = abs((analysis_time - cycle_time).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, record)
    return best[1] if best else None


def _mrms_records(registry, cycle_time, listings):
    from common.ingest.manifest import staged_input_from_path

    records = {}
    for spec in registry.products:
        record = _aligned(
            cycle_time, listings[spec.product_id],
            lambda path, at, spec=spec: staged_input_from_path(
                spec.product_id, path, source="local-benchmark", family="mrms",
                analysis_time=at),
        )
        if record is not None:
            records[spec.product_id] = record
    return records


def _select_cycle(registry, requested):
    """Pick the scan to replay and its aligned MRMS records."""
    listings = {spec.product_id: _scan(spec.directory) for spec in registry.products}
    protected = [spec.product_id for spec in registry.products if spec.protected]
    if requested is not None:
        cycle_time = _cycle_for(_utc(requested))
        records = _mrms_records(registry, cycle_time, listings)
        missing = [product for product in protected if product not in records]
        if missing:
            raise SystemExit(f"Cycle {cycle_time.isoformat()} lacks protected inputs: {missing}")
        return cycle_time, records

    settled = time.time() - WRITE_SETTLE_SECONDS
    composite = [(at, path) for at, path in listings.get(DETECTION_PRODUCT, ())
                 if path.stat().st_mtime < settled]
    candidates = sorted({_cycle_for(at) for at, _ in composite}, reverse=True)
    best = None
    for cycle_time in candidates[:SELECTION_CANDIDATES]:
        records = _mrms_records(registry, cycle_time, listings)
        if any(product not in records for product in protected):
            continue
        if best is None or len(records) > len(best[1]):
            best = (cycle_time, records)
    if best is None:
        raise SystemExit("No recent scan has every protected MRMS input; pass --cycle")
    return best


def _build_manifests(registry, cycle_time, mrms, args):
    """Detection, integration and final snapshots exactly as ingest stages them."""
    import util.file as fs
    from common.ingest.manifest import CycleInputManifest, StagedInput, staged_input_from_path
    from common.ingest.mrms.config import get_goes_modifiers
    from common.ingest.synoptic.main import parse_rap_analysis_time
    from common.pipeline.coordinator import _previous_detection_records

    protected = tuple(mrms[spec.product_id] for spec in registry.products if spec.protected)
    detection = CycleInputManifest(cycle_time=cycle_time,
                                   inputs=(*protected, *_previous_detection_records(protected)))

    extra = []
    if not args.mrms_core_only:
        rap_files = []
        for path in sorted(Path(fs.RAP_DIR).glob("*.grib2")):
            analysis_time = parse_rap_analysis_time(path)
            if analysis_time is not None and analysis_time <= cycle_time:
                rap_files.append((analysis_time, path))
        rap = _aligned(cycle_time, rap_files[-3:], lambda path, at: staged_input_from_path(
            "RAP", path, source="synoptic", family="rap", analysis_time=at))
        if rap is not None:
            extra.append(rap)
    if not args.disable_glm and not args.mrms_core_only:
        spec = next((item for item in get_goes_modifiers() if item.is_glm), None)
        if spec is not None:
            day = cycle_time.strftime("%Y%m%d")
            files = _scan(spec.outdir, lambda name: name.startswith("OR_" + spec.product)
                          and name.endswith(".nc") and day in name)
            glm = _aligned(cycle_time, files, lambda path, at: staged_input_from_path(
                spec.label, path, source="s3_sync", family="goes", analysis_time=at))
            if glm is not None:
                extra.append(glm)
    integration = detection.with_inputs(extra)

    optional = tuple(record for product, record in mrms.items()
                     if not registry.require(product).protected)
    present = {record.product for record in optional}
    anchors = list(optional)
    for spec in registry.products:
        if not spec.protected and spec.product_id not in present:
            anchors.append(StagedInput(
                product=spec.product_id, path=str(spec.directory / "history-anchor.grib2"),
                analysis_time=cycle_time, source="selection-anchor", family="mrms",
                validated=False))
    final = integration.with_inputs((*optional, *_previous_detection_records(tuple(anchors))))
    for name, manifest in (("detection", detection), ("integration", integration),
                           ("final", final)):
        problems = manifest.validate_alignment()
        if problems:
            raise SystemExit(f"{name} manifest failed alignment: {'; '.join(problems)}")
    return detection, integration, final


def _rebase(manifest, old_root, new_root):
    from common.ingest.manifest import CycleInputManifest

    def moved(record):
        # Strict: a trial must never read an input from the source tree.
        path = Path(new_root) / Path(record.path).relative_to(old_root)
        return dataclasses.replace(record, path=str(path))

    return CycleInputManifest(
        cycle_time=manifest.cycle_time,
        inputs=tuple(moved(record) for record in manifest.inputs),
        mrms_tolerance_seconds=manifest.mrms_tolerance_seconds,
        goes_tolerance_seconds=manifest.goes_tolerance_seconds,
        rap_max_age_seconds=manifest.rap_max_age_seconds,
    )


def _copy_into(source, source_root, seed_root):
    target = Path(seed_root) / Path(source).relative_to(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _snapshot_database(source, destination, radar_time):
    """Consistent copy of the StormProb DB rewound to before the replayed scan."""
    from EdgeWARN.stormprob.database import _time

    destination.parent.mkdir(parents=True, exist_ok=True)

    def backup(uri):
        destination.unlink(missing_ok=True)
        reader = sqlite3.connect(uri, uri=True)
        writer = sqlite3.connect(destination)
        try:
            reader.backup(writer)
            if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("snapshot failed integrity_check")
        finally:
            reader.close()
            writer.close()

    # ``mode=ro`` honours the live writer's locks but needs a writable
    # directory for journal files. ``immutable=1`` works on a read-only mount
    # but ignores locks, so its copy is integrity-checked and retried.
    attempts = [f"file:{source}?mode=ro"] + [f"file:{source}?mode=ro&immutable=1"] * 3
    for attempt, uri in enumerate(attempts):
        try:
            backup(uri)
            break
        except sqlite3.DatabaseError as exc:
            error = exc
            time.sleep(0.5 if attempt else 0.0)
    else:
        destination.unlink(missing_ok=True)
        print(f"WARN: StormProb database not seeded ({error}); trials start without "
              "StormProb history")
        return

    writer = sqlite3.connect(destination)
    try:
        cutoff = _time(radar_time)
        writer.execute("PRAGMA foreign_keys=ON")
        with writer:
            writer.execute("DELETE FROM forecasts WHERE analysis_time >= ?", (cutoff,))
            writer.execute("DELETE FROM cell_observations WHERE analysis_time >= ?", (cutoff,))
            writer.execute("""DELETE FROM cycles WHERE analysis_time >= ?
                AND cycle_id NOT IN (SELECT cycle_id FROM cell_observations)
                AND cycle_id NOT IN (SELECT cycle_id FROM forecasts)""", (cutoff,))
    except sqlite3.DatabaseError as exc:
        print(f"WARN: StormProb database rewind skipped ({exc}); the copy may already "
              "contain the replayed scan")
    finally:
        writer.close()


def _seed(source_root, seed_root, manifests, radar_time):
    """Copy the replayed scan's inputs and the Core state before it."""
    import util.file as fs

    copied = set()
    for manifest in manifests:
        for record in manifest.inputs:
            path = Path(record.path)
            if record.validated and path.is_file() and path not in copied:
                _copy_into(path, source_root, seed_root)
                copied.add(path)

    # Prior state only shapes tracking, history and alert matching, so a file
    # the live writer has not finished (or that is unreadable) is skipped.
    skipped = []

    def copy_state(path):
        try:
            _copy_into(path, source_root, seed_root)
            return True
        except OSError:
            skipped.append(path)
            return False

    def files(directory):
        directory = Path(directory)
        return sorted(path for path in directory.rglob("*") if path.is_file()) \
            if directory.is_dir() else []

    stamp = radar_time.strftime("%Y%m%d-%H%M%S")
    for path in files(fs.STORMCELL_DIR):
        if not (path.name.startswith("stormcells_") and path.stem >= f"stormcells_{stamp}"):
            copy_state(path)
    for path in files(fs.CELL_DIR):
        copy_state(path)

    official = Path(fs.MRMS_NWS_DIR)
    snapshots = [path for path in files(official / "timestamps")
                 if path.suffix == ".json" and path.stem <= stamp]
    for path in reversed(snapshots):
        if copy_state(path):
            break
    published = Path(fs.EDGEWARN_ALERTS_DIR)
    for path in (*files(official / "ids"), *files(published / "ids")):
        copy_state(path)
    for path in files(published / "timestamps"):
        if path.suffix == ".json" and path.stem < stamp:
            copy_state(path)

    from EdgeWARN.stormprob.database import database_path
    database = database_path(source_root)
    if database.is_file():
        _snapshot_database(database, database_path(seed_root), radar_time)
    return len(copied), skipped


# --------------------------------------------------------------------------
# Trial process
# --------------------------------------------------------------------------

def _rusage():
    try:
        import resource
    except ImportError:
        return {}
    usage = resource.getrusage(resource.RUSAGE_SELF)
    scale = 1.0 if sys.platform == "darwin" else 1024.0
    return {
        "user_cpu_s": usage.ru_utime, "sys_cpu_s": usage.ru_stime,
        "max_rss_mib": usage.ru_maxrss * scale / MIB,
        "minor_faults": usage.ru_minflt, "major_faults": usage.ru_majflt,
        "block_in": usage.ru_inblock, "block_out": usage.ru_oublock,
        "voluntary_ctx": usage.ru_nvcsw, "involuntary_ctx": usage.ru_nivcsw,
    }


def _drain(log_queue, stop):
    while not (stop.is_set() and log_queue.empty()):
        try:
            message = log_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        text = str(message)
        os.write(1, (text if text.endswith("\n") else text + "\n").encode("utf-8", "replace"))


def _warm_rap_cache(manifest_payload):
    """Decode the pinned RAP file into the trial's field cache, untimed.

    The cache key includes the RAP file's real path, so a cache copied from
    the source tree never hits in a trial root. In production the hourly
    file is decoded once and then served from the cache by later cycles.
    """
    import util.file as fs
    from common.ingest.manifest import CycleInputManifest
    from EdgeWARN.process.integrate.config import get_rap_products
    from EdgeWARN.process.integrate.integrate_rap import _rap_field_cache_dir
    from util.grib_loader import RAPPointExtractor

    manifest = CycleInputManifest.from_dict(manifest_payload)
    record = manifest.latest_for_directory(fs.RAP_DIR) if manifest is not None else None
    if record is None:
        print("WARN: --warm-rap found no pinned RAP input")
        return
    extractor = RAPPointExtractor(record.local_path, cache_dir=_rap_field_cache_dir())
    extractor.extract_batch(get_rap_products().get("products", []), {0: (35.0, 263.0)})


def _trial(context):
    """Body of one forked trial process; writes its measurements as JSON."""
    log = os.open(context["log_path"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(log, 1)
    os.dup2(log, 2)
    result = {"status": "failed", "errors": [], "cells": 0}
    trial_dir = context["trial_dir"]
    try:
        import util.file as fs
        fs.initialize_filesystem(trial_dir, config_dir=context["config_dir"])
        if context["warm_rap"]:
            _warm_rap_cache(context["manifests"]["integration"])

        profiler = Profiler(context["prefixes"], trace_memory=context["trace_memory"],
                            sample_seconds=context["sample_seconds"],
                            third_party=context["third_party"])
        profiler.install()
        overhead = profiler.calibrate()
        usage_before = _rusage()
        started = time.perf_counter()

        startup = profiler.open_span("stage:startup")
        from EdgeWARN import initialize_runtime
        from common.ingest.mrms.config import get_registry
        from util.io import IOManager
        initialize_runtime(base_dir=trial_dir, io_manager=IOManager("[EdgeWARN]"))
        registry = get_registry()
        if registry is not None:
            fs.ensure_mrms_directories(registry)
        profiler.close_span(startup.key)

        import EdgeWARN.pipeline as pipeline
        manifests = context["manifests"]
        shared_state = {
            "detection_inputs_ready": True,
            "detection_manifest": manifests["detection"],
            "input_manifest": manifests["detection"],
            "edgewarn_integration_inputs_ready": True,
            "integration_manifest": manifests["integration"],
            "optional_inputs_complete": True,
            "ctam_manifest": manifests["final"],
            "edgewarn_generated_file": "",
            "errors": {},
        }
        if registry is not None:
            shared_state["mrms_registry"] = {
                "base_dir": str(registry.base_dir), "config_dir": context["config_dir"],
                "fingerprint": registry.fingerprint,
            }
        ready = threading.Event()
        ready.set()
        log_queue = queue.Queue()
        drain_stop = threading.Event()
        drainer = threading.Thread(target=_drain, args=(log_queue, drain_stop),
                                   name="bench-log-drain", daemon=True)
        drainer.start()
        stdout, stderr = sys.stdout, sys.stderr
        cycle = profiler.open_span("stage:cycle")
        options = context["options"]
        try:
            pipeline.edgewarn_cycle_worker(
                log_queue, shared_state, ready, ready,
                datetime.fromisoformat(context["cycle_time"]),
                options["lat_limits"], options["lon_limits"], context["detection_config"],
                profile=True,
                disable_ctam=options["disable_ctam"],
                disable_ctam_modules=options["disable_ctam_modules"],
                disable_tracking=options["disable_tracking"],
                disable_polygon_expansion=options["disable_polygon_expansion"],
                mrms_core_only=options["mrms_core_only"],
                optional_complete_event=ready,
                disable_stormprob=options["disable_stormprob"],
                frozen_discovery=context["discovery"],
            )
        except Exception as exc:
            result["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            profiler.close_span(cycle.key)
            sys.stdout, sys.stderr = stdout, stderr
            drain_stop.set()
            drainer.join(timeout=5.0)
        wall = time.perf_counter() - started
        usage_after = _rusage()

        stage = shared_state.get("edgewarn_stage") or {}
        result["status"] = stage.get("status", "failed")
        result["errors"].extend(stage.get("errors", []))
        generated = shared_state.get("edgewarn_generated_file")
        if generated and Path(generated).is_file():
            try:
                result["cells"] = len(json.loads(Path(generated).read_text()).get("features", []))
            except (OSError, ValueError, AttributeError):
                pass

        calls = profiler.total_calls()
        process = {
            "wall_s": wall,
            "peak_rss_sampled_mib": profiler.peak_rss / MIB,
            "py_peak_abs_mib": profiler.peak_py / MIB,
            "threads_max": profiler.max_threads,
            "gc_pause_s": profiler.gc_pause,
            "gc_gen0_runs": profiler.gc_runs[0],
            "gc_gen1_runs": profiler.gc_runs[1],
            "gc_gen2_runs": profiler.gc_runs[2],
            "gc_collected": profiler.gc_collected,
            "instrumented_modules": len(profiler.instrumented),
            "instrumented_functions": len(profiler.originals),
            "unwrapped_generators": profiler.skipped,
            "instrumented_calls": calls,
            "overhead_per_call_us": overhead * 1e6,
            "overhead_total_est_s": calls * overhead,
            "cells_detected": result["cells"],
        }
        for name, value in usage_after.items():
            if name == "max_rss_mib":
                process[name] = value
            else:
                process[name] = value - usage_before.get(name, 0)
        process["cpu_util"] = ((process.get("user_cpu_s", 0.0) + process.get("sys_cpu_s", 0.0))
                               / wall if wall > 0 else 0.0)
        profiler.uninstall()

        result["process"] = process
        result["spans"] = {key: span.metrics() for key, span in profiler.spans.items()}
        result["functions"] = {key: stat.metrics(overhead)
                               for key, stat in profiler.functions.items()}
        result["callpaths"] = {" > ".join(path): stat.metrics(overhead)
                               for path, stat in profiler.paths.items()}
    except BaseException as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    Path(context["result_path"]).write_text(json.dumps(result))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _flatten(index, result):
    rows = []
    for metric, value in result.get("process", {}).items():
        rows.append((index, "process", "trial", metric, value))
    for key, metrics in result.get("spans", {}).items():
        kind, _, name = key.partition(":")
        for metric, value in metrics.items():
            rows.append((index, kind, name, metric, value))
    for key, metrics in result.get("functions", {}).items():
        kind, name = ("import", key[len("import:"):]) if key.startswith("import:") else ("function", key)
        for metric, value in metrics.items():
            rows.append((index, kind, name, metric, value))
    for key, metrics in result.get("callpaths", {}).items():
        for metric, value in metrics.items():
            rows.append((index, "callpath", key, metric, value))
    return rows


def _summarize(rows):
    import numpy as np

    grouped = {}
    for _trial_index, kind, component, metric, value in rows:
        grouped.setdefault((kind, component, metric), []).append(float(value))
    summary = {}
    for key, values in grouped.items():
        array = np.asarray(values, dtype=float)
        summary[key] = {
            "trials": array.size,
            "mean": float(array.mean()),
            "median": float(np.median(array)),
            "p95": float(np.percentile(array, 95)),
            "p99": float(np.percentile(array, 99)),
            "min": float(array.min()),
            "max": float(array.max()),
            "std": float(array.std(ddof=1)) if array.size > 1 else 0.0,
        }
    return summary


def _unit(metric):
    for suffix, unit in (("_s", "s"), ("_mib", "MiB"), ("_us", "us")):
        if metric.endswith(suffix):
            return unit
    return "ratio" if metric.endswith("_util") else "count"


KIND_ORDER = {"process": 0, "stage": 1, "phase": 2, "import": 3, "function": 4, "callpath": 5}


def _write_csv(path, summary, raw_rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = ("trials", "mean", "median", "p95", "p99", "min", "max", "std")
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("kind", "component", "metric", "unit", *stats))
        for (kind, component, metric) in sorted(
                summary, key=lambda key: (KIND_ORDER.get(key[0], 9), key[1], key[2])):
            values = summary[(kind, component, metric)]
            writer.writerow((kind, component, metric, _unit(metric),
                             *(values[name] if name == "trials" else f"{values[name]:.9g}"
                               for name in stats)))
    trials_path = path.with_name(path.stem + "_trials.csv")
    with trials_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("trial", "kind", "component", "metric", "unit", "value"))
        for trial, kind, component, metric, value in raw_rows:
            writer.writerow((trial, kind, component, metric, _unit(metric), f"{float(value):.9g}"))
    return trials_path


def _short(name, width):
    return name if len(name) <= width else "…" + name[-(width - 1):]


def _table(title, summary, kind, metric, extras, top, width=58):
    names = [component for (k, component, m) in summary if k == kind and m == metric]
    if not names:
        return
    names.sort(key=lambda component: summary[(kind, component, metric)]["mean"], reverse=True)
    unit = _unit(metric)
    print(f"\n{title}  [{metric}, {unit}; extras are medians]")
    header = f"{'component':<{width}} {'mean':>10} {'median':>10} {'p95':>10} {'p99':>10}"
    header += "".join(f" {label:>11}" for label, _ in extras)
    print(header)
    print("-" * len(header))
    for component in names[:top]:
        values = summary[(kind, component, metric)]
        line = (f"{_short(component, width):<{width}} {values['mean']:>10.4f} "
                f"{values['median']:>10.4f} {values['p95']:>10.4f} {values['p99']:>10.4f}")
        for _label, extra in extras:
            stat = summary.get((kind, component, extra))
            line += f" {stat['median']:>11.4g}" if stat else f" {'':>11}"
        print(line)
    if len(names) > top:
        print(f"... {len(names) - top} more in the CSV")


def _report(summary, results, cycle_time, args):
    print(f"\nRealtime Core benchmark: cycle {cycle_time.isoformat()}, "
          f"{len(results)} trial(s), tracemalloc={'on' if not args.no_tracemalloc else 'off'}")
    for index, result in results:
        errors = "; ".join(result.get("errors", [])[:2])
        print(f"  trial {index}: status={result.get('status')} cells={result.get('cells')}"
              + (f" errors={errors}" if errors else ""))

    process = [(metric, values) for (kind, _c, metric), values in summary.items()
               if kind == "process"]
    if process:
        print(f"\n{'process metric':<28} {'mean':>12} {'median':>12} {'p95':>12} {'p99':>12}")
        for metric, values in sorted(process):
            print(f"{metric:<28} {values['mean']:>12.4f} {values['median']:>12.4f} "
                  f"{values['p95']:>12.4f} {values['p99']:>12.4f}")

    memory = (("cpu s", "process_cpu_s"), ("py pk MiB", "py_peak_mib"),
              ("rss pk MiB", "rss_peak_mib"))
    _table("Stages", summary, "stage", "wall_s", memory, args.top)
    _table("Pipeline phases", summary, "phase", "wall_s", memory, args.top)
    function_extras = (("calls", "calls"), ("self s", "wall_self_corr_s"),
                       ("cpu s", "cpu_incl_s"), ("py pk MiB", "py_peak_mib"),
                       ("rss pk MiB", "rss_peak_mib"))
    _table("Functions by inclusive wall", summary, "function", "wall_incl_corr_s",
           function_extras, args.top)
    _table("Functions by self wall", summary, "function", "wall_self_corr_s",
           (("calls", "calls"), ("call p99 s", "call_p99_s"), ("cpu self s", "cpu_self_s")),
           args.top)
    _table("Functions by Python heap peak", summary, "function", "py_peak_mib",
           (("calls", "calls"), ("net MiB", "py_net_mib"), ("rss pk MiB", "rss_peak_mib")),
           args.top)
    _table("Lazy imports during the cycle", summary, "import", "wall_incl_corr_s",
           (("py pk MiB", "py_peak_mib"),), args.top)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=2,
                        help="number of measured trials, each in a fresh process (default 2)")
    parser.add_argument("--base-dir", "--base_dir", dest="base_dir",
                        help="runtime tree to read inputs and prior state from (read only; "
                             "default: EDGEWARN_BASE_DIR / filesystem.yaml resolution)")
    parser.add_argument("--config-dir", help="configuration directory (default: repo config/)")
    parser.add_argument("--cycle", type=datetime.fromisoformat,
                        help="ISO time of the scan to replay (default: newest settled scan "
                             "with the most aligned inputs)")
    parser.add_argument("--output", type=Path,
                        help="summary CSV path (default: workspaces/benchmarks/"
                             "bench_realtime_pipeline_<UTC>.csv)")
    parser.add_argument("--work-dir", type=Path,
                        help="scratch parent for the seed and trial runtime roots "
                             "(default: system temporary directory)")
    parser.add_argument("--keep-work-dir", action="store_true",
                        help="keep the seed and trial runtime roots for inspection")
    parser.add_argument("--no-tracemalloc", action="store_true",
                        help="skip Python heap tracing for lower timing overhead")
    parser.add_argument("--sample-ms", type=float, default=2.0,
                        help="RSS/thread sampler interval in milliseconds (default 2)")
    parser.add_argument("--packages", nargs="+", default=list(DEFAULT_PREFIXES),
                        help="first-party package prefixes to instrument (default: %(default)s)")
    parser.add_argument("--no-third-party", action="store_true",
                        help="do not wrap the curated xarray/eccodes/shapely/ONNX/json entry points")
    parser.add_argument("--warm-rap", action="store_true",
                        help="decode the RAP field cache untimed before each trial, as "
                             "production cycles after the first of each hour find it")
    parser.add_argument("--top", type=int, default=25,
                        help="rows per console table (default 25; the CSV holds everything)")
    if "--" in argv:
        split = argv.index("--")
        own, core = argv[:split], argv[split + 1:]
    else:
        own, core = argv, []
    args = parser.parse_args(own)
    if args.trials < 1:
        parser.error("--trials must be at least 1")
    if args.sample_ms <= 0 or args.top < 1:
        parser.error("--sample-ms and --top must be positive")
    return args, core


def _core_options(source_root, config_dir, core_argv):
    """Resolve Core flags exactly as run_edgewarn.py does."""
    from util.io import IOManager

    argv = ["run_edgewarn.py", "--base_dir", str(source_root), *core_argv]
    if config_dir:
        argv += ["--config-dir", str(config_dir)]
    saved = sys.argv
    sys.argv = argv
    try:
        return IOManager("[Benchmark]").get_args()
    finally:
        sys.argv = saved


def _contains(parent, child):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def main(argv=None):
    args, core_argv = _parse_args(sys.argv[1:] if argv is None else argv)
    import util.file as fs

    source_root = Path(args.base_dir or fs.BASE_DIR).expanduser().resolve()
    if not (source_root / "data").is_dir():
        raise SystemExit(f"{source_root} is not a runtime tree (no data/ directory)")
    core = _core_options(source_root, args.config_dir, core_argv)
    config_dir = core.config_dir

    from EdgeWARN.ctam.preflight import (PreflightError, StormProbDependencyError,
                                         check_core_startup)
    try:
        discovery = check_core_startup(
            config_dir=config_dir, base_dir=source_root, module_root=core.ctam_module_dir,
            disable_ctam=core.disable_ctam, disable_ctam_modules=core.disable_ctam_modules,
            disable_stormprob=core.disable_stormprob, mrms_core_only=core.mrms_core_only)
    except (PreflightError, StormProbDependencyError, RuntimeError) as exc:
        raise SystemExit(f"Core startup preflight failed: {exc}")

    fs.initialize_filesystem(source_root, config_dir=config_dir)
    from common.ingest.mrms.config import get_registry
    registry = get_registry()
    if registry is None:
        raise SystemExit("The benchmark requires the version 2 MRMS registry (ingest.yaml)")
    cycle_time, mrms = _select_cycle(registry, args.cycle)
    manifests = _build_manifests(registry, cycle_time, mrms, core)
    radar_time = mrms[DETECTION_PRODUCT].analysis_time

    work_parent = Path(args.work_dir or tempfile.gettempdir()).expanduser().resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="edgewarn-bench-", dir=work_parent))
    if _contains(source_root, work) or _contains(work, source_root):
        shutil.rmtree(work, ignore_errors=True)
        raise SystemExit("--work-dir must not overlap the source runtime tree")
    seed = work / "seed"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or REPO_ROOT / "workspaces" / "benchmarks"
              / f"bench_realtime_pipeline_{stamp}.csv").expanduser().resolve()
    logs = output.with_name(output.stem + "_logs")
    logs.mkdir(parents=True, exist_ok=True)

    from EdgeWARN.process.detect.config import DetectionConfig
    detection_config = DetectionConfig.from_yaml(
        config_dir=config_dir, refl_threshold=core.refl_threshold,
        min_seed_percentage=core.min_seed_percentage, drop_offset=core.drop_offset)
    options = {name: getattr(core, name) for name in (
        "lat_limits", "lon_limits", "disable_ctam", "disable_ctam_modules", "disable_tracking",
        "disable_polygon_expansion", "mrms_core_only", "disable_stormprob")}
    options["lat_limits"] = tuple(options["lat_limits"])
    options["lon_limits"] = tuple(options["lon_limits"])

    print(f"Source runtime tree: {source_root} (read only)")
    print(f"Replaying cycle {cycle_time.isoformat()} (radar {radar_time.isoformat()}); "
          f"{len(mrms)} MRMS products aligned, "
          f"{len(manifests[2].inputs)} inputs in the final snapshot")
    try:
        copied, skipped = _seed(source_root, seed, manifests, radar_time)
        # Nothing below may touch the source tree again.
        fs.initialize_filesystem(seed, config_dir=config_dir)
        print(f"Seeded {copied} input files and prior Core state into {seed}")
        if skipped:
            print(f"WARN: skipped {len(skipped)} unreadable prior-state file(s), "
                  f"e.g. {skipped[0]}")

        # Imports the realtime parent already holds, so a forked trial starts
        # as warm as the worker run_edgewarn.py spawns per cycle.
        import EdgeWARN.pipeline  # noqa: F401
        method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
        mp = multiprocessing.get_context(method)

        results, raw_rows = [], []
        for index in range(1, args.trials + 1):
            trial_dir = work / f"trial_{index}"
            if trial_dir.exists():
                shutil.rmtree(trial_dir)
            shutil.copytree(seed, trial_dir, symlinks=False)
            context = {
                "trial_dir": str(trial_dir),
                "config_dir": str(config_dir) if config_dir else None,
                "cycle_time": cycle_time.isoformat(),
                "manifests": {name: _rebase(manifest, source_root, trial_dir).as_dict()
                              for name, manifest in zip(("detection", "integration", "final"),
                                                        manifests)},
                "options": options,
                "detection_config": detection_config,
                "discovery": discovery,
                "prefixes": tuple(args.packages),
                "trace_memory": not args.no_tracemalloc,
                "sample_seconds": args.sample_ms / 1000.0,
                "third_party": not args.no_third_party,
                "warm_rap": args.warm_rap,
                "log_path": str(logs / f"trial_{index}.log"),
                "result_path": str(work / f"trial_{index}.json"),
            }
            print(f"Trial {index}/{args.trials} ...", end="", flush=True)
            started = time.perf_counter()
            process = mp.Process(target=_trial, args=(context,), name=f"bench-trial-{index}")
            process.start()
            process.join()
            elapsed = time.perf_counter() - started
            result_path = Path(context["result_path"])
            if not result_path.is_file():
                print(f" process exited {process.exitcode} without results; see {context['log_path']}")
                continue
            result = json.loads(result_path.read_text())
            print(f" {result.get('status')} in {elapsed:.1f}s, {result.get('cells')} cells, "
                  f"{result.get('process', {}).get('instrumented_calls', 0):,} calls")
            results.append((index, result))
            raw_rows.extend(_flatten(index, result))
            if not args.keep_work_dir:
                shutil.rmtree(trial_dir, ignore_errors=True)
    finally:
        if not args.keep_work_dir:
            shutil.rmtree(work, ignore_errors=True)

    if not raw_rows:
        print("No trial produced measurements", file=sys.stderr)
        return 1
    summary = _summarize(raw_rows)
    trials_path = _write_csv(output, summary, raw_rows)
    _report(summary, results, cycle_time, args)
    print(f"\nSummary CSV: {output}\nPer-trial CSV: {trials_path}\nTrial logs: {logs}")
    if args.keep_work_dir:
        print(f"Work directory kept: {work}")
    failed = [index for index, result in results if result.get("status") != "completed"]
    if failed:
        print(f"Trials not completed: {failed} (see their logs)", file=sys.stderr)
    return 0 if len(results) == args.trials and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
