# Realtime Core execution-time profiler

`scripts/bench_realtime_pipeline.py` is an execution-time profiler and
benchmark for one realtime Core cycle. It shows where a cycle spends wall
time, CPU and memory, from the whole process down to single functions and
call paths. It repeats the cycle over several trials and reports the mean,
median, p95 and p99 of each measurement.

The profiler replays a real scan through
`EdgeWARN.pipeline.edgewarn_cycle_worker`, the worker that `run_edgewarn.py`
starts for each cycle. It pins the detection, integration and final
optional-input manifests the way the ingest service publishes them. Detection,
tracking and lineage, enrichment, CTAM, StormProb, alerts and publication run
unchanged.

```bash
conda activate EdgeWARN
python scripts/bench_realtime_pipeline.py --base-dir /path/to/runtime
python scripts/bench_realtime_pipeline.py --base-dir /path/to/runtime --trials 5 \
    --cycle 2026-11-24T01:48:00Z --output bench.csv
python scripts/bench_realtime_pipeline.py --base-dir /path/to/runtime -- --disable-tracking
```

Core flags after `--` are resolved the same way `run_edgewarn.py` resolves
them, and so is the CTAM and StormProb startup preflight.

## Isolation

`--base-dir` is read only. The profiler selects one scan: the scan you pass
with `--cycle`, or else the newest settled scan with the most aligned inputs.
It copies that scan's inputs and the Core state before it into a temporary
seed:

- earlier stormcell files and the lineage buffer
- cell histories
- the official alert snapshot for the scan, and the published alerts
- a StormProb database copy rewound to before the scan

Each trial runs in a new forked process, on a new copy of the seed, so every
trial starts from the same state. A prior-state file that cannot be read is
skipped and counted in a warning. The work directory (`--work-dir`, or the
system temporary directory if not given) must not overlap the source tree. The
profiler deletes the work directory at the end unless you pass
`--keep-work-dir`.

## What is measured

| Kind | Granularity |
| --- | --- |
| `process` | Trial wall time, user/system CPU, max and sampled RSS, Python heap peak, page faults, block I/O, context switches, GC pauses, thread count, instrumentation overhead, detected cells |
| `stage` | Process startup (`initialize_runtime`) and the complete cycle |
| `phase` | Every `util.performance` tracker span (`Detection - Expand Gates`, `Integration - RAP`, `Integration - CTAM`, ...) |
| `import` | Each first-party module imported lazily during the cycle |
| `function` | Every function and method in the instrumented packages (`EdgeWARN`, `common`, `util` by default), plus chosen xarray, eccodes, rasterio, shapely, ONNX Runtime and json entry points |
| `callpath` | The same functions split by call path, including work sent to thread pools, with recursion collapsed |

Function and call-path metrics:

- call count
- inclusive and self wall time, raw and with overhead removed (`*_corr_s`)
- inclusive and self thread CPU time
- latency of a single call: p50, p95, p99 and max
- Python heap peak above entry and net retained (`tracemalloc`)
- RSS peak above entry and net change

Stage and phase metrics add whole-process CPU and absolute peak RSS.

## Reading the numbers

- Inclusive times of work that runs at the same time in different threads
  overlap. Thread CPU counts only the calling thread.
- `tracemalloc` slows Python code that allocates a lot of memory. Use
  `--no-tracemalloc` when exact timings matter more than heap peaks. The
  `*_corr_s` columns remove the measured per-call wrapper overhead
  (`overhead_per_call_us`).
- Python heap peaks are exact. RSS peaks come from reads at call boundaries
  plus a background sampler (`--sample-ms`, default 2 ms). An RSS spike
  shorter than the sample interval inside one call can be missed.
- Inputs are copied for each trial, so they are already in the page cache.
- Generators, coroutines and C callables are not wrapped. Their cost is added
  to the self time of the closest wrapped caller.

## Output

- `--output` (default `workspaces/benchmarks/bench_realtime_pipeline_<UTC>.csv`)
  holds the summary in long format. Each row has `kind`, `component`,
  `metric`, `unit`, `trials`, `mean`, `median`, `p95`, `p99`, `min`, `max` and
  `std`.
- `<output>_trials.csv` holds the raw value of every metric for each trial.
- `<output>_logs/trial_<n>.log` holds the pipeline log of each trial.

The console shows the process, stage and phase tables, then the functions
ranked by inclusive wall time, by self wall time and by Python heap peak, and
the lazy imports. `--top` sets the number of rows in each table.
