# Ingestion Architecture

EdgeWARN-Core uses a filesystem-first ingest model. Remote products are
selected and staged under the configured runtime base directory, then
consumed by the service that owns the corresponding processing or rendering
work. Shared implementations live under `src/common/`; `src/EdgeWARN/ingest/`
is a compatibility re-export layer.

## Service ownership

```text
run_edgewarn.py
  MRMS detection + integration ──┐
  raw RAP ────────────────────────┼─> exact cycle manifest
  scan-time GLM (when enabled) ──┘       │
                                         ├─ mrms-ready ─> run_ewmrs.py
                                         └─ rap-ready  ─> run_ewmrs.py

run_ewmrs.py
  committed-record consumer, MRMS rendering, RAP Uint16 conversion
  GOES ABI ingest + rendering, METAR, NWS alerts, WPC surface analysis

run_nexrad.py
  NEXRAD Level-II discovery/download/parse/staging + polar rendering
```

The primary service does not import or start EWMRS, NEXRAD, METAR, NWS, WPC,
or the GOES ABI loops. EWMRS does not download MRMS or RAP and does not render
NEXRAD. NEXRAD is an independent service rather than an EWMRS child.

## Shared staged MRMS/RAP cycle

`src/common/pipeline/coordinator.py:run_staged_ingest_cycle` is used by the
realtime primary and historical EdgeWARN processing. It starts MRMS detection,
MRMS integration, and (when requested) RAP ingestion concurrently. Each
ingest path is async-first and has a synchronous fallback where supported.

The coordinator returns a `CycleState` containing an immutable
`CycleInputManifest`. A manifest records the requested UTC cycle, the exact
local path for every selected input, product/family/source identity, encoded
analysis time, validation status, and whether an input is current or previous
history. Alignment is checked from product timestamps; filesystem modification
time is not used as the observation timestamp.

Readiness transitions are emitted in dependency order:

1. Detection MRMS inputs: the configured detection subset is complete and
   timestamp-valid, so the EdgeWARN detection worker may run.
2. EWMRS MRMS cycle trigger: emitted every Core ingest cycle. EWMRS scans each
   configured MRMS source directory independently, reuses complete renders for
   unchanged source timestamps, and renders newly available layers. There is
   no aggregate all-products readiness gate.
3. Base EdgeWARN integration inputs: MRMS inputs plus a valid raw RAP input,
   unless RAP is disabled (for example, `mrms-core-only`).
4. EdgeWARN integration inputs: base inputs plus scan-time GLM when GOES/GLM is
   enabled. GOES ABI availability is not part of this primary barrier.

Production realtime mode calls the coordinator with `include_goes=False`.
Scan-time GLM is downloaded separately by the primary and added to the
integration manifest only after its own timestamp/alignment validation.
`include_goes` remains available to coordinator callers and tests, but it is
not the production ABI-render path.

The primary publishes successful phases through
`src/util/runtime/handoff.py`. Publication is atomic and idempotent:

```text
<BASE_DIR>/state/realtime/cycles/<cycle-id>/mrms-ready.json
<BASE_DIR>/state/realtime/cycles/<cycle-id>/rap-ready.json
```

The record contains the canonical UTC cycle ID, producer/run metadata, the
manifest's staged paths, tolerances, and warnings. `mrms-ready` is published as
an EWMRS cycle trigger even when one or more MRMS products are unavailable;
`rap-ready` remains gated on a valid exact RAP input. Publication failure is
logged as a handoff problem and does not rewrite an existing incompatible
record or make the primary cycle falsely successful.

`run_ewmrs.py` runs `util.runtime.ewmrs_consumer.EwmrsRecordConsumer` as a
supervised child. It drains `mrms-ready` and `rap-ready` in cycle order and
strictly re-reads each record at the consumption boundary. An `mrms-ready`
record is a cycle trigger: each MRMS layer selects its newest complete local
source independently, reuses an existing complete render for that source
timestamp, and leaves unavailable layers for a later cycle. RAP conversion
continues to use the exact path recorded by `rap-ready`.
There is a separate durable checkpoint for each phase under
`<BASE_DIR>/state/realtime/consumers/`. The MRMS checkpoint advances after a
best-effort per-layer scan completes; unavailable layers are reconsidered on
the next Core cycle. Renderer exceptions remain retryable without advancing.
Malformed records and invalid exact-input RAP records are logged and marked
unrecoverable so they cannot block the backlog indefinitely. Backlogs beyond
`cycle.max_backlog_cycles` are also explicitly abandoned.

RAP Uint16 conversion is EWMRS-owned derived processing. For each accepted
`rap-ready` record, configured layers are written as:

```text
<BASE_DIR>/gui/RAP/<outdir>/<YYYYMMDD-HHMM00>/data.u16
<BASE_DIR>/gui/RAP/<outdir>/<YYYYMMDD-HHMM00>/metadata.json
```

`data.u16` contains the full `Ni * Nj` little-endian grid. Metadata identifies
the layer, source file, timestamp, shape/grid, dtype, byte order, scale,
missing-value sentinel, units, matched GRIB keys, and optional layer metadata.
`outdir` is the configured output directory and may differ from the layer
name; `colormap_key` is internal renderer metadata, not an API layer name.

Historical processing uses the same shared staged coordinator with EWMRS and
GOES disabled. `src/process_historical.py` selects the best available MRMS
cycle minute by minute, runs detection from the returned manifest, and runs
integration only when its manifest inputs are available.

## MRMS

`src/common/ingest/mrms/` provides the MRMS catalog, timestamp selection,
async/synchronous S3 and HTTPS download paths, decompression/parsing, atomic
staging, and cleanup. Important entry points are:

- `download_detection_files_async(dt, ...)`
- `download_integration_files_async(dt, ...)`
- `download_all_files_async(dt, ...)`
- `download_detection_files(dt, ...)` and `download_integration_files(dt, ...)`
  for synchronous fallback paths

Detection and integration modifiers are deliberately separate. A structured
`DownloadBatchResult` reports attempted, downloaded, and failed products;
readiness requires every requested product to be present and successful.
For each MRMS product, an S3 listing miss, failed S3 fetch, or S3 processing
error triggers an HTTPS lookup/download for the requested timestamp before
that product is reported unavailable. The HTTPS matcher accepts the exact
minute or the configured narrow timestamp window; it does not substitute an
arbitrarily newer file. Both source paths stage files atomically.
`max_entries` and `remove_old_files` default to the runtime/catalog settings,
so configuration remains the source of truth unless a caller explicitly
overrides them. Cleanup is restricted to configured runtime directories.

PrecipRate is excluded from the MRMS scan-discovery readiness subset because
its upstream latency can delay selection of an otherwise usable cycle. It
remains in the full ingest catalog and the integration/render paths, so the
selected cycle still downloads it, computes `maxPrecipRate`, and serves the
`MRMS_PrecipRate` EWMRS product.

ProbSevere is a distinct MRMS-family product with its own bucket-path and JSON
handling. Its product identity must be preserved in manifests and downstream
processing.

## GOES ABI and scan-time GLM

GOES ABI is an EWMRS-owned background pipeline. The supervised GOES ingest loop
polls `noaa-goes19` for `ABI-L1b-RadC` channel files, preferring async download
with sync fallback. It stages channels locally; the poll-based render loop
selects a complete configured ABI set (currently C01-C16) near the target
time and renders each distinct input selection once. A scan-window filename is
valid across its encoded start/end interval, and readiness requires every
configured channel.

GOES ABI source files are staged in configured per-channel runtime directories.
Single-channel GUI products are written under `<BASE_DIR>/gui` as tiled
float16/gzip chunks with schema-version-2 product and timestamp indexes. RGB
composites are derived client-side; they are not staged or rendered server-side.
See `docs/core/goes_pipeline.md` for the render and API representation.

GLM uses the same NOAA GOES-19 source family (`GLM-L2-LCFA`) but has different
ownership and readiness semantics: the realtime primary downloads scan-time
GLM when enabled and gates EdgeWARN integration on a valid pinned GLM input.
It is not part of the EWMRS ABI render loop.

## RAP / Synoptic

`src/common/ingest/synoptic/` stages RAP files for EdgeWARN integration. The
selection is local-first and walks backward through eligible UTC analysis hours
from the requested scan. The default maximum analysis age is 180 minutes and
can be overridden with `EDGEWARN_RAP_MAX_AGE_MINUTES`. Freshness is determined
from the analysis timestamp encoded in the RAP filename, not filesystem mtime.

Definitive S3 404s advance to the next eligible analysis without retrying the
same key through the synchronous client; transport/authentication failures may
use one synchronous source fallback. When the search window is exhausted, the
readiness error includes the configured limit and checked-key results. RAP
cleanup uses the same encoded-time policy and retains at most the newest three
eligible analyses under `<BASE_DIR>/data/RAP`.

## METAR

`src/common/ingest/metar.py` ingests hourly Aviation Weather METAR cycle files,
using a cached station database for coordinates. It parses reports, enriches
station locations, filters to configured CONUS bounds, writes hourly snapshots,
and cleans old snapshots:

```text
<BASE_DIR>/data/METAR/METAR_YYYYMMDD-HHz.json
```

The EWMRS service runs the async entry point on the configured hourly boundary;
sync and async paths share the same parsing/output behavior.

## NWS alerts

`src/common/ingest/nws/` downloads the active-alert feed from the configured
National Weather Service URL, filters blocklisted event types, maps UGC zones
to geometry, and updates a deduplicated registry. It reconciles the registry
against the current active ID set and applies expiration/TTL cleanup while
writing timestamp snapshots for API serving.

NWS zone assets are an operator-managed prerequisite. Run
`edgewarn sync-nws-zones --apply` before enabling NWS on a new runtime; EWMRS
fails preflight if enabled NWS ingest has no usable zone assets. The loop polls
the active feed on its configured interval. Its registry and timestamp
snapshots are stored under `<BASE_DIR>/data/Alerts/official/` (raw downloads,
when retained, are under `<BASE_DIR>/data/NWS_Raw/`).

## NEXRAD Level II

`src/common/ingest/nexrad/` owns independent realtime Level-II processing:
volume discovery, station filtering, chunked S3 download, VCP probing,
stream/boundary handling, parsing, worker-pool execution, staged writes, and
retention. `run_nexrad.py` supervises both the ingest and render loops; it does
not depend on the MRMS/RAP phase records.

Staged Level-II data and manifests live under:

```text
<BASE_DIR>/data/NEXRAD_Level2/<SITE>/...
<BASE_DIR>/data/NEXRAD_Level2/manifests/...
```

The NEXRAD renderer polls those local staged outputs and writes gzip-compressed
polar intermediates under:

```text
<BASE_DIR>/gui/NEXRAD/<SITE>/<ELEVATION>/
  <SITE>_<PRODUCT>_<ELEVATION>_<YYYYMMDD-HHMMSS>.bin.gz
```

Discovery, chunk-listing, downloads, full scans, and worker liveness are
bounded and observable; stale worker heartbeats cause restart handling. The
resulting files are served by the EWMRS/NEXRAD API adapters documented in
`docs/api/ewmrs_api_endpoints.md`.

## WPC surface analysis

`src/common/ingest/wpc/` downloads the latest valid coded surface analysis,
using fixed WPC analysis hours and a previous-analysis fallback. It parses the
coded product, converts fronts/troughs and pressure centers to GeoJSON, writes
the latest artifact, writes timestamped copies for the current and previous
analysis, and removes old timestamped files:

```text
<BASE_DIR>/wpc/surface_analysis/latest.geojson
<BASE_DIR>/wpc/surface_analysis/wpc_sfc_YYYYMMDD-HH0000.geojson
```

TLS certificate verification is required for WPC downloads. The EWMRS WPC
loop runs on the configured analysis boundary and exposes these artifacts
through the WPC API routes.

## Runtime layout

All generated data is rooted at the resolved `<BASE_DIR>`:

```text
<BASE_DIR>/
├── data/      # MRMS, RAP, METAR, NWS alerts, NEXRAD, and other staged data
├── gui/       # MRMS/GOES/RAP/NEXRAD render artifacts and indexes
├── state/     # realtime cycle records, consumer checkpoints, service state
└── wpc/       # WPC surface-analysis GeoJSON
```

The primary CLI accepts `--base_dir`/`--base-dir`; the API and accessory
services resolve their supported base-directory flags/environment settings
through their service parsers. Do not introduce repository-local output paths:
the runtime filesystem is the source of truth for staged inputs, durable
handoff, rendered artifacts, and API visibility.
